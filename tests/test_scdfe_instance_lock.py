"""Host-wide leases and real multi-process scdfe_c coexistence on loopback."""
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from pyModbusTCP.server import DataBank, ModbusServer

from core.scdfe_control import CONTROL_TABLE, ServerInstanceLock, ensure_control_row
from web.service import ScadaStore


ROOT = Path(__file__).resolve().parents[1]
_pythonpath = os.pathsep.join(
    value for value in (str(ROOT), os.environ.get("PYTHONPATH", "")) if value
)
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": _pythonpath}


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("timed out waiting for process/state transition")


class ServerLockTests(unittest.TestCase):
    def test_main_and_different_rtus_are_independent_and_reusable(self):
        locks = [ServerInstanceLock(rtu) for rtu in (None, 17007, 17008)]
        for lock in locks:
            self.addCleanup(lock.release)
            self.assertTrue(lock.acquire())
            with self.assertRaises(RuntimeError):
                lock.acquire()
        for rtu, lock in zip((None, 17007, 17008), locks):
            self.assertTrue(ServerInstanceLock.held(rtu))
            lock.release()
            lock.release()
            self.assertFalse(ServerInstanceLock.held(rtu))
            self.assertTrue(lock.acquire())
        for invalid in (0, -1, True, "7", 7.0):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ServerInstanceLock(invalid)

    def test_simultaneous_process_start_has_exactly_one_owner(self):
        source = """
import pathlib, sys, time
from core.scdfe_control import ServerInstanceLock
root, token, role = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
while not (root / 'start').exists():
    time.sleep(.01)
lock = ServerInstanceLock(None if role == 'main' else int(role))
owned = lock.acquire()
(root / token).write_text('1' if owned else '0')
while not (root / 'stop').exists():
    time.sleep(.01)
lock.release()
"""
        for role in ("main", "17007"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                processes = []
                try:
                    for index in range(8):
                        processes.append(subprocess.Popen(
                            [sys.executable, "-c", source, directory, str(index), role],
                            cwd=directory, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                    (root / "start").touch()
                    wait_for(lambda: all((root / str(i)).exists() and (root / str(i)).stat().st_size
                                         for i in range(len(processes))))
                    self.assertEqual(sum((root / str(i)).read_text() == "1" for i in range(8)), 1)
                finally:
                    (root / "stop").touch()
                    for process in processes:
                        output, errors = process.communicate(timeout=15)
                        self.assertEqual(process.returncode, 0, output + errors)
                self.assertFalse(ServerInstanceLock.held(None if role == "main" else int(role)))

    def test_process_crash_releases_main_and_rtu_locks(self):
        source = """
import os
from core.scdfe_control import ServerInstanceLock
main, rtu = ServerInstanceLock(), ServerInstanceLock(17007)
assert main.acquire() and rtu.acquire()
os._exit(23)
"""
        result = subprocess.run([sys.executable, "-c", source], cwd=ROOT, env=ENV,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertFalse(ServerInstanceLock.held())
        self.assertFalse(ServerInstanceLock.held(17007))

    def test_linux_binding_conflicts_and_errors_close_socket(self):
        # Exercise error handling on Windows too; real kernel exclusion is
        # exercised by the same subprocess tests when this suite runs on Linux.
        with patch("core.scdfe_control.os.name", "posix"), \
                patch("core.scdfe_control.sys.platform", "linux"), \
                patch("core.scdfe_control.socket.AF_UNIX", 1, create=True), \
                patch("core.scdfe_control.socket.socket") as factory:
            sock = Mock()
            factory.return_value = sock
            lock = ServerInstanceLock(17007)
            self.assertTrue(lock.acquire())
            sock.bind.assert_called_once_with("\0SCADA.scdfe_c.rtu-17007")
            lock.release()
            sock.close.assert_called_once()
            for code in (errno.EADDRINUSE, errno.EACCES):
                sock = Mock()
                factory.return_value = sock
                sock.bind.side_effect = OSError(code, "injected bind failure")
                if code == errno.EADDRINUSE:
                    self.assertFalse(lock.acquire())
                else:
                    with self.assertRaises(OSError):
                        lock.acquire()
                sock.close.assert_called_once()


class RuntimeInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = DataBank(h_regs_size=64)
        self.server = ModbusServer(host="127.0.0.1", port=0, no_block=True, data_bank=self.bank)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.stores = [ScadaStore(self.root / name) for name in ("first.sqlite3", "second.sqlite3")]
        for store in self.stores:
            ensure_control_row(store)
            store.update_row(CONTROL_TABLE, [1], {
                "scan_interval_ms": 50, "poll_interval_ms": 50, "retry_interval_ms": 50,
                "worker_start_delay_ms": 0,
                "log_dir": str(self.root / store.db_path.stem),
            })
            for index, rtu in enumerate((17007, 17008, 17009), start=1):
                address = index * 10
                self.bank.set_holding_registers(address, [rtu])
                store.create_row("fes_rtu_define", {
                    "rtu_id": rtu, "channel_id": index, "channel_type": "modbus-tcp",
                    "process_enable": 0,
                })
                store.create_row("fes_yc_define", {"rtu_id": rtu, "pnt_no": 1, "raw_scale": 1.0})
                store.create_row("fes_channel_modbus_tcp_define", {
                    "rtu_id": rtu, "remote_host": "127.0.0.1",
                    "remote_port": self.server._service.server_address[1],
                    "unit_id": 1, "response_timeout": 0.2,
                })
                store.create_row("fes_channel_modbus_define", {"id": index, "rtu_id": rtu, "irtuaddr": 1})
                store.create_row("fes_channel_modbus_define_call", {
                    "id": index, "rtu_id": rtu, "irtuaddr": 1, "call_seq": 0,
                    "iplace": address, "inum": 1, "icode": 3,
                })
                store.create_row("fes_channel_modbus_define_data", {
                    "id": index, "rtu_id": rtu, "call_seq": 0, "cnt": 1,
                    "reg_addr": address, "trans_type": 2,
                })
        self.processes = []
        self.addCleanup(self.stop_all)

    @staticmethod
    def command(store, rtu=None):
        command = [sys.executable, "-m", "scdfe_c", "--db", str(store.db_path),
                   "--scan-interval", "0.05", "--poll-interval", "0.05", "--retry-interval", "0.05"]
        return command if rtu is None else command + ["--rtu-id", str(rtu)]

    def start(self, store, rtu=None):
        output = (self.root / f"output-{len(self.processes)}.txt").open("w+b")
        process = subprocess.Popen(self.command(store, rtu), cwd=self.root, env=ENV,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        self.processes.append((process, store, rtu, output))
        try:
            if rtu is None:
                wait_for(lambda: store.get_row(CONTROL_TABLE, [1])["process_status"] == 2
                         or process.poll() is not None)
            else:
                wait_for(lambda: store.get_row("fes_yc_define", [rtu, 1])["raw_value"] == rtu
                         or process.poll() is not None)
            self.assertIsNone(process.poll(), self.read_output(output))
        except Exception:
            print(self.read_output(output))
            raise
        return process

    @staticmethod
    def read_output(output):
        output.seek(0)
        return output.read().decode("utf-8", errors="replace")

    def stop_all(self):
        # Stop the automatic owner before requesting debug exits, so it cannot
        # take over a debug RTU that is temporarily re-enabled for shutdown.
        for process, store, rtu, output in sorted(self.processes, key=lambda item: item[2] is not None):
            try:
                if process.poll() is None:
                    if rtu is None:
                        store.update_row(CONTROL_TABLE, [1], {"process_enable": 0})
                    else:
                        store.update_row("fes_rtu_define", [rtu], {"process_enable": 1})
                    process.wait(timeout=15)
                self.assertEqual(process.returncode, 0, self.read_output(output))
            finally:
                output.close()

    def invoke(self, store, rtu=None, extra=()):
        return subprocess.run(self.command(store, rtu) + list(extra), cwd=self.root, env=ENV,
                              capture_output=True, encoding="utf-8", timeout=15)

    def test_main_singleton_across_databases_and_working_directories(self):
        first, second = self.stores
        owner = self.start(first)
        for store in self.stores:
            result = self.invoke(store, extra=["--max-scans", "1"])
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("自动守护实例", result.stdout)
        before = second.get_row(CONTROL_TABLE, [1])
        result = subprocess.run([sys.executable, "-m", "scdfe_c", "--db", str(self.root / "missing.db")],
                                cwd=ROOT, env=ENV, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "missing.db").exists())
        self.assertEqual(second.get_row(CONTROL_TABLE, [1]), before)
        first.update_row(CONTROL_TABLE, [1], {"process_enable": 0})
        self.assertEqual(owner.wait(timeout=15), 0)
        result = self.invoke(second, extra=["--max-scans", "1"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_multiple_debuggers_coexist_with_auto_and_reject_duplicate_rtus(self):
        first, second = self.stores
        first.update_row("fes_rtu_define", [17007], {"process_enable": 1})
        self.start(first)
        wait_for(lambda: first.get_row("fes_yc_define", [17007, 1])["raw_value"] == 17007)
        debug8 = self.start(first, 17008)
        debug9 = self.start(first, 17009)
        before_control = second.get_row(CONTROL_TABLE, [1])
        for rtu in (17007, 17008, 17009):
            before = second.get_row("fes_rtu_define", [rtu])
            result = self.invoke(second, rtu, ["--max-cycles", "1"])
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(f"RTU {rtu}", result.stdout)
            self.assertEqual(second.get_row("fes_rtu_define", [rtu]), before)
        self.assertEqual(second.get_row(CONTROL_TABLE, [1]), before_control)
        self.assertIsNone(debug8.poll())
        self.assertIsNone(debug9.poll())
        first.update_row("fes_rtu_define", [17008], {"process_enable": 1})
        self.assertEqual(debug8.wait(timeout=15), 0)
        wait_for(lambda: first.get_row("fes_rtu_define", [17008])["process_status"] == 2)
        self.assertTrue(ServerInstanceLock.held(17008))

    def test_debug_owner_blocks_auto_in_other_database_until_exit(self):
        first, second = self.stores
        debug = self.start(first, 17007)
        second.update_row("fes_rtu_define", [17007], {"process_enable": 1})
        second.update_row("fes_rtu_define", [17008], {"process_enable": 1})
        before = second.get_row("fes_rtu_define", [17007])
        self.start(second)
        wait_for(lambda: second.get_row("fes_yc_define", [17008, 1])["raw_value"] == 17008)
        self.assertEqual(second.get_row("fes_rtu_define", [17007]), before)
        first.update_row("fes_rtu_define", [17007], {"process_enable": 1})
        self.assertEqual(debug.wait(timeout=15), 0)
        wait_for(lambda: second.get_row("fes_yc_define", [17007, 1])["raw_value"] == 17007)


if __name__ == "__main__":
    unittest.main()

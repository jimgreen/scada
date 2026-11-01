"""Selected-RTU debug mode, ownership, and real subprocess/loopback checks."""
from contextlib import redirect_stdout, redirect_stderr
import io
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from pyModbusTCP.server import DataBank, ModbusServer

import scdfe_c as runtime
from core.scdfe_control import CONTROL_TABLE, ServerInstanceLock, ensure_control_row
from tests.test_scdfe_c import FakeMaster
from web.service import ScadaStore


class FakeThread:
    def __init__(self, **_kwargs):
        self.alive = False
        self.native_id = 123

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.alive = False


class DebugTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ScadaStore(self.root / "scada.sqlite3")
        ensure_control_row(self.store)
        for rtu, channel in ((7, 70), (8, 80)):
            self.store.create_row("fes_rtu_define", {
                "rtu_id": rtu, "channel_id": channel, "channel_type": "modbus-tcp",
                "process_enable": 0, 
            })
            self.store.create_row("fes_yc_define", {
                "rtu_id": rtu, "pnt_no": 1, "value": 0.0, "raw_base": 2.0, "raw_scale": 1.1,
            })
            self.store.create_row("fes_yx_define", {"rtu_id": rtu, "pnt_no": 1, "value": 0})
        for kind in ("yk", "yt"):
            self.store.create_row(f"fes_{kind}_define", {
                "rtu_id": 7, "pnt_no": 1, "value": 1, "cmd_time": 100, "cmd_handle_time": 0,
            })

    def debug(self, **kwargs):
        config = {"poll_interval": 0, "retry_interval": 0.001, "max_cycles": 2,
                  "master_factory": lambda *_: FakeMaster()}
        config.update(kwargs)
        with redirect_stdout(io.StringIO()):
            return runtime.run_channel_debug(self.store, 7, **config)

    def test_only_selected_rtu_and_no_commands_or_extra_process(self):
        before_control = self.store.get_row(CONTROL_TABLE, [1])
        before_other = {t: self.store.get_row(t, [8] if t == "fes_rtu_define" else [8, 1])
                        for t in ("fes_rtu_define", "fes_yc_define", "fes_yx_define")}
        master = FakeMaster()

        def factory(store, spec):
            self.assertEqual((spec.rtu_id, spec.channel_id), (7, 70))
            self.assertTrue(ServerInstanceLock.held(7))
            self.assertFalse(ServerInstanceLock.held())
            return master

        with patch.object(runtime.ScdfeSupervisor, "__init__", side_effect=AssertionError("supervisor")), \
                patch.object(runtime.mp, "get_context", side_effect=AssertionError("child process")):
            stats = self.debug(master_factory=factory)
        self.assertEqual((stats.cycles, stats.updated_yc, stats.updated_yx), (2, 1, 1))
        yc = self.store.get_row("fes_yc_define", [7, 1])
        self.assertEqual((yc["raw_value"], yc["value"]), (221.5, 2 + 1.1 * 221.5))
        self.assertEqual(self.store.get_row("fes_yx_define", [7, 1])["value"], 1)
        self.assertEqual((master.yk, master.yt), ([], []))
        self.assertEqual(self.store.get_row("fes_yk_define", [7, 1])["cmd_handle_time"], 0)
        self.assertEqual(self.store.get_row(CONTROL_TABLE, [1]), before_control)
        for table, before in before_other.items():
            self.assertEqual(self.store.get_row(table, [8] if table == "fes_rtu_define" else [8, 1]), before)
        self.assertTrue(master.closed)
        self.assertFalse(ServerInstanceLock.held(7))
        state = self.store.get_row("fes_rtu_define", [7])
        self.assertEqual((state["process_enable"], state["process_status"], state["process_pid"]), (0, 0, 0))
        logs = runtime.ScdfeLogger.list_files(self.root / "logs")
        self.assertEqual(len(logs), 1)
        self.assertEqual((logs[0]["process"], logs[0]["rtu_id"]),
                         ("scdfe_c-debug-rtu-7", 7))
        self.assertNotIn("channel_id", logs[0])
        log = runtime.ScdfeLogger.read_file(self.root / "logs", logs[0]["name"])
        self.assertIn(f"pid={os.getpid()}", log["content"])
        self.assertEqual(log["rtu_id"], 7)
        self.assertIn("rtu=7 channel=70", log["content"])

    def test_commands_require_explicit_option(self):
        master = FakeMaster()
        stats = self.debug(send_commands=True, master_factory=lambda *_: master)
        self.assertEqual((stats.sent_yk, stats.sent_yt), (1, 1))
        self.assertEqual((len(master.yk), len(master.yt)), (1, 1))
        self.assertGreater(self.store.get_row("fes_yk_define", [7, 1])["cmd_handle_time"], 0)

    def test_all_six_protocols_share_selected_rtu_dispatch(self):
        for protocol in sorted(runtime.SUPPORTED_CHANNEL_TYPES):
            with self.subTest(protocol=protocol):
                self.store.update_row("fes_rtu_define", [7], {"channel_type": protocol})

                def factory(_store, spec):
                    self.assertEqual((spec.rtu_id, spec.channel_id, spec.channel_type), (7, 70, protocol))
                    return FakeMaster()

                self.assertEqual(self.debug(master_factory=factory, max_cycles=1).cycles, 1)

    def test_rejects_enabled_unknown_and_unsupported_rtu_without_state_changes(self):
        for changes in ({"process_enable": 1}, {"process_enable": 0, "channel_type": "custom"}):
            self.store.update_row("fes_rtu_define", [7], changes)
            before = self.store.get_row("fes_rtu_define", [7])
            with self.assertRaises(ValueError):
                self.debug()
            self.assertEqual(self.store.get_row("fes_rtu_define", [7]), before)
            self.assertFalse(ServerInstanceLock.held(7))
        with self.assertRaisesRegex(ValueError, "不存在"):
            runtime.run_channel_debug(self.store, 999)

    def test_busy_rtu_refuses_second_debugger(self):
        lock = runtime._rtu_lock(7)
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        before = self.store.get_row("fes_rtu_define", [7])
        with self.assertRaisesRegex(ValueError, "使用"):
            self.debug()
        self.assertEqual(self.store.get_row("fes_rtu_define", [7]), before)

    def test_definition_reload_keeps_lock_and_counts_cycles_across_reloads(self):
        masters = []

        def factory(store, spec):
            self.assertTrue(ServerInstanceLock.held(7))
            master = FakeMaster()
            if not masters:
                collect = master.collect_once

                def change_scaling():
                    store.update_row("fes_yc_define", [7, 1], {"raw_scale": 2.0})
                    return collect()

                master.collect_once = change_scaling
            masters.append(master)
            return master

        with patch.object(runtime, "WORKER_DEFINITION_CHECK_INTERVAL", 0):
            stats = self.debug(master_factory=factory)
        self.assertEqual((stats.cycles, len(masters)), (2, 2))
        self.assertTrue(all(m.closed for m in masters))
        self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["value"], 2 + 2 * 221.5)

    def test_reconnect_and_reenable_exit(self):
        masters = []

        def factory(store, _spec):
            master = FakeMaster()
            if not masters:
                master.connect = lambda: (_ for _ in ()).throw(OSError("test disconnect"))
            else:
                collect = master.collect_once

                def reenable():
                    store.update_row("fes_rtu_define", [7], {"process_enable": 1})
                    return collect()

                master.collect_once = reenable
            masters.append(master)
            return master

        stats = self.debug(master_factory=factory, max_cycles=3, scan_interval=0.001, poll_interval=0.02)
        self.assertEqual((stats.errors, stats.cycles, len(masters)), (1, 1, 2))
        self.assertTrue(all(m.closed for m in masters))
        self.assertFalse(ServerInstanceLock.held(7))

    def test_supervisor_skips_debug_owner_without_touching_its_state(self):
        supervisor = runtime.ScdfeSupervisor(self.store.db_path, thread_factory=FakeThread)
        self.addCleanup(supervisor.stop)
        self.store.update_row("fes_rtu_define", [8], {"process_enable": 1})
        lock = runtime._rtu_lock(7)
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        self.store.update_row("fes_rtu_define", [7], {
            "process_status": 2, "process_pid": 777,
        })
        before = self.store.get_row("fes_rtu_define", [7])
        with patch.object(supervisor, "_start_writer"):
            result = supervisor.reconcile()
            self.assertEqual(result["started"], 1)
            self.assertEqual({key.rtu_id for key in supervisor.workers}, {8})
            self.assertEqual(self.store.get_row("fes_rtu_define", [7]), before)
            self.store.update_row("fes_rtu_define", [7], {"process_enable": 1})
            self.assertEqual(supervisor.reconcile()["started"], 0)
        lock.release()
        with patch.object(supervisor, "_start_writer"):
            self.assertEqual(supervisor.reconcile()["started"], 1)

    def test_disabled_rtu_is_not_guarded_and_state_is_preserved(self):
        supervisor = runtime.ScdfeSupervisor(self.store.db_path, thread_factory=FakeThread)
        self.addCleanup(supervisor.stop)
        self.store.update_row("fes_rtu_define", [7], {
            "process_enable": 0,
            "process_status": 5, "process_pid": 9876,
            "process_error": "manual debug owner",
        })
        expected = self.store.get_row("fes_rtu_define", [7])
        with patch.object(supervisor, "_start_writer"):
            result = supervisor.reconcile()
        self.assertEqual(result["started"], 0)
        self.assertEqual(self.store.get_row("fes_rtu_define", [7]), expected)
        self.assertFalse(ServerInstanceLock.held(7))

    def test_supervisor_retains_lock_until_thread_and_writer_finished(self):
        supervisor = runtime.ScdfeSupervisor(self.store.db_path, thread_factory=FakeThread)
        self.addCleanup(supervisor.stop)
        spec = runtime.ChannelSpec(7, 70, "modbus-tcp")
        self.assertTrue(supervisor._start_worker(spec))
        managed = supervisor.workers[spec.key]
        with patch.object(managed.thread, "join"):
            self.assertFalse(supervisor._stop_worker(spec.key, reason="test"))
        self.assertTrue(ServerInstanceLock.held(7))
        writer = FakeThread()
        writer.start()
        supervisor._writer_process = writer
        try:
            with patch.object(supervisor, "_drain_writer", return_value=False):
                self.assertFalse(supervisor._stop_worker(spec.key, reason="pending writes"))
            self.assertTrue(ServerInstanceLock.held(7))
        finally:
            supervisor._writer_process = None
        self.assertTrue(supervisor._stop_worker(spec.key, reason="done"))
        self.assertFalse(ServerInstanceLock.held(7))

    def test_thread_start_failure_and_debug_failure_release_lock(self):
        def fail(**_kwargs):
            raise RuntimeError("test start failure")

        supervisor = runtime.ScdfeSupervisor(self.store.db_path, thread_factory=fail)
        self.addCleanup(supervisor.stop)
        self.assertFalse(supervisor._start_worker(runtime.ChannelSpec(7, 70, "modbus-tcp")))
        self.assertFalse(ServerInstanceLock.held(7))
        with patch.object(runtime._DebugChannelWorker, "run", side_effect=OSError("test failure")), \
                self.assertRaises(OSError):
            self.debug()
        self.assertFalse(ServerInstanceLock.held(7))

    def test_cli_validates_and_restores_signal_handlers(self):
        for args in (["--channel-id", "70"], ["--rtu-id", "7", "--channel-id", "70"],
                     ["--max-cycles", "1"], ["--send-commands"], ["--rtu-id", "0"],
                     ["--rtu-id", "7", "--max-scans", "1"],
                     ["--rtu-id", "7", "--max-cycles", "0"],
                     ["--rtu-id", "7", "--poll-interval", "nan"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                runtime.main(["--db", str(self.store.db_path), *args])
            self.assertEqual(exc.exception.code, 2)
        missing = self.root / "missing.sqlite3"
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runtime.main(["--db", str(missing), "--rtu-id", "7"])
        self.assertFalse(missing.exists())
        handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        with patch.object(runtime, "build_master", return_value=FakeMaster()), redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.main(["--db", str(self.store.db_path), "--rtu-id", "7",
                                           "--max-cycles", "1"]), 0)
        self.assertEqual({s: signal.getsignal(s) for s in handlers}, handlers)

    def test_debug_log_cleanup_is_scoped(self):
        names = ["scdfe_c-debug-rtu-7", "scdfe_c-main", "scdfe_c-debug-rtu-8"]
        for name in names:
            path = self.root / f"{name}-20200101-00.log"
            path.write_text("old", encoding="utf-8")
            os.utime(path, (0, 0))
        removed = runtime.ScdfeLogger.cleanup(self.root, retention_hours=1, process_name=names[0])
        self.assertEqual(len(removed), 1)
        self.assertEqual(len(list(self.root.glob("*.log"))), 2)

    def test_cli_signals_stop_cooperatively(self):
        # Use a fresh interpreter: earlier protocol tests may have closed an
        # asyncio wakeup socket, which must not contaminate OS-signal testing.
        source = """
import signal, sys
from unittest.mock import patch
import scdfe_c as runtime
from tests.test_scdfe_c import FakeMaster
master = FakeMaster()
def collect():
    signal.raise_signal(int(sys.argv[2]))
    return []
master.collect_once = collect
with patch.object(runtime, 'build_master', return_value=master):
    result = runtime.main(['--db', sys.argv[1], '--rtu-id', '7'])
assert result == 0 and master.closed
"""
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig=sig):
                result = subprocess.run([sys.executable, "-c", source, str(self.store.db_path), str(int(sig))],
                                        cwd=Path(runtime.__file__).parent, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(ServerInstanceLock.held(7))
                self.assertEqual(self.store.get_row("fes_rtu_define", [7])["process_status"], 0)

    def test_rtu_lock_is_released_after_process_crash(self):
        source = (
            "import os, sys; from core.scdfe_control import ServerInstanceLock; "
            "lock = ServerInstanceLock(7); "
            "assert lock.acquire(); os._exit(0)"
        )
        result = subprocess.run([sys.executable, "-c", source, str(self.store.db_path)],
                                cwd=Path(runtime.__file__).parent, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(ServerInstanceLock.held(7))

    def test_subprocess_loopback_and_duplicate_exclusion(self):
        bank = DataBank(h_regs_size=32)
        server = ModbusServer(host="127.0.0.1", port=0, no_block=True, data_bank=bank)
        server.start()
        self.addCleanup(server.stop)
        bank.set_holding_registers(10, [123])
        self.store.create_row("fes_channel_modbus_tcp_define", {
            "rtu_id": 7, "remote_host": "127.0.0.1", "remote_port": server._service.server_address[1],
            "unit_id": 1, "response_timeout": 0.2,
        })
        self.store.create_row("fes_channel_modbus_define", {"id": 1, "rtu_id": 7, "irtuaddr": 1})
        self.store.create_row("fes_channel_modbus_define_call", {
            "id": 1, "rtu_id": 7, "irtuaddr": 1, "call_seq": 0, "iplace": 10, "inum": 1, "icode": 3,
        })
        self.store.create_row("fes_channel_modbus_define_data", {
            "id": 1, "rtu_id": 7, "call_seq": 0, "cnt": 1, "reg_addr": 10, "trans_type": 2,
        })
        command = [sys.executable, "-m", "scdfe_c", "--db", str(self.store.db_path),
                   "--rtu-id", "7", "--poll-interval", "0.05", "--scan-interval", "0.01"]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        process = subprocess.Popen(command, cwd=Path(runtime.__file__).parent, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8")
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.store.get_row("fes_yc_define", [7, 1])["raw_value"] == 123:
                    break
                if process.poll() is not None:
                    self.fail(process.communicate()[0])
                time.sleep(0.03)
            else:
                self.fail("standalone debug process did not acquire loopback data")
            self.assertNotEqual(process.pid, os.getpid())
            self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["value"], 2 + 1.1 * 123)
            other = subprocess.run(command + ["--max-cycles", "1"], cwd=Path(runtime.__file__).parent,
                                   env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(other.returncode, 2, other.stdout + other.stderr)
            self.store.update_row("fes_rtu_define", [7], {"process_enable": 1})
            output, _ = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, output)
            # Windows venv python.exe can be a launcher for a child Python.
            # Check the actual runtime PID emitted consistently in its logs.
            pids = {int(p) for p in re.findall(r"pid=(\d+)", output)}
            self.assertEqual(len(pids), 1)
            self.assertNotIn(os.getpid(), pids)
            self.assertFalse(ServerInstanceLock.held(7))
            self.store.update_row("fes_rtu_define", [7], {"process_enable": 0})
            result = subprocess.run(command + ["--max-cycles", "2"], cwd=Path(runtime.__file__).parent,
                                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("cycles=2", result.stdout)
            with self.store.connection() as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()

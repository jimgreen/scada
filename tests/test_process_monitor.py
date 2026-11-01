"""Process monitoring/control regression using a temporary DB and loopback TCP."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pyModbusTCP.server import DataBank, ModbusServer

from core.process_registry import PROCESS_TABLE, ProcessLease, ensure_process, process_is_alive
from core.scdfe_control import CONTROL_TABLE, ensure_control_row
from scdfe_c import ScdfeSupervisor
from web.process_control import ProcessController
from web.service import ApiError, ScadaStore


class ProcessMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ScadaStore(self.root / "scada.sqlite3")
        ensure_control_row(self.store)
        self.store.update_row(CONTROL_TABLE, [1], {
            "process_enable": 0, "scan_interval_ms": 100,
            "poll_interval_ms": 50, "retry_interval_ms": 100, "worker_start_delay_ms": 0,
        })
        self.controller = ProcessController(self.store, timeout=15)

    def tearDown(self):
        # Cooperative cleanup also runs after assertions fail; no real devices.
        self.store.update_row(CONTROL_TABLE, [1], {"process_enable": 0})
        with self.store.connection() as connection:
            connection.execute(f"UPDATE {PROCESS_TABLE} SET process_enable=0")
        if self.controller._thread:
            self.controller._thread.join(18)
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            if not any(row["alive"] for row in self.controller.snapshot()["items"]):
                break
            time.sleep(0.1)
        self.assertFalse(any(row["alive"] for row in self.controller.snapshot()["items"]), "cleanup left a process running")
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"condition timed out: {self.controller.snapshot()}")

    def command(self, target, action, **extra):
        self.controller.submit({"target": target, "action": action, **extra})
        self.wait_for(lambda: not self.controller.snapshot()["busy"], 20)
        operation = self.controller.snapshot()["operation"]
        self.assertEqual(operation["status"], "completed", operation)
        return operation

    def row(self, key):
        return self.store.get_row(PROCESS_TABLE, [key])

    def loopback(self):
        bank = DataBank(h_regs_size=32)
        server = ModbusServer(host="127.0.0.1", port=0, no_block=True, data_bank=bank)
        server.start()
        self.addCleanup(server.stop)
        bank.set_holding_registers(10, [123])
        for rid in (7, 8):
            self.store.create_row("fes_rtu_define", {
                "rtu_id": rid, "channel_id": rid * 10, "channel_type": "modbus-tcp",
                "process_enable": int(rid == 7),
            })
            self.store.create_row("fes_yc_define", {"rtu_id": rid, "pnt_no": 1, "raw_scale": 1, "raw_base": 0})
            self.store.create_row("fes_channel_modbus_tcp_define", {
                "rtu_id": rid, "remote_host": "127.0.0.1", "remote_port": server._service.server_address[1],
                "unit_id": 1, "response_timeout": 0.2,
            })
            self.store.create_row("fes_channel_modbus_define", {"id": rid, "rtu_id": rid, "irtuaddr": 1})
            self.store.create_row("fes_channel_modbus_define_call", {
                "id": rid, "rtu_id": rid, "irtuaddr": 1, "call_seq": 0, "iplace": 10, "inum": 1, "icode": 3,
            })
            self.store.create_row("fes_channel_modbus_define_data", {
                "id": rid, "rtu_id": rid, "call_seq": 0, "cnt": 1, "reg_addr": 10, "trans_type": 2,
            })
        return bank

    def value(self, rtu):
        return self.store.get_row("fes_yc_define", [rtu, 1])["raw_value"]

    def test_independent_control_and_ordered_all_loopback(self):
        bank = self.loopback()
        self.command("all", "start")
        self.wait_for(lambda: self.value(7) == 123)
        main_pid, writer_pid = self.row("main")["process_pid"], self.row("writer")["process_pid"]
        self.assertEqual(self.row("writer")["parent_pid"], main_pid)
        self.assertNotEqual(main_pid, os.getpid())
        self.command("debug:8", "start")
        self.wait_for(lambda: self.value(8) == 123)
        debug_pid = self.row("debug:8")["process_pid"]
        self.command("debug:8", "start")
        self.assertEqual(self.row("debug:8")["process_pid"], debug_pid)
        self.command("writer", "stop")
        self.assertEqual(self.row("main")["process_pid"], main_pid)
        self.assertEqual(self.store.get_row("fes_rtu_define", [7])["process_enable"], 1)
        self.assertFalse(process_is_alive(writer_pid))
        bank.set_holding_registers(10, [234])
        self.wait_for(lambda: self.value(8) == 234)
        self.assertEqual(self.value(7), 123)
        self.command("writer", "start")
        self.wait_for(lambda: self.value(7) == 234)
        self.assertNotEqual(self.row("writer")["process_pid"], writer_pid)
        self.assertEqual(self.row("main")["process_pid"], main_pid)
        self.command("debug:8", "stop")
        self.assertFalse(self.row("debug:8")["process_pid"])
        self.command("debug:8", "start")
        self.assertNotEqual(self.row("debug:8")["process_pid"], debug_pid)
        self.command("main", "stop")
        bank.set_holding_registers(10, [345])
        self.wait_for(lambda: self.value(8) == 345)
        operation = self.command("all", "stop")
        steps = [step["message"] for step in operation["steps"]]
        self.assertIn("RTU 8", steps[0])
        self.assertIn("数据库写进程", steps[1])
        self.assertIn("主进程", steps[2])
        self.assertFalse(any(r["alive"] for r in self.controller.snapshot()["items"]))
        self.command("all", "start")
        self.wait_for(lambda: self.value(7) == 345)
        self.assertTrue(all(r["alive"] for r in self.controller.snapshot()["items"]))
        self.command("all", "stop")
        self.command("debug:8", "configure", include_in_all=False)
        self.command("all", "start")
        self.assertFalse(self.row("debug:8")["process_pid"])
        self.command("all", "stop")
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_manually_started_debug_is_discovered_and_stopped(self):
        self.loopback()
        process = subprocess.Popen(
            [sys.executable, "-m", "scdfe_c", "--db", str(self.store.db_path), "--rtu-id", "8",
             "--scan-interval", "0.1", "--poll-interval", "0.05"],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            self.wait_for(lambda: any(r["kind"] == "debug" and r["alive"] for r in self.controller.snapshot()["items"]))
            self.wait_for(lambda: self.value(8) == 123)
            self.command("debug:8", "stop")
            self.assertEqual(process.wait(timeout=10), 0)
        finally:
            ensure_process(self.store, "debug:8")
            self.store.update_row(PROCESS_TABLE, ["debug:8"], {"process_enable": 0})
            process.wait(timeout=10)

    def test_control_lock_shared_between_web_instances_and_failures_stop_sequence(self):
        entered, release = threading.Event(), threading.Event()
        def hold(_):
            entered.set()
            release.wait(5)
        try:
            with patch.object(self.controller, "_start", side_effect=hold):
                self.controller.submit({"target": "main", "action": "start"})
                self.assertTrue(entered.wait(2))
                other = ProcessController(self.store)
                with self.assertRaises(ApiError) as error:
                    other.submit({"target": "main", "action": "start"})
                self.assertEqual(error.exception.status, 409)
                with self.assertRaises(ApiError):
                    other.configure_main({"process_status": 1})
                release.set()
                self.controller._thread.join(3)
        finally:
            release.set()
        with patch.object(self.controller, "_stop", side_effect=TimeoutError("写库排空超时")) as stop:
            self.controller.submit({"target": "all", "action": "stop"})
            self.controller._thread.join(3)
            stop.assert_called_once_with("writer")
            operation = self.controller.snapshot()["operation"]
            self.assertEqual(operation["status"], "failed")
            self.assertIn("写库排空超时", operation["error"])

    def test_late_exit_does_not_overwrite_new_identity(self):
        old = ProcessLease(self.store, "writer")
        new = ProcessLease(self.store, "writer")
        old.finish("late failure")
        self.assertEqual(self.row("writer")["owner_token"], new.token)
        self.assertEqual(self.row("writer")["process_pid"], os.getpid())
        new.finish()

    def test_pause_keeps_writer_until_all_workers_stop(self):
        supervisor = ScdfeSupervisor(self.store.db_path)
        fake = type("Worker", (), {"stop_event": threading.Event()})()
        supervisor._workers["blocked"] = fake
        with patch.object(supervisor, "_stop_worker", return_value=False), \
                patch.object(supervisor, "_stop_writer") as stop:
            self.assertFalse(supervisor._pause_communication("test"))
            stop.assert_not_called()
            self.assertTrue(fake.stop_event.is_set())
        supervisor._workers.clear()
        supervisor.stop()

    def test_invalid_debug_does_not_register_or_change_rtu(self):
        self.loopback()
        for key in ("debug:7", "debug:999"):
            with self.assertRaises(ApiError):
                self.controller.submit({"target": key, "action": "start"})
        self.assertFalse(any(r["kind"] == "debug" for r in self.controller.snapshot()["items"]))
        self.assertEqual(self.store.get_row("fes_rtu_define", [7])["process_enable"], 1)

    def test_control_errors_release_lock_even_when_status_write_fails(self):
        with patch.object(self.controller, "_start", side_effect=OSError("database unavailable")), \
                patch.object(self.controller, "_row", side_effect=OSError("database unavailable")), \
                self.assertLogs("scada.process-control", level="ERROR"):
            # A writer start has no direct process ownership preflight.
            self.controller.submit({"target": "writer", "action": "start"})
            self.controller._thread.join(3)
        result = self.controller.snapshot()
        self.assertFalse(result["busy"])
        self.assertEqual(result["operation"]["status"], "failed")
        self.assertIn("database unavailable", result["operation"]["error"])
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("thread creation failed")):
            with self.assertRaises(RuntimeError):
                self.controller.submit({"target": "writer", "action": "start"})
        self.assertFalse(self.controller.snapshot()["busy"])
        self.assertEqual(self.controller.snapshot()["operation"]["status"], "failed")
        self.controller._thread = None


if __name__ == "__main__":
    unittest.main()

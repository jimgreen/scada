from pathlib import Path
import multiprocessing as mp
import sqlite3
import tempfile
import threading
import time
import unittest

from scdfe_c import BATCH_WRITER_STOP, ScdfeLogger, _batch_writer_entry, _flush_batch
from scdfe_c import ScdfeSupervisor
from core.scdfe_control import CONTROL_TABLE, InstanceLock, ensure_control_row, validate_control
from web.service import ScadaStore


class ScdfeControlTests(unittest.TestCase):
    def test_singleton_control_row_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ScadaStore(Path(directory) / "scada.sqlite3")
            row = ensure_control_row(store)
            self.assertEqual((row["id"], row["process_enable"], row["process_status"]), (1, 1, 0))
            self.assertEqual(store.get_row(CONTROL_TABLE, [1])["log_dir"], "logs")
            self.assertEqual((row["log_retention_hours"], row["worker_start_delay_ms"]), (720, 250))
            validate_control({"process_enable": 0, "scan_interval_ms": 100})
            with self.assertRaises(ValueError):
                validate_control({"process_pid": 42})
            with self.assertRaises(ValueError):
                validate_control({"scan_interval_ms": 1})

    def test_schema_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stale.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE fes_scdfe_c_define (id INTEGER PRIMARY KEY, "
                    "log_retention_days INTEGER)"
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(RuntimeError, "当前 schema 不一致"):
                ScadaStore(path)

    def test_process_specific_files_are_listed_and_tail_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = ScdfeLogger(root, process_name="scdfe_c-main")
            child = ScdfeLogger(root, process_name="scdfe_c-rtu-2-ch-1", rtu_id=2, channel_id=1)
            main.process("main")
            child.error("child")
            names = {item["name"] for item in ScdfeLogger.list_files(root)}
            hour = __import__("datetime").datetime.now().strftime("%Y%m%d-%H")
            self.assertIn("scdfe_c-main-" + hour + ".log", names)
            self.assertIn("scdfe_c-rtu-2-ch-1-" + hour + ".log", names)
            item = ScdfeLogger.read_file(root, "scdfe_c-rtu-2-ch-1-" + hour + ".log", tail=1)
            self.assertIn("child", item["content"])
            self.assertEqual(item["hour"], hour)
            with self.assertRaises(ValueError):
                ScdfeLogger.read_file(root, "..\\secret.log")

    def test_main_process_enable_zero_requests_cooperative_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ScadaStore(Path(directory) / "scada.sqlite3")
            supervisor = ScdfeSupervisor(store.db_path, schema_dir=store.catalog.directory,
                                         log_dir=Path(directory) / "logs", scan_interval=0.1)
            try:
                store.update_row(CONTROL_TABLE, [1], {"process_enable": 0})
                result = supervisor.reconcile()
                self.assertEqual(result["running"], 0)
                self.assertTrue(supervisor.stop_event.is_set())
                self.assertEqual(store.get_row(CONTROL_TABLE, [1])["process_status"], 0)
            finally:
                supervisor.stop()

    def test_drain_writer_uses_public_join_without_private_queue_state(self):
        class Queue:
            def __init__(self):
                self.join_called = threading.Event()

            def join(self):
                self.join_called.set()

            def __getattr__(self, name):
                if name == "_unfinished_tasks":
                    raise AssertionError("private JoinableQueue state must not be inspected")
                raise AttributeError(name)

        class Process:
            def is_alive(self):
                return True

        with tempfile.TemporaryDirectory() as directory:
            store = ScadaStore(Path(directory) / "scada.sqlite3")
            supervisor = ScdfeSupervisor(store.db_path, schema_dir=store.catalog.directory,
                                         log_dir=Path(directory) / "logs")
            supervisor._writer_queue = Queue()
            supervisor._writer_process = Process()
            self.assertTrue(supervisor._drain_writer(timeout=1))
            self.assertTrue(supervisor._writer_queue.join_called.is_set())
            supervisor._writer_queue = None
            supervisor._writer_process = None
            supervisor.stop()

    def test_drain_writer_returns_when_writer_has_exited(self):
        class Queue:
            def join(self):
                threading.Event().wait(10)

        class Process:
            def is_alive(self):
                return False

        with tempfile.TemporaryDirectory() as directory:
            store = ScadaStore(Path(directory) / "scada.sqlite3")
            supervisor = ScdfeSupervisor(store.db_path, schema_dir=store.catalog.directory,
                                         log_dir=Path(directory) / "logs")
            supervisor._writer_queue = Queue()
            supervisor._writer_process = Process()
            started = time.monotonic()
            self.assertFalse(supervisor._drain_writer(timeout=2))
            self.assertLess(time.monotonic() - started, 1)
            supervisor._writer_queue = None
            supervisor._writer_process = None
            supervisor.stop()

    def test_instance_lock_is_os_released_and_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scada.sqlite3"
            first = InstanceLock(path)
            second = InstanceLock(path)
            self.assertTrue(first.acquire())
            try:
                self.assertFalse(second.acquire())
            finally:
                first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_dedicated_batch_writer_commits_updates_and_flushes_on_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ScadaStore(root / "scada.sqlite3")
            store.create_row("fes_rtu_define", {
                "rtu_id": 7, "channel_id": 70, "channel_type": "custom",
            })
            store.create_row("fes_yc_define", {
                "rtu_id": 7, "pnt_no": 1, "name": "电压", "value": 0.0,
            })
            context = mp.get_context("spawn")
            queue = context.JoinableQueue()
            process = context.Process(
                target=_batch_writer_entry,
                args=(str(store.db_path), str(store.catalog.directory), str(root / "logs"), queue, 0.05, 10),
            )
            process.start()
            queue.put({
                "op": "update", "table": "fes_yc_define",
                "key": {"rtu_id": 7, "pnt_no": 1},
                "values": {"value": 221.5, "refresh_time": 123},
            })
            queue.join()
            self.assertEqual(store.get_row("fes_yc_define", [7, 1])["value"], 221.5)
            queue.put(BATCH_WRITER_STOP)
            queue.join()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(2)
            queue.close()
            queue.join_thread()
            self.assertEqual(process.exitcode, 0)

    def test_batch_flush_coalesces_duplicate_row_updates(self):
        class Queue:
            def __init__(self):
                self.acknowledged = 0

            def task_done(self):
                self.acknowledged += 1

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ScadaStore(root / "scada.sqlite3")
            store.create_row("fes_rtu_define", {
                "rtu_id": 7, "channel_id": 70, "channel_type": "custom",
            })
            store.create_row("fes_yc_define", {
                "rtu_id": 7, "pnt_no": 1, "name": "电压", "value": 0.0,
            })
            queue = Queue()
            logger = ScdfeLogger(root / "logs", process_name="scdfe_c-db-writer")
            messages = [
                {"op": "update", "table": "fes_yc_define",
                 "key": {"rtu_id": 7, "pnt_no": 1},
                 "values": {"value": 221.5, "refresh_time": 123}},
                {"op": "update", "table": "fes_yc_define",
                 "key": {"rtu_id": 7, "pnt_no": 1},
                 "values": {"value": 222.5, "refresh_time": 124}},
            ]
            self.assertTrue(_flush_batch(store, logger, messages, queue))
            row = store.get_row("fes_yc_define", [7, 1])
            self.assertEqual(row["value"], 222.5)
            self.assertEqual(row["refresh_time"], 124)
            self.assertEqual(row["_version"], 2)
            self.assertEqual(queue.acknowledged, 2)

    def test_batch_writer_failure_exits_after_acknowledging_failed_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ScadaStore(root / "scada.sqlite3")
            store.create_row("fes_rtu_define", {
                "rtu_id": 7, "channel_id": 70, "channel_type": "custom",
            })
            context = mp.get_context("spawn")
            queue = context.JoinableQueue()
            process = context.Process(
                target=_batch_writer_entry,
                args=(str(store.db_path), str(store.catalog.directory), str(root / "logs"), queue, 0.05, 10),
            )
            process.start()
            queue.put({
                "op": "update", "table": "fes_rtu_define",
                "key": {"rtu_id": 7},
                "values": {"column_that_does_not_exist": 1},
            })
            queue.join()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(2)
            queue.close()
            queue.join_thread()
            self.assertNotEqual(process.exitcode, 0)


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
from copy import deepcopy
import multiprocessing as mp
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scdfe_c import BATCH_WRITER_STOP, ScdfeLogger, _batch_writer_entry, _flush_batch
from scdfe_c import ScdfeSupervisor
from core.scdfe_control import CONTROL_TABLE, InstanceLock, ensure_control_row, validate_control
from web.service import ScadaStore


def point_batch(*updates):
    """Build grouped point messages without hiding key/field ordering."""
    return {"op": "batch", "groups": [
        {"table": item["table"], "key_names": tuple(item["key"]),
         "value_names": tuple(item["values"]),
         "rows": [(*item["values"].values(), *item["key"].values())]}
        for item in updates
    ]}


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
            queue.put(point_batch({
                    "table": "fes_yc_define",
                    "key": {"rtu_id": 7, "pnt_no": 1},
                    "values": {"value": 221.5, "refresh_time": 123},
                }))
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
            connection = store.open_connection()
            messages = [
                point_batch(
                    {"op": "update", "table": "fes_yc_define",
                     "key": {"rtu_id": 7, "pnt_no": 1},
                     "values": {"value": 221.5, "refresh_time": 123}},
                    {"op": "update", "table": "fes_yc_define",
                     "key": {"rtu_id": 7, "pnt_no": 1},
                     "values": {"value": 222.5, "refresh_time": 124}},
                ),
            ]
            self.assertTrue(_flush_batch(store, logger, messages, queue, connection))
            row = store.get_row("fes_yc_define", [7, 1])
            self.assertEqual(row["value"], 222.5)
            self.assertEqual(row["refresh_time"], 124)
            self.assertEqual(row["_version"], 2)
            self.assertEqual(queue.acknowledged, 1)
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            connection.close()

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


class BatchWriterTransactionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.store = ScadaStore(root / "scada.sqlite3")
        self.logger = ScdfeLogger(root / "logs", process_name="scdfe_c-db-writer")
        for rtu_id in (7, 8):
            self.store.create_row("fes_rtu_define", {
                "rtu_id": rtu_id, "channel_id": rtu_id * 10,
                "channel_type": "custom",
            })
            for table in ("fes_yc_define", "fes_yx_define"):
                self.store.create_row(table, {"rtu_id": rtu_id, "pnt_no": 1, "value": 0})
        self.connection = self.store.open_connection()
        self.addCleanup(self.connection.close)
        self.acknowledged = 0

    def task_done(self):
        self.acknowledged += 1

    @staticmethod
    def update(table="fes_yc_define", rtu_id=7, **values):
        return {"op": "update", "table": table,
                "key": {"rtu_id": rtu_id, "pnt_no": 1}, "values": values}

    def test_tuple_subclasses_are_normalized_before_key_slicing(self):
        class Row(tuple):
            def __getitem__(self, index):
                raise AssertionError("normalize tuple subclass before slicing")

        group = {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
                 "value_names": ("value",), "rows": [Row((12.5, 1, 7))]}
        self.assertTrue(_flush_batch(self.store, self.logger, [{"op": "batch", "groups": [group]}],
                                     self, self.connection))
        row = self.store.get_row("fes_yc_define", [7, 1])
        self.assertEqual((row["value"], row["_version"]), (12.5, 2))

    def test_stop_is_acknowledged_even_when_error_log_cannot_be_written(self):
        messages = iter([self.update(value=10), self.update("fes_yx_define", missing_column=1),
                         BATCH_WRITER_STOP])
        test = self

        class Queue:
            def get(self, **_kwargs):
                return next(messages)

            def task_done(self):
                test.acknowledged += 1

        with patch.object(ScdfeLogger, "error", side_effect=OSError("log disk full")):
            with self.assertRaisesRegex(OSError, "log disk full"):
                _batch_writer_entry(str(self.store.db_path), str(self.store.catalog.directory),
                                    str(self.logger.directory), Queue(), 0.2, 100)
        self.assertEqual(self.acknowledged, 3)
        self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["_version"], 1)

    def test_mixed_tables_stations_and_partial_updates_preserve_latest_fields(self):
        reordered = self.update(value=12.5, status=3)
        reordered["key"] = {"pnt_no": 1, "rtu_id": 7}
        messages = [
            self.update(value=10.0, refresh_time=123),
            point_batch(
                self.update(rtu_id=8, status=4, value=80.0),
                reordered,
                self.update("fes_yx_define", value=1, status=2),
            ),
            self.update(refresh_time=124),
        ]
        original = deepcopy(messages)
        self.assertTrue(_flush_batch(self.store, self.logger, messages, self, self.connection))
        self.assertEqual(messages, original)
        row = self.store.get_row("fes_yc_define", [7, 1])
        self.assertEqual((row["value"], row["status"], row["refresh_time"], row["_version"]),
                         (12.5, 3, 124, 2))
        row = self.store.get_row("fes_yc_define", [8, 1])
        self.assertEqual((row["value"], row["status"], row["_version"]), (80.0, 4, 2))
        row = self.store.get_row("fes_yx_define", [7, 1])
        self.assertEqual((row["value"], row["status"], row["_version"]), (1, 2, 2))
        self.assertEqual(self.store.get_row("fes_yx_define", [8, 1])["_version"], 1)
        self.assertEqual(self.acknowledged, len(messages))

    def test_same_shape_with_different_field_order_binds_values_correctly(self):
        messages = [self.update(value=7.5, status=3), self.update(rtu_id=8, status=4, value=8.5)]
        self.assertTrue(_flush_batch(self.store, self.logger, messages, self, self.connection))
        for rtu_id, value, status in ((7, 7.5, 3), (8, 8.5, 4)):
            row = self.store.get_row("fes_yc_define", [rtu_id, 1])
            self.assertEqual((row["value"], row["status"], row["_version"]), (value, status, 2))

    def test_invalid_message_rejects_entire_batch_and_acknowledges_queue(self):
        bad_messages = [
            None, {"op": "unknown"}, {"op": "batch", "updates": []},
            {"op": "batch", "updates": [42]},
            {"op": "update", "table": "", "key": {"rtu_id": 7}, "values": {"value": 1}},
            {"op": "update", "table": "fes_yc_define", "key": {}, "values": {"value": 1}},
            {"op": "update", "table": "fes_yc_define", "key": {"rtu_id": []}, "values": {"value": 1}},
            self.update(),
        ]
        for bad in bad_messages:
            with self.subTest(message=bad):
                self.acknowledged = 0
                self.assertFalse(_flush_batch(self.store, self.logger,
                                              [self.update(value=20), bad], self, self.connection))
                row = self.store.get_row("fes_yc_define", [7, 1])
                self.assertEqual((row["value"], row["_version"]), (0, 1))
                self.assertFalse(self.connection.in_transaction)
                self.assertEqual(self.acknowledged, 2)

    def test_later_sql_failure_rolls_back_prior_groups(self):
        messages = [self.update(value=20), self.update("fes_yx_define", missing_column=1)]
        self.assertFalse(_flush_batch(self.store, self.logger, messages, self, self.connection))
        row = self.store.get_row("fes_yc_define", [7, 1])
        self.assertEqual((row["value"], row["_version"]), (0, 1))
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.acknowledged, 2)
        self.assertTrue(_flush_batch(self.store, self.logger, [self.update(value=30)],
                                    self, self.connection))
        self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["value"], 30)

    def test_busy_commit_rolls_back_and_retries_without_double_version_increment(self):
        real_connection = self.connection

        class BusyOnce:
            commits = 0

            def __getattr__(self, name):
                return getattr(real_connection, name)

            def commit(self):
                self.commits += 1
                if self.commits == 1:
                    raise sqlite3.OperationalError("database is locked")
                return real_connection.commit()

        connection = BusyOnce()
        with patch("scdfe_c.time.sleep"):
            self.assertTrue(_flush_batch(self.store, self.logger, [self.update(value=25)], self, connection))
        row = self.store.get_row("fes_yc_define", [7, 1])
        self.assertEqual((row["value"], row["_version"]), (25, 2))
        self.assertEqual(connection.commits, 2)
        self.assertEqual(self.acknowledged, 1)

    def test_connection_open_failure_acknowledges_batch(self):
        with patch.object(self.store, "open_connection", side_effect=sqlite3.OperationalError("cannot open")):
            self.assertFalse(_flush_batch(self.store, self.logger, [self.update(value=25)], self))
        self.assertEqual(self.acknowledged, 1)
        self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["_version"], 1)

    def test_group_rows_merge_with_single_updates_and_different_key_orders(self):
        first = {"table": "fes_yc_define", "key_names": ("rtu_id", "pnt_no"),
                 "value_names": ("value", "status"), "rows": [(10.0, 2, 7, 1), (80.0, 3, 8, 1)]}
        last = {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
                "value_names": ("value",), "rows": [(12.0, 1, 7), (13.0, 1, 7)]}
        messages = [{"op": "batch", "groups": [first]}, self.update(refresh_time=123),
                    {"op": "batch", "groups": [last]}]
        original = deepcopy(messages)
        self.assertTrue(_flush_batch(self.store, self.logger, messages, self, self.connection))
        self.assertEqual(messages, original)
        row = self.store.get_row("fes_yc_define", [7, 1])
        self.assertEqual((row["value"], row["status"], row["refresh_time"], row["_version"]),
                         (13.0, 2, 123, 2))
        self.assertEqual(self.store.get_row("fes_yc_define", [8, 1])["value"], 80.0)
        self.assertEqual(self.acknowledged, 3)

    def test_single_message_duplicates_merge_across_rows_and_partial_groups(self):
        groups = [
            {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
             "value_names": ("value", "status"),
             "rows": [(10.0, 2, 1, 7), [12.0, 3, 1, 7], (80.0, 4, 1, 8)]},
            {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
             "value_names": ("refresh_time",), "rows": [[123, 1, 7]]},
            {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
             "value_names": ("value",), "rows": [(13.0, 1, 7)]},
        ]
        messages = [{"op": "batch", "groups": groups}]
        original = deepcopy(messages)
        self.assertTrue(_flush_batch(self.store, self.logger, messages, self, self.connection))
        self.assertEqual(messages, original)
        row = self.store.get_row("fes_yc_define", [7, 1])
        self.assertEqual((row["value"], row["status"], row["refresh_time"], row["_version"]),
                         (13.0, 3, 123, 2))
        row = self.store.get_row("fes_yc_define", [8, 1])
        self.assertEqual((row["value"], row["status"], row["_version"]), (80.0, 4, 2))
        self.assertEqual(self.acknowledged, 1)

    def test_single_message_duplicate_across_groups_retains_previous_fields(self):
        groups = [
            {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
             "value_names": ("value", "status"), "rows": [(7.5, 2, 1, 7)]},
            {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
             "value_names": ("status", "refresh_time"), "rows": [(3, 123, 1, 7)]},
        ]
        self.assertTrue(_flush_batch(self.store, self.logger, [{"op": "batch", "groups": groups}],
                                     self, self.connection))
        row = self.store.get_row("fes_yc_define", [7, 1])
        self.assertEqual((row["value"], row["status"], row["refresh_time"], row["_version"]),
                         (7.5, 3, 123, 2))
        self.assertEqual(self.acknowledged, 1)

    def test_single_unique_message_combines_shapes_without_mixing_tables_or_stations(self):
        groups = [
            {"table": "fes_yc_define", "key_names": ["pnt_no", "rtu_id"],
             "value_names": ["value", "status"], "rows": [[7.5, 2, 1, 7]]},
            {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
             "value_names": ("value", "status"), "rows": [(8.5, 3, 1, 8)]},
            {"table": "fes_yx_define", "key_names": ("pnt_no", "rtu_id"),
             "value_names": ("value",), "rows": [(1, 1, 7)]},
        ]
        messages = [{"op": "batch", "groups": groups}]
        original = deepcopy(messages)
        self.assertTrue(_flush_batch(self.store, self.logger, messages, self, self.connection))
        self.assertEqual(messages, original)
        for rtu_id, value, status in ((7, 7.5, 2), (8, 8.5, 3)):
            row = self.store.get_row("fes_yc_define", [rtu_id, 1])
            self.assertEqual((row["value"], row["status"], row["_version"]), (value, status, 2))
        row = self.store.get_row("fes_yx_define", [7, 1])
        self.assertEqual((row["value"], row["_version"]), (1, 2))
        self.assertEqual(self.store.get_row("fes_yx_define", [8, 1])["_version"], 1)
        self.assertEqual(self.acknowledged, 1)

    def test_unique_marker_only_applies_within_one_group(self):
        for first_marked, last_marked in ((True, True), (True, False), (False, True)):
            for split_messages in (False, True):
                with self.subTest(first=first_marked, last=last_marked, split=split_messages):
                    groups = [
                        {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
                         "value_names": ("value", "status"), "rows": [(7.5, 2, 1, 7)],
                         "_unique_rows": first_marked},
                        {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
                         "value_names": ("status", "refresh_time"), "rows": [(3, 123, 1, 7)],
                         "_unique_rows": last_marked},
                    ]
                    messages = ([{"op": "batch", "groups": [group]} for group in groups]
                                if split_messages else [{"op": "batch", "groups": groups}])
                    original = deepcopy(messages)
                    version = self.store.get_row("fes_yc_define", [7, 1])["_version"]
                    self.acknowledged = 0
                    self.assertTrue(_flush_batch(self.store, self.logger, messages, self, self.connection))
                    row = self.store.get_row("fes_yc_define", [7, 1])
                    self.assertEqual((row["value"], row["status"], row["refresh_time"], row["_version"]),
                                     (7.5, 3, 123, version + 1))
                    self.assertEqual(self.acknowledged, len(messages))
                    self.assertEqual(messages, original)

    def test_unique_marker_keeps_row_validation_and_list_conversion(self):
        group = {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
                 "value_names": ("value",), "rows": [[7.5, 1, 7]], "_unique_rows": True}
        for bad_row in (42, "bad", (7.5, 1), (7.5, 1, 7, 8), (7.5, [], 7)):
            with self.subTest(row=bad_row):
                self.acknowledged = 0
                messages = [{"op": "batch", "groups": [{**group, "rows": [group["rows"][0], bad_row]}]}]
                self.assertFalse(_flush_batch(self.store, self.logger, messages, self, self.connection))
                row = self.store.get_row("fes_yc_define", [7, 1])
                self.assertEqual((row["value"], row["_version"]), (0, 1))
                self.assertEqual(self.acknowledged, 1)
                self.assertFalse(self.connection.in_transaction)
        messages = [{"op": "batch", "groups": [group]}]
        original = deepcopy(messages)
        self.assertTrue(_flush_batch(self.store, self.logger, messages, self, self.connection))
        row = self.store.get_row("fes_yc_define", [7, 1])
        self.assertEqual((row["value"], row["_version"]), (7.5, 2))
        self.assertEqual(messages, original)

    def test_shared_parameters_bind_reordered_slots_and_keys(self):
        group = {"table": "fes_yc_define", "key_names": ("rtu_id", "pnt_no"),
                 "value_names": ("value", "status", "raw_status", "refresh_time"),
                 "value_indexes": (2, 0, 0, 1),
                 "rows": [(3, 123, 7.5, 7, 1), [4, 124, 8.5, 8, 1]]}
        messages = [{"op": "batch", "groups": [group]}]
        original = deepcopy(messages)
        self.assertTrue(_flush_batch(self.store, self.logger, messages, self, self.connection))
        for rtu_id, value, status, stamp in ((7, 7.5, 3, 123), (8, 8.5, 4, 124)):
            row = self.store.get_row("fes_yc_define", [rtu_id, 1])
            self.assertEqual((row["value"], row["raw_status"], row["status"],
                              row["refresh_time"], row["_version"]), (value, status, status, stamp, 2))
        self.assertEqual(messages, original)

    def test_shared_parameters_merge_partial_overrides_without_changing_other_columns(self):
        group = {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
                 "value_names": ("raw_value", "value", "status", "raw_status"),
                 "value_indexes": (0, 1, 2, 2), "rows": [(7.5, 10.25, 3, 1, 7)]}
        last = {"table": "fes_yc_define", "key_names": ("pnt_no", "rtu_id"),
                "value_names": ("raw_time", "refresh_time"),
                "value_indexes": (0, 0), "rows": [(123, 1, 7)]}
        messages = [{"op": "batch", "groups": [group]}, self.update(value=8.5, status=4),
                    {"op": "batch", "groups": [last]}]
        original = deepcopy(messages)
        self.assertTrue(_flush_batch(self.store, self.logger, messages, self, self.connection))
        row = self.store.get_row("fes_yc_define", [7, 1])
        self.assertEqual((row["raw_value"], row["value"], row["status"],
                          row["raw_status"], row["raw_time"], row["refresh_time"], row["_version"]),
                         (7.5, 8.5, 4, 3, 123, 123, 2))
        self.assertEqual(messages, original)
        self.assertEqual(self.acknowledged, 3)

    def test_invalid_shared_parameter_indexes_reject_entire_batch(self):
        valid = point_batch(self.update(value=20))["groups"][0]
        for indexes in (None, "0", (), (0, 0), (-1,), (1,), (True,), (0.0,), ("0",), ([0],)):
            with self.subTest(indexes=indexes):
                self.acknowledged = 0
                bad = {**valid, "value_indexes": indexes}
                self.assertFalse(_flush_batch(self.store, self.logger,
                                              [{"op": "batch", "groups": [valid, bad]}],
                                              self, self.connection))
                self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["_version"], 1)
                self.assertFalse(self.connection.in_transaction)
                self.assertEqual(self.acknowledged, 1)

    def test_invalid_group_rejects_whole_batch_before_writing(self):
        valid = point_batch(self.update(value=20))["groups"][0]
        invalid = [
            None,
            {**valid, "rows": [(20, 7)]},
            {**valid, "rows": [(20, [], 1)]},
            {**valid, "rows": []},
            {**valid, "rows": [42]},
            {**valid, "key_names": ()},
            {**valid, "key_names": ("rtu_id", "rtu_id")},
            {**valid, "value_names": ("value", "value")},
            {**valid, "value_names": ("value; DROP TABLE fes_yc_define",)},
            {**valid, "table": "fes_yc_define;--"},
        ]
        for bad in invalid:
            with self.subTest(group=bad):
                self.acknowledged = 0
                self.assertFalse(_flush_batch(self.store, self.logger,
                                              [{"op": "batch", "groups": [valid, bad]}],
                                              self, self.connection))
                self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["_version"], 1)
                self.assertFalse(self.connection.in_transaction)
                self.assertEqual(self.acknowledged, 1)


if __name__ == "__main__":
    unittest.main()

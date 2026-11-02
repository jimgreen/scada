"""scdfe_c 通道管理层的离线回归测试。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import pickle
from pathlib import Path
from queue import Full
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import scdfe_c as runtime
from scdfe_c import (
    ChannelSpec,
    ChannelWorker,
    ScdfeLogger,
    ScdfeSupervisor,
    definition_signature,
    discover_channels,
)
from scdfe_c import _worker_thread_entry
from web.service import ScadaStore


@dataclass
class Value:
    kind: str
    point_no: int
    value: object
    quality: int = 0


class CaptureWriterQueue:
    def __init__(self):
        self.messages = []
        self.fail_next = False
        self.acknowledged = 0

    def put(self, message, **_kwargs):
        if self.fail_next:
            self.fail_next = False
            raise Full
        self.messages.append(pickle.loads(pickle.dumps(message)))

    def task_done(self):
        self.acknowledged += 1


class FakeMaster:
    def __init__(self):
        self.connected = False
        self.closed = False
        self.yk = []
        self.yt = []

    def connect(self):
        self.connected = True
        return self

    def close(self):
        self.closed = True
        self.connected = False

    def collect_once(self):
        return [Value("yc", 1, 221.5), Value("yx", 1, True)]

    def send_yk_point(self, point_no, value):
        self.yk.append((point_no, value))
        return True

    def send_yt_point(self, point_no, value):
        self.yt.append((point_no, value))
        return True


class ScdfeCTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ScadaStore(self.root / "scada.sqlite3")
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 7, "channel_id": 70, "channel_type": "modbus-tcp",
            "name": "测试站",
        })
        self.store.create_row("fes_channel_modbus_tcp_define", {
            "rtu_id": 7, "remote_host": "127.0.0.1", "remote_port": 1502,
            "unit_id": 1, "response_timeout": 0.2,
        })
        self.store.create_row("fes_yc_define", {
            "rtu_id": 7, "pnt_no": 1, "name": "电压", "value": 0.0,
        })
        self.store.create_row("fes_yx_define", {
            "rtu_id": 7, "pnt_no": 1, "name": "运行", "value": 0,
        })
        self.store.create_row("fes_yk_define", {
            "rtu_id": 7, "pnt_no": 1, "name": "开关", "value": 0, "cmd_time": 0,
        })
        self.store.create_row("fes_yt_define", {
            "rtu_id": 7, "pnt_no": 1, "name": "设定", "value": 0.0, "cmd_time": 0,
        })
        self.addCleanup(self.temp.cleanup)

    def test_discover_channels_and_definition_signature_ignores_runtime_values(self):
        specs = discover_channels(self.store)
        self.assertEqual([(item.rtu_id, item.channel_id, item.channel_type)
                          for item in specs], [(7, 70, "modbus-tcp")])
        before = definition_signature(self.store, 7)
        self.store.update_row("fes_yc_define", [7, 1], {
            "value": 333.0, "refresh_time": int(time.time() * 1000),
        })
        self.assertEqual(before, definition_signature(self.store, 7))
        self.store.update_row("fes_yc_define", [7, 1], {"name": "新电压"})
        self.assertNotEqual(before, definition_signature(self.store, 7))

    def test_worker_exits_after_definition_change_for_supervisor_reload(self):
        previous_interval = runtime.WORKER_DEFINITION_CHECK_INTERVAL
        runtime.WORKER_DEFINITION_CHECK_INTERVAL = 0.0
        self.addCleanup(setattr, runtime, "WORKER_DEFINITION_CHECK_INTERVAL", previous_interval)

        class DefinitionChangingMaster(FakeMaster):
            def __init__(self, store):
                super().__init__()
                self.store = store
                self.collected = False

            def collect_once(self):
                if not self.collected:
                    self.collected = True
                    self.store.update_row("fes_rtu_define", [7], {"name": "测试站-定义已变更"})
                return []

        master = DefinitionChangingMaster(self.store)
        worker = ChannelWorker(
            self.store, ChannelSpec(7, 70, "modbus-tcp", "测试站"),
            log_dir=self.root / "logs", poll_interval=0,
            master_factory=lambda _store, _spec: master,
        )
        stats = worker.run(max_cycles=2)
        self.assertEqual(stats.cycles, 1)
        self.assertTrue(master.closed)
        self.assertEqual(self.store.get_row("fes_rtu_define", [7])["status"], 0)

    def test_per_process_hourly_log_and_retention_cleanup(self):
        log_dir = self.root / "logs"
        logger = ScdfeLogger(log_dir, process_name="scdfe_c-rtu-7")
        logger.normal("normal")
        logger.alarm("alarm")
        logger.error("error")
        logger.process("process")
        files = list(log_dir.rglob("*.log"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^scdfe_c-rtu-7-\d{8}-\d{2}\.log$")
        self.assertEqual(len(files[0].relative_to(log_dir).parts), 5)
        content = files[0].read_text(encoding="utf-8")
        self.assertRegex(content, r"(?m)^\d{2}:\d{2}:\d{2} \[(?:日志|警告|错误|进程)\]")
        self.assertNotRegex(content, r"(?m)^\d{4}-\d{2}-\d{2}T")
        self.assertNotIn("+08:00", content)
        self.assertIn("[日志]", content)
        self.assertIn("[警告]", content)
        self.assertIn("[错误]", content)
        self.assertNotRegex(content, r"(?:pid|rtu|channel|chanel)=\d+")
        old = log_dir / "scdfe_c-main-20200101-00.log"
        old.write_text("old\n", encoding="utf-8")
        os.utime(old, (time.time() - 3 * 86400, time.time() - 3 * 86400))
        removed = ScdfeLogger.cleanup(log_dir, retention_hours=1, now=time.time())
        self.assertIn(old, removed)
        self.assertFalse(old.exists())

    def test_worker_channel_change_and_failure_share_rtu_log(self):
        log_dir = self.root / "logs"
        with patch.object(runtime, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 1, 9, 0).astimezone()
            for rtu_id, channel_id in ((7, 70), (7, 71), (8, 80)):
                worker = ChannelWorker(
                    self.store, ChannelSpec(rtu_id, channel_id, "modbus-tcp", "测试站"),
                    log_dir=log_dir,
                )
                worker.logger.process("channel definition loaded")
            with patch.object(runtime, "ScadaStore", side_effect=RuntimeError("startup failed")):
                _worker_thread_entry(
                    str(self.store.db_path), str(self.store.catalog.directory),
                    {"rtu_id": 7, "channel_id": 71, "channel_type": "modbus-tcp",
                     "name": "测试站"},
                    str(log_dir), 1, 1, threading.Event(),
                )
        self.assertEqual({p.relative_to(log_dir).as_posix() for p in log_dir.rglob("*.log")}, {
            "2026/11/01/09/scdfe_c-rtu-7-20261101-09.log",
            "2026/11/01/09/scdfe_c-rtu-8-20261101-09.log",
        })
        info = ScdfeLogger.read_file(log_dir, "2026/11/01/09/scdfe_c-rtu-7-20261101-09.log")
        self.assertEqual(info["rtu_id"], 7)
        self.assertNotIn("channel_id", info)
        self.assertNotRegex(info["content"], r"(?:pid|rtu|channel|chanel)=\d+")
        self.assertIn("startup failed", info["content"])
        self.assertNotIn("rtu=8", info["content"])

    def test_worker_updates_measurements_and_sends_changed_commands(self):
        master = FakeMaster()
        spec = ChannelSpec(7, 70, "modbus-tcp", "测试站")
        worker = ChannelWorker(
            self.store, spec, log_dir=self.root / "logs",
            master_factory=lambda _store, _spec: master,
            poll_interval=0,
        )
        self.addCleanup(worker._close_command_connection)
        stats = worker.run(max_cycles=1)
        self.assertEqual((stats.cycles, stats.updated_yc, stats.updated_yx), (1, 1, 1))
        self.assertTrue(master.closed)
        self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["value"], 221.5)
        self.assertEqual(self.store.get_row("fes_yx_define", [7, 1])["value"], 1)
        self.assertEqual(master.yk, [])
        self.assertEqual(master.yt, [])

        # 命令的 cmd_time/value 变化会被识别；单独的状态回写不会重复下发。
        worker._master = master
        worker._command_seen = {
            ("yk", 1): (0, 0), ("yt", 1): (0, 0.0),
        }
        self.store.update_row("fes_yk_define", [7, 1], {"value": 1, "cmd_time": 123})
        self.store.update_row("fes_yt_define", [7, 1], {"value": 12.5, "cmd_time": 124})
        self.assertEqual(worker._sync_commands(), (1, 1))
        self.assertEqual(master.yk, [(1, True)])
        self.assertEqual(master.yt, [(1, 12.5)])
        self.assertEqual(worker._sync_commands(), (0, 0))

        pending_master = FakeMaster()
        pending_worker = ChannelWorker(
            self.store, spec, log_dir=self.root / "logs",
            master_factory=lambda _store, _spec: pending_master,
        )
        self.addCleanup(pending_worker._close_command_connection)
        pending_worker._master = pending_master
        self.store.update_row("fes_yk_define", [7, 1], {
            "value": 0, "cmd_time": 200, "cmd_handle_time": 100,
        })
        self.assertEqual(pending_worker._sync_commands(), (1, 0))
        self.assertEqual(pending_master.yk, [(1, False)])

    def test_worker_queues_one_batch_for_a_measurement_cycle(self):
        class Queue:
            def __init__(self):
                self.messages = []

            def put(self, message, **_kwargs):
                self.messages.append(message)

        queue = Queue()
        worker = ChannelWorker(
            self.store, ChannelSpec(7, 70, "modbus-tcp", "测试站"),
            log_dir=self.root / "logs", writer_queue=queue,
        )
        values = [Value("yc", 1, 221.5), Value("yx", 1, True)]
        self.assertEqual(worker._sync_measurements(values), (1, 1))
        self.assertEqual(len(queue.messages), 1)
        self.assertEqual(queue.messages[0]["op"], "batch")
        self.assertTrue(queue.messages[0]["_preserve_versions"])
        self.assertEqual(sum(len(g["rows"]) for g in queue.messages[0]["groups"]), 2)
        self.assertTrue(all(g["_unique_rows"] for g in queue.messages[0]["groups"]))

    def test_worker_skips_stable_measurements_until_heartbeat(self):
        class Queue:
            def __init__(self):
                self.messages = []

            def put(self, message, **_kwargs):
                self.messages.append(message)

        queue = Queue()
        worker = ChannelWorker(
            self.store, ChannelSpec(7, 70, "modbus-tcp", "测试站"),
            log_dir=self.root / "logs", writer_queue=queue,
            measurement_heartbeat_interval=60,
        )
        values = [Value("yc", 1, 221.5), Value("yx", 1, True)]
        self.assertEqual(worker._sync_measurements(values), (1, 1))
        self.assertEqual(worker._sync_measurements(values), (0, 0))
        self.assertEqual(len(queue.messages), 1)
        changed = [Value("yc", 1, 222.5), Value("yx", 1, True)]
        self.assertEqual(worker._sync_measurements(changed), (1, 0))
        self.assertEqual(len(queue.messages), 2)
        worker._measurement_cache[("yc", 1)] = (222.5, 222.5, 0, time.monotonic() - 61)
        worker._measurement_cache[("yx", 1)] = (1, 1, 0, time.monotonic() - 61)
        self.assertEqual(worker._sync_measurements(changed), (1, 1))
        heartbeat_groups = queue.messages[2]["groups"]
        self.assertEqual(set(heartbeat_groups[0]["value_names"]),
                         {"refresh_time"})

        # A zero interval keeps an explicit full-write mode for installations
        # that require every poll to refresh all fields.
        every_poll_queue = Queue()
        every_poll_worker = ChannelWorker(
            self.store, ChannelSpec(7, 70, "modbus-tcp", "测试站"),
            log_dir=self.root / "logs", writer_queue=every_poll_queue,
            measurement_heartbeat_interval=0,
        )
        self.assertEqual(every_poll_worker._sync_measurements(values), (1, 1))
        self.assertEqual(every_poll_worker._sync_measurements(values), (1, 1))
        self.assertEqual(len(every_poll_queue.messages), 2)

    def test_packed_rows_preserve_partial_updates_and_are_detached_from_inputs(self):
        class Queue:
            def __init__(self):
                self.messages = []
                self.acknowledged = 0

            def put(self, message, **_kwargs):
                self.messages.append(message)

            def task_done(self):
                self.acknowledged += 1

        queue = Queue()
        worker = ChannelWorker(self.store, ChannelSpec(7, 70, "custom"),
                               log_dir=self.root / "logs", writer_queue=queue)
        first = {"value": 10.0, "status": 3, "refresh_time": 123}
        rows = [("fes_yc_define", 7, 1, first), ("fes_yx_define", 7, 1, {"value": 1}),
                ("fes_yc_define", 7, 1, {"value": 12.0})]
        self.assertEqual(worker._write_rows(rows), [True, True, True])
        first.update(value=999.0, status=99, refresh_time=999)
        # Exercise the transport round trip; no live input dictionaries should
        # remain referenced by the queued payload waiting for its feeder.
        messages = pickle.loads(pickle.dumps(queue.messages))
        self.assertEqual(runtime._batch_message_size(messages[0]), 2)
        self.assertTrue(runtime._flush_batch(self.store, worker.logger, messages, queue))
        yc = self.store.get_row("fes_yc_define", [7, 1])
        self.assertEqual((yc["value"], yc["status"], yc["refresh_time"], yc["_version"]),
                         (12.0, 3, 123, 2))
        self.assertEqual(self.store.get_row("fes_yx_define", [7, 1])["value"], 1)
        self.assertEqual(queue.acknowledged, 1)

    def test_worker_accepts_mapping_measurements(self):
        class Queue:
            def __init__(self):
                self.messages = []

            def put(self, message, **_kwargs):
                self.messages.append(message)

        queue = Queue()
        worker = ChannelWorker(
            self.store, ChannelSpec(7, 70, "modbus-tcp", "测试站"),
            log_dir=self.root / "logs", writer_queue=queue,
            measurement_heartbeat_interval=0,
        )
        self.assertEqual(worker._sync_measurements([
            {"kind": "yc", "point_no": 1, "value": 12.5},
            {"kind": "yx", "point_no": 1, "value": True, "quality": None},
        ]), (1, 1))
        self.assertEqual(len(queue.messages), 1)

    def test_measurement_fields_match_for_queued_and_direct_writes(self):
        self.store.update_row("fes_yc_define", [7, 1], {"raw_base": 2.0, "raw_scale": 1.1})
        for queued in (False, True):
            with self.subTest(queued=queued):
                queue = CaptureWriterQueue() if queued else None
                worker = ChannelWorker(self.store, ChannelSpec(7, 70, "custom"),
                                       log_dir=self.root / "logs", writer_queue=queue)
                with patch("scdfe_c._now_ms", return_value=123456):
                    self.assertEqual(worker._sync_measurements([
                        Value("yc", 1, 12.5, 4), Value("yx", 1, True, 8),
                    ]), (1, 1))
                if queue is not None:
                    self.assertTrue(runtime._flush_batch(self.store, worker.logger, queue.messages, queue))
                yc = self.store.get_row("fes_yc_define", [7, 1])
                yx = self.store.get_row("fes_yx_define", [7, 1])
                self.assertEqual(yc["raw_value"], 12.5)
                self.assertEqual(yc["value"], 2.0 + 1.1 * 12.5)
                self.assertNotEqual(yc["raw_value"], yc["value"])
                for field in ("raw_status", "status"):
                    self.assertEqual(yc[field], 4, field)
                for field in ("raw_time", "refresh_time"):
                    self.assertEqual(yc[field], 123456, field)
                for field in ("value",):
                    self.assertEqual(yx[field], 1, field)
                for field in ("status",):
                    self.assertEqual(yx[field], 8, field)
                for field in ("refresh_time",):
                    self.assertEqual(yx[field], 123456, field)

    def test_scaling_reload_and_heartbeat_preserve_raw_sample(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                self.store.update_row("fes_yc_define", [7, 1], {"raw_base": 2.0, "raw_scale": 1.1})
                queue = CaptureWriterQueue() if queued else None
                worker = ChannelWorker(self.store, ChannelSpec(7, 70, "custom"),
                                       log_dir=self.root / "logs", writer_queue=queue,
                                       measurement_heartbeat_interval=0)

                def sync(stamp):
                    with patch("scdfe_c._now_ms", return_value=stamp):
                        self.assertEqual(worker._sync_measurements([Value("yc", 1, 12.5, 3)]), (1, 0))
                    if queue:
                        self.assertTrue(runtime._flush_batch(self.store, worker.logger, queue.messages, queue))
                        queue.messages.clear()

                sync(100)
                sync(200)
                row = self.store.get_row("fes_yc_define", [7, 1])
                self.assertEqual((row["raw_time"], row["refresh_time"]), (100, 200))
                before = definition_signature(self.store, 7)
                self.store.update_row("fes_yc_define", [7, 1], {"raw_base": -5.0, "raw_scale": 0.0})
                self.assertNotEqual(before, definition_signature(self.store, 7))
                worker._load_yc_scaling()
                sync(300)
                row = self.store.get_row("fes_yc_define", [7, 1])
                self.assertEqual((row["raw_value"], row["value"], row["raw_time"]), (12.5, -5.0, 300))

    def test_invalid_scaling_or_sample_is_isolated_to_one_point(self):
        worker = ChannelWorker(self.store, ChannelSpec(7, 70, "custom"),
                               log_dir=self.root / "logs")
        worker._load_yc_scaling()
        for base, scale, value in ((0, 1, float("nan")), (0, 1, float("inf")),
                                   (0, float("inf"), 10), (0, "invalid", 10)):
            worker._yc_scaling[1] = (base, scale)
            worker._measurement_cache.clear()
            self.assertEqual(worker._sync_measurements([
                Value("yc", 1, value), Value("yx", 1, True),
            ]), (0, 1))
            self.assertNotIn(("yc", 1), worker._measurement_cache)
        self.assertEqual(worker.stats.errors, 4)

    def test_duplicate_measurements_keep_full_update_across_heartbeats(self):
        samples = [
            (None, [Value("yc", 1, 12.5, 3), Value("yc", 1, 12.5, 3)], 12.5, 3),
            ((12.5, 12.5, 3, 0), [Value("yc", 1, 12.5, 3), Value("yc", 1, 14.0, 4)], 14.0, 4),
            ((12.5, 12.5, 3, 0), [Value("yc", 1, 14.0, 4), Value("yc", 1, 12.5, 3)], 12.5, 3),
        ]
        for cached, values, expected_value, expected_quality in samples:
            with self.subTest(cached=cached, values=values):
                queue = CaptureWriterQueue()
                worker = ChannelWorker(self.store, ChannelSpec(7, 70, "custom"),
                                       log_dir=self.root / "logs", writer_queue=queue,
                                       measurement_heartbeat_interval=0)
                worker._load_yc_scaling()
                if cached is not None:
                    worker._measurement_cache[("yc", 1)] = cached
                before = self.store.get_row("fes_yc_define", [7, 1])["_version"]
                with patch("scdfe_c._now_ms", return_value=321):
                    self.assertEqual(worker._sync_measurements(values), (2, 0))
                self.assertEqual(runtime._batch_message_size(queue.messages[0]), 1)
                self.assertTrue(runtime._flush_batch(self.store, worker.logger, queue.messages, queue))
                row = self.store.get_row("fes_yc_define", [7, 1])
                self.assertEqual((row["value"], row["raw_value"], row["status"], row["refresh_time"],
                                  row["_version"]),
                                 (expected_value, expected_value, expected_quality, 321, before + 1))

    def test_full_queue_leaves_changed_and_heartbeat_points_eligible_for_retry(self):
        queue = CaptureWriterQueue()
        worker = ChannelWorker(self.store, ChannelSpec(7, 70, "custom"),
                               log_dir=self.root / "logs", writer_queue=queue)
        worker._load_yc_scaling()
        worker._measurement_cache = {("yc", 1): (12.5, 12.5, 0, 0), ("yx", 1): (1, 1, 0, 0)}
        original = dict(worker._measurement_cache)
        values = [Value("yc", 1, 15.5), Value("yx", 1, True)]
        queue.fail_next = True
        with patch("scdfe_c.time.monotonic", return_value=100):
            with self.assertRaises(Full):
                worker._sync_measurements(values)
            self.assertEqual(worker._measurement_cache, original)
            self.assertEqual(queue.messages, [])
            self.assertEqual(worker._sync_measurements(values), (1, 1))
            self.assertEqual(worker._sync_measurements(values), (0, 0))
        self.assertEqual(len(queue.messages), 1)
        self.assertEqual(runtime._batch_message_size(queue.messages[0]), 2)
        yx_group = next(g for g in queue.messages[0]["groups"] if g["table"] == "fes_yx_define")
        self.assertEqual(set(yx_group["value_names"]), {"refresh_time"})

    def test_bad_measurement_does_not_block_other_points_in_same_sample(self):
        queue = CaptureWriterQueue()
        worker = ChannelWorker(self.store, ChannelSpec(7, 70, "custom"),
                               log_dir=self.root / "logs", writer_queue=queue)
        self.assertEqual(worker._sync_measurements([
            Value("yc", 1, 12.5), Value("yc", 2, "invalid"), Value("yx", 1, True),
        ]), (1, 1))
        self.assertEqual(worker.stats.errors, 1)
        self.assertNotIn(("yc", 2), worker._measurement_cache)
        self.assertTrue(runtime._flush_batch(self.store, worker.logger, queue.messages, queue))
        self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["value"], 12.5)
        self.assertEqual(self.store.get_row("fes_yx_define", [7, 1])["value"], 1)

    def test_command_poll_skips_rows_when_command_versions_are_unchanged(self):
        worker = ChannelWorker(
            self.store, ChannelSpec(7, 70, "modbus-tcp", "测试站"),
            log_dir=self.root / "logs",
        )
        self.addCleanup(worker._close_command_connection)
        first = worker._command_rows()
        self.assertIsNotNone(first)
        worker._sync_commands()
        self.assertIsNone(worker._command_rows())
        self.store.update_row("fes_yk_define", [7, 1], {"value": 1, "cmd_time": 123})
        changed = worker._command_rows()
        self.assertIsNotNone(changed)
        self.assertEqual(changed[0][1]["value"], 1)

    def test_worker_entry_catches_unexpected_thread_exception(self):
        # A bad schema path fails during thread-local store construction.  The
        # entry point must log and return instead of raising into the scheduler
        # or terminating unrelated communication threads.
        _worker_thread_entry(
            str(self.store.db_path), str(self.root / "missing-schema"),
            {"rtu_id": 7, "channel_id": 70, "channel_type": "modbus-tcp",
             "name": "测试站"},
            str(self.root / "logs"), 0.0, 0.1, threading.Event(), None,
        )
        self.assertTrue((self.root / "logs").exists())

    def test_one_failed_communication_thread_does_not_stop_another(self):
        release = threading.Event()
        healthy_started = threading.Event()

        def broken():
            _worker_thread_entry(
                str(self.store.db_path), str(self.root / "missing-schema"),
                {"rtu_id": 7, "channel_id": 70, "channel_type": "modbus-tcp",
                 "name": "失败通道"},
                str(self.root / "logs"), 0.0, 0.1, threading.Event(), None,
            )

        def healthy():
            healthy_started.set()
            release.wait(2)

        broken_thread = threading.Thread(target=broken, name="scdfe-c-broken", daemon=True)
        healthy_thread = threading.Thread(target=healthy, name="scdfe-c-healthy", daemon=True)
        broken_thread.start()
        healthy_thread.start()
        broken_thread.join(1)
        self.assertFalse(broken_thread.is_alive())
        self.assertTrue(healthy_started.wait(1))
        self.assertTrue(healthy_thread.is_alive())
        release.set()
        healthy_thread.join(1)

    def test_channel_exception_isolated_from_other_channel_worker(self):
        class BrokenMaster:
            def connect(self):
                raise RuntimeError("broken channel")

            def close(self):
                return None

        bad_stop = threading.Event()
        good_stop = threading.Event()
        bad_worker = ChannelWorker(
            self.store, ChannelSpec(7, 70, "modbus-tcp", "bad"),
            log_dir=self.root / "logs", poll_interval=0.01, retry_interval=0.02,
            master_factory=lambda _store, _spec: BrokenMaster(), stop_event=bad_stop,
        )
        good_worker = ChannelWorker(
            self.store, ChannelSpec(7, 70, "modbus-tcp", "good"),
            log_dir=self.root / "logs", poll_interval=0.01, retry_interval=0.02,
            master_factory=lambda _store, _spec: FakeMaster(), stop_event=good_stop,
        )
        good_result = []
        bad_thread = threading.Thread(target=bad_worker.run, name="scdfe-c-bad", daemon=True)
        good_thread = threading.Thread(
            target=lambda: good_result.append(good_worker.run(max_cycles=2)),
            name="scdfe-c-good", daemon=True,
        )
        bad_thread.start()
        good_thread.start()
        good_thread.join(3)
        self.assertFalse(good_thread.is_alive())
        self.assertEqual(good_result[0].cycles, 2)
        self.assertTrue(bad_thread.is_alive())
        bad_stop.set()
        bad_thread.join(2)
        self.assertFalse(bad_thread.is_alive())

    def test_supervisor_can_start_and_stop_empty_or_filtered_configuration(self):
        # 仅验证主调度线程构造、扫描和停止边界；真实协议连接由通信线程和现场设备决定。
        supervisor = ScdfeSupervisor(
            self.store.db_path, schema_dir=self.store.catalog.directory,
            log_dir=self.root / "logs", scan_interval=0.1, retry_interval=0.1,
        )
        self.addCleanup(supervisor.stop)
        result = supervisor.reconcile()
        self.assertEqual(result["started"], 1)
        self.assertEqual(result["running"], 1)
        self.assertIsNotNone(supervisor._writer_process)
        self.assertTrue(supervisor._writer_process.is_alive())
        worker = next(iter(supervisor.workers.values()))
        self.assertTrue(worker.thread.is_alive())
        expected_thread_id = worker.thread.native_id or worker.thread.ident
        self.assertEqual(self.store.get_row("fes_rtu_define", [7])["process_pid"], expected_thread_id)
        supervisor.stop()
        self.assertEqual(supervisor.workers, {})
        self.assertIsNone(supervisor._writer_process)

    def test_supervisor_rebuilds_writer_and_workers_after_writer_failure(self):
        supervisor = ScdfeSupervisor(
            self.store.db_path, schema_dir=self.store.catalog.directory,
            log_dir=self.root / "logs", scan_interval=0.1, retry_interval=0.1,
        )
        self.addCleanup(supervisor.stop)
        self.assertEqual(supervisor.reconcile()["started"], 1)
        old_writer = supervisor._writer_process
        old_worker = next(iter(supervisor.workers.values())).thread
        self.assertIsNotNone(old_writer)
        old_writer.terminate()
        old_writer.join(5)
        self.assertFalse(old_writer.is_alive())

        result = supervisor.reconcile()

        self.assertEqual(result["started"], 1)
        self.assertEqual(result["running"], 1)
        self.assertIsNot(supervisor._writer_process, old_writer)
        self.assertTrue(supervisor._writer_process.is_alive())
        self.assertIsNot(supervisor.workers[next(iter(supervisor.workers))].thread, old_worker)

    def test_worker_interval_changes_reload_existing_worker(self):
        supervisor = ScdfeSupervisor(
            self.store.db_path, schema_dir=self.store.catalog.directory,
            log_dir=self.root / "logs", scan_interval=0.1,
            poll_interval=1.0, retry_interval=5.0,
        )
        self.addCleanup(supervisor.stop)
        self.assertEqual(supervisor.reconcile()["started"], 1)
        key = next(iter(supervisor.workers))
        old_thread = supervisor.workers[key].thread
        old_thread_id = old_thread.ident

        self.store.update_row("fes_scdfe_c_define", [1], {
            "poll_interval_ms": 500, "retry_interval_ms": 100,
        })
        result = supervisor.reconcile()

        self.assertEqual(result["started"], 1)
        self.assertEqual(result["running"], 1)
        self.assertAlmostEqual(supervisor.poll_interval, 0.5)
        self.assertAlmostEqual(supervisor.retry_interval, 0.1)
        self.assertIsNot(supervisor.workers[key].thread, old_thread)
        self.assertNotEqual(supervisor.workers[key].thread.ident, old_thread_id)

    def test_supervisor_spaces_out_sequential_worker_starts(self):
        class RecordingThread:
            def __init__(self, starts):
                self.starts = starts
                self.daemon = False
                self.ident = id(self)
                self.native_id = self.ident
                self._alive = False

            def start(self):
                self.starts.append(time.monotonic())
                self._alive = True

            def is_alive(self):
                return self._alive

            def join(self, timeout=None):
                del timeout
                self._alive = False

            def terminate(self):
                self._alive = False
        starts = []
        def thread_factory(*args, **kwargs):
            del args, kwargs
            return RecordingThread(starts)

        self.store.create_row("fes_rtu_define", {
            "rtu_id": 8, "channel_id": 80, "channel_type": "modbus-tcp",
            "name": "测试站2",
        })
        supervisor = ScdfeSupervisor(
            self.store.db_path, schema_dir=self.store.catalog.directory,
            log_dir=self.root / "logs", retry_interval=0.1,
            worker_start_delay=0.08, thread_factory=thread_factory,
        )
        self.addCleanup(supervisor.stop)
        supervisor._start_worker(ChannelSpec(7, 70, "modbus-tcp", "测试站"))
        supervisor._start_worker(ChannelSpec(8, 80, "modbus-tcp", "测试站2"))

        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - starts[0], 0.07)

    def test_rtu_process_enable_stops_child_without_manual_restart_operation(self):
        supervisor = ScdfeSupervisor(
            self.store.db_path, schema_dir=self.store.catalog.directory,
            log_dir=self.root / "logs", scan_interval=0.1, retry_interval=0.1,
        )
        self.addCleanup(supervisor.stop)
        self.assertEqual(supervisor.reconcile()["started"], 1)
        self.store.update_row("fes_rtu_define", [7], {"process_enable": 0})
        result = supervisor.reconcile()
        self.assertEqual(result["stopped"], 1)
        state = self.store.get_row("fes_rtu_define", [7])
        self.assertEqual((state["process_enable"], state["process_status"], state["process_pid"]),
                         (0, 0, 0))

        self.store.update_row("fes_rtu_define", [7], {"process_enable": 1})
        result = supervisor.reconcile()
        self.assertEqual(result["started"], 1)
        state = self.store.get_row("fes_rtu_define", [7])
        self.assertIn(state["process_status"], (1, 2, 5))

if __name__ == "__main__":
    unittest.main()

"""Hourly process logs under idle, busy and paused operation (temporary DBs)."""
from datetime import datetime, timedelta
import os
from pathlib import Path
from queue import Empty
import tempfile
import time
import unittest
from unittest.mock import patch

import scdfe_c as runtime
from core.process_registry import PROCESS_TABLE
from core.scdfe_control import CONTROL_TABLE
from web.service import ScadaStore


class Clock:
    def __init__(self):
        self.local = datetime(2026, 11, 1, 23, 59, 50).astimezone()
        self.elapsed = 100.0

    def advance(self, seconds):
        self.elapsed += seconds
        self.local += timedelta(seconds=seconds)

    def now(self, *_args):
        return self.local

    def monotonic(self):
        return self.elapsed


class ScriptedQueue:
    """Drive the real writer loop without sleeps or platform queue internals."""
    def __init__(self, clock, events):
        self.clock = clock
        self.events = iter(events)
        self.acknowledged = 0

    def get(self, *, timeout):
        seconds, message = next(self.events)
        self.clock.advance(seconds)
        if message is Empty:
            raise Empty
        return message

    def task_done(self):
        self.acknowledged += 1


class HourlyLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.logs = self.root / "logs"
        self.store = ScadaStore(self.root / "scada.sqlite3")
        self.clock = Clock()

    def content(self, process, hour):
        return (self.logs / f"scdfe_c-{process}-{hour}.log").read_text(encoding="utf-8")

    def writer(self, queue, max_batch=500):
        with patch.object(runtime, "datetime", self.clock), \
                patch.object(runtime.time, "monotonic", self.clock.monotonic):
            runtime._batch_writer_entry(
                str(self.store.db_path), str(self.store.catalog.directory),
                str(self.logs), queue, max_batch=max_batch,
            )

    def test_main_and_writer_use_one_timestamp_for_line_and_hour_at_midnight(self):
        before = self.clock.local
        after = before + timedelta(seconds=10)
        for role in ("main", "db-writer"):
            logger = runtime.ScdfeLogger(self.logs, process_name=f"scdfe_c-{role}")
            with patch.object(runtime, "datetime") as clock:
                clock.now.side_effect = [before, after, after, after]
                logger.process("before midnight")
                logger.normal("after midnight")
                logger.alarm("warning")
                logger.error("error")
                self.assertEqual(clock.now.call_count, 4)
            old = self.content(role, "20261101-23")
            new = self.content(role, "20261102-00")
            self.assertEqual(len(old.splitlines()), 1)
            self.assertIn("23:59:50 [PROCESS]", old)
            self.assertEqual(len(new.splitlines()), 3)
            self.assertTrue(all(line.startswith("00:00:00 ") for line in new.splitlines()))
            self.assertTrue(all("+08:00" not in line and "2026-" not in line for line in old + new))
            for level in ("INFO", "WARNING", "ERROR"):
                self.assertIn(f"[{level}]", new)
        self.assertEqual(len(list(self.logs.glob("*.log"))), 4)

    def test_idle_writer_rolls_hours_and_throttles_summaries(self):
        queue = ScriptedQueue(self.clock, [
            (1, Empty), (8, Empty), (21, Empty), (30, Empty),
            (0, runtime.BATCH_WRITER_STOP),
        ])
        self.writer(queue)
        self.assertEqual(queue.acknowledged, 1)
        old = self.content("db-writer", "20261101-23")
        new = self.content("db-writer", "20261102-00")
        self.assertEqual(old.count("运行摘要"), 1)
        self.assertEqual(new.count("运行摘要"), 2)
        self.assertIn("累计成功批次=0 消息数=0 输入更新数=0 待写消息=0 待写更新=0", new)
        self.assertEqual(self.store.get_row(PROCESS_TABLE, ["writer"])["process_status"], 0)

    def test_busy_writer_counts_threshold_timeout_and_final_flush(self):
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 7, "channel_id": 70, "channel_type": "custom",
        })
        self.store.create_row("fes_yc_define", {"rtu_id": 7, "pnt_no": 1})

        def update(value):
            return {"op": "batch", "groups": [{
                "table": "fes_yc_define", "key_names": ("rtu_id", "pnt_no"),
                "value_names": ("value",), "rows": [(value, 7, 1)],
            }]}

        queue = ScriptedQueue(self.clock, [
            (0, update(1)), (30, update(2)),  # threshold flush
            (0, update(3)), (30, Empty),  # timeout flush
            (30, update(4)), (0, runtime.BATCH_WRITER_STOP),  # final flush
        ])
        self.writer(queue, max_batch=2)
        self.assertEqual(queue.acknowledged, 5)
        self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["value"], 4)
        content = self.content("db-writer", "20261102-00")
        self.assertIn("累计成功批次=1 消息数=2 输入更新数=2", content)
        self.assertIn("累计成功批次=2 消息数=3 输入更新数=3 待写消息=1 待写更新=1", content)
        self.assertIn("累计成功批次=3 消息数=4 输入更新数=4 待写消息=0 待写更新=0", content)

    def test_paused_writer_still_allows_main_hourly_logs_and_retention(self):
        supervisor = runtime.ScdfeSupervisor(self.store.db_path, log_dir=self.logs)
        self.store.update_row(PROCESS_TABLE, ["writer"], {"process_enable": 0})
        self.store.update_row(CONTROL_TABLE, [1], {"log_retention_hours": 1})
        old_files = []
        for role in ("main", "db-writer"):
            path = self.logs / f"scdfe_c-{role}-20200101-00.log"
            path.write_text("expired", encoding="utf-8")
            expired = time.time() - 7200
            os.utime(path, (expired, expired))
            old_files.append(path)
        with patch.object(runtime, "datetime", self.clock), \
                patch.object(runtime.time, "monotonic", self.clock.monotonic), \
                patch.object(runtime.ScdfeLogger, "cleanup", wraps=runtime.ScdfeLogger.cleanup) as cleanup:
            for seconds in (0, 1, 29, 30):
                self.clock.advance(seconds)
                supervisor.reconcile()
            self.assertEqual(cleanup.call_count, 2)
        self.assertTrue(all(not path.exists() for path in old_files))
        self.assertEqual(self.content("main", "20261101-23").count("运行摘要"), 1)
        new = self.content("main", "20261102-00")
        self.assertEqual(new.count("运行摘要"), 2)
        self.assertIn("通信线程=0/0 写库进程=停止 写库pid=0", new)
        self.assertEqual(list(self.logs.glob("scdfe_c-db-writer-*.log")), [])
        self.assertFalse(supervisor.stop_event.is_set())

    def test_main_logs_during_long_scan_wait_and_uses_changed_log_directory(self):
        supervisor = runtime.ScdfeSupervisor(self.store.db_path)
        self.store.update_row(PROCESS_TABLE, ["writer"], {"process_enable": 0})
        self.store.update_row(CONTROL_TABLE, [1], {"scan_interval_ms": 60000})
        waits = []

        def wait(seconds):
            waits.append(seconds)
            self.clock.advance(seconds)
            if len(waits) == 2:
                self.store.update_row(CONTROL_TABLE, [1], {"log_dir": "new-logs"})
            return False

        with patch.object(runtime, "datetime", self.clock), \
                patch.object(runtime.time, "monotonic", self.clock.monotonic), \
                patch.object(supervisor.stop_event, "wait", side_effect=wait):
            supervisor.run(max_scans=2)
            self.assertEqual(waits, [30, 30])
            self.assertEqual(self.content("main", "20261102-00").count("运行摘要"), 2)
        new_file = self.root / "new-logs" / "scdfe_c-main-20261102-00.log"
        self.assertIn("主进程运行摘要", new_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the cross-platform scdfe_c writer IPC bridge."""
from __future__ import annotations

import multiprocessing as mp
from pathlib import Path
import tempfile
import unittest

from scdfe_c import BATCH_WRITER_STOP, _batch_writer_entry, _writer_submit_command
from scdfe_c.web.service import ScadaStore
from scdfe_c.writer_ipc import WriterClient, WriterRequestError, WriterUnavailable, endpoint_path


def _run_writer(db: str, schema: str, logs: str, queue, ready) -> None:
    _batch_writer_entry(db, schema, logs, queue, 0.03, 100, ready)


class WriterIpcTests(unittest.TestCase):
    def test_writer_rejects_non_positive_points_and_nonfinite_yt(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "scada.sqlite3"
            store = ScadaStore(db)
            store.create_row("fes_rtu_define", {
                "rtu_id": 1, "channel_id": 10, "channel_type": "iec104",
            })
            store.create_row("fes_yt_define", {
                "rtu_id": 1, "pnt_no": 1, "name": "设定", "value": 0.0,
                "man_disable": 0, "auto_disable": 0, "cmd_time": 0,
                "cmd_handle_time": 0, "eng_min": 0.0, "eng_max": 100.0,
            })
            with store.connection() as connection:
                for point_no in (0, -1):
                    with self.assertRaises(ValueError):
                        _writer_submit_command(store, connection, {
                            "rtu_id": 1, "kind": "yt", "point_no": point_no, "value": 1.0,
                        })
                for value in (None, float("nan"), float("inf"), float("-inf"), 10 ** 400):
                    with self.assertRaises(ValueError):
                        _writer_submit_command(store, connection, {
                            "rtu_id": 1, "kind": "yt", "point_no": 1, "value": value,
                        })

    def test_writer_accepts_boolean_yk_and_rejects_unsupported_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "scada.sqlite3"
            store = ScadaStore(db)
            store.create_row("fes_rtu_define", {
                "rtu_id": 1, "channel_id": 10, "channel_type": "can",
            })
            store.create_row("fes_yk_define", {
                "rtu_id": 1, "pnt_no": 1, "name": "开关", "value": 0,
                "man_disable": 0, "auto_disable": 0, "cmd_time": 0, "cmd_handle_time": 0,
            })
            with store.connection() as connection:
                item = _writer_submit_command(store, connection, {
                    "rtu_id": 1, "kind": "yk", "point_no": 1, "value": True,
                })
            self.assertEqual(item["value"], 1)
            store.update_row("fes_rtu_define", [1], {"channel_type": "custom"})
            with store.connection() as connection:
                with self.assertRaises(ValueError):
                    _writer_submit_command(store, connection, {
                        "rtu_id": 1, "kind": "yk", "point_no": 1, "value": False,
                    })

    def test_command_is_committed_and_endpoint_removed_on_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "scada.sqlite3"
            store = ScadaStore(db)
            store.create_row("fes_rtu_define", {
                "rtu_id": 1, "channel_id": 10, "channel_type": "iec104",
            })
            store.create_row("fes_yk_define", {
                "rtu_id": 1, "pnt_no": 1, "name": "开关", "value": 0,
                "man_disable": 0, "auto_disable": 0, "cmd_time": 0, "cmd_handle_time": 0,
            })
            context = mp.get_context("spawn")
            queue = context.JoinableQueue()
            ready = context.Event()
            process = context.Process(target=_run_writer, args=(
                str(db), str(store.catalog.directory), str(root / "logs"), queue, ready,
            ))
            process.start()
            try:
                self.assertTrue(ready.wait(8))
                item = WriterClient(db).submit_command(1, "yk", 1, 1)
                self.assertEqual(item["value"], 1)
                self.assertEqual(store.get_row("fes_yk_define", [1, 1])["value"], 1)
                queue.put(BATCH_WRITER_STOP)
                queue.join()
                process.join(8)
                self.assertEqual(process.exitcode, 0)
                self.assertFalse(endpoint_path(db).exists())
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(3)
                queue.close()
                queue.join_thread()

    def test_rejected_or_stopped_writer_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "scada.sqlite3"
            ScadaStore(db)
            with self.assertRaises(WriterUnavailable):
                WriterClient(db).submit_command(1, "yk", 1, 1)


if __name__ == "__main__":
    unittest.main()

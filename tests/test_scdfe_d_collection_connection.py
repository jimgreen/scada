from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import time
import unittest

from scdfe_d.service import Collector
from tests import test_scdfe_d as fixtures


class CollectionConnectionTests(unittest.TestCase):
    setUp = fixtures.DeviceTests.setUp

    def _wait(self, predicate):
        deadline = time.monotonic() + 8
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("background collector did not reach the requested state")
            time.sleep(0.02)

    def _latest_batch(self):
        last = self.store.overview()["last_refresh"]
        return last["refresh_id"] if last else 0

    def test_periodic_writer_reuse_recovery_and_close(self):
        wal = Path(str(self.store.db_path) + "-wal")
        with self.store.connection() as con:
            con.execute("PRAGMA journal_mode=WAL")
        open_connection = self.store._open_connection
        opens = []

        def track_writer():
            con = open_connection()
            if threading.current_thread().name == "scdfe-d-collector":
                opens.append(con)
            return con

        self.store._open_connection = track_writer
        collector = Collector(self.store)
        self.addCleanup(collector.close)
        collector.start(1)
        self._wait(lambda: self._latest_batch() >= 2)
        self.assertEqual(len(opens), 1)
        self.assertTrue(wal.is_file())
        with self.store.connection() as con:
            con.execute(
                """CREATE TRIGGER fail_test_scan BEFORE UPDATE ON Manual_generator
                BEGIN SELECT RAISE(ABORT,'test rollback'); END"""
            )
        before = self._latest_batch()
        # 触发源点变化迫使设备表执行 UPDATE，从而验证事务回滚与连接重建恢复
        with closing(sqlite3.connect(self.source)) as src, src:
            src.execute(
                "UPDATE fes_yc_define SET value=value+1,refresh_time=refresh_time+10"
            )
        self._wait(lambda: "test rollback" in collector.last_error)
        self.assertEqual(self._latest_batch(), before)
        with self.store.connection() as con:
            con.execute("DROP TRIGGER fail_test_scan")
        self._wait(lambda: self._latest_batch() > before and not collector.last_error)
        self.assertGreaterEqual(len(opens), 2)
        collector.close()
        self.assertFalse(wal.exists())
        self.assertFalse(collector.running)
        self.assertGreater(Collector(self.store).once()["refresh_id"], before)

    def test_persistent_writer_releases_transactions_for_auto_checkpoint(self):
        with self.store.connection() as con:
            con.execute("PRAGMA journal_mode=WAL")
        wal = Path(str(self.store.db_path) + "-wal")
        with closing(self.store._open_connection()) as writer:
            page_size = writer.execute("PRAGMA page_size").fetchone()[0]
            writer.execute("PRAGMA wal_autocheckpoint=16")
            for i in range(16):
                with closing(sqlite3.connect(self.source)) as src, src:
                    src.execute(
                        "UPDATE fes_yc_define SET value=?,refresh_time=refresh_time+1",
                        (10 + i,),
                    )
                self.store.collect_once(_writer=writer)
                self.assertFalse(writer.in_transaction)
            with closing(sqlite3.connect(self.store.db_path)) as other:
                busy, frames, checkpointed = other.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)"
                ).fetchone()
                self.assertEqual((busy, frames), (0, checkpointed))
            self.assertLess(wal.stat().st_size, 32 * (page_size + 24))

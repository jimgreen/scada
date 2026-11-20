from contextlib import closing
import sqlite3
import unittest

from tests import test_scdfe_d as fixtures


class CollectorWriteTests(unittest.TestCase):
    setUp = fixtures.DeviceTests.setUp

    def test_unchanged_sample_advances_batch_without_rewriting_device_row(self):
        first = self.store.collect_once()
        with self.store.connection() as connection:
            connection.execute("CREATE TABLE write_log (id INTEGER PRIMARY KEY)")
            connection.execute("""CREATE TRIGGER observe_device_writes AFTER UPDATE ON Manual_generator
                BEGIN INSERT INTO write_log VALUES(NULL); END""")

        second = self.store.collect_once()
        self.assertGreater(second["refresh_id"], first["refresh_id"])
        with self.store.connection() as connection:
            batch = connection.execute(
                "SELECT refresh_id FROM measurement_value"
            ).fetchone()[0]
            writes = connection.execute("SELECT count(*) FROM write_log").fetchone()[0]
            self.assertEqual(batch, second["refresh_id"])
            self.assertEqual(writes, 0)

        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute(
                "UPDATE fes_yc_define SET value=12,refresh_time=refresh_time+1"
            )
        self.store.collect_once()
        with self.store.connection() as connection:
            written = connection.execute(
                "SELECT p,p_pre,p_qual,p_rtu,p_pnt FROM Manual_generator"
            ).fetchone()
            self.assertEqual(tuple(written), (12, 10, 0, 1, 1))
            self.assertEqual(
                connection.execute("SELECT count(*) FROM write_log").fetchone()[0], 1
            )

    def test_missing_rebound_point_clears_old_binding_runtime_fields(self):
        self.store.collect_once()
        with self.store.connection() as connection:
            connection.execute("UPDATE Manual_generator SET p_pnt=99 WHERE idx=1")
        self.store.collect_once()
        with self.store.connection() as connection:
            data = connection.execute(
                "SELECT p,p_qual,p_pre,p_time,p_pnt FROM Manual_generator"
            ).fetchone()
            self.assertEqual(tuple(data), (None, 128, None, None, 99))

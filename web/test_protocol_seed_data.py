"""All-protocol bulk test data seeding regression tests."""
from pathlib import Path
import tempfile
import unittest

from web.seed_protocol_test_data import seed_protocol_test_data
from web.service import ScadaStore


class ProtocolSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ScadaStore(Path(self.temp.name) / "scada.sqlite3")

    def test_each_protocol_station_has_100_100_20_20_points(self):
        report = seed_protocol_test_data(self.store)
        self.assertEqual(report["rtu_count"], 6)
        self.assertEqual(report["points_per_rtu"], {"yc": 100, "yx": 100, "yk": 20, "yt": 20})
        with self.store.connection() as connection:
            stations = connection.execute(
                "SELECT rtu_id, channel_type FROM fes_rtu_define WHERE code LIKE 'TEST_%' ORDER BY rtu_id"
            ).fetchall()
            self.assertEqual([row[1] for row in stations],
                             ["iec104", "modbus-tcp", "modbus-rtu", "can", "bacnet", "opc"])
            for row in stations:
                counts = [connection.execute(
                    f"SELECT COUNT(*) FROM fes_{kind}_define WHERE rtu_id=?", (row[0],)
                ).fetchone()[0] for kind in ("yc", "yx", "yk", "yt")]
                self.assertEqual(counts, [100, 100, 20, 20])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM fes_channel_modbus_define_data"
            ).fetchone()[0], 400)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM fes_channel_modbus_define_ytyk"
            ).fetchone()[0], 80)

    def test_repeat_does_not_modify_or_create_backup(self):
        first = seed_protocol_test_data(self.store)
        second = seed_protocol_test_data(self.store)
        self.assertGreater(sum(first["added"].values()), 0)
        self.assertEqual(sum(second["added"].values()), 0)
        self.assertIsNone(second["backup"])


if __name__ == "__main__":
    unittest.main()

"""All-protocol bulk test data seeding regression tests."""
from pathlib import Path
from collections import Counter
import tempfile
import unittest

from web.seed_protocol_test_data import seed_protocol_test_data
from web.service import ScadaStore


class ProtocolSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ScadaStore(Path(self.temp.name) / "scada.sqlite3")

    def test_each_protocol_station_has_ten_rtu_replicas(self):
        report = seed_protocol_test_data(self.store)
        self.assertEqual(report["rtu_count"], 60)
        self.assertEqual(report["points_per_rtu"], {"yc": 1000, "yx": 1000, "yk": 200, "yt": 200})
        with self.store.connection() as connection:
            stations = connection.execute(
                "SELECT rtu_id, channel_type FROM fes_rtu_define WHERE code LIKE 'TEST_%' ORDER BY rtu_id"
            ).fetchall()
            self.assertEqual(len(stations), 60)
            self.assertEqual(
                {channel_type: sum(row[1] == channel_type for row in stations)
                 for channel_type in {row[1] for row in stations}},
                {"iec104": 10, "modbus-tcp": 10, "modbus-rtu": 10,
                 "can": 10, "bacnet": 10, "opc": 10},
            )
            for row in stations:
                counts = [connection.execute(
                    f"SELECT COUNT(*) FROM fes_{kind}_define WHERE rtu_id=?", (row[0],)
                ).fetchone()[0] for kind in ("yc", "yx", "yk", "yt")]
                self.assertEqual(counts, [1000, 1000, 200, 200])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM fes_channel_modbus_define_data"
            ).fetchone()[0], 40000)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM fes_channel_modbus_define_ytyk"
            ).fetchone()[0], 8000)
            for table in ("fes_yc_define", "fes_yx_define", "fes_yk_define", "fes_yt_define"):
                self.assertEqual(connection.execute(
                    f"SELECT COUNT(*) - COUNT(DISTINCT code) FROM {table}"
                ).fetchone()[0], 0)
            for table, count in (("fes_yc_define", 60000), ("fes_yt_define", 12000)):
                self.assertEqual(tuple(connection.execute(
                    f"SELECT COUNT(DISTINCT raw_value), COUNT(DISTINCT value), "
                    f"SUM(raw_value=value), SUM(value != raw_base+raw_scale*raw_value) FROM {table}"
                ).fetchone()), (count, count, 0, 0))
            for table in ("fes_yc_define", "fes_yx_define"):
                columns = self.store.catalog.public(table)["columns"]
                self.assertFalse(any(c["eng_name"].startswith(
                    ("value_dev", "status_dev", "refresh_time_dev")) for c in columns))
                self.assertEqual(connection.execute(
                    f"SELECT COUNT(DISTINCT status) FROM {table}"
                ).fetchone()[0], 4)
            for protocol in ("modbus-tcp", "modbus-rtu"):
                from api import ModbusTcpMaster, ModbusRtuMaster
                cls = ModbusTcpMaster if protocol == "modbus-tcp" else ModbusRtuMaster
                for rtu_id, kind in stations:
                    if kind == protocol:
                        # Constructor validates the full poll plan without I/O.
                        master = cls.from_store(self.store, rtu_id)
                        master.close()
            opc = connection.execute(
                "SELECT chan_addr, opc_name FROM fes_rtu_define r "
                "JOIN fes_channel_opc_define p ON r.rtu_id=p.rtu_id"
            ).fetchall()
            self.assertTrue(all(a == b for a, b in opc))
            self.assertEqual(len({a for a, _ in opc}), 10)
            bacnet_ports = connection.execute(
                "SELECT chan_port, bind_port FROM fes_rtu_define r "
                "JOIN fes_channel_bacnet_define p ON r.rtu_id=p.rtu_id"
            ).fetchall()
            self.assertEqual(len({p for ports in bacnet_ports for p in ports}), 20)

    def test_repeat_does_not_modify_or_create_backup(self):
        # Expand an existing six-station database, preserving user-edited rows.
        seed_protocol_test_data(self.store, replicas=1)
        self.store.update_row("fes_yc_define", [1, 1], {"name": "用户电压", "value": 123.5})
        with self.store.connection() as connection:
            before = {table: [tuple(r) for r in connection.execute(f'SELECT * FROM {table}')]
                      for table in self.store.catalog.names()}
        first = seed_protocol_test_data(self.store)
        self.assertEqual(first["added"]["fes_rtu_define"], 54)
        with self.store.connection() as connection:
            for table, rows in before.items():
                after = Counter(tuple(r) for r in connection.execute(f'SELECT * FROM {table}'))
                self.assertFalse(Counter(rows) - after, table)
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM fes_rtu_define WHERE process_enable=0'
            ).fetchone()[0], 54)
        second = seed_protocol_test_data(self.store)
        self.assertGreater(sum(first["added"].values()), 0)
        self.assertEqual(sum(second["added"].values()), 0)
        self.assertIsNone(second["backup"])


if __name__ == "__main__":
    unittest.main()

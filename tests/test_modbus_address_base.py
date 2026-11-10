"""Acceptance checks for persisted Modbus reference-address conventions."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

from client.modbus_tcp import ModbusTcpMaster, normalize_modbus_address
from scdfe_t.trans import TransPoint
from server.modbus_trans_server import ModbusTcpTransServer
from tools.migrate_modbus_address_base import migrate


class ModbusAddressBaseTests(unittest.TestCase):
    def test_reference_bases_convert_to_wire_addresses(self):
        self.assertEqual(normalize_modbus_address(0), 0)
        self.assertEqual(normalize_modbus_address(1, 1), 0)
        self.assertEqual(normalize_modbus_address(10001, 10001), 0)
        self.assertEqual(normalize_modbus_address(30001, 30001), 0)
        self.assertEqual(normalize_modbus_address(40002, 40001), 1)
        with self.assertRaises(ValueError):
            normalize_modbus_address(40000, 40001)
        with self.assertRaises(ValueError):
            normalize_modbus_address(1, 2)

    def test_poll_plan_uses_zero_based_wire_address(self):
        master = ModbusTcpMaster("127.0.0.1", rtu_id=7)
        self.addCleanup(master.close)
        requests = []
        master._invoke = lambda name, address, count, unit: (
            requests.append((name, address, count, unit)) or [321]
        )
        master.configure(
            [{"rtu_id": 7, "call_seq": 0, "iplace": 40001,
              "address_base": 40001, "inum": 1, "icode": 3}],
            [{"rtu_id": 7, "call_seq": 0, "cnt": 1, "reg_addr": 40001,
              "trans_type": 2}],
        )
        self.assertEqual(master.collect_once()[0].value, 321)
        self.assertEqual(requests, [("read_holding_registers", 0, 1, 1)])

    def test_write_definition_uses_zero_based_wire_address(self):
        master = ModbusTcpMaster("127.0.0.1", rtu_id=7)
        self.addCleanup(master.close)
        requests = []
        master._invoke = lambda name, address, argument, unit: (
            requests.append((name, address, argument, unit)) or True
        )
        master.configure([], [], [{
            "rtu_id": 7, "ifyk": 0, "cnt": 1, "reg_addr": 40001,
            "address_base": 40001, "icode": 6, "ytyk_type": 2,
        }])
        self.assertTrue(master.send_yt_point(1, 123))
        self.assertEqual(requests, [("write_single_register", 0, 123, 1)])

        master.configure([], [], [{
            "rtu_id": 7, "ifyk": 1, "cnt": 1, "reg_addr": 40001,
            "address_base": 40001, "icode": 15, "ytyk_type": 1,
        }])
        self.assertTrue(master.send_yk_point(1, True))
        self.assertEqual(requests[-1], ("write_multiple_coils", 0, [True], 1))

    def test_transmission_mapping_normalizes_reference_address(self):
        point = TransPoint.from_row({
            "channel_id": 1, "pnt_no": 1, "kind": "yc", "source_rtu_id": 7,
            "source_pnt_no": 1, "address": 40001, "address_base": 40001,
            "register_area": "input", "data_type": "int16",
        })
        self.assertEqual((point.address, point.address_base), (40001, 40001))
        server = ModbusTcpTransServer(
            host="127.0.0.1", port=0,
            points=[point.__dict__], point_values={("yc", 1): 12},
        )
        self.addCleanup(server.close)
        self.assertEqual(server._mappings[("yc", 1)]["address"], 0)
        self.assertIn(("input", 0), server._by_address)

    def test_old_database_migration_adds_columns_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "legacy.sqlite3"
            con = sqlite3.connect(db)
            tables = (
                "fes_channel_modbus_define",
                "fes_channel_modbus_define_call",
                "fes_channel_modbus_define_data",
                "fes_channel_modbus_define_ytyk",
                "fes_scdfe_t_point_define",
            )
            con.execute("CREATE TABLE _scada_schema(table_name TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)")
            for table in tables:
                con.execute(f"CREATE TABLE {table}(id INTEGER)")
                con.execute("INSERT INTO _scada_schema VALUES (?, 'legacy')", (table,))
            con.commit()
            con.close()

            plan = migrate(db)
            self.assertTrue(plan["missing"]["fes_channel_modbus_define"])
            self.assertTrue(plan["fingerprint_mismatch"])
            self.assertFalse(plan["changed"])

            applied = migrate(db, apply=True)
            self.assertTrue(applied["changed"])
            self.assertTrue(Path(applied["backup"]).is_file())
            with closing(sqlite3.connect(db)) as check:
                for table in tables:
                    columns = {row[1] for row in check.execute(f"PRAGMA table_info({table})")}
                    if table != "fes_channel_modbus_define_data":
                        self.assertIn("address_base", columns)
                self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")

            second = migrate(db, apply=True)
            self.assertFalse(second["changed"])
            self.assertIsNone(second["backup"])


if __name__ == "__main__":
    unittest.main()

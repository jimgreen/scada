import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from scdfe_c.web.service import ScadaStore
from tools.remove_rdfid_fields import migrate


TABLES = (
    "fes_channel_bacnet_define",
    "fes_channel_can_define",
    "fes_channel_can_frame_define",
    "fes_channel_iec104_define",
    "fes_channel_modbus_define",
    "fes_channel_modbus_define_call",
    "fes_channel_modbus_define_ytyk",
    "fes_channel_modbus_rtu_define",
    "fes_channel_modbus_tcp_define",
    "fes_yc_define",
    "fes_yk_define",
    "fes_yt_define",
    "fes_yx_define",
)


class RemoveRdfidFieldsTests(unittest.TestCase):
    def test_plan_apply_preserves_rows_and_removes_rdfid_from_every_table(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "scada.sqlite3"
            store = ScadaStore(db)
            store.create_row("fes_rtu_define", {
                "rtu_id": 7, "channel_id": 70, "channel_type": "custom", "name": "测试站",
            })
            store.create_row("fes_yc_define", {
                "rtu_id": 7, "pnt_no": 1, "name": "电压", "value": 12.5,
            })
            store.close()
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("BEGIN")
                for table in TABLES:
                    connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "rdfid" TEXT')
                connection.execute(
                    'UPDATE "fes_yc_define" SET "rdfid"=? WHERE "rtu_id"=? AND "pnt_no"=?',
                    ("legacy-point", 7, 1),
                )
                connection.commit()

            plan = migrate(db)
            self.assertEqual(plan["tables"], list(TABLES))
            self.assertFalse(plan["changed"])
            applied = migrate(db, apply=True)
            self.assertTrue(applied["changed"])
            self.assertTrue(Path(applied["backup"]).is_file())

            with closing(sqlite3.connect(db)) as connection:
                for table in TABLES:
                    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                    self.assertNotIn("rdfid", columns, table)
                self.assertEqual(
                    connection.execute(
                        'SELECT value FROM "fes_yc_define" WHERE "rtu_id"=7 AND "pnt_no"=1'
                    ).fetchone()[0],
                    12.5,
                )
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

            current = ScadaStore(db)
            current.close()
            second = migrate(db, apply=True)
            self.assertFalse(second["changed"])
            self.assertIsNone(second["backup"])


if __name__ == "__main__":
    unittest.main()

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from scdfe_c.web.service import ScadaStore
from tools.migrate_scdfe_t_point_runtime import migrate


class ScdfeTPointRuntimeSchemaTests(unittest.TestCase):
    def test_existing_database_migration_adds_live_point_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_schema = root / "old-schema"
            shutil.copytree(Path("dbdef/json"), old_schema)
            path = old_schema / "fes_scdfe_t_point_define.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            runtime_names = {
                "value", "status", "refresh_time", "forward_time", "command_receive_time",
            }
            payload["columns"] = [
                column for column in payload["columns"]
                if column["eng_name"] not in runtime_names
            ]
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            db = root / "old.sqlite3"
            old = ScadaStore(db, schema_dir=old_schema)
            old.create_row("fes_scdfe_t_define", {
                "channel_id": 1, "name": "旧通道", "protocol": "modbus-tcp",
            })
            old.create_row("fes_scdfe_t_point_define", {
                "channel_id": 1, "pnt_no": 1, "kind": "yc",
                "source_rtu_id": 1, "source_pnt_no": 1,
            })
            old.close()

            plan = migrate(db, Path("dbdef/json"))
            self.assertEqual(set(plan["missing"]), runtime_names)
            self.assertFalse(plan["changed"])

            result = migrate(db, Path("dbdef/json"), apply=True)
            self.assertTrue(result["changed"])
            self.assertTrue(Path(result["backup"]).is_file())

            current = ScadaStore(db)
            try:
                row = current.get_row("fes_scdfe_t_point_define", [1, 1, "yc"])
                self.assertEqual(
                    {name: row[name] for name in runtime_names},
                    {
                        "value": 0.0,
                        "status": 128,
                        "refresh_time": 0,
                        "forward_time": 0,
                        "command_receive_time": 0,
                    },
                )
            finally:
                current.close()


if __name__ == "__main__":
    unittest.main()

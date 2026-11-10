import json
from pathlib import Path
import re
import tempfile
import unittest

from scdfe_c.web.service import SCHEMA_DIR, ScadaStore, SchemaCatalog
from tools.migrate_long_to_int import plan


ROOT = Path(__file__).resolve().parents[1]
LEGACY_TOKEN = re.compile(r"(?<![A-Za-z0-9_])LONG(?![A-Za-z0-9_])")


class SchemaIntTypeTests(unittest.TestCase):
    def test_authoritative_schema_and_exports_have_no_legacy_long_type(self):
        for path in sorted(SCHEMA_DIR.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            self.assertFalse(
                any(column.get("disp_type") == "LONG" for column in payload["columns"]),
                path.name,
            )
            for column in payload["columns"]:
                if column["data_type"] == "int":
                    self.assertNotEqual(column.get("disp_type"), "LONG", column)

        for path in [*sorted((ROOT / "dbdef" / "def").glob("*.py")),
                     *sorted((ROOT / "dbdef").glob("*.dbdef"))]:
            self.assertIsNone(LEGACY_TOKEN.search(path.read_text(encoding="utf-8")), path)

        migration = plan(ROOT, ROOT / "db" / "scada.sqlite3")
        self.assertEqual(migration["long_count"], 0)
        self.assertFalse(migration["changed"])

    def test_integer_metadata_maps_to_integer_sqlite_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ScadaStore(Path(directory) / "schema.sqlite3")
            self.addCleanup(store.close)
            with store.connection() as connection:
                for table in store.catalog.names():
                    payload = store.catalog.get(table)
                    sqlite_columns = {
                        row[1]: row[2]
                        for row in connection.execute(f'PRAGMA table_info("{table}")')
                    }
                    for column in payload["columns"]:
                        if column["data_type"] in {"int", "bool"}:
                            self.assertEqual(sqlite_columns[column["eng_name"]], "INTEGER", (table, column))

    def test_catalog_rejects_legacy_long_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            schema_dir = Path(directory)
            payload = {
                "head": {"chn_name": "测试", "eng_name": "legacy"},
                "columns": [{
                    "chn_name": "标识", "eng_name": "id", "primary_key": True,
                    "data_type": "int", "disp_type": "LONG", "disp_menu": "",
                }],
            }
            (schema_dir / "legacy.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "LONG.*INT"):
                SchemaCatalog(schema_dir)


if __name__ == "__main__":
    unittest.main()

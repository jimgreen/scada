import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "optimize_schema_indexes", ROOT / "tools" / "optimize_schema_indexes.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class SchemaIndexOptimizationTests(unittest.TestCase):
    def test_command_history_legacy_index_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scada.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE _scada_schema(table_name TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
                CREATE TABLE fes_ytyk_message(
                    _version INTEGER NOT NULL DEFAULT 1,
                    cmd_time INTEGER NOT NULL,
                    is_yk INTEGER NOT NULL,
                    rtu_id INTEGER NOT NULL,
                    pnt_no INTEGER NOT NULL,
                    name TEXT, value REAL, source INTEGER, logs TEXT,
                    PRIMARY KEY(cmd_time,is_yk,rtu_id,pnt_no)
                );
                CREATE INDEX fes_ytyk_message_rtu ON fes_ytyk_message(rtu_id);
                """
            )
            connection.execute(
                "INSERT INTO _scada_schema VALUES ('fes_ytyk_message', 'old')"
            )
            connection.executemany(
                "INSERT INTO fes_ytyk_message(cmd_time,is_yk,rtu_id,pnt_no,value) VALUES (?,?,?,?,?)",
                [(100 + i, 0, 7, i, float(i)) for i in range(200)]
            )
            connection.commit()
            connection.close()

            result = MODULE.apply(path)
            self.assertIsNotNone(result["backup"])
            self.assertEqual(result["integrity_check"], "ok")
            self.assertEqual(result["foreign_key_violations"], 0)
            connection = sqlite3.connect(path)
            indexes = [row[1] for row in connection.execute(
                "PRAGMA index_list(fes_ytyk_message)"
            )]
            self.assertNotIn("fes_ytyk_message_rtu", indexes)
            self.assertNotIn("fes_ytyk_message_idx_1_rtu_id_cmd_time", indexes)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM fes_ytyk_message").fetchone()[0],
                200,
            )
            connection.close()

            second = MODULE.apply(path)
            self.assertIsNone(second["backup"])
            self.assertTrue(second["already_optimized"])

    def test_legacy_history_table_is_not_given_a_new_index(self):
        payload = json.loads(
            (ROOT / "dbdef" / "json" / "fes_ytyk_message.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("indexes", payload)


if __name__ == "__main__":
    unittest.main()

import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
import gc

from web.service import ScadaStore
from tools.remove_main_restart_fields import migrate


class RemoveMainRestartFieldsTests(unittest.TestCase):
    def test_migration_removes_all_restart_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "scada.sqlite3"
            store = ScadaStore(db)
            del store
            gc.collect()
            with closing(sqlite3.connect(db)) as connection:
                for table in ("fes_rtu_define", "fes_scdfe_c_define", "fes_process_define"):
                    for column in ("process_restart", "process_restart_time", "process_restart_count"):
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} INTEGER DEFAULT 0"
                        )
                connection.commit()

            report = migrate(db)
            self.assertEqual(report["integrity_check"], "ok")
            self.assertTrue(Path(report["backup"]).is_file())
            with closing(sqlite3.connect(db)) as connection:
                fields = {
                    table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    for table in ("fes_rtu_define", "fes_scdfe_c_define", "fes_process_define")
                }
                for table in fields:
                    self.assertFalse({"process_restart", "process_restart_time", "process_restart_count"} & fields[table])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()

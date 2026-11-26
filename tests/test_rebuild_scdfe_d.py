import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from scdfe_d.service import DeviceStore
from tools.rebuild_scdfe_d import rebuild


class RebuildDeviceDatabaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.model = self.root / "models.db"
        with closing(sqlite3.connect(self.model)) as connection, connection:
            connection.execute(
                "CREATE TABLE rtdb_schema_tables (table_name TEXT, ordinal INTEGER)"
            )
            connection.execute(
                "INSERT INTO rtdb_schema_tables VALUES ('ACGenerator',0)"
            )
            connection.execute(
                "CREATE TABLE rtdb_schema_columns (table_name TEXT, ordinal INTEGER, eng_name TEXT)"
            )
            connection.execute(
                "INSERT INTO rtdb_schema_columns VALUES ('ACGenerator',0,'idx'),('ACGenerator',1,'name')"
            )
            connection.execute(
                "CREATE TABLE rtdb_data_ACGenerator (c0 INTEGER,c1 TEXT)"
            )
            connection.execute(
                "INSERT INTO rtdb_data_ACGenerator VALUES (7,?),(8,?)",
                ("旧名称", "旧未量测设备"),
            )

        self.points = self.root / "points.db"
        with closing(sqlite3.connect(self.points)) as connection, connection:
            for kind in ("yc", "yx"):
                connection.execute(f"""CREATE TABLE fes_{kind}_define (
                    rtu_id INTEGER,pnt_no INTEGER,name TEXT,value REAL,status INTEGER,refresh_time INTEGER)""")
            connection.execute(
                "INSERT INTO fes_yc_define VALUES (1,19,?,10,0,?)",
                ("有功功率", int(time.time() * 1000)),
            )

        self.old = self.root / "old.db"
        store = DeviceStore(self.old, workspace_root=self.root)
        store.import_model(self.model.name)
        device = store.devices({"table": "ACGenerator"})[0]
        store.save_measurement(
            dict(
                device_id=device["device_id"],
                metric="p",
                kind="yc",
                name="有功功率",
                unit="kW",
                source_database=self.points.name,
                rtu_id=1,
                pnt_no=19,
                weight=2.5,
                scale=2,
                offset=1,
                max_age_seconds=200,
            )
        )
        store.collect_once()
        with closing(sqlite3.connect(self.points)) as connection, connection:
            connection.execute(
                "UPDATE fes_yc_define SET value=12,refresh_time=refresh_time+1"
            )
        store.collect_once()
        with store.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "ALTER TABLE ACGenerator ADD COLUMN obsolete_model_field TEXT"
            )
            connection.execute(
                "UPDATE d_settings SET value='5.0' WHERE key='interval_seconds'"
            )
        store.close()

    def test_preserves_measurements_and_recreates_model_tables(self):
        with closing(sqlite3.connect(self.model)) as connection, connection:
            connection.execute("DELETE FROM rtdb_data_ACGenerator WHERE c0=8")
            connection.execute(
                "UPDATE rtdb_data_ACGenerator SET c1=? WHERE c0=7", ("新名称",)
            )
            connection.execute(
                "INSERT INTO rtdb_data_ACGenerator VALUES (9,?)", ("新设备",)
            )
        destination = self.root / "new.db"
        result = rebuild(
            self.old, self.model.name, destination, workspace_root=self.root
        )
        self.assertEqual(result["mappings"], 1)
        self.assertEqual(result["dropped_unmapped_devices"], 1)
        with closing(
            sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0], "wal"
            )
            self.assertEqual(
                connection.execute(
                    "SELECT idx,name FROM ACGenerator ORDER BY idx"
                ).fetchall(),
                [(7, "新名称"), (9, "新设备")],
            )
            self.assertNotIn(
                "obsolete_model_field",
                [r[1] for r in connection.execute("PRAGMA table_info(ACGenerator)")],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT p,p_pre,p_weight,p_rtu,p_pnt FROM ACGenerator WHERE idx=7"
                ).fetchone(),
                (25.0, 21.0, 2.5, 1, 19),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT scale,offset FROM scada_point_mapping"
                ).fetchone(),
                (2.0, 1.0),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM d_settings WHERE key='interval_seconds'"
                ).fetchone()[0],
                "5.0",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM measurement_refresh"
                ).fetchone()[0],
                2,
            )
            embedded = connection.execute(
                "SELECT eng_name FROM rtdb_schema_columns WHERE table_name='ACGenerator' ORDER BY ordinal"
            ).fetchall()
            self.assertEqual(
                [r[0] for r in embedded],
                [r[1] for r in connection.execute("PRAGMA table_info(ACGenerator)")],
            )

    def test_does_not_overwrite_or_publish_when_binding_cannot_be_migrated(self):
        destination = self.root / "target.db"
        destination.write_bytes(b"already exists")
        with self.assertRaises(FileExistsError):
            rebuild(self.old, self.model.name, destination, workspace_root=self.root)
        self.assertEqual(destination.read_bytes(), b"already exists")
        with closing(sqlite3.connect(self.model)) as connection, connection:
            connection.execute("DELETE FROM rtdb_data_ACGenerator WHERE c0=7")
        failed = self.root / "missing.db"
        with self.assertRaisesRegex(ValueError, r"ACGenerator\[7\]"):
            rebuild(self.old, self.model.name, failed, workspace_root=self.root)
        self.assertFalse(failed.exists())

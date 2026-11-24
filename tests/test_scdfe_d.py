from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from core.common import ApiError
from scdfe_d.service import DeviceStore, Collector


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "c.sqlite3"
        with closing(sqlite3.connect(self.source)) as c, c:
            for kind in ("yc", "yx"):
                c.execute(
                    f"CREATE TABLE fes_{kind}_define (rtu_id INTEGER,pnt_no INTEGER,name TEXT,value REAL,status INTEGER,refresh_time INTEGER)"
                )
            c.execute(
                "INSERT INTO fes_yc_define VALUES(1,1,?,10,0,?)",
                ("有功功率", int(time.time() * 1000)),
            )
            c.execute(
                "INSERT INTO fes_yx_define VALUES(1,1,?,1,0,?)",
                ("开关位置", int(time.time() * 1000)),
            )
        self.store = DeviceStore(self.root / "d.sqlite3", workspace_root=self.root)
        self.device = self.store.create_device(
            dict(name="发电机 1", category="generator")
        )
        self.point = self.store.save_measurement(
            dict(
                device_id=self.device["device_id"],
                metric="p",
                kind="yc",
                name="有功功率",
                unit="kW",
                source_database="c.sqlite3",
                rtu_id=1,
                pnt_no=1,
            )
        )

    def row(self):
        return self.store.measurements({"device_id": self.device["device_id"]})[
            "items"
        ][0]

    def test_collect_and_previous_only_on_new_source_sample(self):
        self.store.collect_once()
        self.assertEqual(self.row()["value"], 10)
        self.assertIsNone(self.row()["previous_value"])
        self.store.collect_once()
        self.assertIsNone(self.row()["previous_value"])
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute("UPDATE fes_yc_define SET value=12,refresh_time=refresh_time+1")
        self.store.collect_once()
        self.assertEqual(self.row()["previous_value"], 10)
        with closing(sqlite3.connect(self.store.db_path)) as c, c:
            row = c.execute(
                "SELECT p,p_qual,p_pre,p_time,p_notuse,p_weight,p_rtu,p_pnt FROM Manual_generator"
            ).fetchone()
            self.assertEqual(
                c.execute(
                    "SELECT type FROM sqlite_master WHERE name='Manual_generator'"
                ).fetchone()[0],
                "table",
            )
            self.assertEqual(
                [r[1] for r in c.execute("PRAGMA table_info(Manual_generator)")],
                [
                    "idx",
                    "name",
                    "p",
                    "p_qual",
                    "p_pre",
                    "p_time",
                    "p_notuse",
                    "p_weight",
                    "p_rtu",
                    "p_pnt",
                ],
            )
        self.assertEqual(row[:3], (12, 0, 10))
        self.assertEqual(row[4:], (0, 1, 1, 1))

    def test_quality_stale_missing_and_disabled_do_not_fabricate_valid_data(self):
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute("UPDATE fes_yc_define SET status=128")
        self.store.collect_once()
        self.assertEqual(self.row()["reason"], "bad_quality")
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute("UPDATE fes_yc_define SET status=0,refresh_time=1")
        self.store.collect_once()
        self.assertEqual(self.row()["reason"], "stale")
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute("DELETE FROM fes_yc_define")
        self.store.collect_once()
        self.assertEqual(self.row()["reason"], "missing_point")
        self.assertEqual(self.row()["valid"], 0)

    def test_mapping_version_scope_and_kind(self):
        saved = self.store.save_measurement(
            dict(self.point, kind="yx", metric="position", version=1),
            self.point["measurement_id"],
        )
        self.store.collect_once()
        self.assertEqual(self.row()["value"], 1)
        self.assertIsNone(self.row()["previous_value"])
        with self.assertRaises(ApiError):
            self.store.save_measurement(dict(saved, version=1), saved["measurement_id"])
        for path in ("../outside.sqlite3", str(self.source), "c.sqlite3/../../outside"):
            with self.assertRaises(ApiError):
                self.store.source_path(path)

    def test_custom_weight_and_notuse(self):
        self.store.save_measurement(
            dict(self.point, version=1, weight=4, notuse=1),
            self.point["measurement_id"],
        )
        self.store.collect_once()
        row = self.row()
        self.assertEqual(row["value"], 10)
        self.assertEqual(row["weight"], 4)
        self.assertEqual(row["notuse"], 1)
        self.assertEqual(row["reason"], "notuse")
        with closing(sqlite3.connect(self.store.db_path)) as c:
            tables = {
                r[0]
                for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for legacy in (
                "measurement_value",
                "measurement_refresh",
                "scada_point_mapping",
                "device_measurement",
                "device",
            ):
                self.assertNotIn(legacy, tables)

    def test_invalid_database_is_not_modified(self):
        path = self.root / "wrong.sqlite3"
        with closing(sqlite3.connect(path)) as c, c:
            c.execute("CREATE TABLE unrelated(x)")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            DeviceStore(path, workspace_root=self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_collector_ownership_and_disabled_mapping(self):
        first, second = Collector(self.store), Collector(self.store)
        self.addCleanup(first.close)
        first.start(1)
        with self.assertRaises(ApiError):
            second.start(1)
        with self.assertRaises(ApiError):
            second.once()
        first.close()
        self.store.save_measurement(
            dict(self.point, version=1, enabled=0), self.point["measurement_id"]
        )
        result = second.once()
        self.assertEqual(result["valid_count"], 0)
        self.assertEqual(self.row()["reason"], "notuse")

    def test_unavailable_source_keeps_last_value_but_invalidates_quality(self):
        self.store.collect_once()
        self.source.rename(self.root / "offline.sqlite3")
        result = self.store.collect_once()
        self.assertIn("c.sqlite3", result["errors"])
        self.assertEqual(self.row()["value"], 10)
        self.assertEqual(self.row()["valid"], 0)
        self.assertEqual(self.row()["quality"] & 128, 128)

    def test_mapping_columns_drive_collection(self):
        self.store.collect_once()
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute(
                "INSERT INTO fes_yc_define VALUES(1,2,?,42,0,?)",
                ("另一个有功", int(time.time() * 1000)),
            )
        with self.store.connection() as c:
            c.execute("UPDATE Manual_generator SET p_pnt=2,p_weight=3 WHERE idx=1")
        self.store.collect_once()
        self.assertEqual(self.row()["value"], 42)
        self.assertEqual(self.row()["pnt_no"], 2)
        self.assertEqual(self.row()["weight"], 3)
        self.assertIsNone(self.row()["previous_value"])

    def test_model_class_tables_preserve_indices_and_extension_identity(self):
        path = self.root / "model.sqlite3"
        with closing(sqlite3.connect(path)) as c, c:
            c.execute(
                "CREATE TABLE rtdb_schema_tables(table_name TEXT,ordinal INTEGER)"
            )
            c.execute(
                "CREATE TABLE rtdb_schema_columns(table_name TEXT,ordinal INTEGER,eng_name TEXT)"
            )
            c.executemany(
                "INSERT INTO rtdb_schema_tables VALUES(?,?)",
                [("ACGenerator", 0), ("ACStorageGen", 1), ("HydroNode", 2)],
            )
            c.executemany(
                "INSERT INTO rtdb_schema_columns VALUES(?,?,?)",
                [
                    ("ACGenerator", 0, "idx"),
                    ("ACGenerator", 1, "name"),
                    ("ACGenerator", 2, "rated_capacity"),
                    ("ACStorageGen", 0, "idx"),
                    ("ACStorageGen", 1, "idx_acgenerator"),
                    ("HydroNode", 0, "idx"),
                    ("HydroNode", 1, "name"),
                    ("HydroNode", 2, "pressure"),
                ],
            )
            c.execute("CREATE TABLE rtdb_data_ACGenerator(c0,c1,c2)")
            c.execute("INSERT INTO rtdb_data_ACGenerator VALUES(7,'储能发电机',100)")
            c.execute("CREATE TABLE rtdb_data_ACStorageGen(c0,c1)")
            c.execute("INSERT INTO rtdb_data_ACStorageGen VALUES(2,7)")
            c.execute("CREATE TABLE rtdb_data_HydroNode(c0,c1,c2)")
            c.execute("INSERT INTO rtdb_data_HydroNode VALUES(3,'氢气母管',42)")
        before = path.read_bytes()
        self.assertEqual(self.store.import_model("model.sqlite3")["tables"], 3)
        self.assertEqual(before, path.read_bytes())
        gen_device = next(
            d for d in self.store.devices({}) if d["model_table"] == "ACGenerator"
        )
        storage_device = next(
            d for d in self.store.devices({}) if d["model_table"] == "ACStorageGen"
        )
        self.store.save_measurement(
            dict(self.point, device_id=storage_device["device_id"], metric="soc")
        )
        self.store.save_measurement(
            dict(self.point, device_id=gen_device["device_id"], metric="p")
        )
        self.store.collect_once()
        with self.store.connection() as c:
            self.assertEqual(
                c.execute(
                    "SELECT idx,name,soc,soc_rtu,soc_pnt FROM ACStorageGen"
                ).fetchone()[:],
                (2, "储能发电机", 10, 1, 1),
            )
            columns = {r[1] for r in c.execute("PRAGMA table_info(ACGenerator)")}
            self.assertTrue({"p", "p_rtu"} <= columns)
            self.assertFalse({"rated_capacity", "soc", "soc_pnt"} & columns)
            self.assertEqual(
                c.execute(
                    "SELECT pressure,pressure_qual,pressure_notuse,pressure_pnt FROM HydroNode"
                ).fetchone()[:],
                (None, 128, 1, None),
            )
            self.assertEqual(
                c.execute(
                    "SELECT type FROM pragma_table_info('HydroNode') WHERE name='run'"
                ).fetchone()[0],
                "INTEGER",
            )
            self.assertEqual(
                {r[0] for r in c.execute("SELECT table_name FROM rtdb_schema_tables")},
                {"Manual_generator", "ACGenerator", "ACStorageGen", "HydroNode"},
            )
            c.execute("UPDATE ACStorageGen SET soc_notuse=1 WHERE idx=2")
        self.assertEqual(
            [
                r["metric"]
                for r in self.store.measurements({"table": "ACStorageGen"})["items"]
            ],
            ["soc"],
        )
        self.store.collect_once()
        with self.store.connection() as c:
            self.assertEqual(
                c.execute("SELECT soc_notuse,soc_qual FROM ACStorageGen").fetchone()[:],
                (1, 128),
            )

    def test_removing_last_binding_removes_quantity_columns_and_registry_entries(self):
        self.store.delete_measurement(
            self.point["measurement_id"], self.point["version"]
        )
        with self.store.connection() as c:
            self.assertEqual(
                [r[1] for r in c.execute("PRAGMA table_info(Manual_generator)")],
                ["idx", "name"],
            )
            self.assertEqual(
                [
                    r[0]
                    for r in c.execute(
                        "SELECT eng_name FROM rtdb_schema_columns WHERE table_name='Manual_generator' ORDER BY ordinal"
                    )
                ],
                ["idx", "name"],
            )


class DeviceHTTPTests(unittest.TestCase):
    setUp = DeviceTests.setUp

    def server(self):
        from scdfe_d.web.server import make_server

        server = make_server(
            port=0, db_path=self.store.db_path, workspace_root=self.root
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(5)

        self.addCleanup(cleanup)
        return server, "http://127.0.0.1:" + str(server.server_port)

    @staticmethod
    def call(base, path, method="GET", body=None, headers=None):
        req = Request(
            base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=dict({"Content-Type": "application/json"}, **(headers or {})),
        )
        try:
            with urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            with response:
                return response.code, json.load(response)

    def test_http_collect_mapping_source_filter_and_wide_properties(self):
        server, base = self.server()
        status, data = self.call(base, "/api/collector/once", "POST", {})
        self.assertEqual(status, 200)
        status, data = self.call(
            base, f"/api/devices/{self.device['device_id']}/values"
        )
        self.assertEqual(data["item"]["p"], 10)
        status, data = self.call(
            base,
            "/api/source-points?source_database=c.sqlite3&kind=yx&q="
            + __import__("urllib.parse", fromlist=["quote"]).quote("位置"),
        )
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["value"], 1)
        status, data = self.call(base, "/api/measurements?q=does-not-exist")
        self.assertEqual(data["total"], 0)
        with urlopen(base + "/mappings") as response:
            html = response.read().decode()
        self.assertIn("workbench-sidebar", html)
        self.assertIn("源 SCADA 测点", html)

    def test_database_selection_creation_boundary_and_busy(self):
        server, base = self.server()
        status, data = self.call(base, "/api/database/validate?path=c.sqlite3")
        self.assertFalse(data["valid"])
        status, data = self.call(base, "/api/database/tree?directory=..")
        self.assertEqual(status, 400)
        status, data = self.call(
            base,
            "/api/database/create",
            "POST",
            dict(directory=".", name="new.sqlite3"),
        )
        self.assertEqual(status, 201)
        self.assertTrue(data["validation"]["valid"])
        status, data = self.call(
            base, "/api/collector/start", "POST", {"interval_seconds": 1}
        )
        self.assertEqual(status, 200)
        status, data = self.call(
            base,
            "/api/database",
            "POST",
            dict(database="new.sqlite3", expected_database="d.sqlite3"),
        )
        self.assertEqual(status, 409)
        self.call(base, "/api/collector/stop", "POST", {})
        old_token = server._database_token
        status, data = self.call(
            base,
            "/api/database",
            "POST",
            dict(database="new.sqlite3", expected_database="d.sqlite3"),
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["database"], "new.sqlite3")
        status, data = self.call(
            base,
            "/api/collector/once",
            "POST",
            {},
            headers={"X-Scada-Database": old_token},
        )
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()

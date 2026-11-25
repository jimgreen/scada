import unittest

from tests import test_scdfe_d as fixture


class DeviceParameterHTTPTests(unittest.TestCase):
    setUp = fixture.DeviceTests.setUp
    server = fixture.DeviceHTTPTests.server
    call = staticmethod(fixture.DeviceHTTPTests.call)

    def test_compact_catalog_and_live_projection_preserve_device_identity_and_status(
        self,
    ):
        _, base = self.server()
        self.assertEqual(self.call(base, "/api/collector/once", "POST", {})[0], 200)
        status, catalog = self.call(base, "/api/device-catalog")
        self.assertEqual(status, 200)
        (device,) = catalog["devices"]
        self.assertEqual(device["tables"], [{"table": "Manual_generator", "idx": 1}])
        self.assertNotIn("model_json", device)
        self.assertEqual(catalog["sources"], ["c.sqlite3"])
        self.assertEqual(
            catalog["tables"], [{"table_name": "Manual_generator", "count": 1}]
        )

        route = "/api/device-measurements?table=Manual_generator&idx=1"
        full = self.call(base, route)[1]
        status, live = self.call(base, route + "&view=live")
        self.assertEqual(status, 200)
        self.assertEqual(live["device"], full["device"])
        (live_row,) = live["items"]
        (full_row,) = full["items"]
        self.assertEqual(live_row, {key: full_row[key] for key in live_row})
        self.assertEqual(
            (live_row["value"], live_row["enabled"], live_row["valid"]), (10, 1, 1)
        )
        self.assertNotIn("weight", live_row)
        self.assertNotIn("version", live_row)

    def test_updating_one_device_does_not_rewrite_another_device_row(self):
        self.store.create_device(dict(name="发电机 2", category="generator"))
        _, base = self.server()
        with self.store.connection() as con:
            con.execute("""CREATE TRIGGER protect_other_device BEFORE UPDATE ON Manual_generator
                           WHEN NEW.idx=2 BEGIN SELECT RAISE(ABORT,'unexpected write'); END""")
        change = dict(
            measurement_id=self.point["measurement_id"],
            version=self.point["version"],
            enabled=1,
            notuse=0,
            weight=3.5,
        )
        status, result = self.call(
            base,
            "/api/device-parameters",
            "PUT",
            dict(table="Manual_generator", idx=1, items=[change]),
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["weight"], 3.5)

    def test_device_parameters_are_versioned_atomic_and_used_in_next_collection(self):
        _, base = self.server()
        self.assertEqual(self.call(base, "/api/collector/once", "POST", {})[0], 200)
        route = "/api/device-measurements?table=Manual_generator&idx=1"
        status, result = self.call(base, route)
        self.assertEqual(status, 200)
        (row,) = result["items"]
        self.assertEqual((row["metric"], row["value"], row["quality"]), ("p", 10, 0))
        self.assertGreater(row["source_time_ms"], 0)
        update = dict(
            measurement_id=row["measurement_id"],
            version=row["version"],
            enabled=1,
            notuse=1,
            weight=2.5,
            max_age_seconds=90,
            max_value=50.0,
            min_value=0.0,
        )
        payload = dict(table="Manual_generator", idx=1, items=[update])
        endpoint = "/api/device-parameters"
        invalid = dict(payload, items=[update, dict(update, measurement_id=999_999)])
        self.assertEqual(self.call(base, endpoint, "PUT", invalid)[0], 400)
        # 上限 <= 下限时拒绝修改（返回 400 且原值保持不变）
        for bad_max, bad_min in ((10.0, 10.0), (5.0, 20.0)):
            bad_limit_payload = dict(
                payload,
                items=[dict(update, max_value=bad_max, min_value=bad_min)],
            )
            self.assertEqual(self.call(base, endpoint, "PUT", bad_limit_payload)[0], 400)
        self.assertEqual(
            self.call(base, route)[1]["items"][0]["version"], row["version"]
        )
        status, result = self.call(base, endpoint, "PUT", payload)
        self.assertEqual(status, 200)
        (saved,) = result["items"]
        self.assertEqual(
            (saved["notuse"], saved["weight"], saved["valid"], saved["quality"], saved["max_value"], saved["min_value"]),
            (1, 2.5, 0, 0, 50.0, 0.0),
        )
        self.assertEqual(self.call(base, endpoint, "PUT", payload)[0], 409)
        self.assertEqual(self.call(base, "/api/collector/once", "POST", {})[0], 200)
        self.assertEqual(self.call(base, route)[1]["items"][0]["reason"], "notuse")
        with self.store.connection() as con:
            physical = con.execute(
                "SELECT p_notuse,p_weight,p_qual,p_max,p_min FROM Manual_generator WHERE idx=1"
            ).fetchone()
            self.assertEqual(tuple(physical), (1, 2.5, 0, 50.0, 0.0))

    def test_device_detail_reports_unmapped_quantity_on_another_member(self):
        other = self.store.create_device(dict(name="发电机 2", category="generator"))
        _, base = self.server()
        status, result = self.call(
            base, "/api/device-measurements?table=Manual_generator&idx=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["device"]["device_id"], other["device_id"])
        (row,) = result["items"]
        self.assertEqual(
            (row["metric"], row["measurement_id"], row["quality"], row["reason"]),
            ("p", None, 1, "unmapped"),
        )
        self.assertEqual(
            self.call(base, "/api/device-measurements?table=Manual_generator&idx=99")[
                0
            ],
            404,
        )

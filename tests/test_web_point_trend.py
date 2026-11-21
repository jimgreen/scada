"""Exercise selected-point trend sampling through each SCDFE HTTP server."""

from contextlib import closing, contextmanager, nullcontext
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from scdfe_c.web.server import make_server as make_c
from scdfe_d.web.server import make_server as make_d
from scdfe_s.web.server import make_server as make_s
from scdfe_t.web.server import make_server as make_t


MISSING = {"value": None, "quality": None, "time_ms": None}
HISTORY = "/api/point-trend/history"
MANAGEMENT = "/point-trends"
MANAGEMENT_ASSETS = ("/static/point-trends-page.js", "/static/point-trends-page.css")


@contextmanager
def running_server(factory, root=None):
    workspace = tempfile.TemporaryDirectory() if root is None else nullcontext(root)
    with workspace as directory:
        root = Path(directory)
        server = factory("127.0.0.1", 0, db_path=root / "trend.sqlite3", workspace_root=root)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            yield server, root
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()


def request(server, body=None, *, method="POST", path="/api/point-trend", headers=None):
    with closing(http.client.HTTPConnection(*server.server_address, timeout=5)) as connection:
        payload = json.dumps(body) if body is not None else None
        connection.request(method, path, payload, {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        if response.getheader("Content-Type", "").startswith("application/json"):
            result = json.load(response)
        else:
            result = response.read().decode("utf-8")
        return response.status, result


def define_rtu(store, rtu):
    store.create_row("fes_rtu_define", {
        "rtu_id": rtu, "channel_id": rtu, "channel_type": "iec104", "name": f"RTU {rtu}",
    })


class PointTrendTests(unittest.TestCase):
    def assert_samples(self, server, points, expected):
        before = time.time_ns() // 1_000_000
        status, result = request(server, {"points": points})
        after = time.time_ns() // 1_000_000
        self.assertEqual(status, 200, result)
        self.assertIsInstance(result["time_ms"], int)
        self.assertLessEqual(before, result["time_ms"])
        self.assertLessEqual(result["time_ms"], after)
        self.assertEqual(result["values"], expected)

    def test_c_and_s_sample_independent_rtus_and_kinds_in_request_order(self):
        for factory in (make_c, make_s):
            with self.subTest(factory=factory.__module__), running_server(factory) as (server, _):
                store = server.store
                for rtu in (1, 2):
                    define_rtu(store, rtu)
                stamp = 1_700_000_100_000
                for kind, rtu, value, quality in (("yc", 1, 10.5, 0), ("yc", 2, 22.5, 128), ("yx", 1, 1, 0)):
                    store.create_row(f"fes_{kind}_define", {
                        "rtu_id": rtu, "pnt_no": 7, "name": kind,
                        "value": value, "status": quality, "refresh_time": stamp,
                    })
                large_point = (1 << 53) + 1
                store.create_row("fes_yc_define", {
                    "rtu_id": 1, "pnt_no": large_point, "name": "精确整数点号",
                    "value": 14.5, "status": 0, "refresh_time": 0,
                })
                points = [
                    {"kind": "yx", "rtu_id": 1, "pnt_no": 7},
                    {"kind": "yc", "rtu_id": 1, "pnt_no": 8},
                    {"kind": "yc", "rtu_id": 2, "pnt_no": 7},
                    {"kind": "yc", "rtu_id": "1", "pnt_no": str(large_point)},
                    {"kind": "yc", "rtu_id": 1, "pnt_no": 7},
                ]
                expected = [
                    {"value": 1, "quality": 0, "time_ms": stamp}, MISSING,
                    {"value": 22.5, "quality": 128, "time_ms": stamp},
                    {"value": 14.5, "quality": 0, "time_ms": None},
                    {"value": 10.5, "quality": 0, "time_ms": stamp},
                ]
                self.assert_samples(server, points, expected)
                with store.connection() as connection:
                    connection.execute(
                        "UPDATE fes_yc_define SET value=30, refresh_time=? WHERE rtu_id=1 AND pnt_no=7",
                        (stamp + 1,),
                    )
                    connection.commit()
                self.assert_samples(server, points, [*expected[:-1], {"value": 30, "quality": 0, "time_ms": stamp + 1}])

    def test_t_samples_forwarding_value_by_channel_number_and_kind(self):
        with running_server(make_t) as (server, _):
            store = server.trans_store.store
            define_rtu(store, 1)
            store.create_row("fes_yc_define", {"rtu_id": 1, "pnt_no": 7, "name": "源", "value": 999})
            for channel in (1, 2):
                store.create_row("fes_scdfe_t_define", {"channel_id": channel, "name": f"转发 {channel}", "protocol": "iec104"})
            stamp = 1_700_000_200_000
            for channel, kind, value, quality in ((1, "yc", 12.75, 0), (2, "yc", 24.5, 128), (1, "yx", 1, 0)):
                store.create_row("fes_scdfe_t_point_define", {
                    "channel_id": channel, "kind": kind, "pnt_no": 7, "name": "转发点",
                    "source_rtu_id": 1, "source_pnt_no": 7, "enabled": 1,
                    "value": value, "status": quality, "refresh_time": stamp, "forward_time": stamp + 1,
                })
            self.assert_samples(server, [
                {"kind": "yc", "channel_id": 2, "pnt_no": 7},
                {"kind": "yx", "channel_id": 1, "pnt_no": 7},
                {"kind": "yc", "channel_id": 1, "pnt_no": 7},
            ], [
                {"value": 24.5, "quality": 128, "time_ms": stamp},
                {"value": 1, "quality": 0, "time_ms": stamp},
                {"value": 12.75, "quality": 0, "time_ms": stamp},
            ])

    def test_d_samples_scaled_value_and_reports_missing_measurement(self):
        with running_server(make_d) as (server, root):
            source = root / "source.sqlite3"
            stamp = time.time_ns() // 1_000_000
            with closing(sqlite3.connect(source)) as connection, connection:
                for kind in ("yc", "yx"):
                    connection.execute(
                        f"CREATE TABLE fes_{kind}_define (rtu_id INTEGER,pnt_no INTEGER,name TEXT,"
                        "value REAL,status INTEGER,refresh_time INTEGER)"
                    )
                connection.execute("INSERT INTO fes_yc_define VALUES (1,7,'源',10,0,?)", (stamp,))
            store = server.store
            device = store.create_device({"name": "发电机 1", "category": "generator"})
            measurement = store.save_measurement({
                "device_id": device["device_id"], "metric": "p", "kind": "yc", "name": "有功功率",
                "unit": "kW", "source_database": source.name, "rtu_id": 1, "pnt_no": 7,
                "scale": 2, "offset": 3,
            })
            mid = measurement["measurement_id"]
            store.collect_once()
            self.assert_samples(server, [{"measurement_id": mid + 1}, {"measurement_id": mid}], [
                MISSING, {"value": 23, "quality": 0, "time_ms": stamp},
            ])
            with store.connection() as connection:
                connection.execute("UPDATE Manual_generator SET p=24, p_qual=128, p_time=?", (stamp + 1,))
                connection.commit()
            self.assert_samples(server, [{"measurement_id": mid}], [
                {"value": 24, "quality": 128, "time_ms": stamp + 1},
            ])

    def test_rejects_invalid_descriptors_and_oversized_batch(self):
        with running_server(make_c) as (server, _):
            point = {"kind": "yc", "rtu_id": 1, "pnt_no": 7}
            bad = [
                {"points": "yc"},
                {"points": [{**point, "kind": "fes_rtu_define"}]},
                {"points": [{**point, "kind": ["yc"]}]},
                {"points": [{**point, "rtu_id": True}]},
                {"points": [{**point, "pnt_no": -1}]},
                {"points": [{**point, "pnt_no": 1 << 63}]},
                {"points": [{"measurement_id": 1}]},
                {"points": [{**point, "pnt_no": number} for number in range(1, 26)]},
            ]
            for body in bad:
                with self.subTest(body=body):
                    status, result = request(server, body)
                    self.assertEqual(status, 400, result)

    def test_sampling_management_navigation_and_assets_for_all_roles(self):
        for factory in (make_c, make_s, make_t, make_d):
            with self.subTest(factory=factory.__module__), running_server(factory) as (server, _):
                status, homepage = request(server, method="GET", path="/")
                self.assertEqual(status, 200)
                self.assertIn(f'href="{MANAGEMENT}"', homepage)
                status, page = request(server, method="GET", path=MANAGEMENT)
                self.assertEqual(status, 200)
                self.assertRegex(page, r"<h1\b[^>]*>\s*采样管理\s*</h1>")
                self.assertIn(f'href="{MANAGEMENT}" aria-current="page"', page)
                self.assertEqual(request(server, method="GET", path=f'{MANAGEMENT}.html'), (200, page))
                for asset in MANAGEMENT_ASSETS:
                    self.assertIn(asset, page)
                    status, source = request(server, method="GET", path=asset)
                    self.assertEqual(status, 200, asset)
                    self.assertTrue(source, asset)
                self.assertEqual(request(server, method="POST", path=MANAGEMENT)[0], 405)

    def wait_for_background_samples(self, server, count, *, after_ms=0):
        # Observe the worker directly so no HTTP history read can trigger sampling.
        path = server._point_trend_history.path
        deadline = time.monotonic() + 8
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
            while time.monotonic() < deadline:
                rows = connection.execute(
                    "SELECT time_ms FROM trend_sample WHERE time_ms>? ORDER BY time_ms LIMIT ?",
                    (after_ms, count),
                ).fetchall()
                if len(rows) == count:
                    return [row[0] for row in rows]
                time.sleep(0.05)
        self.fail(f"background sampler did not write {count} sample(s) after {after_ms}")

    def test_history_samples_without_an_open_page_and_resumes_after_reopen(self):
        with tempfile.TemporaryDirectory() as workspace:
            with running_server(make_c, workspace) as (server, _):
                define_rtu(server.store, 1)
                stamp = time.time_ns() // 1_000_000
                server.store.create_row("fes_yc_define", {
                    "rtu_id": 1, "pnt_no": 7, "name": "P", "value": 13.5,
                    "status": 0, "refresh_time": stamp,
                })
                status, result = request(server, {
                    "interval_seconds": 1,
                    "series": [{"point": {"kind": "yc", "rtu_id": 1, "pnt_no": 7}, "name": "P"}],
                    "view": {"follow": True},
                }, method="PUT", path=HISTORY)
                self.assertEqual(status, 200, result)
                settings = result["settings"]
                self.assertEqual(settings["retention_seconds"], 3600)
                self.assertEqual(settings["series"][0]["point"], {"kind": "yc", "rtu_id": "1", "pnt_no": "7"})
                first, second = self.wait_for_background_samples(server, 2)
                status, result = request(server, method="GET", path=f"{HISTORY}?after_ms={first}")
                self.assertEqual(status, 200, result)
                self.assertEqual(result["settings"], settings)
                self.assertEqual(result["samples"][0], {
                    "time_ms": second, "values": [{"value": 13.5, "quality": 0, "time_ms": stamp}],
                })

            stopped_at = time.time_ns() // 1_000_000
            with running_server(make_c, workspace) as (server, _):
                third, = self.wait_for_background_samples(server, 1, after_ms=stopped_at)
                status, result = request(server, method="GET", path=HISTORY)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["settings"], settings)
                observed = {sample["time_ms"] for sample in result["samples"]}
                self.assertTrue({first, second, third} <= observed)

    def test_history_remove_and_restore_series_preserves_sampling_configuration(self):
        with running_server(make_c) as (server, _):
            point = {"kind": "yc", "rtu_id": 1, "pnt_no": 7}
            define_rtu(server.store, 1)
            server.store.create_row("fes_yc_define", {"rtu_id": 1, "pnt_no": 7, "name": "P", "value": 13.5})
            settings = {"interval_seconds": 2, "retention_seconds": 600,
                        "series": [{"point": point, "name": "P"}], "view": {"follow": True}}
            status, result = request(server, settings, method="PUT", path=HISTORY)
            self.assertEqual(status, 200, result)
            settings = result["settings"]
            previous_revision = result["revision"]
            self.assertRegex(previous_revision, r'^"[0-9a-f]{64}"$')
            observed, = self.wait_for_background_samples(server, 1)

            cleared = dict(settings, series=[])
            status, result = request(server, cleared, method="PUT", path=HISTORY,
                                     headers={"If-Match": previous_revision})
            self.assertEqual(status, 200, result)
            revision = result["revision"]
            self.assertNotEqual(revision, previous_revision)
            status, result = request(server, settings, method="PUT", path=HISTORY,
                                     headers={"If-Match": previous_revision})
            self.assertEqual(status, 409, result)
            self.assertEqual(result["error"]["code"], "trend_settings_changed")
            self.assertEqual(request(server, method="GET", path=HISTORY),
                             (200, {"settings": cleared, "samples": [], "revision": revision}))

            status, result = request(server, settings, method="PUT", path=HISTORY,
                                     headers={"If-Match": revision})
            self.assertEqual(status, 200, result)
            status, result = request(server, method="GET", path=HISTORY)
            self.assertEqual(status, 200, result)
            self.assertEqual(result["settings"], settings)
            retained = {sample["time_ms"]: sample["values"][0]["value"] for sample in result["samples"]}
            self.assertEqual(retained.get(observed), 13.5)

    def test_history_rejects_duplicate_points_and_invalid_retention(self):
        with running_server(make_c) as (server, _):
            status, result = request(server, method="GET", path=HISTORY)
            self.assertEqual(status, 200, result)
            self.assertIsNone(result["settings"])
            self.assertEqual(result["samples"], [])
            self.assertRegex(result["revision"], r'^"[0-9a-f]{64}"$')
            status, saved = request(server, {"series": [], "retention_seconds": 600}, method="PUT", path=HISTORY,
                                    headers={"If-Match": result["revision"]})
            self.assertEqual(status, 200, saved)
            duplicate = {"kind": "yc", "rtu_id": "1", "pnt_no": "7"}
            invalid = [
                {"series": [{"point": duplicate}, {"point": {"kind": "yc", "rtu_id": 1, "pnt_no": 7}}]},
                *({"series": [], "retention_seconds": value} for value in (0, 86401, True, "600")),
            ]
            for body in invalid:
                with self.subTest(body=body):
                    status, result = request(server, body, method="PUT", path=HISTORY)
                    self.assertEqual(status, 400, result)
            self.assertEqual(request(server, method="GET", path=HISTORY), (200, saved))


if __name__ == "__main__":
    unittest.main()

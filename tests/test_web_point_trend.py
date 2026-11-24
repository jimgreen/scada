"""Exercise selected-point trend sampling for scdfe_d HTTP server."""
from contextlib import closing, contextmanager, nullcontext
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from scdfe_d.web.server import make_server as make_d

MISSING = {"value": None, "quality": None, "time_ms": None}
MANAGEMENT = "/point-trends"


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


class DevicePointTrendTests(unittest.TestCase):
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
            })
            mid = measurement["measurement_id"]
            store.collect_once()
            status, result = request(server, {"points": [{"measurement_id": mid + 1}, {"measurement_id": mid}]})
            self.assertEqual(status, 200, result)
            self.assertEqual(result["values"], [MISSING, {"value": 10.0, "quality": 0, "time_ms": stamp}])
            status, homepage = request(server, method="GET", path="/")
            self.assertEqual(status, 200)
            self.assertIn(f'href="{MANAGEMENT}"', homepage)
            status, page = request(server, method="GET", path=MANAGEMENT)
            self.assertEqual(status, 200)
            self.assertRegex(page, r"<h1\b[^>]*>\s*采样管理\s*</h1>")


if __name__ == "__main__":
    unittest.main()

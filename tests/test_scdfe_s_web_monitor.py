from pathlib import Path
import http.client
import json
import tempfile
import threading
import unittest

from scdfe_s.web.server import make_server


class ScdfeSWebMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "scada.sqlite3"
        self.httpd = make_server("127.0.0.1", 0, db_path=self.db)
        self.store = self.httpd.store
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(3)
        self.httpd.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        raw = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if raw is not None else {}
        conn.request(method, path, raw, headers)
        response = conn.getresponse()
        payload = response.read()
        content_type = response.getheader("Content-Type", "")
        conn.close()
        if "json" in content_type:
            return response.status, json.loads(payload.decode())
        return response.status, payload.decode()

    def test_independent_monitor_pages_and_apis(self):
        for path in ("/processes", "/communication", "/logs", "/channels", "/processes.html"):
            status, _ = self.request("GET", path)
            self.assertEqual(status, 200, path)
        for path in ("/api/monitor/processes", "/api/monitor/communications", "/api/monitor/logs"):
            status, payload = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn("items", payload)

    def test_channel_and_point_crud_alias(self):
        status, payload = self.request("POST", "/api/channels", {
            "rtu_id": 1, "channel_id": 11, "channel_type": "iec104",
            "name": "测试通道", "process_enable": 0,
        })
        self.assertEqual(status, 201)
        self.assertEqual(payload["item"]["channel_id"], 11)
        status, payload = self.request("POST", "/api/channels/1/points/yc", {
            "pnt_no": 1, "name": "电压", "value": 1.5,
        })
        self.assertEqual(status, 201)
        status, payload = self.request("GET", "/api/channels/1/points/yc")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["pnt_no"], 1)
        status, payload = self.request("PATCH", "/api/channels/1/points/yc/1", {
            "name": "电压更新", "value": 2.5,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["item"]["name"], "电压更新")
        status, _ = self.request("DELETE", "/api/channels/1/points/yc/1", {})
        self.assertEqual(status, 200)
        status, _ = self.request("DELETE", "/api/channels/1", {})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()

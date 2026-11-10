"""Regression checks for the scdfe_s multi-channel Web pages and API."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from scdfe_s.web.server import make_server


class ScdfeSWebUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "scada.sqlite3"
        self.httpd = make_server("127.0.0.1", 0, db_path=self.db)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"} if raw is not None else {}
        conn.request(method, path, raw, headers)
        response = conn.getresponse()
        content_type = response.getheader("Content-Type", "")
        payload = response.read()
        conn.close()
        if "application/json" in content_type:
            payload = json.loads(payload.decode("utf-8"))
        return response.status, payload

    def test_independent_pages_and_static_assets_are_available(self):
        for path in (
            "/", "/channels", "/points", "/communication", "/processes", "/logs",
            "/static/channels.js", "/static/monitor.js",
            "/static/workbench.css", "/static/workbench.js",
            "/static/layout-resizer.js",
        ):
            status, payload = self.call("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(payload, path)

    def test_four_remote_page_shows_refresh_and_command_receive_times(self):
        status, page = self.call("GET", "/points")
        self.assertEqual(status, 200)
        page = page.decode("utf-8")
        yc_page = page.split('data-point-page="yc"', 1)[1].split("</section>", 1)[0]
        yx_page = page.split('data-point-page="yx"', 1)[1].split("</section>", 1)[0]
        yk_page = page.split('data-point-page="yk"', 1)[1].split("</section>", 1)[0]
        self.assertIn("刷新时间", yc_page)
        self.assertNotIn("指令接收时间", yc_page)
        self.assertIn("刷新时间", yx_page)
        self.assertNotIn("指令接收时间", yx_page)
        self.assertIn("指令接收时间", yk_page)
        status, script = self.call("GET", "/static/channels.js")
        self.assertEqual(status, 200)
        script = script.decode("utf-8")
        self.assertIn("formatPointTime", script)
        self.assertIn("cmd_handle_time", script)

    def test_multiple_channel_and_four_remote_point_crud(self):
        for rtu_id in (101, 102):
            status, body = self.call("POST", "/api/channels", {
                "rtu_id": rtu_id, "channel_id": rtu_id,
                "channel_type": "custom",
                "name": f"通道 {rtu_id}", "process_enable": 0,
            })
            self.assertEqual(status, 201, body)

        status, body = self.call("GET", "/api/channels")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 2)

        for kind, value in (("yc", 1.25), ("yx", 1), ("yk", 0), ("yt", 5.5)):
            status, body = self.call("POST", "/api/channels/101/points/" + kind, {
                "pnt_no": 1, "name": kind,
                "value": value,
            })
            self.assertEqual(status, 201, body)
            status, body = self.call("GET", "/api/channels/101/points/" + kind)
            self.assertEqual(status, 200)
            self.assertEqual(body["total"], 1)

        status, body = self.call("PATCH", "/api/channels/101", {"name": "已更新"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["item"]["name"], "已更新")
        status, body = self.call("PATCH", "/api/channels/101/points/yc/1", {"name": "更新测点", "value": 9.5})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["item"]["value"], 9.5)

        status, body = self.call("DELETE", "/api/channels/101/points/yc/1", {})
        self.assertEqual(status, 200, body)
        status, body = self.call("DELETE", "/api/channels/101", {})
        self.assertEqual(status, 200, body)
        status, body = self.call("GET", "/api/channels")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)

    def test_monitor_apis_are_read_only_until_explicit_control(self):
        for path in ("/api/monitor/processes", "/api/monitor/communications", "/api/monitor/logs"):
            status, body = self.call("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn("items", body)
        status, body = self.call("POST", "/api/monitor/communications/not-a-service/start", {})
        self.assertEqual(status, 404, body)


if __name__ == "__main__":
    unittest.main()

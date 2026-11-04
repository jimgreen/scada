"""WEB 遥控/遥调指令入口的隔离数据库回归。"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scdfe_c.web.server import make_server
from scdfe_c.web.service import ScadaStore


class WebCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "scada.sqlite3"
        self.store = ScadaStore(self.db)
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 7, "channel_id": 70, "channel_type": "modbus-tcp", "name": "测试站",
        })
        self.store.create_row("fes_channel_modbus_tcp_define", {
            "rtu_id": 7, "remote_host": "127.0.0.1", "remote_port": 1502,
            "unit_id": 1, "response_timeout": 0.2,
        })
        self.store.create_row("fes_yk_define", {
            "rtu_id": 7, "pnt_no": 1, "name": "开关", "value": 0,
            "status": 0, "cmd_time": 0, "cmd_handle_time": 0,
        })
        self.store.create_row("fes_yt_define", {
            "rtu_id": 7, "pnt_no": 1, "name": "设定", "value": 0.0,
            "status": 0, "eng_min": 0.0, "eng_max": 100.0,
            "cmd_time": 0, "cmd_handle_time": 0,
        })
        self.server = make_server("127.0.0.1", 0, db_path=self.db)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def call(self, path, method="GET", data=None, headers=None):
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = Request(
            self.url + path, method=method, headers=request_headers,
            data=json.dumps(data).encode("utf-8") if data is not None else None,
        )
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_yk_and_yt_are_queued_with_monotonic_command_time(self):
        yk = self.store.get_row("fes_yk_define", [7, 1])
        yt = self.store.get_row("fes_yt_define", [7, 1])
        status, body = self.call(
            "/api/rtus/7/commands/yk/1", "POST", {"value": 1},
            {"If-Match": str(yk["_version"])},
        )
        self.assertEqual((status, body["command_status"]), (202, "pending"))
        self.assertGreater(body["item"]["cmd_time"], body["item"]["cmd_handle_time"])
        status, body = self.call(
            "/api/rtus/7/commands/yt/1", "POST", {"value": 55.5},
            {"If-Match": str(yt["_version"])},
        )
        self.assertEqual((status, body["item"]["value"]), (202, 55.5))

    def test_command_requires_version_and_rejects_stale_or_pending(self):
        row = self.store.get_row("fes_yk_define", [7, 1])
        self.assertEqual(self.call("/api/rtus/7/commands/yk/1", "POST", {"value": 1})[0], 428)
        status, _ = self.call(
            "/api/rtus/7/commands/yk/1", "POST", {"value": 1},
            {"If-Match": str(row["_version"])},
        )
        self.assertEqual(status, 202)
        current = self.store.get_row("fes_yk_define", [7, 1])
        self.assertEqual(self.call(
            "/api/rtus/7/commands/yk/1", "POST", {"value": 0},
            {"If-Match": str(current["_version"])},
        )[0], 409)
        self.assertEqual(self.call(
            "/api/rtus/7/commands/yk/1", "POST", {"value": 0},
            {"If-Match": str(row["_version"])},
        )[0], 409)

    def test_command_checks_range_lock_and_protocol_capability(self):
        yt = self.store.get_row("fes_yt_define", [7, 1])
        self.assertEqual(self.call(
            "/api/rtus/7/commands/yt/1", "POST", {"value": 101},
            {"If-Match": str(yt["_version"])},
        )[0], 400)
        self.store.update_row("fes_yk_define", [7, 1], {"man_disable": 1})
        yk = self.store.get_row("fes_yk_define", [7, 1])
        self.assertEqual(self.call(
            "/api/rtus/7/commands/yk/1", "POST", {"value": 1},
            {"If-Match": str(yk["_version"])},
        )[0], 409)
        self.store.update_row("fes_rtu_define", [7], {"channel_type": "can"})
        self.store.update_row("fes_yk_define", [7, 1], {"man_disable": 0})
        yk = self.store.get_row("fes_yk_define", [7, 1])
        status, body = self.call(
            "/api/rtus/7/commands/yk/1", "POST", {"value": 1},
            {"If-Match": str(yk["_version"])},
        )
        self.assertEqual((status, body["command_status"]), (202, "pending"))
        self.assertEqual(body["item"]["value"], 1)


if __name__ == "__main__":
    unittest.main()

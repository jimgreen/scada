import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scdfe_c.web.server import make_server


class RtuCascadeDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "scada.sqlite3"
        self.server = make_server("127.0.0.1", 0, db_path=self.db)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def call(self, path, method="GET", body=None, headers=None):
        request = Request(
            self.url + path,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
            data=None if body is None else json.dumps(body).encode("utf-8"),
        )
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def seed(self):
        status, body = self.call("/api/rtus", "POST", {
            "rtu_id": 77, "channel_id": 770, "channel_type": "iec104", "name": "级联测试站",
        })
        self.assertEqual(status, 201, body)
        status, body = self.call("/api/rtus/77/channel/parameters", "POST", {
            "remote_host": "127.0.0.1", "remote_port": 2404,
        })
        self.assertEqual(status, 201, body)
        for kind in ("yc", "yx", "yt", "yk"):
            status, body = self.call(f"/api/rtus/77/points/{kind}", "POST", {"pnt_no": 1, "name": kind})
            self.assertEqual(status, 201, body)
        status, body = self.call("/api/tables/fes_channel_iec104_point_define/rows", "POST", {
            "rtu_id": 77, "kind": "yc", "pnt_no": 1, "io_address": 100,
        })
        self.assertEqual(status, 201, body)

    def test_preview_and_delete_remove_all_rtu_owned_rows(self):
        self.seed()
        status, preview = self.call("/api/rtus/77/delete-preview")
        self.assertEqual(status, 200, preview)
        counts = {item["table"]: item["count"] for item in preview["references"]}
        self.assertEqual(counts["fes_rtu_define"], 1)
        self.assertEqual(counts["fes_channel_iec104_define"], 1)
        self.assertEqual(counts["fes_channel_iec104_point_define"], 1)
        self.assertEqual(preview["total"], 7)

        version = self.call("/api/rtus/77")[1]["item"]["_version"]
        status, result = self.call("/api/rtus/77", "DELETE", headers={"If-Match": str(version)})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["total_deleted"], 7)
        for table in (
            "fes_rtu_define", "fes_channel_iec104_define", "fes_channel_iec104_point_define",
            "fes_yc_define", "fes_yx_define", "fes_yt_define", "fes_yk_define",
        ):
            self.assertEqual(self.server.store.list_rows(table, where={"rtu_id": 77})["total"], 0)
        self.assertEqual(self.call("/api/rtus/77")[0], 404)

    def test_stale_version_does_not_delete_children(self):
        self.seed()
        status, result = self.call("/api/rtus/77", "DELETE", headers={"If-Match": "999"})
        self.assertEqual(status, 409, result)
        self.assertEqual(self.call("/api/rtus/77")[0], 200)
        self.assertEqual(self.server.store.list_rows("fes_yc_define", where={"rtu_id": 77})["total"], 1)


if __name__ == "__main__":
    unittest.main()

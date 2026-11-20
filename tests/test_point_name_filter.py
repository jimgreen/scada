"""Name filtering must happen before pagination and remain channel/kind scoped."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from scdfe_c.web.server import make_server as make_c
from scdfe_s.web.server import make_server as make_s
from scdfe_t.web.server import make_server as make_t
from scdfe_t.web.service import TransStore


class PointNameFilterTests(unittest.TestCase):
    def test_all_roles_filter_names_before_pagination(self):
        for role, factory in (("s", make_s), ("c", make_c), ("t", make_t)):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "points.sqlite3"
                if role == "t":
                    service = TransStore(database)
                    for cid in (1, 2):
                        service.create_channel({"channel_id": cid, "name": "TEST", "protocol": "modbus-tcp"})
                    service.store.close()
                server = factory("127.0.0.1", 0, db_path=database)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    store = server.trans_store.store if role == "t" else server.store
                    with store.connection() as con:
                        if role == "t":
                            con.executemany(
                                "INSERT INTO fes_scdfe_t_point_define(channel_id,kind,pnt_no,name,source_rtu_id,source_pnt_no) VALUES(?,?,?,?,1,?)",
                                [(cid, kind, number, name, number) for cid, kind, number, name in self.rows()],
                            )
                            route = "/api/trans/channels/1/points"
                        else:
                            con.executemany("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,'modbus-tcp')", [(1, 1), (2, 2)])
                            for cid, kind, number, name in self.rows():
                                con.execute(f"INSERT INTO fes_{kind}_define(rtu_id,pnt_no,name) VALUES(?,?,?)", (cid, number, name))
                            route = "/api/channels/1/points/yc" if role == "s" else "/api/rtus/1/points/yc"
                        con.commit()

                    def read(name, **extra):
                        query = {"name_contains": name, "limit": 1, "sort": "pnt_no", **extra}
                        if role == "t":
                            query.pop("sort")
                            query["kind"] = "yc"
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                        connection.request("GET", route + "?" + urlencode(query))
                        response = connection.getresponse()
                        data = json.loads(response.read())
                        connection.close()
                        self.assertEqual(response.status, 200, data)
                        return data

                    first = read("储能")
                    self.assertEqual(first["total"], 2)
                    self.assertEqual(first["items"][0]["pnt_no"], 501)
                    self.assertEqual(read("储能", offset=1)["items"][0]["pnt_no"], 502)
                    self.assertEqual(read("储能", offset=2)["total"], 2)
                    self.assertEqual(read("不存在")["items"], [])
                    self.assertEqual(read("501")["total"], 0, "Point numbers must not match the name filter")
                    self.assertEqual(read("%_\\")["total"], 1, "SQL wildcards must be literal text")
                    self.assertEqual(read("soc")["total"], 1)
                    self.assertEqual(read("")["total"], 503)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(3)

    @staticmethod
    def rows():
        return [(1, "yc", n, f"普通测点 {n}") for n in range(1, 501)] + [
            (1, "yc", 501, "储能 SOC"), (1, "yc", 502, "储能功率"),
            (1, "yc", 503, "字面%_\\测点"),
            (2, "yc", 501, "其他通道储能"), (1, "yx", 501, "储能状态"),
        ]


if __name__ == "__main__":
    unittest.main()

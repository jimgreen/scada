from pathlib import Path
import http.client
import json
import tempfile
import threading
import unittest

from scdfe_s.web.server import _rtu_point_counts, make_server


class ScdfeSWebMonitorTests(unittest.TestCase):
    def test_selected_rtu_controls_only_its_service(self):
        from unittest.mock import Mock, patch
        for rid in (1,2):
            self.store.create_row('fes_rtu_define',dict(rtu_id=rid,channel_id=rid,channel_type='modbus-tcp'))
        first,second=Mock(),Mock()
        for action in ('start','stop','poll'):
            getattr(second,action).return_value={'rtu_id':2}
        self.httpd._channel_service_cache.update({'modbus-tcp-rtu-1':first,'modbus-tcp-rtu-2':second})
        with patch.object(self.httpd.modbus_tcp_service,'start') as default_start:
            for action in ('start','stop','poll'):
                status,body=self.request('POST',f'/api/rtus/2/service/{action}',{})
                self.assertEqual(status,200,body)
                self.assertEqual(body['item']['rtu_id'],2)
                getattr(second,action).assert_called_once_with()
                getattr(first,action).assert_not_called()
            default_start.assert_not_called()
        self.assertEqual(self.request('GET','/api/rtus/2/service/start')[0],405)
        self.assertEqual(self.request('POST','/api/rtus/999/service/start',{})[0],404)
        self.assertEqual(self.request('POST','/api/rtus/2/service/start',{'rtu_id':1})[0],400)

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

    def test_point_counts_are_aggregated_across_all_rtu_tables(self):
        for rtu_id in (1, 2):
            self.store.create_row("fes_rtu_define", {
                "rtu_id": rtu_id, "channel_id": 100 + rtu_id,
                "channel_type": "iec104", "name": f"RTU-{rtu_id}",
                "process_enable": 0,
            })
        for table, rtu_id, point_no in (
            ("fes_yc_define", 1, 1),
            ("fes_yx_define", 1, 2),
            ("fes_yk_define", 1, 3),
            ("fes_yt_define", 2, 1),
        ):
            self.store.create_row(table, {
                "rtu_id": rtu_id, "pnt_no": point_no,
                "name": f"{table}-{point_no}", "value": 0,
            })
        self.assertEqual(_rtu_point_counts(self.store), {1: 3, 2: 1})

    def test_communication_monitor_can_scope_to_one_rtu(self):
        for rtu_id in (1, 2):
            self.store.create_row("fes_rtu_define", {
                "rtu_id": rtu_id, "channel_id": 100 + rtu_id,
                "channel_type": "iec104", "name": f"RTU-{rtu_id}",
                "process_enable": 0,
            })
        status, payload = self.request(
            "GET", "/api/monitor/communications?rtu_id=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["rtu_id"], 2)
        self.assertEqual(set(self.httpd._channel_service_cache), {"iec104-rtu-2"})
        selected_service = self.httpd._channel_service_cache["iec104-rtu-2"]
        status, payload = self.request("GET", "/api/monitor/communications")
        self.assertEqual(status, 200)
        self.assertEqual({item["rtu_id"] for item in payload["items"]}, {1, 2})
        self.assertIs(self.httpd._channel_service_cache["iec104-rtu-2"], selected_service)
        status, payload = self.request("GET", "/api/monitor/communications?rtu_id=99")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"items": [], "total": 0})
        status, _ = self.request(
            "GET", "/api/monitor/communications?rtu_id=invalid"
        )
        self.assertEqual(status, 400)

    def test_scoped_channel_services_keep_protocol_port_and_binding(self):
        for rtu_id, protocol in ((1, "iec104"), (2, "modbus-tcp")):
            self.store.create_row("fes_rtu_define", {
                "rtu_id": rtu_id, "channel_id": 100 + rtu_id,
                "channel_type": protocol, "name": f"RTU-{rtu_id}",
                "process_enable": 0,
            })
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO fes_channel_iec104_define(channel_id, remote_port) VALUES(101, 24041)"
            )
        services = self.httpd.channel_services(rtu_id=1)
        self.assertEqual(set(services), {"iec104-rtu-1"})
        self.assertEqual(services["iec104-rtu-1"]._config["port"], 24041)
        self.assertEqual(services["iec104-rtu-1"]._config["rtu_id"], 1)
        other = self.httpd.channel_services(rtu_id=2)
        self.assertEqual(set(other), {"modbus-tcp-rtu-2"})
        self.assertIs(self.httpd.channel_services(rtu_id=1)["iec104-rtu-1"], services["iec104-rtu-1"])
        self.store.update_row("fes_rtu_define", [1], {"channel_type": "can"})
        self.assertEqual(set(self.httpd.channel_services(rtu_id=1)), {"can-rtu-1"})

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

    def test_generic_yx_patch_only_updates_database(self):
        status, _ = self.request("POST", "/api/channels", {
            "rtu_id": 2, "channel_id": 12, "channel_type": "iec104",
            "name": "IEC104 测试通道", "process_enable": 0,
        })
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/api/channels/2/points/yx", {
            "pnt_no": 1, "name": "开关量", "value": 0,
        })
        self.assertEqual(status, 201)

        from unittest.mock import Mock

        service = Mock()
        service.running = True
        service.incremental_update.return_value = {"value": 1, "sent": True}
        self.httpd._channel_service_cache["iec104-rtu-2"] = service

        status, payload = self.request("PATCH", "/api/channels/2/points/yx/1", {
            "name": "开关量更新", "value": 1,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["item"]["value"], 1)
        self.assertEqual(payload["item"]["name"], "开关量更新")
        self.assertGreater(payload["item"]["refresh_time"], 0)
        service.incremental_update.assert_not_called()
        stored = self.store.get_row("fes_yx_define", [2, 1])
        self.assertEqual(stored["_version"], payload["item"]["_version"])

    def test_yc_yx_value_or_status_change_stamps_refresh_time(self):
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 10, "channel_id": 10, "channel_type": "custom", "name": "测试 RTU",
        })
        for kind, value in (("yc", 1.5), ("yx", 0)):
            table = f"fes_{kind}_define"
            self.store.create_row(table, {
                "rtu_id": 10, "pnt_no": 1, "name": kind,
                "value": value, "status": 0, "refresh_time": 1,
            })
            before = self.store.get_row(table, [10, 1])

            changed_status = self.store.update_row(table, [10, 1], {"status": 1})
            self.assertGreater(changed_status["refresh_time"], before["refresh_time"], kind)

            unchanged = self.store.update_row(table, [10, 1], {
                "value": value, "status": 1,
            })
            self.assertEqual(unchanged["refresh_time"], changed_status["refresh_time"], kind)

            changed_value = self.store.update_row(table, [10, 1], {
                "value": 2.5 if kind == "yc" else 1,
            })
            self.assertGreater(changed_value["refresh_time"], unchanged["refresh_time"], kind)


if __name__ == "__main__":
    unittest.main()

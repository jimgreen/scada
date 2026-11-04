from pathlib import Path
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest

from client import IEC104Master
from scdfe_c.web.service import ScadaStore
from scdfe_t import TransRuntime
from scdfe_t.web.server import make_server


class _Writer:
    def __init__(self):
        self.calls = []

    def submit_command(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True

    def close(self):
        return None


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ScdfeTTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "scada.sqlite3"
        self.store = ScadaStore(self.db)
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 1, "channel_id": 1, "channel_type": "iec104",
            "code": "SRC1", "name": "源站",
        })
        self.store.create_row("fes_yc_define", {
            "rtu_id": 1, "pnt_no": 1, "code": "YC1", "name": "电压", "value": 12.5,
        })
        self.store.create_row("fes_yx_define", {
            "rtu_id": 1, "pnt_no": 1, "code": "YX1", "name": "状态", "value": 1,
        })
        self.store.create_row("fes_yk_define", {
            "rtu_id": 1, "pnt_no": 1, "code": "YK1", "name": "开关", "value": 0,
        })
        self.store.create_row("fes_yt_define", {
            "rtu_id": 1, "pnt_no": 1, "code": "YT1", "name": "设定", "value": 5.0,
        })

    def tearDown(self):
        self.temp.cleanup()

    def add_channel(self, protocol, port):
        self.store.create_row("fes_scdfe_t_define", {
            "channel_id": 1, "code": "TRANS1", "name": "转发1", "protocol": protocol,
            "bind_host": "127.0.0.1", "bind_port": port, "common_address": 1, "unit_id": 1,
            "scan_interval_ms": 50,
        })
        maps = {
            # pnt_no is the transmission-table primary key; source_pnt_no
            # remains the source RTU point number and may be reused by kind.
            "yc": (1, 0x4001 if protocol == "iec104" else 100, 1, "input", 0, "float32"),
            "yx": (2, 2 if protocol == "iec104" else 1, 1, "discrete", 0, "bool"),
            "yk": (3, 0x6003 if protocol == "iec104" else 200, 1, "coil", 0, "bool"),
            "yt": (4, 0x6204 if protocol == "iec104" else 300, 3, "holding", 10, "float32"),
        }
        for kind, (pnt_no, ioa, type_id, area, address, dtype) in maps.items():
            self.store.create_row("fes_scdfe_t_point_define", {
                "channel_id": 1, "pnt_no": pnt_no, "name": kind, "kind": kind,
                "source_kind": kind, "source_rtu_id": 1, "source_pnt_no": 1,
                "enabled": 1, "io_address": ioa, "type_id": type_id,
                "register_area": area, "address": address, "data_type": dtype,
            })


class TransProtocolLoopbackTests(ScdfeTTestBase):
    def test_iec104_read_and_command_are_transferred(self):
        self.add_channel("iec104", _free_port())
        runtime = TransRuntime(self.db, channel_id=1)
        writer = _Writer()
        runtime.writer = writer
        spec, points, _ = runtime.load()
        server = runtime.build_server(spec, points)
        server.start()
        self.addCleanup(server.stop)
        client = IEC104Master("127.0.0.1", spec.bind_port, common_address=1, init="NONE")
        self.addCleanup(client.close)
        for kind in ("yc", "yx", "yk", "yt"):
            client.add_point(kind, {"yc": 1, "yx": 2, "yk": 3, "yt": 4}[kind])
        client.connect(timeout=3)
        runtime.refresh()
        self.assertTrue(client.general_interrogation())
        time.sleep(0.2)
        self.assertAlmostEqual(float(client.value("yc", 1).value), 12.5, places=4)
        self.assertTrue(bool(client.value("yx", 2).value))
        client.send_yk_point(3, True)
        deadline = time.time() + 2
        while not writer.calls and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(writer.calls)
        args, _ = writer.calls[-1]
        self.assertEqual(args[:3], (1, "yk", 1))
        self.assertEqual(args[3], 1)

    def test_modbus_tcp_read_and_command_are_transferred(self):
        self.add_channel("modbus-tcp", _free_port())
        runtime = TransRuntime(self.db, channel_id=1)
        writer = _Writer()
        runtime.writer = writer
        spec, points, _ = runtime.load()
        server = runtime.build_server(spec, points)
        server.start()
        self.addCleanup(server.close)
        runtime.refresh()
        from pymodbus.client import ModbusTcpClient
        client = ModbusTcpClient("127.0.0.1", port=spec.bind_port)
        self.assertTrue(client.connect())
        self.addCleanup(client.close)
        response = client.read_input_registers(0, count=2, device_id=1)
        self.assertEqual(response.registers, [16712, 0])  # float32 12.5, big-endian
        result = client.write_coil(0, True, device_id=1)
        self.assertFalse(result.isError())
        deadline = time.time() + 2
        while not writer.calls and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(writer.calls)
        args, _ = writer.calls[-1]
        self.assertEqual(args[:3], (1, "yk", 1))
        self.assertEqual(args[3], 1)


class TransWebApiTests(ScdfeTTestBase):
    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        raw = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if raw is not None else {}
        conn.request(method, path, raw, headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode())
        conn.close()
        return response.status, data

    def setUp(self):
        super().setUp()
        self.httpd = make_server("127.0.0.1", 0, db_path=self.db)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.httpd.shutdown()
        self.thread.join(3)
        self.httpd.server_close()

    def test_health_channel_and_point_crud(self):
        status, data = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        channel = {
            "channel_id": 9, "code": "WEB9", "name": "WEB通道", "protocol": "modbus-tcp",
            "bind_host": "127.0.0.1", "bind_port": _free_port(), "common_address": 1, "unit_id": 1,
        }
        status, data = self.request("POST", "/api/trans/channels", channel)
        self.assertEqual(status, 201)
        self.assertEqual(data["item"]["channel_id"], 9)
        point = {
            "pnt_no": 1, "kind": "yc", "source_kind": "yc", "source_rtu_id": 1,
            "source_pnt_no": 1, "enabled": 1, "register_area": "input", "address": 0,
            "data_type": "float32",
        }
        status, data = self.request("POST", "/api/trans/channels/9/points", point)
        self.assertEqual(status, 201)
        self.assertEqual(data["item"]["source_rtu_id"], 1)
        status, data = self.request("GET", "/api/trans/channels/9/points")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)


if __name__ == "__main__":
    unittest.main()

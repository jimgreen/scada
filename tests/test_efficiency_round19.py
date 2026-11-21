"""Regression and efficiency tests for cross-service protocol, SQLite, and Web optimizations."""
from __future__ import annotations

import gzip
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from client.opc import OpcUaMaster
from core import iec104_python as c104
from core.packet_logging import DatabasePacketLogSwitch, packet_log_switch, packet_recording_state
from core.packet_trace import PacketTrace
from scdfe_c.web.server import ScadaHTTPServer as CWebServer
from scdfe_c.web.service import SCHEMA_DIR, ScadaStore
from scdfe_d.service import DeviceStore
from scdfe_d.web.server import DeviceHTTPServer as DWebServer
from scdfe_s.web.server import ScadaHTTPServer as SWebServer
from scdfe_t.web.server import TransHTTPServer as TWebServer
from server.modbus_trans_server import ModbusTcpTransServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http_get(server, path: str, headers: dict[str, str] | None = None):
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


class _FakeCanMessage:
    def __init__(self):
        self.arbitration_id = 0x123
        self.dlc = 8
        self.is_extended_id = False
        self.is_fd = False
        self.is_remote_frame = False
        self.is_error_frame = False
        self.bitrate_switch = False
        self.error_state_indicator = False
        self.timestamp = 1.25
        self.data_reads = 0

    @property
    def data(self):
        self.data_reads += 1
        return b"\x01\x02\x03\x04\x05\x06\x07\x08"


class _FakeOpcBackend:
    def __init__(self, values):
        self.values = values

    def read_node_data(self, node_id):
        return self.values[node_id]


class EfficiencyRound19Tests(unittest.TestCase):
    def _temp_root(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_iec104_periodic_tracking_burst_and_selector_modify(self):
        port = _free_port()
        server = c104.Server(ip="127.0.0.1", port=port, tick_rate_ms=20)
        station = server.add_station(common_address=1)
        points = [station.add_point(io_address=0x4001 + i, type=c104.Type.M_ME_NC_1) for i in range(60)]
        self.assertEqual(station.periodic_points(), ())
        points[0].report_ms = 200
        self.assertEqual(station.periodic_points(), (points[0],))
        points[0].report_ms = 0
        self.assertEqual(station.periodic_points(), ())

        tx_frames = []
        rx_frames = []
        server.on_send_raw(lambda srv, data: tx_frames.append(data))
        server.start()
        self.addCleanup(server.stop)

        client = c104.Client(tick_rate_ms=20, command_timeout_ms=3000)
        conn = client.add_connection(ip="127.0.0.1", port=port, init=c104.Init.NONE)
        conn.on_receive_raw(lambda c, data: rx_frames.append(data))
        cstation = conn.add_station(common_address=1)
        received = {}
        done = threading.Event()

        def on_recv(point, previous, message):
            received[point.io_address] = point.value
            if len(received) == len(points):
                done.set()
            return c104.ResponseState.SUCCESS

        for i in range(len(points)):
            cp = cstation.add_point(io_address=0x4001 + i, type=c104.Type.M_ME_NC_1)
            cp.on_receive(on_recv)

        client.start()
        self.addCleanup(client.stop)

        deadline = time.monotonic() + 5
        while not conn.is_connected and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(conn.is_connected)
        self.assertTrue(conn.test_fr())

        for i, p in enumerate(points):
            p.value = float(i) * 1.5
        self.assertTrue(server.transmit_batch(c104.Batch(c104.Cot.SPONTANEOUS, points)))
        self.assertTrue(done.wait(5), f"only received {len(received)}/{len(points)} points")
        for i in range(len(points)):
            self.assertAlmostEqual(received[0x4001 + i], float(i) * 1.5, places=3)
        self.assertGreaterEqual(len(tx_frames), 1)
        self.assertGreaterEqual(len(rx_frames), 1)

    def test_modbus_trans_encode_fast_path_exactness(self):
        points = [
            {"kind": "yc", "pnt_no": 1, "register_area": "input", "address": 0, "data_type": "int16"},
            {"kind": "yc", "pnt_no": 2, "register_area": "input", "address": 1, "data_type": "uint16"},
            {"kind": "yc", "pnt_no": 3, "register_area": "input", "address": 2, "data_type": "uint64"},
            {"kind": "yc", "pnt_no": 4, "register_area": "input", "address": 6, "data_type": "int32", "scale": 0.1, "offset": 10.0},
            {"kind": "yc", "pnt_no": 5, "register_area": "input", "address": 8, "data_type": "float32", "byte_order": "little", "word_order": "little"},
        ]
        server = ModbusTcpTransServer(points=points, host="127.0.0.1", port=0)
        for key, value in [
            (("yc", 1), -1234),
            (("yc", 2), 65535),
            (("yc", 3), (1 << 64) - 1),
            (("yc", 4), 25),
            (("yc", 5), 12.75),
        ]:
            row = server._mappings[key]
            encoded = server._encode(row, value)
            decoded = server._decode(row, encoded)
            if isinstance(value, float):
                self.assertAlmostEqual(decoded, value, places=4)
            else:
                self.assertEqual(decoded, value)

    def test_can_trace_skips_payload_work_when_disabled(self):
        class _Dummy(PacketTrace):
            PACKET_PROTOCOL = "CAN"
            interface = "virtual"
            channel = "bus0"

        dummy = _Dummy()
        msg = _FakeCanMessage()
        dummy._trace_can("RX", msg)
        self.assertEqual(msg.data_reads, 0)

        captured = []
        dummy.set_packet_handler(lambda direction, data, detail: captured.append((direction, data, detail)))
        dummy._trace_can("RX", msg)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "RX")
        self.assertEqual(msg.data_reads, 1)

    def test_opc_precompiled_poll_plan_matches_explicit_points(self):
        values = {"ns=2;s=YC_1": (12.5, "Good"), "ns=2;s=YX_1": (1, "Good")}
        master = OpcUaMaster("opc.tcp://127.0.0.1:4840", rtu_id=1, backend=_FakeOpcBackend(values))
        points = [
            {"rtu_id": 1, "kind": "yc", "pnt_no": 1, "node_id": "ns=2;s=YC_1", "data_type": "float"},
            {"rtu_id": 1, "kind": "yx", "pnt_no": 1, "node_id": "ns=2;s=YX_1", "data_type": "bool"},
        ]
        master.configure(points)
        planned = master.collect_once()
        direct = master.collect_once(points)
        self.assertEqual([(v.kind, v.point_no, v.value) for v in planned],
                         [(v.kind, v.point_no, v.value) for v in direct])
        self.assertEqual(planned[0].value, 12.5)
        self.assertIs(planned[1].value, True)

    def test_packet_recording_state_single_connection_no_n_plus_1(self):
        root = self._temp_root()
        db = root / "scada.sqlite3"
        store = ScadaStore(db, SCHEMA_DIR)
        self.addCleanup(store.close)
        for idx in range(1, 21):
            store.create_row("fes_rtu_define", {
                "rtu_id": idx,
                "channel_id": idx,
                "name": f"CH{idx}",
                "channel_type": "iec104",
                "packet_recording": idx % 2,
            })
        packet_log_switch.cache_clear()
        real_connect = sqlite3.connect
        connect_calls = 0

        def counting_connect(*args, **kwargs):
            nonlocal connect_calls
            connect_calls += 1
            return real_connect(*args, **kwargs)

        with patch("core.packet_logging.sqlite3.connect", side_effect=counting_connect):
            state = packet_recording_state(db, "c")
        self.assertEqual(len(state["channels"]), 20)
        self.assertEqual(connect_calls, 1)

        switch = DatabasePacketLogSwitch(db, "c")
        connect_calls = 0
        with patch("core.packet_logging.sqlite3.connect", side_effect=counting_connect):
            results = [switch.enabled(idx) for idx in range(1, 21)]
        self.assertEqual(connect_calls, 1)
        self.assertEqual(results, [bool(idx % 2) for idx in range(1, 21)])

    def test_scdfe_d_indexes_pooling_and_source_connection_reuse(self):
        root = self._temp_root()
        src_db = root / "scada.sqlite3"
        scada = ScadaStore(src_db, SCHEMA_DIR)
        self.addCleanup(scada.close)
        scada.create_row("fes_rtu_define", {"rtu_id": 1, "channel_id": 1, "name": "R1", "channel_type": "iec104"})
        now = int(time.time() * 1000)
        scada.create_row("fes_yc_define", {"rtu_id": 1, "pnt_no": 1, "name": "P", "value": 42.5, "status": 0, "refresh_time": now})

        dev_db = root / "device.sqlite3"
        store = DeviceStore(dev_db, workspace_root=root, connection_pool_size=2)
        self.addCleanup(store.close)
        with store.connection() as con:
            indexes = {row[1] for row in con.execute("SELECT type, name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"d_device_members_device_table", "d_measurement_value_refresh", "d_measurement_active_device"} <= indexes)

        # Pooled connections are reused across calls.
        with store.connection() as con1:
            id1 = id(con1)
        with store.connection() as con2:
            id2 = id(con2)
        self.assertEqual(id1, id2)

        device = store.create_device({"name": "G1", "category": "generator"})
        store.save_measurement({
            "device_id": device["device_id"],
            "metric": "p",
            "kind": "yc",
            "source_database": "scada.sqlite3",
            "rtu_id": 1,
            "pnt_no": 1,
            "enabled": 1,
        })

        source_cache = {}
        real_connect = sqlite3.connect
        source_opens = 0

        def count_source_connect(database, *args, **kwargs):
            nonlocal source_opens
            if isinstance(database, str) and "scada.sqlite3" in database:
                source_opens += 1
            return real_connect(database, *args, **kwargs)

        try:
            with patch("scdfe_d.service.sqlite3.connect", side_effect=count_source_connect):
                r1 = store.collect_once(_source_cache=source_cache)
                r2 = store.collect_once(_source_cache=source_cache)
            self.assertEqual(source_opens, 1)
            self.assertEqual(r1["valid_count"], 1)
            self.assertEqual(r2["valid_count"], 1)
        finally:
            for entry in source_cache.values():
                entry[0].close()

    def test_web_gzip_and_static_etag_across_all_four_servers(self):
        root = self._temp_root()
        c_db = root / "c.sqlite3"
        s_db = root / "s.sqlite3"
        t_db = root / "t.sqlite3"
        d_db = root / "d.sqlite3"

        servers = [
            ("c", CWebServer(("127.0.0.1", 0), db_path=c_db, schema_dir=SCHEMA_DIR, workspace_root=root), "/static/app.js", "/api/schemas"),
            ("s", SWebServer(("127.0.0.1", 0), db_path=s_db, schema_dir=SCHEMA_DIR, workspace_root=root), "/static/protocol.js", "/api/schemas"),
            ("t", TWebServer(("127.0.0.1", 0), db_path=t_db, schema_dir=SCHEMA_DIR, workspace_root=root), "/static/app.js", "/api/trans/channels"),
            ("d", DWebServer(("127.0.0.1", 0), db_path=d_db, workspace_root=root), "/static/app.js", "/api/overview"),
        ]
        for label, srv, static_path, api_path in servers:
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)

            # 1. Static asset returns ETag + gzip when requested, and 304 on If-None-Match.
            status, headers, body = _http_get(srv, static_path, {"Accept-Encoding": "gzip"})
            self.assertEqual(status, 200, label)
            self.assertEqual(headers.get("Content-Encoding"), "gzip", label)
            self.assertIn("ETag", headers, label)
            decompressed = gzip.decompress(body)
            self.assertGreater(len(decompressed), len(body), label)

            status304, _, body304 = _http_get(srv, static_path, {"If-None-Match": headers["ETag"]})
            self.assertEqual(status304, 304, label)
            self.assertEqual(body304, b"", label)

            # 2. Without Accept-Encoding, response stays uncompressed plain bytes.
            status_plain, headers_plain, body_plain = _http_get(srv, static_path)
            self.assertEqual(status_plain, 200, label)
            self.assertNotIn("Content-Encoding", headers_plain, label)
            self.assertEqual(body_plain, decompressed, label)

            # 3. API JSON endpoint works both plain and gzip-compressed (when >= 1KB).
            st_api, hdr_api, raw_api = _http_get(srv, api_path, {"Accept-Encoding": "gzip"})
            self.assertEqual(st_api, 200, label)
            decoded = gzip.decompress(raw_api) if hdr_api.get("Content-Encoding") == "gzip" else raw_api
            parsed = json.loads(decoded.decode("utf-8"))
            self.assertIsInstance(parsed, dict, label)
            if len(decoded) >= 1024:
                self.assertEqual(hdr_api.get("Content-Encoding"), "gzip", label)
                self.assertLess(len(raw_api), len(decoded), label)


if __name__ == "__main__":
    unittest.main()

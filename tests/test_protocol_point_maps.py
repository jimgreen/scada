"""Database-backed protocol point maps and CAN payload decoding."""
from pathlib import Path
import struct
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from uuid import uuid4

from client import BacnetMaster, CanMaster, IEC104Master, OpcUaMaster
from server import IEC104Server
from scdfe_c.web.service import ScadaStore


class ProtocolPointMapTests(unittest.TestCase):
    def _store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = ScadaStore(Path(directory.name) / "scada.sqlite3")
        store.create_row("fes_rtu_define", {
            "rtu_id": 7, "channel_id": 70, "channel_type": "iec104",
            "process_enable": 0,
        })
        store.create_row("fes_channel_iec104_define", {
            "id": 1, "rtu_id": 7, "remote_host": "127.0.0.1", "remote_port": 2404,
        })
        store.create_row("fes_yc_define", {"rtu_id": 7, "pnt_no": 1, "name": "YC"})
        store.create_row("fes_yx_define", {"rtu_id": 7, "pnt_no": 1, "name": "YX"})
        store.create_row("fes_yk_define", {"rtu_id": 7, "pnt_no": 1, "name": "YK"})
        store.create_row("fes_yt_define", {"rtu_id": 7, "pnt_no": 1, "name": "YT"})
        return store

    def test_iec104_from_store_uses_database_ioa(self):
        store = self._store()
        store.create_row("fes_channel_iec104_point_define", {
            "rtu_id": 7, "kind": "yc", "pnt_no": 1,
            "io_address": 9001, "type_id": 1,
        })
        master = IEC104Master.from_store(store, 7)
        self.addCleanup(master.close)
        self.assertEqual(master._points["yc", 1].io_address, 9001)

    def test_bacnet_opc_can_from_store_load_native_maps(self):
        store = self._store()
        store.update_row("fes_rtu_define", [7], {"channel_type": "bacnet"})
        store.create_row("fes_channel_bacnet_define", {
            "id": 1, "rtu_id": 7, "remote_host": "127.0.0.1", "dev_id": 1001,
        })
        store.create_row("fes_channel_bacnet_point_define", {
            "rtu_id": 7, "kind": "yc", "pnt_no": 1,
            "object_type": "analog-value", "instance": 42,
        })
        for kind, object_type in (("yx", "binary-input"), ("yk", "binary-output"),
                                  ("yt", "analog-output")):
            store.create_row("fes_channel_bacnet_point_define", {
                "rtu_id": 7, "kind": kind, "pnt_no": 1,
                "object_type": object_type, "instance": 42,
            })
        bacnet = BacnetMaster.from_store(store, 7)
        self.assertEqual((bacnet._points[0]["object_type"], bacnet._points[0]["instance"]),
                         ("analog-value", 42))
        store.update_row("fes_rtu_define", [7], {"channel_type": "opc"})
        store.create_row("fes_channel_opc_define", {
            "id": 1, "rtu_id": 7, "opc_name": "opc.tcp://127.0.0.1:4840",
        })
        store.create_row("fes_channel_opc_point_define", {
            "rtu_id": 7, "kind": "yc", "pnt_no": 1, "node_id": "ns=4;s=AI42",
        })
        for kind, node_id in (("yx", "ns=4;s=DI42"), ("yk", "ns=4;s=DO42"),
                              ("yt", "ns=4;s=AO42")):
            store.create_row("fes_channel_opc_point_define", {
                "rtu_id": 7, "kind": kind, "pnt_no": 1, "node_id": node_id,
            })
        opc = OpcUaMaster.from_store(store, 7)
        self.assertEqual(opc._points[0]["node_id"], "ns=4;s=AI42")
        store.update_row("fes_rtu_define", [7], {"channel_type": "can"})
        store.create_row("fes_channel_can_define", {
            "rtu_id": 7, "interface": "virtual", "channel": "map-" + uuid4().hex,
        })
        store.create_row("fes_channel_can_point_define", {
            "rtu_id": 7, "kind": "yc", "pnt_no": 1, "frame_id": 0x123,
            "bit_length": 32, "value_type": "float", "byte_order": "big",
        })
        can = CanMaster.from_store(store, 7)
        self.addCleanup(can.close)
        self.assertEqual(can._points[0]["frame_id"], 0x123)
        self.assertEqual(can._bus_options["can_filters"], [{
            "can_id": 0x123, "can_mask": 0x7FF, "extended": False,
        }])

    def test_can_mapping_decodes_and_encodes_four_remote_values(self):
        channel = "map-" + uuid4().hex
        master = CanMaster(interface="virtual", channel=channel, rtu_id=7)
        peer = CanMaster(interface="virtual", channel=channel, auto_start=True)
        self.addCleanup(master.close)
        self.addCleanup(peer.close)
        master.configure([
            {"kind": "yc", "pnt_no": 1, "frame_id": 0x100,
             "bit_length": 32, "value_type": "float", "byte_order": "big"},
            {"kind": "yx", "pnt_no": 1, "frame_id": 0x101,
             "bit_offset": 7, "bit_length": 1, "value_type": "bool"},
            {"kind": "yk", "pnt_no": 1, "frame_id": 0x102, "tx_frame_id": 0x202,
             "bit_offset": 7, "bit_length": 1, "value_type": "bool"},
            {"kind": "yt", "pnt_no": 1, "frame_id": 0x103, "tx_frame_id": 0x203,
             "bit_length": 32, "value_type": "float", "byte_order": "big"},
        ])
        master.connect()
        peer.send(0x100, struct.pack(">f", 12.5))
        peer.send(0x101, b"\x01")
        values = master.collect_once(timeout=0.2, max_frames=10)
        self.assertEqual([(value.kind, value.value) for value in values],
                         [("yc", 12.5), ("yx", True)])
        master.send_yk_point(1, True)
        self.assertEqual(peer.recv(0.2).arbitration_id, 0x202)
        master.send_yt_point(1, 3.5)
        frame = peer.recv(0.2)
        self.assertEqual((frame.arbitration_id, struct.unpack(">f", bytes(frame.data[:4]))[0]),
                         (0x203, 3.5))

    def test_can_command_mapping_can_wait_for_configured_response(self):
        channel = "map-" + uuid4().hex
        master = CanMaster(interface="virtual", channel=channel, rtu_id=7)
        peer = CanMaster(interface="virtual", channel=channel, auto_start=True)
        self.addCleanup(master.close)
        self.addCleanup(peer.close)
        master.configure([{
            "kind": "yk", "pnt_no": 1, "frame_id": 0x102,
            "tx_frame_id": 0x202, "response_id": 0x282,
            "bit_offset": 7, "bit_length": 1, "value_type": "bool",
            "response_data_hex": "AA 55",
        }])
        master.connect()

        def respond():
            request = peer.recv(1)
            self.assertEqual((request.arbitration_id, request.data[:1]), (0x202, b"\x01"))
            peer.send(0x282, b"\xAA\x55\x01")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(respond)
            response = master.send_yk_point(1, True, wait_response=True, response_timeout=1)
            future.result(timeout=2)
        self.assertEqual((response.arbitration_id, response.data[:2]), (0x282, b"\xAA\x55"))

    def test_iec104_server_from_store_uses_protocol_mapping(self):
        store = self._store()
        store.update_row("fes_rtu_define", [7], {"channel_type": "iec104"})
        store.create_row("fes_channel_iec104_point_define", {
            "rtu_id": 7, "kind": "yx", "pnt_no": 1,
            "io_address": 7001, "type_id": 1, "report_ms": 200,
        })
        server = IEC104Server.from_store(store, 7, ip="127.0.0.1", port=25079)
        self.assertEqual(server.rtu_id, 7)
        self.assertEqual(server._points["yx", 1].io_address, 7001)
        self.assertEqual(server._points["yx", 1].report_ms, 200)


if __name__ == "__main__":
    unittest.main()

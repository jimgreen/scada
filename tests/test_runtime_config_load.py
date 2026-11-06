import tempfile
import unittest
from pathlib import Path

from scdfe_c import ChannelSpec, build_master
from scdfe_c.web.service import ScadaStore


class RuntimeConfigLoadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ScadaStore(Path(self.temp.name) / "scada.sqlite3")
        self.addCleanup(self.temp.cleanup)

    def test_bacnet_mapping_wins_over_default_without_wide_rows(self):
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 7, "channel_id": 70, "channel_type": "bacnet",
        })
        self.store.create_row("fes_yc_define", {"rtu_id": 7, "pnt_no": 1})
        self.store.create_row("fes_yc_define", {"rtu_id": 7, "pnt_no": 2})
        self.store.create_row("fes_channel_bacnet_point_define", {
            "rtu_id": 7, "kind": "yc", "pnt_no": 1,
            "object_type": "analog-input", "instance": 900,
            "property_identifier": "Present_Value",
        })
        self.store.create_row("fes_channel_bacnet_define", {
            "id": 1, "rtu_id": 7, "bind_ip": "127.0.0.1", "bind_port": 47809, "dev_id": 1001,
        })
        master = build_master(self.store, ChannelSpec(7, 70, "bacnet"))
        self.addCleanup(master.close)
        result = {(p["kind"], p["pnt_no"]): p for p in master._points}
        self.assertEqual(result[("yc", 1)]["object_type"], "analog-input")
        self.assertEqual(result[("yc", 1)]["instance"], 900)
        self.assertEqual((result[("yc", 2)]["object_type"], result[("yc", 2)]["instance"]), ("analog-value", 2))

    def test_opc_mapping_wins_over_default_and_missing_point_gets_fallback(self):
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 8, "channel_id": 80, "channel_type": "opc",
        })
        self.store.create_row("fes_yx_define", {"rtu_id": 8, "pnt_no": 1})
        self.store.create_row("fes_yx_define", {"rtu_id": 8, "pnt_no": 2})
        self.store.create_row("fes_channel_opc_point_define", {
            "rtu_id": 8, "kind": "yx", "pnt_no": 1,
            "node_id": "ns=4;s=Configured.YX1", "data_type": "bool",
        })
        self.store.create_row("fes_channel_opc_define", {
            "id": 1, "rtu_id": 8, "opc_name": "opc.tcp://127.0.0.1:4840",
        })
        master = build_master(self.store, ChannelSpec(8, 80, "opc"))
        self.addCleanup(master.close)
        result = {(p["kind"], p["pnt_no"]): p for p in master._points}
        self.assertEqual(result[("yx", 1)]["node_id"], "ns=4;s=Configured.YX1")
        self.assertEqual(result[("yx", 2)]["node_id"], "ns=2;s=YX_002")


if __name__ == "__main__":
    unittest.main()

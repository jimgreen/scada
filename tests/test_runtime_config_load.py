import tempfile
import unittest
from pathlib import Path

from scdfe_c import ChannelSpec, _default_point_map
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
            "object_type": "custom-value", "instance": 900,
            "property_identifier": "Present_Value",
        })
        result = _default_point_map(self.store, ChannelSpec(7, 70, "bacnet"))
        self.assertEqual(result[("yc", 1)]["object_type"], "custom-value")
        self.assertEqual(result[("yc", 1)]["instance"], 900)
        self.assertEqual(result[("yc", 2)], {
            "object_type": "analog-value", "instance": 2,
        })

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
        result = _default_point_map(self.store, ChannelSpec(8, 80, "opc"))
        self.assertEqual(result[("yx", 1)]["node_id"], "ns=4;s=Configured.YX1")
        self.assertEqual(result[("yx", 2)]["node_id"], "ns=2;s=YX_002")


if __name__ == "__main__":
    unittest.main()

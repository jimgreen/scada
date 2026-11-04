"""IEC104 Web test service lifecycle, updates, and client loopback."""
from pathlib import Path
import tempfile
import time
import unittest

from client import IEC104Master
from scdfe_s.web.iec104_service import IEC104Service
from scdfe_c.web.service import ScadaStore


class IEC104ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ScadaStore(Path(self.temp.name) / "scada.sqlite3")
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 1, "channel_id": 1, "channel_type": "iec104", "name": "测试站",
            "process_enable": 0,
        })
        self.store.create_row("fes_channel_iec104_define", {
            "id": 1, "rtu_id": 1, "rtu_addr": 1,
            "remote_host": "127.0.0.1", "remote_port": 25141,
        })
        for kind, table, value, type_id, io_address in (
            ("yc", "fes_yc_define", 220.0, 1, 0x4001),
            ("yx", "fes_yx_define", 1, 1, 1),
            ("yk", "fes_yk_define", 0, 1, 0x6001),
            ("yt", "fes_yt_define", 5.0, 3, 0x6201),
        ):
            self.store.create_row(table, {"rtu_id": 1, "pnt_no": 1, "name": kind, "value": value})
            self.store.create_row("fes_channel_iec104_point_define", {
                "rtu_id": 1, "kind": kind, "pnt_no": 1,
                "io_address": io_address, "type_id": type_id,
            })
        self.service = IEC104Service(self.store, config_path=Path(self.temp.name) / "iec.json")
        self.service.configure({"rtu_id": 1, "port": 25141})
        self.service.start()
        self.addCleanup(self.service.close)

    def tearDown(self):
        self.temp.cleanup()

    def test_full_and_incremental_updates_without_client(self):
        self.assertEqual(self.service.incremental_update("yc", 1, 221.5)["value"], 221.5)
        result = self.service.full_update({"yx": {"1": False}, "yc": {"1": 222.0}})
        self.assertEqual(result["requested"], 2)
        self.assertEqual(self.service.status()["point_count"], 4)

    def test_client_interrogation_spontaneous_update_and_command(self):
        client = IEC104Master("127.0.0.1", 25141, common_address=1, init="NONE")
        self.addCleanup(client.close)
        for kind in ("yc", "yx", "yk", "yt"):
            client.add_point(kind, 1)
        client.connect(timeout=3)
        time.sleep(0.1)
        self.assertGreaterEqual(self.service.status()["active_connections"], 1)
        self.assertTrue(client.general_interrogation())
        time.sleep(0.1)
        self.assertEqual(client.value("yc", 1).value, 220.0)
        self.service.incremental_update("yc", 1, 223.0)
        time.sleep(0.1)
        self.assertEqual(client.value("yc", 1).value, 223.0)
        client.send_yk_point(1, True)
        time.sleep(0.1)
        self.assertEqual(self.service.commands()[0]["kind"], "yk")


if __name__ == "__main__":
    unittest.main()

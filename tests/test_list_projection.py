"""Regression tests for narrow WEB list projections."""
from pathlib import Path
import tempfile
import unittest

from scdfe_c.web.service import ApiError, ScadaStore


class ListProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ScadaStore(Path(self.temp.name) / "scada.sqlite3")
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 7, "channel_id": 70, "channel_type": "modbus-tcp", "name": "站 7",
        })
        self.store.create_row("fes_yc_define", {
            "rtu_id": 7, "pnt_no": 1, "code": "YC_1", "name": "电压",
            "value": 12.5, "status": 1, "raw_value": 125,
        })
        self.addCleanup(self.temp.cleanup)

    def test_projection_keeps_keys_version_and_values(self):
        full = self.store.list_rows("fes_yc_define", fields=None)
        projected = self.store.list_rows("fes_yc_define", fields=["name", "value"])
        self.assertEqual(projected["total"], full["total"])
        self.assertEqual(projected["items"], [{
            "rtu_id": 7, "pnt_no": 1, "name": "电压", "value": 12.5, "_version": 1,
        }])
        self.assertEqual(projected["items"][0]["rtu_id"], full["items"][0]["rtu_id"])

    def test_projection_rejects_unknown_or_empty_fields(self):
        with self.assertRaises(ApiError):
            self.store.list_rows("fes_yc_define", fields=["missing"])
        with self.assertRaises(ApiError):
            self.store.list_rows("fes_yc_define", fields=[])


if __name__ == "__main__":
    unittest.main()

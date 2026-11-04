import tempfile
import unittest
from pathlib import Path

from scdfe_c.web.service import ScadaStore
from scdfe_t.web.service import TransStore


class ScdfeTransSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "scada.sqlite3"
        self.trans = TransStore(self.db)
        self.store = self.trans.store
        self.trans.create_channel({
            "channel_id": 1,
            "code": "TRANS1",
            "name": "转发1",
            "protocol": "modbus-tcp",
            "bind_host": "127.0.0.1",
            "bind_port": 15021,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_point_schema_uses_kind_in_composite_key(self):
        self.assertEqual(
            self.store.catalog.public("fes_scdfe_t_point_define")["primary_keys"],
            ["channel_id", "pnt_no", "kind"],
        )

    def test_same_point_number_can_be_mapped_for_each_kind(self):
        for kind in ("yc", "yx", "yk", "yt"):
            row = self.trans.create_point(1, {
                "pnt_no": 1,
                "kind": kind,
                "source_rtu_id": 7,
                "source_pnt_no": 1,
            })
            self.assertEqual(row["source_kind"], kind)
        rows = self.trans.points(1)
        self.assertEqual({row["kind"] for row in rows}, {"yc", "yx", "yk", "yt"})
        self.assertEqual(len(rows), 4)
        self.assertEqual(self.trans._point_key(1, "yt", 1), [1, 1, "yt"])


if __name__ == "__main__":
    unittest.main()

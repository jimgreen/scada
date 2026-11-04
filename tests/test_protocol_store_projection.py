import json
import tempfile
import unittest
from pathlib import Path

from client.bacnet import _BacnetProtocol
from client.can import CanMaster
from client.iec104 import IEC104Master
from server.iec104_server import IEC104Server
from client.opc import _OpcUaProtocol
from scdfe_c.web.service import ScadaStore


class ProtocolStoreProjectionTests(unittest.TestCase):
    def test_projection_columns_exist_in_current_schema(self):
        schema_dir = Path(__file__).parents[1] / "dbdef" / "json"
        cases = (
            (_BacnetProtocol, "fes_channel_bacnet_define"),
            (_OpcUaProtocol, "fes_channel_opc_define"),
            (IEC104Master, "fes_channel_iec104_define"),
            (IEC104Server, "fes_channel_iec104_define"),
            (CanMaster, "fes_channel_can_define"),
        )
        for adapter, table in cases:
            schema = json.loads((schema_dir / f"{table}.json").read_text(encoding="utf-8"))
            columns = {column["eng_name"] for column in schema["columns"]}
            projection = getattr(adapter, "_STORE_DEFINITION_PROJECTION", None)
            if projection is None:
                projection = getattr(adapter, "_STORE_PARAMETER_PROJECTION")
            selected = {name.strip() for name in projection.split(",")}
            self.assertTrue(selected <= columns, (adapter.__name__, selected - columns))

    def test_rtu_projection_is_minimal_and_shared_shape(self):
        for adapter in (_BacnetProtocol, _OpcUaProtocol, IEC104Master, IEC104Server, CanMaster):
            self.assertEqual(adapter._STORE_RTU_PROJECTION, "rtu_id,channel_type")

    def test_joined_bacnet_and_opc_point_queries_keep_mapping_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ScadaStore(Path(directory) / "scada.sqlite3")
            store.create_row("fes_rtu_define", {
                "rtu_id": 7, "channel_id": 70, "channel_type": "bacnet",
            })
            for table, point in (("fes_yc_define", 1), ("fes_yx_define", 2),
                                 ("fes_yk_define", 3), ("fes_yt_define", 4)):
                store.create_row(table, {"rtu_id": 7, "pnt_no": point})
            store.create_row("fes_channel_bacnet_point_define", {
                "rtu_id": 7, "kind": "yc", "pnt_no": 1,
                "object_type": "analog-value", "instance": 101,
            })
            with store.connection() as connection:
                rows = [dict(row) for row in connection.execute(
                    _BacnetProtocol._STORE_POINT_QUERY, (7, 7, 7, 7, 7))]
            self.assertEqual([(row["kind"], row["pnt_no"]) for row in rows],
                             [("yc", 1), ("yx", 2), ("yk", 3), ("yt", 4)])
            self.assertEqual((rows[0]["object_type"], rows[0]["instance"]),
                             ("analog-value", 101))
            self.assertEqual(rows[0]["rtu_id"], 7)
            self.assertIsNone(rows[1]["object_type"])


if __name__ == "__main__":
    unittest.main()

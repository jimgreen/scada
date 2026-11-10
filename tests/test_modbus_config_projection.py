import json
import unittest
from pathlib import Path

from client.modbus_tcp import _ModbusMasterOperations


class ModbusConfigProjectionTests(unittest.TestCase):
    def test_projections_only_reference_current_schema_columns(self):
        schema_dir = Path(__file__).parents[1] / "dbdef" / "json"
        for table, projection in _ModbusMasterOperations._STORE_PROJECTIONS.items():
            schema = json.loads((schema_dir / f"{table}.json").read_text(encoding="utf-8"))
            columns = {column["eng_name"] for column in schema["columns"]}
            selected = {name.strip() for name in projection.split(",")}
            self.assertTrue(selected <= columns, (table, selected - columns))

    def test_runtime_projection_does_not_load_display_only_fields(self):
        display_columns = {"name"}
        for projection in _ModbusMasterOperations._STORE_PROJECTIONS.values():
            self.assertTrue(display_columns.isdisjoint({name.strip() for name in projection.split(",")}))


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from scdfe_c.web.service import ScadaStore
from core.runtime_status import RuntimeStatusStore, runtime_status_rows


class RuntimeStatusTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = ScadaStore(self.root / "scada.sqlite3")
        self.addCleanup(self.store.close)

    def test_upsert_is_throttled_and_diagnostic_is_decoded_for_readers(self):
        status = RuntimeStatusStore(self.store, "scdfe_t", write_interval_ms=60_000)
        values = {
            "protocol": "iec104",
            "state": "running",
            "running": 1,
            "connection_state": "listening",
            "process_status": 2,
            "rx_count": 12,
            "tx_count": 8,
            "diagnostic_json": {"k": 12, "unexpected_messages": [{"type": 45}]},
        }
        self.assertTrue(status.upsert(7, values, force=True))
        self.assertFalse(status.upsert(7, values))
        rows = runtime_status_rows(self.store, service_name="scdfe_t", channel_id=7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel_id"], 7)
        self.assertEqual(rows[0]["diagnostic"]["k"], 12)
        self.assertEqual(rows[0]["diagnostic"]["unexpected_messages"][0]["type"], 45)
        self.assertEqual(json.loads(rows[0]["diagnostic_json"])["k"], 12)


if __name__ == "__main__":
    unittest.main()

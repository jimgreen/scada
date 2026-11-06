"""Transmission scans, partial publication failures and bounded file reads."""
from pathlib import Path
from tempfile import TemporaryDirectory
import socket
import unittest
from pymodbus.client import ModbusTcpClient
from scdfe_t.web.service import TransStore
from server.modbus_trans_server import ModbusTcpTransServer


class TransmissionEfficiencyTests(unittest.TestCase):
    def test_modbus_scan_failure_isolated_and_cache_matches_remote_registers(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        points = [dict(kind="yc", pnt_no=i, address=i-1, register_area="input", data_type="uint16") for i in (1, 2)]
        server = ModbusTcpTransServer(host="127.0.0.1", port=port, points=points,
                                      point_values={("yc", 1): 10, ("yc", 2): 20})
        server.start()
        self.addCleanup(server.close)
        peer = ModbusTcpClient("127.0.0.1", port=port)
        self.addCleanup(peer.close)
        self.assertTrue(peer.connect())
        failed = server.update_many([("yc", 1, "bad"), ("yc", 2, 32)])
        self.assertEqual(set(failed), {("yc", 1)})
        self.assertEqual(peer.read_input_registers(0, count=2, device_id=1).registers, [10, 32])
        self.assertEqual(server._values[("yc", 1)], 10)
        self.assertFalse(server.update_many([("yc", 1, 31)]))
        self.assertEqual(peer.read_input_registers(0, count=2, device_id=1).registers, [31, 32])

    def test_channel_log_filter_uses_filename_even_without_packet_marker(self):
        from core.web_logs import log_browser
        with TemporaryDirectory() as tmp:
            for cid in (1, 10):
                (Path(tmp)/f"scdfe_t-channel-{cid}-20261106-10.log").write_text(
                    "10:30:30 [报文] 报文 RX HEX=01\n10:30:31 [报文] 报文 TX HEX=02\n", encoding="utf8")
            base = {"channel_id": "1"}
            self.assertEqual(log_browser(tmp, "t", False, base)["total"], 1)
            result = log_browser(tmp, "t", True, {**base, "record": "packet", "direction": "RX"})
            self.assertEqual(len(result["items"]), 1)
            self.assertIn("channel-1-", result["items"][0]["filename"])
            with self.assertRaises(ValueError):
                log_browser(tmp, "t", True, {**base, "process": "scdfe_t-channel-10"})
            for invalid in ("0", "-1", "01", "9223372036854775808"):
                with self.assertRaises(ValueError):
                    log_browser(tmp, "t", False, {"channel_id": invalid})

    def test_log_tail_preserves_utf8_end_and_small_file_exact_total(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            s = TransStore(root / "db.sqlite3")
            self.addCleanup(s.store.close)
            name = "scdfe_t-channel-1-20261106-10.log"
            file = root / "logs" / name
            file.parent.mkdir(exist_ok=True)
            suffix = "开始\r\n中文末行"  # unterminated final line
            file.write_bytes(suffix.encode())
            result = s.read_log(name, 1)
            self.assertEqual((result["lines"], result["total"], result["truncated"]), (["中文末行"], 2, False))
            file.write_bytes(("很长的记录" * 150000 + "\n" + suffix).encode())
            result = s.read_log(name, 1)
            self.assertEqual((result["lines"], result["total"], result["truncated"]), (["中文末行"], None, True))

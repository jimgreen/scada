import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from core.network_endpoint import configured_endpoints, network_endpoint
from scdfe_c.web.service import ScadaStore


class NetworkEndpointTests(unittest.TestCase):
    def test_opc_url_is_parsed_without_credentials_or_path(self):
        for url, host, port in (
            ("opc.tcp://operator:secret@[::1]:4841/scada", "::1", 4841),
            ("opc.tcp://station/scada", "station", 4840),
            ("https://station/scada", "station", 443),
        ):
            with self.subTest(url=url):
                self.assertEqual(network_endpoint("opc", {"opc_name": url}),
                                 {"host": host, "port": port, "role": "remote", "transport": "TCP"})
        for invalid in ("Legacy.OPC.Server", "opc.tcp://[invalid", "opc.tcp://host:bad", "https://host:99999"):
            self.assertIsNone(network_endpoint("opc", {"opc_name": invalid}))
        self.assertIsNone(network_endpoint("iec104", {"remote_port": 2404}))
        self.assertIsNone(network_endpoint("can", {"remote_host": "127.0.0.1", "remote_port": 2404}))

    def test_typed_channel_binding_is_batched_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            store = ScadaStore(path)
            try:
                store.create_row("fes_channel_iec104_define", {
                    "id": 1, "channel_id": 7, "remote_host": "192.0.2.7", "remote_port": 2404,
                })
                store.create_row("fes_channel_modbus_tcp_define", {
                    "channel_id": 7, "remote_host": "192.0.2.8", "remote_port": 1502,
                })
            finally:
                store.close()
            store = ScadaStore(path)
            try:
                rtus = [{"channel_type": p, "channel_id": 7} for _ in range(60)
                        for p in ("iec104", "modbus-tcp", "can")]
                statements = []
                original = store.connection
                @contextmanager
                def traced():
                    with original() as con:
                        con.set_trace_callback(statements.append)
                        try:
                            yield con
                        finally:
                            con.set_trace_callback(None)
                with patch.object(store, "connection", traced):
                    result = configured_endpoints(store, rtus)
                self.assertEqual(result[("iec104", 7)]["host"], "192.0.2.7")
                self.assertEqual(result[("modbus-tcp", 7)]["port"], 1502)
                self.assertNotIn(("can", 7), result)
                self.assertEqual(len([s for s in statements if s.startswith("SELECT channel_id,")]), 2)
                self.assertFalse(any(s.startswith(("INSERT", "UPDATE")) for s in statements))
            finally:
                store.close()

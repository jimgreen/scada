"""Isolation tests for the Web-managed protocol services."""
from pathlib import Path
import socket
import tempfile
import unittest

from client.modbus_tcp import ModbusTcpMaster
from scdfe_s.web.protocol_service import (
    CanService,
    ModbusTcpService,
    ModbusTcpServerService,
    ModbusRtuServerService,
    ProtocolServiceError,
)
from scdfe_c.web.service import ScadaStore


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ProtocolServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ScadaStore(Path(self.temp.name) / "scada.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_modbus_tcp_server_lifecycle_and_loopback(self):
        port = free_port()
        service = ModbusTcpService(self.store)
        service.configure({"mode": "server", "listen_port": port, "unit_id": 1})
        self.assertTrue(service.start()["running"])
        native_port = service._engine.server.transport.sockets[0].getsockname()[1]
        master = ModbusTcpMaster("127.0.0.1", native_port, unit_id=1, timeout=0.5, connect_timeout=0.5)
        try:
            master.connect()
            self.assertEqual(master.read_holding_registers(0, 1), [0])
            self.assertTrue(master.write_single_register(0, 321))
            self.assertEqual(master.read_holding_registers(0, 1), [321])
        finally:
            master.close()
            service.stop()
        self.assertFalse(service.status()["running"])

    def test_can_virtual_bus_frame_lifecycle(self):
        service = CanService(self.store)
        service.configure({"channel": "service-test"})
        self.assertTrue(service.start()["running"])
        try:
            frame = service.send_frame(0x123, "01 02 03")
            self.assertEqual(frame["data"], "01 02 03")
        finally:
            service.stop()
        self.assertFalse(service.status()["running"])

    def test_server_services_have_separate_configuration_and_names(self):
        tcp = ModbusTcpServerService(self.store)
        rtu = ModbusRtuServerService(self.store)
        self.assertEqual(tcp.config()["mode"], "server")
        self.assertEqual(rtu.config()["mode"], "server")
        self.assertIn("server", tcp.config_path.name)
        self.assertIn("server", rtu.config_path.name)
        with self.assertRaises(ProtocolServiceError):
            tcp.configure({"mode": "master"})
        with self.assertRaises(ProtocolServiceError):
            rtu.configure({"mode": "master"})


if __name__ == "__main__":
    unittest.main()

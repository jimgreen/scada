"""Protocol bytes must survive file logging and service reconstruction."""
from pathlib import Path
import tempfile
import socket
import threading
import time
import unittest

from core.packet_trace import PacketTrace, packet_lines
from core.packet_logging import packet_log_switch
from scdfe_c.web.service import ScadaStore
from scdfe_s.web.iec104_service import IEC104Service
from scdfe_c import ScdfeLogger


class PacketLoggingTests(unittest.TestCase):
    def test_packet_has_its_own_type_in_the_same_hourly_file(self):
        with tempfile.TemporaryDirectory() as root:
            logger = ScdfeLogger(root, process_name="scdfe_c-rtu-1")
            logger.normal("普通日志")
            logger.packet("RX", b"\x68\x04", "IEC104")
            logger.packet("TX", b"\x68\x05", "IEC104")
            files = list(Path(root).rglob("*.log"))
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding="utf-8")
            self.assertIn("[日志] 普通日志", content)
            self.assertIn("[报文] 报文 RX", content)
            self.assertIn("[报文] 报文 TX", content)
            self.assertNotIn("[日志] 报文", content)

    def test_t_large_packet_log_reads_only_the_tail(self):
        from scdfe_t.web.service import TransStore
        with tempfile.TemporaryDirectory() as root:
            service = TransStore(Path(root) / "scada.sqlite3")
            path = Path(root) / "logs" / "scdfe_t-channel-1-20261105-01.log"
            path.parent.mkdir()
            path.write_bytes((b"01:00:00 [LOG] old packet\n" * 100_000) +
                             "01:00:01 [日志] 报文 RX HEX=68 04\n".encode())
            result = service.read_log(path.name, tail=1)
            self.assertEqual(result["lines"], ["01:00:01 [日志] 报文 RX HEX=68 04"])
            self.assertTrue(result["truncated"])
            self.assertIsNone(result["total"])

    def test_long_packet_is_complete_and_each_line_is_bounded(self):
        payload = bytes(range(256)) * 20
        lines = list(packet_lines("RX", payload, "IEC104 peer=127.0.0.1:2404"))
        restored = b"".join(bytes.fromhex(line.split("HEX=", 1)[1]) for line in lines)
        self.assertEqual(restored, payload)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) < 4000 and "报文 RX" in line for line in lines))

    def test_trace_is_observation_only_even_when_log_disk_fails(self):
        trace = PacketTrace()
        def failed(*args):
            raise OSError("disk full")
        trace.set_packet_handler(failed)
        payload = b"\x00\x01\xff"
        with self.assertLogs("core.packet_trace", level="ERROR"):
            self.assertIs(trace._trace_packet(True, payload), payload)

    def test_s_iec104_rx_tx_are_in_file_and_survive_service_restart(self):
        with tempfile.TemporaryDirectory() as root:
            store = ScadaStore(Path(root) / "scada.sqlite3")
            packet_log_switch(store.db_path, 's').set_enabled(True)
            service = IEC104Service(store)
            service._on_receive_raw(bytes.fromhex("68 04 07 00 00 00"))
            service._on_send_raw(bytes.fromhex("68 04 0b 00 00 00"))
            files = list((Path(root) / "packets").rglob("scdfe_s-iec104-*.log"))
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding="utf-8")
            self.assertIn("[报文] 报文 RX", content)
            self.assertIn("HEX=68 04 07 00 00 00", content)
            self.assertIn("[报文] 报文 TX", content)
            restored = IEC104Service(store)
            self.assertTrue(any(item["level"] == "PACKET" and "HEX=68 04 07 00 00 00" in item["message"]
                                for item in restored.logs(500, record="packet")))


class LivePacketLoggingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = ScadaStore(self.root / "scada.sqlite3")
        for family in ('s', 't'):
            packet_log_switch(self.store.db_path, family).set_enabled(True)
        self.logger = ScdfeLogger(self.root / "logs", process_name="scdfe_c-rtu-10")

    @staticmethod
    def port(udp=False):
        with socket.socket(type=socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def start_service(self, cls, config):
        service = cls(self.store)
        self.addCleanup(service.close)
        service.configure(config)
        service.start()
        return service

    def connect_client(self, client):
        self.addCleanup(client.close)
        client.set_packet_handler(self.logger.packet)
        client.connect()
        return client

    def content(self, process):
        files = list((self.root / "logs").rglob(f"{process}-*.log"))
        files.extend((self.root / "packets").rglob(f"{process}-*.log"))
        return "\n".join(p.read_text(encoding="utf-8") for p in files)

    def assert_both_directions(self, process):
        content = self.content(process)
        self.assertIn("[报文] 报文 RX", content)
        self.assertIn("[报文] 报文 TX", content)
        self.assertNotIn("[日志] 报文", content)
        self.assertIn("HEX=", content)
        return content

    def test_opc_client_and_s_server_log_real_tcp_bytes(self):
        from client.opc import OpcUaMaster
        from scdfe_s.web.protocol_service import OpcServerService
        service = self.start_service(OpcServerService, {"port": self.port()})
        client = self.connect_client(OpcUaMaster(service._engine.endpoint))
        node = f"ns={service._engine.namespace_index};s=YC_1"
        client.read_node(node)
        client.close()
        service.stop()
        self.assert_both_directions("scdfe_c-rtu-10")
        self.assert_both_directions("scdfe_s-opc-server")

    def test_bacnet_client_and_s_server_log_real_datagrams(self):
        from client.bacnet import BacnetMaster
        from scdfe_s.web.protocol_service import BacnetServerService
        port = self.port(True)
        service = self.start_service(BacnetServerService, {"bind_port": port, "simulation": 0})
        client = self.connect_client(BacnetMaster("127.0.0.1", remote_port=port,
                                                local_ip="127.0.0.1", local_port=self.port(True)))
        client.read_property("analog-value", 1)
        client.close()
        service.stop()
        self.assert_both_directions("scdfe_c-rtu-10")
        self.assert_both_directions("scdfe_s-bacnet-server")

    def test_can_client_and_s_server_log_id_flags_and_payload(self):
        from client.can import CanMaster
        from scdfe_s.web.protocol_service import CanServerService
        channel = "packet-" + self.root.name
        service = self.start_service(CanServerService, {"channel": channel, "receive_own_messages": 0})
        client = self.connect_client(CanMaster(interface="virtual", channel=channel, auto_start=False))
        client.send(0x100, b"\x12\x34")
        self.assertEqual(client.recv(2).arbitration_id, 0x101)
        client.close()
        service.stop()
        content = self.assert_both_directions("scdfe_c-rtu-10")
        self.assertIn("ID=0x100 DLC=2", content)
        self.assertIn("HEX=12 34", content)
        self.assert_both_directions("scdfe_s-can-server")

    def test_iec104_client_and_t_server_log_apdus(self):
        from client.iec104 import IEC104Master
        from scdfe_t.trans import TransRuntime, TransSpec
        port = self.port()
        runtime = TransRuntime(self.store.db_path)
        spec = TransSpec(1, "iec104", "127.0.0.1", port, 1, 1, .1)
        server = runtime.build_server(spec, [])
        server.add_point("yc", 1, value=42)
        self.addCleanup(server.stop)
        server.start()
        client = IEC104Master("127.0.0.1", port, init="NONE")
        client.add_point("yc", 1)
        self.connect_client(client)
        self.assertTrue(client.general_interrogation())
        # c104 schedules point and raw-frame callbacks independently of the GI
        # confirmation. Keep the link open while observing that one request;
        # immediately stopping the native client can cancel queued callbacks.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            logs = [self.content(name) for name in ("scdfe_c-rtu-10", "scdfe_t-channel-1")]
            if client.value("yc", 1).value == 42 and all(
                "[报文] 报文 RX" in log and "[报文] 报文 TX" in log for log in logs
            ):
                break
            time.sleep(.01)
        self.assertEqual(client.value("yc", 1).value, 42)
        client.close()
        server.stop()
        self.assertIn("HEX=68", self.assert_both_directions("scdfe_c-rtu-10"))
        self.assert_both_directions("scdfe_t-channel-1")

    def test_can_periodic_frames_are_logged_after_each_send(self):
        from client.can import CanMaster
        channel = "periodic-" + self.root.name
        receiver = CanMaster(interface="virtual", channel=channel, auto_start=False)
        self.addCleanup(receiver.close)
        receiver.connect()
        sender = self.connect_client(CanMaster(interface="virtual", channel=channel, auto_start=False))
        task = sender.send_periodic(0x123, b"\x01\x02", period=.02)
        try:
            for _ in range(2):
                self.assertEqual(receiver.recv(2).arbitration_id, 0x123)
            # Delivery precedes the sender's logging callback. python-can's
            # stop/close is not a callback join; observe logging while the
            # connection and log directory are still alive, as for IEC104.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self.content("scdfe_c-rtu-10").count("报文 TX") >= 2:
                    break
                time.sleep(.01)
            self.assertGreaterEqual(self.content("scdfe_c-rtu-10").count("报文 TX"), 2)
        finally:
            task.stop()
            sender.close()

    def test_modbus_rtu_serial_transport_logs_crc_bytes(self):
        from client.modbus_rtu import ModbusRtuMaster
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)
            port = listener.getsockname()[1]
            request = []
            response = bytes.fromhex("01 03 02 00 2a 39 9b")
            def slave():
                with listener.accept()[0] as connection:
                    connection.settimeout(5)
                    data = b""
                    while len(data) < 8:
                        part = connection.recv(8 - len(data))
                        if not part:
                            return
                        data += part
                    request.append(data)
                    connection.sendall(response)
                    time.sleep(.1)
            thread = threading.Thread(target=slave, daemon=True)
            thread.start()
            client = self.connect_client(ModbusRtuMaster(f"socket://127.0.0.1:{port}", timeout=2))
            self.assertEqual(client.read_holding_registers(0, 1), [42])
            client.close()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        content = self.assert_both_directions("scdfe_c-rtu-10")
        self.assertIn("HEX=" + request[0].hex(" "), content)
        self.assertIn("HEX=" + response.hex(" "), content)


if __name__ == "__main__":
    unittest.main()

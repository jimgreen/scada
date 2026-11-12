"""Regression coverage for the protocol services' actual data/control paths."""
from datetime import datetime, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pyModbusTCP.server import ModbusServer

from client.can import CanFrame, CanValue
from client.modbus_tcp import ModbusValue
from client.modbus_tcp import ModbusTcpMaster
from server.modbus_server import ModbusTcpServer
from scdfe_c.web.service import ScadaStore
from scdfe_s.web.protocol_service import (
    CanService, CanServerService, ModbusTcpService, ModbusTcpServerService,
    ModbusRtuService, ModbusRtuServerService, ProtocolServiceError,
)


class ReviewProtocolServicesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ScadaStore(Path(self.tmp.name) / "review.sqlite3")

    def modbus(self, protocol="modbus-tcp", *, port=1503, unit=5):
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 7, "channel_id": 70, "channel_type": protocol,
        })
        if protocol == "modbus-tcp":
            parameter = {"remote_host": "127.0.0.1", "remote_port": port}
        else:
            parameter = {"serial_port": "loop://", "parity": "N"}
        self.store.create_row("fes_channel_" + protocol.replace("-", "_") + "_define", {
            "rtu_id": 7, "unit_id": unit, **parameter,
        })
        self.store.create_row("fes_channel_modbus_define_call", {
            "id": 1, "rtu_id": 7, "call_seq": 0, "irtuaddr": unit,
            "iplace": 10, "inum": 2, "icode": 3,
        })
        self.store.create_row("fes_channel_modbus_define_data", {
            "id": 1, "rtu_id": 7, "call_seq": 0, "cnt": 1,
            "reg_addr": 10, "trans_type": 5, "fourbytedata_byteseq": 3,
        })
        for kind in ("yc", "yk", "yt"):
            self.store.create_row(f"fes_{kind}_define", {
                "rtu_id": 7, "pnt_no": 1, "name": kind, "value": 0,
            })
        self.store.create_row("fes_channel_modbus_define_ytyk", {
            "id": 1, "rtu_id": 7, "ifyk": 1, "cnt": 1, "reg_addr": 20,
            "icode": 5, "ytyk_type": 1, "irtuaddr": unit,
        })
        self.store.create_row("fes_channel_modbus_define_ytyk", {
            "id": 2, "rtu_id": 7, "ifyk": 0, "cnt": 1, "reg_addr": 30,
            "icode": 16, "ytyk_type": 5, "fourbytedata_byteseq": 3, "irtuaddr": unit,
        })

    def test_tcp_store_maps_poll_display_and_mapped_commands_real_loopback(self):
        slave = ModbusServer(host="127.0.0.1", port=0, no_block=True)
        slave.start()
        self.addCleanup(slave.stop)
        self.modbus(port=slave._service.server_address[1])
        slave.data_bank.set_holding_registers(10, [0x4148, 0])  # float32 12.5
        service = ModbusTcpService(self.store)
        self.addCleanup(service.close)
        service.start()
        self.assertEqual(len(service._engine._calls), 1)
        self.assertEqual(service.poll()["items"][0]["value"], 12.5)
        self.assertEqual(service.points("yc")[0]["value"], 12.5)
        self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["value"], 0)
        service.command("yk", 1, True)
        self.assertEqual(slave.data_bank.get_coils(20), [True])
        self.assertEqual(slave.data_bank.get_coils(1), [False])
        service.command("yt", 1, 12.5)
        self.assertEqual(slave.data_bank.get_holding_registers(30, 2), [0x4148, 0])
        self.assertEqual(service._engine.native.unit_id, 5)

    def test_rtu_loads_complete_maps_and_explicit_transport_override(self):
        self.modbus("modbus-rtu")
        service = ModbusRtuService(self.store)
        service.configure({"serial_port": "socket://127.0.0.1:23456", "unit_id": 1})
        master = service._build_engine()
        self.addCleanup(master.close)
        self.assertEqual(master.port, "socket://127.0.0.1:23456")
        self.assertEqual(master.unit_id, 1)
        self.assertEqual((len(master._calls), len(master._points), len(master._definitions)), (1, 1, 2))
        self.assertEqual(master._definitions[0]["reg_addr"], 20)

    def test_tcp_explicit_default_override_survives_reload(self):
        self.modbus(unit=5)
        service = ModbusTcpService(self.store)
        self.assertEqual(service.config()["unit_id"], 5)
        service.configure({"unit_id": 1, "remote_port": 1504})
        reloaded = ModbusTcpService(self.store)
        self.assertEqual(reloaded.config()["unit_id"], 1)
        master = reloaded._build_engine()
        self.addCleanup(master.close)
        self.assertEqual((master.port, master.unit_id), (1504, 1))

    def test_server_context_uses_effective_database_unit(self):
        self.modbus(unit=5)
        service = ModbusTcpServerService(self.store)
        engine = service._build_engine()
        self.addCleanup(engine.close)
        self.assertEqual(engine.unit_id, 5)
        self.assertEqual(engine.context.id, 5)  # Engineering maps use pymodbus SimDevice.
        engine.update_point("yc", 1, 12)

    def test_rtu_server_context_uses_effective_database_unit(self):
        self.modbus("modbus-rtu", unit=5)
        service = ModbusRtuServerService(self.store)
        service.configure({"simulation": 0})
        engine = service._build_engine()
        self.addCleanup(engine.close)
        self.assertEqual(engine.unit_id, 5)
        self.assertEqual(engine.context.id, 5)
        engine.update_point("yc", 1, 12)

    def test_rtu_simulation_poll_preserves_snapshot(self):
        service = ModbusRtuServerService(self.store)
        self.addCleanup(service.close)
        service.start()
        self.assertEqual(service.poll()["count"], 4)

    def test_boolean_strings_are_parsed_not_truth_tested(self):
        can = CanService(self.store)
        can.configure({"fd": "0", "receive_own_messages": "false", "is_extended_id": "0"})
        self.assertEqual([can.config()[k] for k in ("fd", "receive_own_messages", "is_extended_id")], [0, 0, 0])
        tcp = ModbusTcpService(self.store)
        tcp.configure({"keep_alive": "0"})
        self.assertEqual(tcp.config()["keep_alive"], 0)
        with self.assertRaises(ProtocolServiceError):
            can.configure({"fd": "garbage"})

    def test_can_mapped_values_and_raw_frames_are_json_safe(self):
        service = CanService(self.store)
        self.addCleanup(service.close)
        frame = CanFrame(0x123, b"\x01\x02")
        service._engine = Mock(connected=True, collect_once=Mock(return_value=[
            frame, CanValue(7, 1, "yc", 42, 0, frame, 123.0),
        ]))
        result = service.poll()
        encoded = json.dumps(result, allow_nan=False)
        self.assertIn("01 02", encoded)
        self.assertEqual(result["items"][1]["frame"]["data"], "01 02")

    def test_can_store_maps_and_server_parameter_merge(self):
        self.store.create_row("fes_rtu_define", {"rtu_id": 7, "channel_id": 70, "channel_type": "can"})
        self.store.create_row("fes_channel_can_define", {"rtu_id": 7, "interface": "virtual", "channel": "db-can-channel"})
        self.store.create_row("fes_channel_can_point_define", {
            "rtu_id": 7, "kind": "yc", "pnt_no": 1, "frame_id": 0x123,
            "bit_length": 16, "value_type": "uint", "byte_order": "big",
        })
        service = CanService(self.store)
        master = service._build_engine()
        self.addCleanup(master.close)
        self.assertEqual(len(master._points), 1)
        server = CanServerService(self.store)
        server.configure({"rtu_id": 7})
        engine = server._build_engine()
        self.addCleanup(engine.close)
        self.assertEqual(engine.channel, "db-can-channel")

    def test_incremental_master_command_dispatches_and_server_updates_all_kinds(self):
        self.modbus()
        service = ModbusTcpService(self.store)
        self.addCleanup(service.close)
        service._engine = Mock(connected=True, send_yk_point=Mock(return_value=True))
        service.incremental_update("yk", 1, True)
        service._engine.send_yk_point.assert_called_once_with(1, True)
        server = ModbusTcpServerService(self.store)
        self.addCleanup(server.close)
        server._engine = Mock(connected=True)
        server.incremental_update("yt", 1, 12.5)
        server._engine.update_point.assert_called_once_with("yt", 1, 12.5)

    def test_status_surfaces_async_transport_failure(self):
        service = CanServerService(self.store)
        self.addCleanup(service.close)
        service._engine = Mock(connected=False, connections=0, error=RuntimeError("send failed"))
        self.assertEqual(service.status()["last_error"], "send failed")

    def test_reconfigure_clears_last_polled_rtu_values(self):
        self.modbus()
        service = ModbusTcpService(self.store)
        self.addCleanup(service.close)
        service._engine = Mock(connected=True, collect_once=Mock(return_value=[
            ModbusValue(7, 0, 1, "yc", 42, datetime.now(timezone.utc)),
        ]))
        service.poll()
        self.assertEqual(service.points("yc")[0]["value"], 42)
        service.stop()
        service.configure({"rtu_id": 7})
        self.assertEqual(service.points("yc")[0]["value"], 0)

    def test_live_server_updates_zero_based_registers_and_packed_bits(self):
        server = ModbusTcpServer(host="127.0.0.1", port=0, unit_id=5)
        server.start()
        self.addCleanup(server.close)
        port = server.server.transport.sockets[0].getsockname()[1]
        master = ModbusTcpMaster("127.0.0.1", port, unit_id=5)
        master.connect()
        self.addCleanup(master.close)
        server.update_point("yc", 1, 12)
        server.update_point("yc", 2, 31)
        self.assertEqual(master.read_holding_registers(0, 3), [12, 31, 0])
        for kind, read in (("yx", master.read_discrete_inputs), ("yk", master.read_coils)):
            server.update_point(kind, 1, True)
            server.update_point(kind, 2, True)
            server.update_point(kind, 18, True)
            server.update_point(kind, 1, False)
            self.assertEqual(read(0, 3), [False, True, False])
            self.assertEqual(read(16, 3), [False, True, False])

    def test_server_poll_observes_remote_writes_to_configured_points(self):
        self.modbus()
        service = ModbusTcpServerService(self.store)
        # Bind the transport directly to an ephemeral port, keeping the Web
        # service's polling and point projection paths intact.
        service._engine = ModbusTcpServer(host="127.0.0.1", port=0, unit_id=5,
                                          points=service.points())
        service._engine.start()
        self.addCleanup(service.close)
        port = service._engine.server.transport.sockets[0].getsockname()[1]
        master = ModbusTcpMaster("127.0.0.1", port, unit_id=5)
        master.connect()
        self.addCleanup(master.close)
        master.write_single_register(0, 42)
        master.write_single_coil(0, True)
        result = service.poll()
        self.assertEqual(result["count"], 3)
        self.assertEqual(service.points("yc")[0]["value"], 42)
        self.assertTrue(service.points("yk")[0]["value"])
        self.assertEqual(self.store.get_row("fes_yc_define", [7, 1])["value"], 0)

    def test_point_projection_keeps_same_rtu_and_cache_snapshot(self):
        self.modbus()
        service = ModbusTcpService(self.store)
        service._latest_values[("yc", 1)] = {"value": 17}
        connection = self.store.connection
        @contextmanager
        def reconfigured_during_query():
            # A concurrent RTU switch and poll must not attach the new RTU's
            # cached value to rows selected for the previous RTU.
            service._latest_values = {("yc", 1): {"value": 99}}
            with connection() as con:
                yield con
        with patch.object(service, "config", return_value={"effective_rtu_id": 7}), \
                patch.object(self.store, "connection", reconfigured_during_query):
            self.assertEqual(service.points("yc")[0]["value"], 17)
        service.update_point("yc", 1, {"value": 5})
        self.assertEqual(service.points("yc")[0]["value"], 5)

    def test_can_operations_use_database_effective_defaults(self):
        self.store.create_row("fes_rtu_define", {"rtu_id": 7, "channel_id": 70, "channel_type": "can"})
        self.store.create_row("fes_channel_can_define", {
            "rtu_id": 7, "interface": "virtual", "channel": "effective-can",
            "is_extended_id": 1, "receive_timeout": 0.012,
        })
        service = CanService(self.store)
        self.addCleanup(service.close)
        service._engine = Mock(connected=True, collect_once=Mock(return_value=[]))
        service.send_frame(0x12345, "01", extended=None)
        service._engine.send.assert_called_once_with(0x12345, b"\x01", is_extended_id=True)
        service.poll()
        service._engine.collect_once.assert_called_once_with(timeout=0.012)


if __name__ == "__main__":
    unittest.main()

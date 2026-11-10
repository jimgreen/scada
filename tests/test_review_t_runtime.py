"""Transmission regressions for the November interaction review.

Every database and listener is private to the test. Protocol assertions use
real Modbus requests so callbacks, codecs and datastore permissions are tested
together rather than merely inspecting implementation dictionaries.
"""
from pathlib import Path
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from pymodbus.client import ModbusTcpClient
from client.iec104 import IEC104Master

from scdfe_c.web.service import ScadaStore
from core.scdfe_control import InstanceLock
from scdfe_t.trans import TransPoint, TransRuntime, main
from server.modbus_trans_server import ModbusTcpTransServer


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RecordingWriter:
    def __init__(self):
        self.calls = []

    def submit_command(self, *args, **kwargs):
        self.calls.append(args)
        return {"ok": True}


class TransmissionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "trans.sqlite3"
        self.store = ScadaStore(self.db)
        self.port = free_port()
        self.store.create_row("fes_scdfe_t_define", {
            "channel_id": 1, "name": "Review", "protocol": "modbus-tcp",
            "bind_host": "127.0.0.1", "bind_port": self.port, "unit_id": 1,
            "scan_interval_ms": 50,
        })
        for rtu in (101, 202):
            self.store.create_row("fes_rtu_define", {
                "rtu_id": rtu, "channel_id": rtu, "channel_type": "modbus-tcp",
                "name": f"RTU {rtu}",
            })
        self.runtime = TransRuntime(self.db, channel_id=1)
        self.writer = RecordingWriter()
        self.runtime.writer = self.writer

    def point(self, kind, no, *, rtu=101, source=1, value=0, **mapping):
        try:
            self.store.get_row(f"fes_{kind}_define", [rtu, source])
        except Exception:
            self.store.create_row(f"fes_{kind}_define", {
                "rtu_id": rtu, "pnt_no": source, "name": kind, "value": value,
            })
        return self.store.create_row("fes_scdfe_t_point_define", {
            "channel_id": 1, "kind": kind, "pnt_no": no,
            "source_rtu_id": rtu, "source_pnt_no": source, "enabled": 1,
            **mapping,
        })

    def connect(self):
        spec, points, sig = self.runtime.load()
        self.runtime.rebuild(spec, points, sig)
        self.addCleanup(self.runtime.server.close)
        self.runtime.refresh()
        client = ModbusTcpClient("127.0.0.1", port=self.port, timeout=2, retries=0)
        self.assertTrue(client.connect())
        self.addCleanup(client.close)
        return client

    def test_heartbeat_does_not_restart_transport_but_config_change_does(self):
        server = Mock()
        self.runtime.definition_check_interval = .001
        with patch.object(self.runtime, "build_server", return_value=server) as build:
            self.assertEqual(self.runtime.run(max_cycles=5), 5)
            self.assertEqual(build.call_count, 1)
            self.assertEqual(server.start.call_count, 1)
        old = self.runtime.load()[2]
        self.store.update_row("fes_scdfe_t_define", [1], {"bind_port": free_port()})
        self.assertNotEqual(self.runtime.load()[2], old)

    def test_same_source_number_in_two_rtus_keeps_commands_separate(self):
        self.point("yk", 11, rtu=101, register_area="coil", address=0, data_type="bool")
        self.point("yk", 12, rtu=202, register_area="coil", address=1, data_type="bool")
        client = self.connect()
        self.assertFalse(client.write_coil(0, True, device_id=1).isError())
        self.assertFalse(client.write_coil(1, False, device_id=1).isError())
        self.assertEqual(self.writer.calls, [(101, "yk", 1, 1), (202, "yk", 1, 0)])

    def test_iec104_updates_without_clients_are_available_on_later_interrogation(self):
        self.store.update_row("fes_scdfe_t_define", [1], {"protocol": "iec104"})
        self.point("yc", 1, value=12, type_id=13, io_address=0x4001)
        spec, points, signature = self.runtime.load()
        self.runtime.rebuild(spec, points, signature)
        self.addCleanup(self.runtime.server.stop)
        with patch.object(self.runtime.logger, "error") as errors:
            self.runtime.refresh()
            self.store.update_row("fes_yc_define", [101, 1], {"value": 42})
            self.runtime.refresh()
            errors.assert_not_called()
        client = IEC104Master("127.0.0.1", self.port, init="MUTED")
        self.addCleanup(client.close)
        client.add_point("yc", 1, data_type=13)
        client.connect(timeout=3)
        self.assertTrue(client.connection.unmute())
        deadline = time.monotonic() + 3
        while client.connection.is_muted and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(client.connection.is_muted)
        self.assertTrue(client.general_interrogation())
        self.assertEqual(client.value("yc", 1).value, 42)

    def test_modbus_command_conversion_is_applied_once(self):
        self.point("yt", 1, value=24, register_area="holding", address=10,
                   data_type="int16", scale=2, offset=10)
        client = self.connect()
        self.assertEqual(client.read_holding_registers(10, count=1, device_id=1).registers, [7])
        self.assertFalse(client.write_register(10, 8, device_id=1).isError())
        self.assertEqual(self.writer.calls, [(101, "yt", 1, 26.0)])

    def test_unsigned_wide_values_are_exposed_on_the_wire(self):
        self.point("yc", 1, source=1, value=4000000001, register_area="input",
                   address=0, data_type="uint32")
        self.point("yc", 2, source=2, value=2**48 + 7, register_area="input",
                   address=10, data_type="uint64")
        client = self.connect()
        for address, size, value in ((0, 2, 4000000001), (10, 4, 2**48 + 7)):
            response = client.read_input_registers(address, count=size, device_id=1)
            self.assertFalse(response.isError())
            encoded = b"".join(word.to_bytes(2, "big") for word in response.registers)
            self.assertEqual(int.from_bytes(encoded, "big"), value)

    def test_uint64_codec_preserves_all_bits_for_integer_values(self):
        # Exercise the protocol adapter directly because engineering values in
        # the SCADA YC/YT tables are intentionally stored as SQLite REAL.
        received = []
        server = ModbusTcpTransServer(host="127.0.0.1", port=self.port, points=[{
            "kind": "yt", "pnt_no": 1, "source_pnt_no": 1,
            "register_area": "holding", "address": 0, "data_type": "uint64",
        }], point_values={("yt", 1): 2**64 - 1},
            command_handler=lambda kind, no, value: received.append(value) or True)
        server.start()
        self.addCleanup(server.close)
        client = ModbusTcpClient("127.0.0.1", port=self.port, timeout=2, retries=0)
        self.assertTrue(client.connect())
        self.addCleanup(client.close)
        self.assertEqual(client.read_holding_registers(0, count=4, device_id=1).registers, [65535] * 4)
        for value in (2**53 + 1, 2**64 - 1):
            data = value.to_bytes(8, "big")
            words = [int.from_bytes(data[i:i + 2], "big") for i in range(0, 8, 2)]
            self.assertFalse(client.write_registers(0, words, device_id=1).isError())
            self.assertEqual(received[-1], value)

    def test_measurement_mappings_reject_remote_writes_and_still_refresh(self):
        self.point("yc", 1, value=12, register_area="holding", address=20, data_type="int16")
        self.point("yx", 1, value=0, register_area="coil", address=2, data_type="bool")
        # A command sharing the same packed Modbus coil word must remain writable.
        self.point("yk", 1, register_area="coil", address=0, data_type="bool")
        client = self.connect()
        self.assertTrue(client.write_register(20, 99, device_id=1).isError())
        self.assertTrue(client.write_coil(2, True, device_id=1).isError())
        self.assertFalse(client.write_coil(0, True, device_id=1).isError())
        self.assertEqual(self.writer.calls, [(101, "yk", 1, 1)])
        self.runtime.refresh()
        self.assertEqual(client.read_holding_registers(20, count=1, device_id=1).registers, [12])
        self.assertFalse(client.read_coils(2, count=1, device_id=1).bits[0])
        self.store.update_row("fes_yc_define", [101, 1], {"value": 18})
        self.store.update_row("fes_yx_define", [101, 1], {"value": 1})
        self.runtime.refresh()
        self.assertEqual(client.read_holding_registers(20, count=1, device_id=1).registers, [18])
        self.assertTrue(client.read_coils(2, count=1, device_id=1).bits[0])

    def test_forward_point_persists_live_value_quality_and_forward_time(self):
        self.point("yc", 1, value=12.5, register_area="input", address=20, data_type="float32")
        client = self.connect()
        row = self.store.get_row("fes_scdfe_t_point_define", [1, 1, "yc"])
        self.assertEqual((row["value"], row["status"]), (12.5, 0))
        self.assertGreater(row["refresh_time"], 0)
        self.assertEqual(row["forward_time"], 0)
        version = row["_version"]

        self.runtime.refresh()
        unchanged = self.store.get_row("fes_scdfe_t_point_define", [1, 1, "yc"])
        self.assertEqual(unchanged["refresh_time"], row["refresh_time"])
        self.assertEqual(unchanged["_version"], version)

        self.store.update_row("fes_yc_define", [101, 1], {
            "value": 18.5, "status": 4, "refresh_time": 200,
        })
        self.runtime.refresh()
        changed = self.store.get_row("fes_scdfe_t_point_define", [1, 1, "yc"])
        self.assertEqual((changed["value"], changed["status"], changed["refresh_time"]), (18.5, 4, 200))
        self.assertGreater(changed["forward_time"], 0)
        self.assertEqual(changed["_version"], version)
        self.assertEqual(client.read_input_registers(20, count=2, device_id=1).registers[0], 16788)

    def test_forward_command_records_receive_time_without_changing_definition_version(self):
        point = self.point("yk", 2, value=0, register_area="coil", address=2, data_type="bool")
        self.connect()
        version = self.store.get_row("fes_scdfe_t_point_define", [1, 2, "yk"])["_version"]
        self.assertTrue(self.runtime.command(self.runtime.points["yk", 2], 1))
        row = self.store.get_row("fes_scdfe_t_point_define", [1, 2, "yk"])
        self.assertEqual(row["value"], 1.0)
        self.assertEqual(row["status"], 0)
        self.assertGreater(row["refresh_time"], 0)
        self.assertGreater(row["command_receive_time"], 0)
        self.assertEqual(row["refresh_time"], row["command_receive_time"])
        self.assertEqual(row["_version"], version)

    def test_mixed_coil_write_cannot_bypass_readonly_measurement(self):
        self.point("yk", 1, register_area="coil", address=0, data_type="bool")
        self.point("yx", 1, value=0, register_area="coil", address=1, data_type="bool")
        client = self.connect()
        self.assertTrue(client.write_coils(0, [True, True], device_id=1).isError())
        self.assertEqual(self.writer.calls, [])
        self.assertEqual(client.read_coils(0, count=2, device_id=1).bits[:2], [False, False])

    def test_managed_runtime_stops_when_web_clears_enable(self):
        self.store.update_row("fes_scdfe_t_define", [1], {"process_enable": 1})
        self.runtime.require_enabled = True
        server = Mock()
        started = threading.Event()
        server.start.side_effect = started.set
        with patch.object(self.runtime, "build_server", return_value=server):
            thread = threading.Thread(target=self.runtime.run)
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                self.store.update_row("fes_scdfe_t_define", [1], {"process_enable": 0})
                thread.join(2)
                self.assertFalse(thread.is_alive())
            finally:
                self.runtime.stop_event.set()
                thread.join(3)
        row = self.store.get_row("fes_scdfe_t_define", [1])
        self.assertEqual((row["process_enable"], row["process_status"], row["process_pid"]), (0, 0, 0))
        server.stop.assert_called_once()

    def test_managed_stop_is_prompt_even_with_a_long_data_scan(self):
        self.store.update_row("fes_scdfe_t_define", [1], {
            "process_enable": 1, "scan_interval_ms": 60000,
        })
        self.runtime.require_enabled = True
        server = Mock()
        scanned = threading.Event()
        with patch.object(self.runtime, "build_server", return_value=server), \
                patch.object(self.runtime, "refresh", side_effect=scanned.set) as refresh:
            thread = threading.Thread(target=self.runtime.run)
            thread.start()
            try:
                self.assertTrue(scanned.wait(2))
                start = int(time.time()*1000)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    row = self.store.get_row("fes_scdfe_t_define", [1])
                    if row["process_status"] == 2 and row["process_heartbeat_time"] >= start+900:
                        break
                    time.sleep(.05)
                else:
                    self.fail("A long scan must publish a live heartbeat before its next data refresh")
                self.store.update_row("fes_scdfe_t_define", [1], {"process_enable": 0})
                thread.join(1)
                self.assertFalse(thread.is_alive(), "Web stop must not wait for the 60-second data scan")
                self.assertEqual(refresh.call_count, 1)
            finally:
                self.runtime.stop_event.set()
                thread.join(3)

    def test_large_source_scan_fans_out_values_and_obeys_sql_variable_limit(self):
        runtime = TransRuntime(self.db, channel_id=1, require_enabled=True)
        self.addCleanup(runtime.store.close)
        with runtime.store.connection() as con:
            con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,999)
            con.execute('BEGIN')
            con.executemany('INSERT INTO fes_yc_define(rtu_id,pnt_no,value,status) VALUES(101,?,?,?)',
                            [(n,n+.25,128 if n==901 else 0) for n in range(1,1002)])
            con.commit()
        def point(target, source):
            return TransPoint.from_row(dict(channel_id=1,pnt_no=target,kind='yc',source_rtu_id=101,
                                             source_pnt_no=source))
        points = [point(n,n) for n in range(1,1002)] + [point(2000,901),point(2001,1900)]
        values = runtime.source_values(points, include_quality=True)
        self.assertEqual(len(values),1002)
        self.assertEqual(values['yc',2000], (901.25,128))
        self.assertEqual(values['yc',901],values['yc',2000])
        self.assertEqual(values['yc',1001],(1001.25,0))
        self.assertNotIn(('yc',2001),values)

    def test_iec104_forwards_quality_only_changes_without_inventing_new_values(self):
        self.store.update_row("fes_scdfe_t_define", [1], {"protocol": "iec104"})
        self.point("yx", 1, value=1, type_id=3, io_address=1)
        # The collector retains the last binary value when a double-point
        # sample becomes invalid. Its engineering status carries that fact.
        self.store.update_row("fes_yx_define", [101, 1], {"status": 128})
        spec, points, sig = self.runtime.load()
        self.runtime.rebuild(spec, points, sig)
        self.addCleanup(self.runtime.server.stop)
        client = IEC104Master("127.0.0.1", self.port, init="NONE")
        self.addCleanup(client.close)
        client.add_point("yx", 1, data_type=2)
        client.connect(timeout=4)
        self.assertTrue(client.general_interrogation())

        def assert_sample(expected_quality):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                sample = client.value("yx", 1)
                if sample.value is True and sample.quality == expected_quality:
                    return
                time.sleep(.01)
            self.assertEqual((sample.value, sample.quality), (True, expected_quality))

        assert_sample(128)
        self.runtime.refresh()
        for quality in (0, 128, 0):
            self.store.update_row("fes_yx_define", [101, 1], {"status": quality})
            self.runtime.refresh()
            assert_sample(quality)

    def test_managed_cli_cancelled_before_start_cannot_resurrect_channel(self):
        before = self.store.get_row("fes_scdfe_t_define", [1])
        control = InstanceLock(self.db, role="trans-control-1")
        self.assertTrue(control.acquire())
        try:
            with patch.object(TransRuntime, "build_server") as build:
                self.assertEqual(main(["--db", str(self.db), "--channel-id", "1", "--managed-start"]), 0)
                build.assert_not_called()
        finally:
            control.release()
        self.assertEqual(self.store.get_row("fes_scdfe_t_define", [1]), before)

    def test_source_loss_and_recovery_are_visible_on_one_iec104_connection(self):
        self.store.update_row("fes_scdfe_t_define", [1], {"protocol": "iec104"})
        self.point("yc", 1, value=42, type_id=13, io_address=0x4001)
        spec, pts, sig = self.runtime.load()
        self.runtime.rebuild(spec, pts, sig)
        self.addCleanup(self.runtime.server.stop)
        client = IEC104Master("127.0.0.1", self.port, init="NONE")
        self.addCleanup(client.close)
        client.add_point("yc", 1, data_type=13)
        client.connect(timeout=4)

        def sample(value, quality):
            self.runtime.refresh()
            self.assertTrue(client.general_interrogation())
            observed = client.value("yc", 1)
            self.assertEqual((observed.value, observed.quality), (value, quality))

        sample(42, 0)
        self.store.delete_row("fes_yc_define", [101, 1])
        sample(42, 128)
        self.store.create_row("fes_yc_define", {"rtu_id":101,"pnt_no":1,"value":42})
        sample(42, 0)
        self.store.update_row("fes_yc_define", [101, 1], {"value":None})
        sample(42, 128)
        self.store.update_row("fes_yc_define", [101, 1], {"value":51})
        sample(51, 0)

    def test_display_edits_and_scan_period_do_not_change_transport_identity(self):
        self.point("yc", 1, value=42)
        initial = self.runtime.load()[2]
        self.store.update_row("fes_scdfe_t_define", [1], {"name":"新标签","scan_interval_ms":500})
        self.store.update_row("fes_scdfe_t_point_define", [1,1,"yc"], {"name":"新点名称"})
        spec, pts, current = self.runtime.load()
        self.assertEqual(spec.scan_interval, .5)
        self.assertEqual(initial,current)
        self.store.update_row("fes_scdfe_t_point_define", [1,1,"yc"], {"address":7})
        self.assertNotEqual(self.runtime.load()[2], current)

    def test_periodic_full_reports_begin_on_existing_iec_connection_after_period_edit(self):
        self.store.update_row("fes_scdfe_t_define",[1],{"protocol":"iec104","process_enable":1,"report_interval_ms":60000})
        self.point("yc",1,value=42,type_id=13,io_address=0x4001)
        self.point("yx",1,value=1,type_id=1,io_address=1)
        self.runtime.definition_check_interval=.05
        self.runtime.require_enabled=True
        thread=threading.Thread(target=self.runtime.run)
        thread.start()
        self.addCleanup(lambda:(self.runtime.stop_event.set(),thread.join(4)))
        deadline=time.monotonic()+4
        while self.store.get_row("fes_scdfe_t_define",[1])["process_status"]!=2:
            self.assertLess(time.monotonic(),deadline)
            time.sleep(.02)
        client=IEC104Master("127.0.0.1",self.port,init="NONE")
        self.addCleanup(client.close)
        client.add_point("yc",1,data_type=13)
        client.add_point("yx",1,data_type=1)
        arrivals={1:[],13:[]}
        def packet(direction,data,detail):
            if direction=='RX' and len(data)>=12 and data[6] in arrivals and data[8]&63==1:
                arrivals[data[6]].append(time.monotonic())
        client.set_packet_handler(packet)
        client.connect(timeout=4)
        self.assertTrue(client.general_interrogation())
        server=self.runtime.server
        self.store.update_row("fes_scdfe_t_define",[1],{"report_interval_ms":175})
        deadline=time.monotonic()+4
        while min(map(len,arrivals.values()))<2:
            self.assertLess(time.monotonic(),deadline,arrivals)
            time.sleep(.02)
        self.assertEqual(client.value("yc",1).value,42)
        self.assertIs(self.runtime.server,server)
        self.assertTrue(client.connected)

    def test_retention_only_removes_expired_hourly_files_of_this_channel(self):
        root=self.runtime.logger.directory
        folder=root/"2026/01/01/00"
        folder.mkdir(parents=True,exist_ok=True)
        paths=[folder/name for name in ("scdfe_t-channel-1-20260101-00.log","scdfe_t-channel-10-20260101-00.log",
                                       "scdfe_t-channel-1-not-hourly.log","scdfe_s-iec104-20260101-00.log",
                                       "scdfe_t-channel-1-20260101-01.log")]
        for path in paths:
            path.write_text('data',encoding='utf-8')
        for path in paths[:-1]:
            os.utime(path,(time.time()-7200,)*2)
        self.store.update_row("fes_scdfe_t_define",[1],{"log_retention_hours":1})
        self.runtime.run(max_cycles=1)
        self.assertEqual([p.exists() for p in paths],[False, True, True, True, True])

    def test_operator_edits_keep_a_live_modbus_peer_connected(self):
        self.point("yc", 1, value=42, register_area="input", address=0, data_type="uint16")
        self.runtime.definition_check_interval = .01
        self.runtime.require_enabled = True
        self.store.update_row("fes_scdfe_t_define", [1], {"process_enable":1})
        worker = threading.Thread(target=self.runtime.run)
        worker.start()
        self.addCleanup(lambda: (self.runtime.stop_event.set(),worker.join(4)))
        deadline = time.monotonic() + 4
        while self.store.get_row("fes_scdfe_t_define", [1])["process_status"] != 2:
            self.assertLess(time.monotonic(),deadline)
            time.sleep(.02)
        client = ModbusTcpClient("127.0.0.1",port=self.port,timeout=2,retries=0)
        self.addCleanup(client.close)
        self.assertTrue(client.connect())
        self.assertEqual(client.read_input_registers(0,count=1,device_id=1).registers,[42])
        peer, server = client.socket, self.runtime.server
        self.store.update_row("fes_scdfe_t_define", [1], {"scan_interval_ms":75, "name":"display only"})
        self.store.update_row("fes_scdfe_t_point_define", [1,1,"yc"], {"name":"display only"})
        self.store.update_row("fes_yc_define", [101,1], {"value":73})
        deadline=time.monotonic()+3
        while client.read_input_registers(0,count=1,device_id=1).registers != [73]:
            self.assertLess(time.monotonic(),deadline)
            time.sleep(.02)
        self.assertIs(client.socket,peer)
        self.assertIs(self.runtime.server,server)

    def test_explicit_cli_start_enables_channel_and_runs(self):
        server = Mock()
        with patch.object(TransRuntime, "build_server", return_value=server):
            self.assertEqual(main(["--db", str(self.db), "--channel-id", "1", "--max-cycles", "1"]), 0)
        server.start.assert_called_once()
        server.stop.assert_called_once()
        row = self.store.get_row("fes_scdfe_t_define", [1])
        self.assertEqual((row["process_enable"], row["process_status"]), (1, 0))


if __name__ == "__main__":
    unittest.main()

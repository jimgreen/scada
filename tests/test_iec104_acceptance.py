"""Acceptance coverage for the IEC104 transport and database contract."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import socket
import tempfile
import time
import unittest

import c104

from client.iec104 import IEC104Master
from core.iec104_protocol import IEC104ProtocolConfig, IEC104ProtocolError, parse_apdu
from scdfe_c.web.service import ApiError, ScadaStore
from scdfe_s.web.iec104_service import IEC104Service
from server.iec104_server import IEC104Server
from tools.migrate_iec104_protocol_parameters import migrate


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class IEC104AcceptanceTests(unittest.TestCase):
    def test_i_s_u_frames_sequences_and_invalid_lengths(self):
        i_frame = parse_apdu(bytes.fromhex("68 08 02 00 04 00 01 02 03 04"))
        self.assertEqual((i_frame.frame_type, i_frame.send_sequence,
                          i_frame.receive_sequence, i_frame.asdu_length),
                         ("I", 1, 2, 4))
        s_frame = parse_apdu(bytes.fromhex("68 04 01 00 02 00"))
        self.assertEqual((s_frame.frame_type, s_frame.receive_sequence), ("S", 1))
        for raw, function in (
            ("68 04 07 00 00 00", "STARTDT_ACT"),
            ("68 04 0b 00 00 00", "STARTDT_CON"),
            ("68 04 13 00 00 00", "STOPDT_ACT"),
            ("68 04 23 00 00 00", "STOPDT_CON"),
            ("68 04 43 00 00 00", "TESTFR_ACT"),
            ("68 04 83 00 00 00", "TESTFR_CON"),
        ):
            frame = parse_apdu(bytes.fromhex(raw))
            self.assertEqual((frame.frame_type, frame.u_function), ("U", function))
        with self.assertRaises(IEC104ProtocolError):
            parse_apdu(bytes.fromhex("68 05 01 00 00 00"))
        with self.assertRaises(IEC104ProtocolError):
            parse_apdu(bytes.fromhex("68 04 01 00 03 00"))
        with self.assertRaises(IEC104ProtocolError):
            parse_apdu(bytes.fromhex("68 08 02 00 04 00 01 02 03 04"), max_asdu_length=3)

    def test_transport_parameters_are_applied_and_validated(self):
        config = IEC104ProtocolConfig(k=16, w=8, t1=15.0, t2=10.0, t3=30.0,
                                      max_asdu_length=200)
        server = IEC104Server(ip="127.0.0.1", port=free_port(),
                              k=16, w=8, t1=15, t2=10, t3=30,
                              max_asdu_length=200)
        try:
            self.assertEqual(server.protocol_config, config.as_dict())
            parameters = server.protocol_parameters
            self.assertEqual((parameters.send_window_size,
                              parameters.receive_window_size,
                              parameters.message_timeout,
                              parameters.confirm_interval,
                              parameters.keep_alive_interval), (16, 8, 15, 10, 30))
        finally:
            server.stop()
        with self.assertRaises(IEC104ProtocolError):
            IEC104ProtocolConfig(k=8, w=9)
        with self.assertRaises(IEC104ProtocolError):
            IEC104ProtocolConfig(max_asdu_length=250)

    def test_schema_exposes_transport_and_time_tag_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ScadaStore(Path(directory) / "schema.sqlite3")
            try:
                channel = {c["eng_name"] for c in store.schema("fes_channel_iec104_define")["columns"]}
                point = {c["eng_name"] for c in store.schema("fes_channel_iec104_point_define")["columns"]}
                forwarding = {c["eng_name"] for c in store.schema("fes_scdfe_t_define")["columns"]}
                forwarding_point = {c["eng_name"] for c in store.schema("fes_scdfe_t_point_define")["columns"]}
                self.assertTrue({"k", "w", "t1", "t2", "t3", "max_asdu_length"} <= channel)
                self.assertIn("time_tag", point)
                self.assertTrue({"k", "w", "t1", "t2", "t3", "max_asdu_length"} <= forwarding)
                self.assertIn("time_tag", forwarding_point)
                store.create_row("fes_rtu_define", {
                    "rtu_id": 1, "channel_id": 1, "channel_type": "iec104",
                })
                store.create_row("fes_channel_iec104_define", {
                    "id": 1, "rtu_id": 1, "k": 16, "w": 8,
                    "t1": 15, "t2": 10, "t3": 30, "max_asdu_length": 200,
                })
                store.create_row("fes_channel_iec104_point_define", {
                    "rtu_id": 1, "kind": "yx", "pnt_no": 1,
                    "type_id": 5, "time_tag": "NONE",
                })
                store.create_row("fes_scdfe_t_define", {
                    "channel_id": 1, "code": "T1", "protocol": "iec104",
                    "k": 8, "w": 8, "t1": 15, "t2": 10, "t3": 20,
                    "max_asdu_length": 249,
                })
                store.create_row("fes_scdfe_t_point_define", {
                    "channel_id": 1, "pnt_no": 1, "kind": "yx",
                    "source_rtu_id": 1, "source_pnt_no": 1,
                    "io_address": 1, "type_id": 5,
                })
                with self.assertRaises(ApiError):
                    store.create_row("fes_channel_iec104_define", {
                        "id": 2, "rtu_id": 1, "k": 4, "w": 8,
                    })
                with self.assertRaises(ApiError):
                    store.create_row("fes_scdfe_t_define", {
                        "channel_id": 2, "code": "T2", "protocol": "iec104",
                        "k": 4, "w": 8,
                    })
            finally:
                store.close()

    def test_select_execute_cancel_and_cp56_timestamp_round_trip(self):
        port = free_port()
        events = []
        server = IEC104Server(ip="127.0.0.1", port=port)
        server.add_point(
            "yk", 1, data_type=1, value=False,
            command_mode="SELECT_AND_EXECUTE",
            command_handler=lambda event: events.append(event) or True,
        )
        server.add_point("yx", 1, data_type=1, time_tag="CP56Time2a", value=False)
        client = IEC104Master("127.0.0.1", port=port, init="NONE")
        client.configure([
            {"kind": "yk", "pnt_no": 1, "data_type": 1,
             "command_mode": "SELECT_AND_EXECUTE"},
            {"kind": "yx", "pnt_no": 1, "data_type": 1,
             "time_tag": "CP56Time2a"},
        ])
        server.start()
        client.connect(timeout=3)
        try:
            self.assertIs(client.send_yk_point(1, True, select_execute=True).value, True)
            self.assertEqual(events[-1].cause, c104.Cot.ACTIVATION)
            self.assertIs(client.cancel_yk_point(1, True).value, True)
            self.assertEqual(events[-1].cause, c104.Cot.DEACTIVATION)
            server.update("yx", 1, True)
            deadline = time.monotonic() + 2
            while client.value("yx", 1).value is not True and time.monotonic() < deadline:
                time.sleep(0.01)
            snapshot = client.value("yx", 1)
            self.assertIs(snapshot.value, True)
            self.assertIsNotNone(snapshot.recorded_at)
        finally:
            client.close()
            server.stop()
            time.sleep(0.2)

    def test_step_position_round_trip(self):
        port = free_port()
        server = IEC104Server(ip="127.0.0.1", port=port)
        server.add_point("yx", 1, data_type=5, value=-3)
        client = IEC104Master("127.0.0.1", port=port, init="NONE")
        client.add_point("yx", 1, data_type=5)
        server.start()
        client.connect(timeout=3)
        try:
            self.assertTrue(client.general_interrogation())
            deadline = time.monotonic() + 2
            while client.value("yx", 1).value != -3 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(client.value("yx", 1).value, -3)
            server.update("yx", 1, 12)
            deadline = time.monotonic() + 2
            while client.value("yx", 1).value != 12 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(client.value("yx", 1).value, 12)
        finally:
            client.close()
            server.stop()
            time.sleep(0.2)

    def test_multiple_clients_receive_continuous_burst(self):
        port = free_port()
        server = IEC104Server(ip="127.0.0.1", port=port, max_connections=4,
                              tick_rate_ms=50, k=32, w=16)
        server.add_point("yc", 1, data_type=13, value=0.0)
        clients = [IEC104Master("127.0.0.1", port=port, init="NONE") for _ in range(4)]
        for client in clients:
            client.add_point("yc", 1, data_type=13)
        server.start()
        try:
            for client in clients:
                client.connect(timeout=3)
            for value in range(25):
                server.update("yc", 1, float(value))
            deadline = time.monotonic() + 3
            while (any(client.value("yc", 1).value != 24.0 for client in clients)
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            self.assertTrue(all(client.value("yc", 1).value == 24.0 for client in clients))
            self.assertEqual(server.open_connection_count, 4)
        finally:
            for client in clients:
                client.close()
            server.stop()
            time.sleep(0.3)

    def test_existing_database_migration_adds_new_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_schema = root / "old-schema"
            shutil.copytree(Path("dbdef/json"), old_schema)
            for name, columns in {
                "fes_channel_iec104_define.json": {
                    "k", "w", "t1", "t2", "t3", "max_asdu_length",
                },
                "fes_scdfe_t_define.json": {
                    "k", "w", "t1", "t2", "t3", "max_asdu_length",
                },
                "fes_channel_iec104_point_define.json": {"time_tag"},
                "fes_scdfe_t_point_define.json": {"time_tag"},
            }.items():
                path = old_schema / name
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["columns"] = [c for c in payload["columns"] if c["eng_name"] not in columns]
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            db = root / "old.sqlite3"
            store = ScadaStore(db, schema_dir=old_schema)
            store.close()
            result = migrate(db, Path("dbdef/json"), apply=True)
            self.assertTrue(result["changed"])
            migrated = ScadaStore(db)
            try:
                with migrated.connection() as connection:
                    for table, expected in {
                        "fes_channel_iec104_define": {
                            "k": 12, "w": 8, "t1": 15.0, "t2": 10.0,
                            "t3": 20.0, "max_asdu_length": 249,
                        },
                        "fes_scdfe_t_define": {
                            "k": 12, "w": 8, "t1": 15.0, "t2": 10.0,
                            "t3": 20.0, "max_asdu_length": 249,
                        },
                    }.items():
                        info = list(connection.execute(f"PRAGMA table_info({table})"))
                        actual = {row[1] for row in info}
                        self.assertTrue(set(expected) <= actual)
                        defaults = {row[1]: row[4] for row in info}
                        self.assertEqual(
                            {name: defaults[name] for name in expected},
                            {"k": "12", "w": "8", "t1": "15.0", "t2": "10.0",
                             "t3": "20.0", "max_asdu_length": "249"},
                        )
                    for table in ("fes_channel_iec104_point_define", "fes_scdfe_t_point_define"):
                        info = list(connection.execute(f"PRAGMA table_info({table})"))
                        actual = {row[1] for row in info}
                        self.assertIn("time_tag", actual)
                        self.assertEqual({row[1]: row[4] for row in info}["time_tag"], "'NONE'")
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from client.can import CanMaster
from scdfe_c.web.service import ScadaStore
from tools.migrate_can_schema import migrate


class CanSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "scada.sqlite3"
        self.store = ScadaStore(self.db)
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 7, "channel_id": 70, "channel_type": "can",
        })
        self.addCleanup(self.store.close)
        self.addCleanup(self.temp.cleanup)

    def test_schema_has_transport_frame_protocol_and_diagnostic_boundaries(self):
        expected = {
            "fes_channel_can_define": {
                "sample_point", "data_sample_point", "bitrate_switch",
                "restart_on_error", "bus_off_recovery_ms", "receive_error_frames",
            },
            "fes_channel_can_point_define": {
                "dlc", "period_ms", "bitrate_switch", "enabled",
                "dbc_message_name", "dbc_signal_name", "unit", "is_signed",
            },
            "fes_channel_can_frame_define": {
                "arbitration_id", "is_extended_id", "is_fd", "bitrate_switch",
                "dlc", "period_ms", "schedule_mode", "replay_source",
                "inject_error_type",
            },
            "fes_channel_canopen_service_define": {
                "node_id", "service_type", "cob_id", "pdo_number",
                "object_index", "object_subindex", "heartbeat_ms", "emcy_error_code",
            },
            "fes_channel_can_j1939_define": {
                "pgn", "source_address", "destination_address", "message_type",
                "dm_number", "transport_protocol", "multi_packet",
            },
            "fes_channel_can_uds_define": {
                "request_id", "response_id", "session_type", "service_type",
                "did", "routine_id", "dtc_group",
            },
            "fes_channel_can_diagnostic": {
                "bus_state", "bus_load_pct", "avg_latency_ms", "dropped_frame_count",
                "ack_error_count", "crc_error_count", "stuff_error_count",
                "temperature_c", "emi_level", "termination_ohms", "waveform_ref",
            },
        }
        for table, fields in expected.items():
            actual = {item["eng_name"] for item in self.store.schema(table)["columns"]}
            self.assertTrue(fields <= actual, (table, fields - actual))

    def test_all_can_definitions_are_owned_by_channel_and_cascade(self):
        self.store.create_row("fes_channel_can_define", {
            "rtu_id": 7, "interface": "virtual", "channel": "schema-can",
            "bitrate": 500000, "data_bitrate": 2000000,
            "sample_point": 87.5, "data_sample_point": 80.0,
        })
        self.store.create_row("fes_channel_can_frame_define", {
            "id": 1, "rtu_id": 7, "arbitration_id": 0x123,
            "is_fd": 1, "dlc": 12, "data_hex": "00 01", "schedule_mode": "periodic",
            "period_ms": 100,
        })
        self.store.create_row("fes_channel_canopen_service_define", {
            "id": 1, "rtu_id": 7, "node_id": 3, "service_type": "SDO",
            "object_index": 0x2000, "object_subindex": 1,
        })
        self.store.create_row("fes_channel_can_j1939_define", {
            "id": 1, "rtu_id": 7, "pgn": 61444, "source_address": 0x80,
            "message_type": "dm", "dm_number": 1,
        })
        self.store.create_row("fes_channel_can_uds_define", {
            "id": 1, "rtu_id": 7, "request_id": 0x700, "response_id": 0x708,
            "service_type": "read_data", "service_id": 0x22, "did": 0xF190,
        })
        self.store.create_row("fes_channel_can_diagnostic", {"rtu_id": 7})

        self.store.delete_rtu_cascade(7)
        with self.store.connection() as connection:
            for table in (
                "fes_channel_can_define", "fes_channel_can_frame_define",
                "fes_channel_canopen_service_define", "fes_channel_can_j1939_define",
                "fes_channel_can_uds_define", "fes_channel_can_diagnostic",
            ):
                self.assertEqual(connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE channel_id=70"
                ).fetchone()[0], 0, table)

    def test_store_projection_passes_new_transport_parameters(self):
        self.store.create_row("fes_channel_can_define", {
            "rtu_id": 7, "interface": "virtual", "channel": "projection-can",
            "bitrate": 250000, "data_bitrate": 1000000, "fd": 1,
            "sample_point": 80.0, "data_sample_point": 75.0,
            "bitrate_switch": 1, "receive_error_frames": 1,
            "restart_on_error": 1, "bus_off_recovery_ms": 250,
        })
        self.store.create_row("fes_channel_can_point_define", {
            "rtu_id": 7, "kind": "yc", "pnt_no": 1, "frame_id": 0x123,
            "is_fd": 1, "dlc": 12, "value_type": "uint",
            "dbc_message_name": "Status", "dbc_signal_name": "Voltage", "unit": "V",
        })
        with patch("client.can.can.Bus") as bus:
            master = CanMaster.from_store(self.store, 7)
            self.assertEqual(master.sample_point, 80.0)
            self.assertEqual(master.data_sample_point, 75.0)
            self.assertTrue(master.bitrate_switch)
            master.connect()
            self.assertEqual(bus.call_args.kwargs["sample_point"], 80.0)
            self.assertEqual(bus.call_args.kwargs["data_sample_point"], 75.0)
            self.assertEqual(bus.call_args.kwargs["restart_ms"], 250)
            master.close()

    def test_legacy_database_migration_is_backuped_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(db)
            connection.executescript(
                """
                CREATE TABLE _scada_schema(table_name TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
                CREATE TABLE fes_rtu_define(rtu_id INTEGER PRIMARY KEY);
                CREATE TABLE fes_channel_can_define(
                    _version INTEGER NOT NULL DEFAULT 1, rtu_id INTEGER PRIMARY KEY,
                    code TEXT, name TEXT, rdfid TEXT, interface TEXT, channel TEXT,
                    bitrate INTEGER, data_bitrate INTEGER, fd INTEGER,
                    receive_own_messages INTEGER, receive_timeout REAL,
                    retry_count INTEGER, is_extended_id INTEGER
                );
                CREATE TABLE fes_channel_can_point_define(
                    _version INTEGER NOT NULL DEFAULT 1, rtu_id INTEGER,
                    kind TEXT, pnt_no INTEGER, frame_id INTEGER, tx_frame_id INTEGER,
                    response_id INTEGER, is_extended_id INTEGER, is_fd INTEGER,
                    byte_offset INTEGER, bit_offset INTEGER, bit_length INTEGER,
                    value_type TEXT, byte_order TEXT, scale REAL, offset REAL,
                    quality_byte_offset INTEGER, quality_mask INTEGER,
                    tx_data_hex TEXT, response_data_hex TEXT,
                    PRIMARY KEY(rtu_id, kind, pnt_no)
                );
                CREATE TABLE fes_channel_j1939_define(
                    _version INTEGER NOT NULL DEFAULT 1, id INTEGER PRIMARY KEY,
                    rtu_id INTEGER
                );
                CREATE TABLE fes_channel_uds_define(
                    _version INTEGER NOT NULL DEFAULT 1, id INTEGER PRIMARY KEY,
                    rtu_id INTEGER
                );
                INSERT INTO _scada_schema VALUES
                    ('fes_channel_can_define', 'legacy'),
                    ('fes_channel_can_point_define', 'legacy'),
                    ('fes_channel_j1939_define', 'legacy'),
                    ('fes_channel_uds_define', 'legacy');
                """
            )
            connection.commit()
            connection.close()

            plan = migrate(db)
            self.assertTrue(plan["missing_tables"])
            self.assertFalse(plan["changed"])
            applied = migrate(db, apply=True)
            self.assertTrue(applied["changed"])
            self.assertTrue(Path(applied["backup"]).is_file())
            second = migrate(db, apply=True)
            self.assertFalse(second["changed"])
            self.assertIsNone(second["backup"])
            with closing(sqlite3.connect(db)) as check:
                self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                for table in (
                    "fes_channel_can_frame_define", "fes_channel_canopen_service_define",
                    "fes_channel_can_j1939_define", "fes_channel_can_uds_define",
                    "fes_channel_can_diagnostic",
                ):
                    self.assertIsNotNone(check.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone())
                self.assertIsNone(check.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
                    ("fes_channel_j1939_define", "fes_channel_uds_define"),
                ).fetchone())


if __name__ == "__main__":
    unittest.main()

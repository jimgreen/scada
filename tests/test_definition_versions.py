"""Regression tests for SQLite-maintained RTU definition/command tokens."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scdfe_c import command_version_token, definition_version_token
from scdfe_c.web.service import ScadaStore


class DefinitionVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ScadaStore(Path(self.temp.name) / "scada.sqlite3")
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 7, "channel_id": 70, "channel_type": "modbus-tcp", "name": "站 7",
        })
        self.store.create_row("fes_yc_define", {
            "rtu_id": 7, "pnt_no": 1, "name": "电压", "value": 0.0,
        })
        self.store.create_row("fes_yk_define", {
            "rtu_id": 7, "pnt_no": 1, "name": "开关", "value": 0, "cmd_time": 0,
        })
        self.addCleanup(self.temp.cleanup)

    def test_runtime_updates_do_not_change_definition_or_command_token(self):
        definition = definition_version_token(self.store, 7)
        command = command_version_token(self.store, 7)
        self.store.update_row("fes_yc_define", [7, 1], {
            "value": 12.5, "status": 1, "refresh_time": 123,
        })
        self.store.update_row("fes_yk_define", [7, 1], {
            "status": 1, "cmd_handle_time": 456, "refresh_time": 456,
        })
        self.assertEqual(definition_version_token(self.store, 7), definition)
        self.assertEqual(command_version_token(self.store, 7), command)

    def test_static_and_command_inputs_advance_only_their_token(self):
        definition = definition_version_token(self.store, 7)
        command = command_version_token(self.store, 7)
        self.store.update_row("fes_yc_define", [7, 1], {"name": "新电压"})
        self.assertGreater(definition_version_token(self.store, 7), definition)
        self.assertEqual(command_version_token(self.store, 7), command)

        command = command_version_token(self.store, 7)
        self.store.update_row("fes_yk_define", [7, 1], {"value": 1, "cmd_time": 789})
        self.assertGreater(command_version_token(self.store, 7), command)
        command = command_version_token(self.store, 7)
        self.store.update_row("fes_yk_define", [7, 1], {"value": 1, "cmd_time": 789})
        self.assertEqual(command_version_token(self.store, 7), command)

    def test_insert_delete_and_rtu_cascade_cleanup_tokens(self):
        definition = definition_version_token(self.store, 7)
        command = command_version_token(self.store, 7)
        self.store.create_row("fes_yc_define", {
            "rtu_id": 7, "pnt_no": 2, "name": "温度", "value": 0.0,
        })
        self.assertGreater(definition_version_token(self.store, 7), definition)
        self.store.delete_row("fes_yc_define", [7, 2])
        self.assertGreater(definition_version_token(self.store, 7), definition)

        self.store.delete_rtu_cascade(7)
        with self.store.connection() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM _scada_definition_version WHERE rtu_id=7",
            ).fetchone())
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM _scada_command_version WHERE rtu_id=7",
            ).fetchone())
        # Keep the local variable meaningful: the command token existed before
        # the cascade and is intentionally removed with its RTU.
        self.assertGreaterEqual(command, 0)


if __name__ == "__main__":
    unittest.main()

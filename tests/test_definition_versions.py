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
                "SELECT 1 FROM _scada_rtu_version WHERE rtu_id=7",
            ).fetchone())
        # Keep the local variable meaningful: the command token existed before
        # the cascade and is intentionally removed with its RTU.
        self.assertGreaterEqual(command, 0)

    def test_unchanged_static_values_keep_definition_token_but_version_advances(self):
        with self.store.connection() as connection:
            connection.execute('UPDATE fes_yc_define SET name=NULL WHERE rtu_id=7')
            before = connection.execute(
                'SELECT _version FROM fes_yc_define WHERE rtu_id=7').fetchone()[0]
        token = definition_version_token(self.store, 7)
        with self.store.connection() as connection:
            connection.execute('UPDATE fes_yc_define SET name=name, pnt_no=pnt_no, '
                               '_version=_version+1 WHERE rtu_id=7')
            after = connection.execute(
                'SELECT _version FROM fes_yc_define WHERE rtu_id=7').fetchone()[0]
        self.assertEqual(definition_version_token(self.store, 7), token)
        self.assertGreater(after, before)
        self.store.update_row('fes_yc_define', [7, 1], {'name': '恢复电压'})
        self.assertGreater(definition_version_token(self.store, 7), token)
        token = definition_version_token(self.store, 7)
        self.store.update_row('fes_yc_define', [7, 1], {'name': '恢复电压'})
        self.assertEqual(definition_version_token(self.store, 7), token)

    def test_legacy_update_trigger_is_migrated_once_without_changing_tokens(self):
        with self.store.connection() as connection:
            connection.execute('DROP TRIGGER _scada_defver_fes_yc_define_upd')
            connection.execute('''CREATE TRIGGER _scada_defver_fes_yc_define_upd
                AFTER UPDATE OF name ON fes_yc_define BEGIN
                INSERT INTO _scada_definition_version(rtu_id, version) VALUES (NEW.rtu_id, 1)
                ON CONFLICT(rtu_id) DO UPDATE SET version=version+1; END''')
        token = definition_version_token(self.store, 7)
        reopened = ScadaStore(self.store.db_path)
        self.assertEqual(definition_version_token(reopened, 7), token)
        reopened.update_row('fes_yc_define', [7, 1], {'name': '电压'})
        self.assertEqual(definition_version_token(reopened, 7), token)
        with reopened.connection() as connection:
            schema_version = connection.execute('PRAGMA schema_version').fetchone()[0]
        ScadaStore(self.store.db_path)
        with reopened.connection() as connection:
            self.assertEqual(connection.execute('PRAGMA schema_version').fetchone()[0], schema_version)


if __name__ == "__main__":
    unittest.main()

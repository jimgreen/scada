"""Database migration preserves retained data and only resets explicit fixtures."""
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from tools.remove_device_values import REMOVED, TABLES, migrate
from web.service import ApiError, ScadaStore


class RemoveDeviceValuesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'scada.sqlite3'
        self.store = ScadaStore(self.db)
        self.store.create_row('fes_rtu_define', {'rtu_id': 7, 'channel_id': 70,
                                               'channel_type': 'iec104', 'code': 'TEST_IEC104_RTU'})
        self.store.create_row('fes_yc_define', {
            'rtu_id': 7, 'pnt_no': 1, 'code': 'TEST_IEC104_YC_001',
            'raw_value': 12.5, 'raw_scale': 2.0, 'raw_base': 3.0, 'value': 28.0})
        self.store.create_row('fes_yx_define', {'rtu_id': 7, 'pnt_no': 1, 'value': 1})

    def add_old_columns(self):
        with self.store.connection() as connection:
            for table in TABLES:
                for name in REMOVED:
                    connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} REAL DEFAULT 99')
                connection.execute("UPDATE _scada_schema SET fingerprint='old' WHERE table_name=?", (table,))

    def test_backup_removal_and_noop_preserve_all_retained_values(self):
        before = {table: self.store.get_row(table, [7, 1]) for table in TABLES}
        self.add_old_columns()
        report = migrate(self.db)
        self.assertEqual(report['integrity_check'], 'ok')
        self.assertEqual(report['foreign_key_violations'], 0)
        with closing(sqlite3.connect(report['backup'])) as connection:
            self.assertEqual(connection.execute('SELECT value_dev FROM fes_yc_define').fetchone()[0], 99)
        current = ScadaStore(self.db)
        for table in TABLES:
            self.assertEqual(len(report['removed'][table]), 12)
            self.assertEqual(current.get_row(table, [7, 1]), before[table])
            with self.assertRaises(ApiError):
                current.update_row(table, [7, 1], {'value_dev': 1})
        repeat = migrate(self.db)
        self.assertIsNone(repeat['backup'])
        self.assertEqual(repeat['removed'], {table: [] for table in TABLES})

    def test_unexpected_schema_aborts_without_deleting_columns(self):
        self.add_old_columns()
        with self.store.connection() as connection:
            connection.execute('ALTER TABLE fes_yc_define ADD COLUMN unrelated TEXT')
        with self.assertRaisesRegex(RuntimeError, '超出本次迁移'):
            migrate(self.db)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT value_dev FROM fes_yc_define').fetchone()[0], 99)

    def test_refresh_only_fixture_values_and_never_command_values(self):
        self.store.create_row('fes_yc_define', {
            'rtu_id': 7, 'pnt_no': 2, 'code': 'USER_YC_002', 'value': 42.0})
        self.store.create_row('fes_yt_define', {
            'rtu_id': 7, 'pnt_no': 1, 'code': 'TEST_IEC104_YT_001',
            'value': 20.0, 'cmd_time': 10, 'cmd_handle_time': 0})
        untouched = self.store.get_row('fes_yc_define', [7, 2])
        command = self.store.get_row('fes_yt_define', [7, 1])
        report = migrate(self.db, refresh_test_values=True)
        self.assertEqual(report['test_rows_updated'], {'fes_yc_define': 1, 'fes_yx_define': 0, 'fes_yt_define': 0})
        row = self.store.get_row('fes_yc_define', [7, 1])
        self.assertNotEqual(row['raw_value'], row['value'])
        self.assertEqual(row['value'], row['raw_base'] + row['raw_scale'] * row['raw_value'])
        self.assertEqual(self.store.get_row('fes_yc_define', [7, 2]), untouched)
        self.assertEqual(self.store.get_row('fes_yt_define', [7, 1]), command)


if __name__ == '__main__':
    unittest.main()

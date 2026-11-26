from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from core.rtdb_schema import sync_embedded_schema
from scdfe_d.service import DeviceStore


def fields(connection, table):
    return [row[0] for row in connection.execute(
        'SELECT eng_name FROM rtdb_schema_columns WHERE table_name=? ORDER BY ordinal', (table,)
    )]


class EmbeddedSchemaTests(unittest.TestCase):
    def test_table_ddl_and_metadata_rollback_together(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.execute('BEGIN')
            connection.execute('CREATE TABLE device (idx INTEGER PRIMARY KEY, name TEXT)')
            sync_embedded_schema(connection)
            connection.commit()
            connection.execute('BEGIN')
            connection.execute('ALTER TABLE device ADD COLUMN p REAL DEFAULT 1.5')
            sync_embedded_schema(connection)
            self.assertEqual(fields(connection, 'device'), ['idx', 'name', 'p'])
            connection.rollback()
            self.assertEqual(fields(connection, 'device'), ['idx', 'name'])
            self.assertEqual([row[1] for row in connection.execute('PRAGMA table_info(device)')], ['idx', 'name'])
            connection.execute('BEGIN')
            connection.execute('DROP TABLE device')
            sync_embedded_schema(connection)
            connection.commit()
            self.assertEqual(fields(connection, 'device'), [])

    def test_device_added_attribute_registers_whole_measurement_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / 'source.db')) as source, source:
                for kind in ('yc', 'yx'):
                    source.execute(f"""CREATE TABLE fes_{kind}_define (rtu_id INTEGER, pnt_no INTEGER,
                        name TEXT, value REAL, status INTEGER, refresh_time INTEGER)""")
                    source.execute(f"INSERT INTO fes_{kind}_define VALUES(1, 6, 'Power', 3, 0, 1)")
            store = DeviceStore(root / 'target.db', workspace_root=root)
            device = store.create_device(dict(category='generator', name='G1'))
            store.save_measurement(dict(device_id=device['device_id'], metric='p', kind='yc',
                                        source_database='source.db', rtu_id=1, pnt_no=6))
            with store.connection() as connection:
                self.assertEqual(fields(connection, device['model_table']), [
                    'idx', 'name', 'p', 'p_qual', 'p_pre', 'p_time', 'p_notuse', 'p_weight', 'p_rtu', 'p_pnt', 'p_max', 'p_min',
                ])
                disp = connection.execute(
                    "SELECT disp_type, disp_menu FROM rtdb_schema_columns WHERE table_name=? AND eng_name='p_qual'",
                    (device['model_table'],),
                ).fetchone()
                self.assertEqual(
                    tuple(disp),
                    ('菜单类型', '{"0":"正常","1":"无配置","2":"通信异常","3":"越上限","4":"越下限"}'),
                )

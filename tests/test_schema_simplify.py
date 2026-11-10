from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from scdfe_c.web.service import ApiError, ScadaStore
from scdfe_t.web.service import TransStore
from tools import simplify_schema as migration


class SimplifySchemaTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db=Path(tmp.name)/'schema.sqlite3'
        self.store=ScadaStore(self.db)
        self.store.create_row('fes_rtu_define',{'rtu_id':8,'channel_id':80,'channel_type':'iec104'})
        self.store.create_row('fes_yc_define',{'rtu_id':8,'pnt_no':1,'name':'温度','value':12.5,'status':128})
        self.store.create_row('fes_scdfe_t_point_define',{'channel_id':9,'pnt_no':1,'kind':'yc',
                                                          'source_rtu_id':8,'source_pnt_no':1})

    def legacy(self):
        with self.store.connection() as con:
            # Include a dependent index/trigger and an RTU present in only one
            # old counter table; both are supported migration cases.
            con.execute('BEGIN')
            sql=con.execute("SELECT sql FROM sqlite_schema WHERE name='fes_yc_define'").fetchone()[0]
            con.execute('CREATE TABLE old_points '+sql[sql.index('('):].removesuffix(' WITHOUT ROWID'))
            con.execute('INSERT INTO old_points SELECT * FROM fes_yc_define')
            con.execute('DROP TABLE fes_yc_define')
            con.execute('ALTER TABLE old_points RENAME TO fes_yc_define')
            con.execute('CREATE INDEX custom_value_idx ON fes_yc_define(value)')
            con.execute("CREATE TRIGGER custom_guard BEFORE DELETE ON fes_yc_define BEGIN SELECT RAISE(ABORT,'guard'); END")
            con.execute('ALTER TABLE fes_scdfe_t_point_define ADD COLUMN source_kind TEXT')
            con.execute("UPDATE fes_scdfe_t_point_define SET source_kind=kind,_version=7")
            con.execute('UPDATE _scada_schema SET fingerprint=? WHERE table_name=?',
                        (migration.OLD_MAPPING_HASH,migration.MAPPING))
            for t, col in zip(migration.OLD,migration.VERSION_COLS):
                con.execute(f'CREATE TABLE {t}(rtu_id INTEGER PRIMARY KEY,version INTEGER)')
                con.execute(f'INSERT INTO {t} SELECT rtu_id,{col} FROM _scada_rtu_version')
            con.execute('INSERT INTO _scada_command_version VALUES(99,14)')
            con.execute('UPDATE _scada_definition_version SET version=42 WHERE rtu_id=8')
            con.execute('DROP TABLE _scada_rtu_version')
            con.commit()

    def test_migration_preserves_rows_generations_and_local_objects(self):
        self.legacy()
        with self.assertRaisesRegex(RuntimeError,'显式迁移'):
            ScadaStore(self.db)
        self.assertFalse(migration.apply(self.db)['changed'])
        result=migration.apply(self.db,write=True,compact=True)
        self.assertTrue(Path(result['backup']).is_file())
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT definition_version FROM _scada_rtu_version WHERE rtu_id=8').fetchone(),(42,))
            self.assertEqual(con.execute('SELECT * FROM _scada_rtu_version WHERE rtu_id=99').fetchone(),(99,0,14))
            self.assertEqual(con.execute('SELECT value,status FROM fes_yc_define').fetchone(),(12.5,128))
            self.assertEqual(con.execute('SELECT kind,_version FROM fes_scdfe_t_point_define').fetchone(),('yc',7))
            self.assertTrue(con.execute("SELECT 1 FROM sqlite_schema WHERE name='custom_value_idx'").fetchone())
            with self.assertRaisesRegex(sqlite3.IntegrityError,'guard'):
                con.execute('DELETE FROM fes_yc_define')
        self.assertIsNone(migration.apply(self.db,write=True)['backup'])
        self.assertEqual(ScadaStore(self.db).get_row('fes_yc_define',[8,1])['name'],'温度')

    def test_conflicting_source_types_are_not_silently_collapsed(self):
        self.legacy()
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE fes_scdfe_t_point_define SET source_kind='yk'")
            con.commit()
        with self.assertRaisesRegex(ValueError,'拒绝丢弃'):
            migration.apply(self.db,write=True)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT kind,source_kind FROM fes_scdfe_t_point_define').fetchone(),('yc','yk'))

    def test_mid_migration_error_rolls_back_the_entire_schema(self):
        self.legacy()
        with patch.object(migration,'rebuild_point',side_effect=RuntimeError('injected')), self.assertRaisesRegex(RuntimeError,'injected'):
            migration.apply(self.db,write=True)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT version FROM _scada_definition_version WHERE rtu_id=8').fetchone(),(42,))
            self.assertEqual(con.execute('SELECT source_kind,_version FROM fes_scdfe_t_point_define').fetchone(),('yc',7))
            self.assertIsNone(con.execute("SELECT 1 FROM sqlite_schema WHERE name='_scada_rtu_version'").fetchone())

    def test_concurrent_automatic_channel_keys_and_legacy_type_input(self):
        gate=threading.Barrier(8)
        trans=TransStore(self.db)
        def new(i):
            gate.wait(timeout=5)
            return trans.create_channel({'name':f'T{i}','protocol':'iec104'})['channel_id']
        with ThreadPoolExecutor(8) as pool:
            keys=list(pool.map(new,range(8)))
        self.assertEqual(len(set(keys)),8)
        point={'kind':'yk','source_kind':'yk','pnt_no':2,'source_rtu_id':8,'source_pnt_no':7}
        self.assertEqual(trans.create_point(keys[0],point)['source_kind'],'yk')
        with self.assertRaises(ApiError):
            trans.update_point(keys[0],[keys[0],2,'yk'],{'source_kind':'yc'})
        self.assertNotIn('source_kind',self.store.get_row(migration.MAPPING,[keys[0],2,'yk']))
        same_number=trans.create_point(keys[0],{'kind':'yx','pnt_no':8,'source_rtu_id':8})
        self.assertEqual((same_number['kind'],same_number['source_pnt_no']),('yx',8))

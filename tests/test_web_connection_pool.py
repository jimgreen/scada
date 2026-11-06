"""Web connection reuse must never share an active transaction or retain writes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from scdfe_c.web.service import ApiError, ScadaStore
from scdfe_c.web.server import make_server


class WebConnectionPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ScadaStore(Path(self.temp.name)/'db.sqlite3', connection_pool_size=2)
        self.addCleanup(self.store.close)
        self.store.create_row('fes_rtu_define', {'rtu_id':7,'channel_id':7,'channel_type':'modbus-tcp'})
        self.store.create_row('fes_yc_define', {'rtu_id':7,'pnt_no':1,'value':0})

    def test_rollback_and_fresh_external_commits_between_leases(self):
        with self.store.connection() as con:
            con.execute('BEGIN')
            con.execute('UPDATE fes_yc_define SET value=999 WHERE rtu_id=7')
        self.assertEqual(self.store.get_row('fes_yc_define',[7,1])['value'],0)
        with self.store.connection() as con:
            con.execute('BEGIN')
            self.assertEqual(con.execute('SELECT value FROM fes_yc_define').fetchone()[0],0)
        external=self.store.open_connection()
        try: external.execute('UPDATE fes_yc_define SET value=42 WHERE rtu_id=7')
        finally: external.close()
        self.assertEqual(self.store.get_row('fes_yc_define',[7,1])['value'],42)

    def test_exception_rolls_back_and_keeps_foreign_keys(self):
        with self.assertRaises(ValueError), self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute('UPDATE fes_yc_define SET value=999 WHERE rtu_id=7')
            raise ValueError('cancel')
        self.assertEqual(self.store.get_row('fes_yc_define',[7,1])['value'],0)
        with self.store.connection() as con:
            self.assertEqual(con.execute('PRAGMA foreign_keys').fetchone()[0],1)
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('INSERT INTO fes_yc_define(rtu_id,pnt_no) VALUES(999,1)')

    def test_concurrent_leases_are_exclusive_and_all_commits_survive(self):
        barrier=threading.Barrier(8)
        def increment(_):
            with self.store.connection() as con:
                identity=id(con)
                barrier.wait(timeout=5)
                con.execute('BEGIN IMMEDIATE')
                con.execute('UPDATE fes_yc_define SET value=value+1 WHERE rtu_id=7')
                con.commit()
                return identity
        with ThreadPoolExecutor(max_workers=8) as executor:
            identities=list(executor.map(increment,range(8)))
        self.assertEqual(len(set(identities)),8)
        self.assertEqual(self.store.get_row('fes_yc_define',[7,1])['value'],8)

    def test_batch_failure_restores_values_quality_and_versions(self):
        table = 'fes_yc_define'
        first = self.store.get_row(table, [7,1])
        second = self.store.create_row(table, {'rtu_id':7,'pnt_no':2,'value':10})
        with self.assertRaises(ApiError) as error:
            self.store.update_rows([(table,[7,1],{'value':20,'status':128}),
                                    (table,[7,999],{'value':30})])
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.store.get_row(table,[7,1]), first)
        self.assertEqual(self.store.update_rows([(table,[7,1],{'value':20,'status':128}),
                                                  (table,[7,2],{'value':30})]), 2)
        for no, value, quality, original in [(1,20,128,first),(2,30,second['status'],second)]:
            row = self.store.get_row(table,[7,no])
            self.assertEqual((row['value'],row['status'],row['_version']),
                             (value,quality,original['_version']+1))

    def test_filtered_total_on_full_short_and_overrun_pages(self):
        for number in range(2,6):
            self.store.create_row('fes_yc_define',{'rtu_id':7,'pnt_no':number,'name':'page-test'})
        for offset, limit, ids in [(0,2,[2,3]),(2,2,[4,5]),(3,2,[5]),(4,2,[]),(20,2,[])]:
            with self.subTest(offset=offset):
                result = self.store.list_rows('fes_yc_define',search='page-test',offset=offset,limit=limit)
                self.assertEqual((result['total'],[r['pnt_no'] for r in result['items']]),(4,ids))

    def test_close_releases_idle_and_outstanding_handles(self):
        with self.store.connection() as held:
            with self.store.connection() as idle:
                pass
            self.store.close()
            with self.assertRaises(sqlite3.ProgrammingError): idle.execute('SELECT 1')
            self.assertEqual(held.execute('SELECT 1').fetchone()[0],1)
        with self.assertRaises(sqlite3.ProgrammingError): held.execute('SELECT 1')
        self.store.db_path.unlink()

    def test_default_store_still_closes_each_connection(self):
        store=ScadaStore(Path(self.temp.name)/'default.sqlite3')
        with store.connection() as con: con.execute('SELECT 1')
        with self.assertRaises(sqlite3.ProgrammingError): con.execute('SELECT 1')
        store.db_path.unlink()

    def test_server_close_and_failed_bind_release_database(self):
        path=Path(self.temp.name)/'web.sqlite3'
        server=make_server('127.0.0.1',0,db_path=path)
        try:
            self.assertTrue(server.store.connection_pool_size)
            other=Path(self.temp.name)/'failed.sqlite3'
            with patch.object(type(server), 'server_bind', side_effect=OSError('bind failed')), self.assertRaises(OSError):
                make_server('127.0.0.1',server.server_port,db_path=other)
            other.unlink()
        finally: server.server_close()
        path.unlink()


if __name__=='__main__': unittest.main()

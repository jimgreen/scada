"""T monitor projections and coherent, multi-RTU source catalog reads."""
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from scdfe_t.web.service import TransStore


class TransMonitorCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name)/'test.sqlite3'
        self.service = TransStore(self.db, connection_pool_size=1)
        self.addCleanup(self.service.store.close)
        with self.service.store.connection() as con:
            con.executemany("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type,name) VALUES(?,?,'iec104',?)",
                            [(n,n,None if n == 3 else f'站{n}') for n in (1,2,3)])
            for kind in ('yc','yx','yk','yt'):
                con.executemany(f'INSERT INTO fes_{kind}_define(rtu_id,pnt_no,name) VALUES(?,?,?)',
                                [(3,8,None),(1,5,'点5'),(1,2,'点2')])

    def test_monitor_exposes_config_used_by_communication_and_details(self):
        for cid, protocol in ((1, 'iec104'), (2, 'modbus-tcp')):
            expected = dict(channel_id=cid, protocol=protocol, bind_host='127.0.0.1',
                            bind_port=25000+cid, scan_interval_ms=50,
                            common_address=7, unit_id=3, report_interval_ms=700,
                            log_retention_hours=48)
            self.service.create_channel(expected)
            item = self.service.runtime_status()['items'][cid-1]
            for key, value in expected.items():
                self.assertEqual(item.get(key), value, key)
            self.assertEqual(item['runtime_status']['protocol'], protocol)

    def test_catalog_counts_kinds_order_and_nullable_names(self):
        for kind in ('yc','yx','yk','yt'):
            with self.subTest(kind=kind):
                result = self.service.source_catalog(kind, 1)
                self.assertEqual([(r['rtu_id'],r['point_count']) for r in result['rtus']], [(1,2),(3,1)])
                self.assertEqual(result['rtus'][1]['name'], '')
                self.assertEqual([p['pnt_no'] for p in result['items']], [2,5])
                detail = self.service.source_catalog(kind, 1, include_rtus=False)
                self.assertEqual(detail['items'], result['items'])
                self.assertEqual(detail['rtus'], [])

    def test_catalog_counts_and_points_share_a_snapshot_under_external_write(self):
        inserted = []
        def write_between_queries(sql):
            if sql.startswith('SELECT rtu_id,pnt_no') and not inserted:
                with closing(sqlite3.connect(self.db)) as other:
                    other.execute('INSERT INTO fes_yc_define(rtu_id,pnt_no,value) VALUES(1,9,3)')
                    other.commit()
                inserted.append(True)
        with self.service.store.connection() as con:
            con.set_trace_callback(write_between_queries)
        try:
            result = self.service.source_catalog('yc', 1)
        finally:
            with self.service.store.connection() as con: con.set_trace_callback(None)
        self.assertEqual(inserted, [True])
        count = next(r['point_count'] for r in result['rtus'] if r['rtu_id']==1)
        self.assertEqual(count, len(result['items']))
        self.assertEqual(count, 2)
        following = self.service.source_catalog('yc', 1)
        self.assertEqual(following['rtus'][0]['point_count'], 3)
        self.assertEqual(len(following['items']), 3)

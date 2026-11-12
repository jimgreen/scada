"""Batch receipts and IEC104 spontaneous delivery must retain exact semantics."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sqlite3
import socket
import tempfile
import threading
import time
import unittest

from scdfe_c.web.service import ApiError, ScadaStore
from scdfe_s.web.iec104_service import IEC104Service
from server.iec104_server import IEC104Server
from client.iec104 import IEC104Master
from core.iec104_protocol import parse_apdu


class BatchReceiptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store = ScadaStore(Path(tmp.name)/'scada.sqlite3', connection_pool_size=1)
        self.addCleanup(self.store.close)
        self.sql = []
        with self.store.connection() as con:
            con.executemany("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,'iec104')", [(r,r) for r in range(1,51)])
            for kind in ('yc','yx'):
                con.executemany(f'INSERT INTO fes_{kind}_define(rtu_id,pnt_no,value) VALUES(?,1,0)', [(r,) for r in range(1,51)])
            con.commit()
            con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32)
            con.set_trace_callback(self.sql.append)

    def test_grouped_receipts_keep_input_order_and_final_versions(self):
        updates = [('fes_yc_define', [r,1], {'value':r}) for r in range(1,51)]
        updates.insert(10, ('fes_yx_define', [2,1], {'value':1}))
        updates.append(('fes_yc_define', '1,1', {'value':99}))
        rows = self.store.update_rows(updates, return_rows=True)
        self.assertEqual([r['rtu_id'] for r in rows], [int(u[1][0]) if not isinstance(u[1],str) else 1 for u in updates])
        self.assertEqual((rows[0]['value'],rows[-1]['value']), (99,99))
        self.assertEqual(rows[0]['_version'], rows[-1]['_version'])
        rows[0]['value'] = -1
        self.assertEqual(rows[-1]['value'],99)  # independent response objects
        reads = [q for q in self.sql if q.startswith(('SELECT','WITH'))]
        self.assertLessEqual(len(reads),5)  # four chunks + one other table

    def test_receipt_missing_after_trigger_rolls_back_batch(self):
        with self.store.connection() as con:
            con.execute('CREATE TRIGGER disappear AFTER UPDATE ON fes_yc_define WHEN NEW.rtu_id=4 BEGIN DELETE FROM fes_yc_define WHERE rtu_id=1; END')
            con.commit()
        with self.assertRaises(ApiError) as error:
            self.store.update_rows([('fes_yc_define',[r,1],{'value':7}) for r in range(1,5)],return_rows=True)
        self.assertEqual(error.exception.status,404)
        self.assertEqual(self.store.get_row('fes_yc_define',[1,1])['value'],0)

    def test_single_primary_key_receipts_match_regular_reads(self):
        rows=self.store.update_rows([('fes_rtu_define',str(r),{'name':f'站{r}'}) for r in range(1,40)],return_rows=True)
        self.assertEqual(rows,[self.store.get_row('fes_rtu_define',[r]) for r in range(1,40)])


class MonitoringBatchTests(unittest.TestCase):
    def test_loopback_mixed_time_tags_split_asdus_quality_and_interrogation(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        server=IEC104Server(ip='127.0.0.1',port=port)
        self.addCleanup(server.stop)
        client=IEC104Master('127.0.0.1',port,init='NONE')
        self.addCleanup(client.close)
        frames=[]; server.set_raw_handlers(on_send=lambda data:frames.append(bytes(data)))
        updates=[]
        for kind in ('yc','yx'):
            for no in range(1,81):
                tag='CP56Time2a' if no>40 else 'NONE'
                server.add_point(kind,no,value=0,time_tag=tag)
                client.add_point(kind,no,time_tag=tag)
                updates.append((kind,no,float(no) if kind=='yc' else True,128 if no%2 else 64))
        server.start(); client.connect(timeout=3)
        server.update_monitoring(updates)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if all(client.value(k,n).value==v and client.value(k,n).quality==q for k,n,v,q in updates): break
            time.sleep(.01)
        for k,n,v,q in updates:
            self.assertEqual((client.value(k,n).value,client.value(k,n).quality),(v,q))
            if n>40: self.assertIsNotNone(client.value(k,n).recorded_at)
        spontaneous=[b for b in frames if len(b)>9 and b[2]&1==0 and b[8]&63==3]
        self.assertEqual(sum(b[7]&127 for b in spontaneous),len(updates))
        self.assertLess(len(spontaneous),len(updates)//4)
        for b in spontaneous: self.assertLessEqual(parse_apdu(b).asdu_length,249)
        self.assertTrue(client.general_interrogation())

    def test_validate_before_mutating_and_group_quality_by_native_type(self):
        server = IEC104Server(ip='127.0.0.1',port=25149)
        self.addCleanup(server.stop)
        server.add_point('yc',1,value=0)
        server.add_point('yc',2,value=0)
        server.add_point('yx',1,value=False)
        native = server._server
        server._server = Mock(transmit_batch=Mock(return_value=True))
        self.addCleanup(lambda:setattr(server,'_server',native))
        with self.assertRaises(Exception):
            server.update_monitoring([('yc',1,4,0),('yk',2,1,0)])
        self.assertEqual(server.snapshot()[('yc',1)],0)
        server.update_monitoring([('yc',1,4,128),('yc',2,5,0),('yx',1,True,64)])
        self.assertEqual(server._server.transmit_batch.call_count,2)
        self.assertEqual(server.monitoring_snapshot(),{('yc',1):(4,128),('yc',2):(5,0),('yx',1):(True,64)})

    def test_sync_retries_failed_send_even_after_native_values_changed(self):
        con=sqlite3.connect(':memory:'); self.addCleanup(con.close)
        for kind in ('yc','yx'):
            con.execute(f'CREATE TABLE fes_{kind}_define(rtu_id INT,pnt_no INT,value REAL,status INT)')
        con.execute('INSERT INTO fes_yc_define VALUES(1,1,7,128)')
        native={('yc',1):(0,0)}
        def publish(updates,transmit):
            for kind,no,value,quality in updates: native[(kind,no)]=(value,quality)
            if transmit and server.update_monitoring.call_count==1: raise OSError('send failed after update')
        server=SimpleNamespace(rtu_id=1,running=True,active_connection_count=1,
            prepare_value=lambda k,n,v:v,monitoring_snapshot=lambda keys:{k:native[k] for k in keys},
            update_monitoring=Mock(side_effect=publish))
        service=SimpleNamespace(_lock=threading.RLock(),_server=server,_database_samples={},_database_pending=set())
        sync=lambda:IEC104Service._sync_database_values(service,con)
        with self.assertRaises(OSError): sync()
        self.assertEqual(service._database_samples,{})
        self.assertEqual(sync(),1)
        self.assertEqual(server.update_monitoring.call_count,2)
        self.assertEqual(sync(),0)
        con.execute('UPDATE fes_yc_define SET status=64')
        self.assertEqual(sync(),1)
        self.assertEqual(native[('yc',1)],(7,64))

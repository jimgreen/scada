"""Cross-service regressions for committed point values and hourly log boundaries."""
import socket
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from unittest.mock import Mock

from client.iec104 import IEC104Master
from scdfe_c import ScdfeLogger
from scdfe_c.web.log_query import search_logs
from scdfe_c.web.service import ApiError, ScadaStore
from scdfe_s.web.iec104_service import IEC104Service, IEC104ServiceError
from scdfe_t.trans import TransRuntime
from scdfe_t.web.service import TransStore

def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]

class CommitVisibilityTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db=Path(tmp.name)/'scada.sqlite3'
        self.store=ScadaStore(self.db,connection_pool_size=1)
        self.addCleanup(self.store.close)
        self.store.create_row('fes_rtu_define',dict(rtu_id=7,channel_id=7,channel_type='iec104'))
        for kind,values in [('yc',[12,23]),('yk',[0])]:
            for n,v in enumerate(values,1):
                self.store.create_row(f'fes_{kind}_define',dict(rtu_id=7,pnt_no=n,value=v))
    def station(self):
        port=free_port()
        self.store.create_row('fes_channel_iec104_define',dict(id=7,rtu_id=7,rtu_addr=1,remote_host='127.0.0.1',remote_port=port))
        for n in [1,2]:
            self.store.create_row('fes_channel_iec104_point_define',dict(rtu_id=7,kind='yc',pnt_no=n,
                                  io_address=0x4000+n,type_id=13))
        svc=IEC104Service(self.store);self.addCleanup(svc.close)
        svc.configure(dict(port=port,rtu_id=7));svc.start()
        return svc,port
    def test_rejected_database_update_never_reaches_the_station_or_peer(self):
        import time
        svc,port=self.station()
        peer=IEC104Master('127.0.0.1',port,init='NONE')
        self.addCleanup(peer.close)
        peer.add_point('yc',1);peer.add_point('yc',2)
        peer.connect(timeout=4);self.assertTrue(peer.general_interrogation())
        with self.store.connection() as con:
            con.execute("CREATE TRIGGER reject_value BEFORE UPDATE ON fes_yc_define WHEN NEW.value=999 BEGIN SELECT RAISE(ABORT,'injected'); END")
        before=svc.server.snapshot()
        for update in (lambda:svc.incremental_update('yc',1,999),
                       lambda:svc.full_update({'yc':{'1':77,'2':999}})):
            with self.assertRaises(IEC104ServiceError):update()
            self.assertEqual(svc.server.snapshot(),before)
        self.assertTrue(peer.general_interrogation())
        deadline=time.monotonic()+2
        while [peer.value('yc',n).value for n in (1,2)] != [12,23] and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual([peer.value('yc',n).value for n in (1,2)],[12,23])
        self.assertEqual([self.store.get_row('fes_yc_define',[7,n])['value'] for n in (1,2)],[12,23])
    def test_invalid_full_update_is_rejected_before_any_value_is_changed(self):
        svc,_=self.station(); before=svc.server.snapshot()
        with self.assertRaises(IEC104ServiceError):svc.full_update({'yc':{'1':77,'444':23}})
        self.assertEqual(svc.server.snapshot(),before)
    def test_measurement_batch_does_not_load_each_wide_row_and_rolls_back_missing_points(self):
        sql=[]
        real=self.store.connection
        @contextmanager
        def traced():
            with real() as con:
                con.set_trace_callback(sql.append)
                try:yield con
                finally:con.set_trace_callback(None)
        with patch.object(self.store,'connection',traced):
            self.assertEqual(self.store.update_rows([('fes_yc_define',[7,n],{'value':n}) for n in (1,2)]),2)
        self.assertFalse([q for q in sql if q.startswith('SELECT')],sql)
        before=self.store.get_row('fes_yc_define',[7,1])
        with self.assertRaises(ApiError) as error:
            self.store.update_rows([('fes_yc_define',[7,n],{'value':42}) for n in (1,9)])
        self.assertEqual(error.exception.status,404)
        self.assertEqual(self.store.get_row('fes_yc_define',[7,1]),before)
        with self.assertRaises(ApiError) as stale:
            self.store.update_row('fes_yc_define',[7,1],{'value':8},before['_version']-1)
        self.assertEqual(stale.exception.code,'version_conflict')
    def test_high_iec_common_address_is_usable_on_the_wire(self):
        trans=TransStore(self.db)
        self.addCleanup(trans.store.close)
        port=free_port(); base=dict(code='HIGH',protocol='iec104',bind_port=port,bind_host='127.0.0.1')
        row=trans.create_channel({**base,'common_address':60000})
        self.assertEqual(row['common_address'],60000)
        with self.assertRaises(ApiError):trans.update_channel(row['channel_id'],{'common_address':65535})
        with self.assertRaises(ApiError):trans.update_channel(row['channel_id'],{'unit_id':248})
        trans.create_point(row['channel_id'],dict(kind='yc',pnt_no=1,source_rtu_id=7,source_pnt_no=1,io_address=0x4001,type_id=13))
        runtime=TransRuntime(self.db,channel_id=row['channel_id']);self.addCleanup(runtime.store.close)
        spec,points,_=runtime.load(); station=runtime.build_server(spec,points);self.addCleanup(station.stop);station.start()
        peer=IEC104Master('127.0.0.1',port,common_address=60000,init='NONE');self.addCleanup(peer.close)
        peer.add_point('yc',1);peer.connect(timeout=4);self.assertTrue(peer.general_interrogation())
        import time
        end=time.monotonic()+2
        while peer.value('yc',1).value!=12 and time.monotonic()<end:time.sleep(.01)
        self.assertEqual(peer.value('yc',1).value,12)
    def test_channel_creation_cannot_claim_a_running_process(self):
        trans=TransStore(self.db)
        with self.assertRaises(ApiError) as error:
            trans.create_channel(dict(code='forged',protocol='iec104',process_pid=123,process_status=2))
        self.assertEqual(error.exception.status,400)
        self.assertEqual(trans.channels(),[])
    def test_forwarded_command_confirmation_does_not_depend_on_the_log_disk(self):
        run=TransRuntime(self.db); self.addCleanup(run.store.close)
        point=SimpleNamespace(kind='yk',source_rtu_id=7,source_pnt_no=1)
        run.writer=Mock()
        with patch.object(run.logger,'normal',side_effect=OSError('full')), self.assertLogs('scdfe_t.trans'):
            self.assertTrue(run.command(point,1))
        run.writer.submit_command.assert_called_once_with(7,'yk',1,1,timeout=5)
        run.writer.submit_command.side_effect=ConnectionError('offline')
        with patch.object(run.logger,'error',side_effect=OSError('full')), self.assertLogs('scdfe_t.trans'):
            self.assertFalse(run.command(point,1))
    def test_publish_failure_reports_the_commit_and_preserves_native_float_precision(self):
        svc,_=self.station()
        import c104
        with patch.object(svc.server,'update',side_effect=OSError('send failed')):
            with self.assertRaisesRegex(IEC104ServiceError,'已写入'):
                svc.incremental_update('yc',1,0.1)
        row=self.store.get_row('fes_yc_define',[7,1])
        self.assertEqual(row['value'], c104.ShortInfo(.1).value)
    def test_multiline_runtime_text_cannot_become_a_packet_or_new_record(self):
        root=self.db.parent/'logs'
        counterfeit='09:00:00 [报文] 报文 RX body HEX=ff'
        for family,process in [('c','scdfe_c-main'),('s','scdfe_s-iec104'),('t','scdfe_t-channel-1')]:
            with self.subTest(family=family):
                log=ScdfeLogger(root,process_name=process)
                log.error('error\r\n'+counterfeit+'\r another line\u2028tail')
                log.packet('TX',b'\x12\xab','peer\n'+counterfeit)
                self.assertEqual(search_logs(root,{'direction':'RX'},family=family)['items'],[])
                rows=search_logs(root,{},family=family)['items']
                self.assertEqual(len(rows),2)
                self.assertTrue(rows[0]['text'].endswith('HEX=12 ab'))
                self.assertIn('tail',rows[1]['text'])

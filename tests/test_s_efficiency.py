"""S simulation writes, indexed reads and compact point views."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scdfe_c.web.service import ScadaStore
from scdfe_s.web.protocol_service import BacnetServerService, OpcServerService, CanServerService, ProtocolServiceError
from scdfe_s.web.iec104_service import IEC104Service


class ServiceEfficiencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = ScadaStore(self.root / "scada.sqlite3", connection_pool_size=2)
        self.addCleanup(self.store.close)
        for rid, proto in [(1, 'can'), (2, 'opc'), (3, 'bacnet'), (4, 'iec104')]:
            self.store.create_row('fes_rtu_define',dict(rtu_id=rid,channel_id=rid,channel_type=proto))
            for n in (1, 2):
                self.store.create_row('fes_yc_define',dict(rtu_id=rid,pnt_no=n,value=n,name=f'p{n}'))
        with self.store.connection() as con:
            con.execute("CREATE TRIGGER reject_second BEFORE UPDATE ON fes_yc_define WHEN NEW.pnt_no=2 AND NEW.value=44 BEGIN SELECT RAISE(ABORT,'reject');END")
            con.commit()

    def test_db_rejection_never_exposes_earlier_point_to_standalone_servers(self):
        for rid, cls in [(1, CanServerService), (2, OpcServerService), (3, BacnetServerService)]:
            with self.subTest(proto=cls.__name__):
                s = cls(self.store)
                try:
                    s.configure(dict(rtu_id=rid,simulation=1));s.start()
                    def results():
                        return ([r['value'] for r in s.points('yc')],
                                [r['value'] for r in s._engine.snapshot() if r['kind']=='yc'])
                    with self.assertRaises(ProtocolServiceError):s.full_update({'yc':{'1':33,'2':44}})
                    self.assertEqual(results(),([1,2],[1,2]))
                    result=s.full_update({'yc':{'1':33,'2':55}})
                    self.assertEqual(result['updated'],2)
                    self.assertEqual(results(),([33,55],[33,55]))
                finally:s.close()

    def test_compact_point_and_single_lookup_keep_sample_overlay(self):
        s=CanServerService(self.store)
        try:
            s.configure(dict(rtu_id=1))
            s._latest_values[('yc',2)]={'value':19,'status':7,'refresh_time':99}
            full=s.points('yc',point_no=2)
            display=s.points('yc',point_no=2,table_view=True)
            self.assertEqual(len(full),1)
            self.assertEqual((full[0]['refresh_time'],display[0]['value'],display[0]['status']),(99,19,7))
            self.assertEqual(set(display[0]),{'kind','rtu_id','pnt_no','name','value','status'})
            self.assertEqual(s.points('yc',point_no=3),[])
        finally:s.close()

    def test_iec_table_join_keeps_unmapped_points_and_custom_addresses(self):
        self.store.create_row('fes_channel_iec104_point_define',dict(rtu_id=4,kind='yc',pnt_no=2,io_address=7101,type_id=13))
        s=IEC104Service(self.store)
        try:
            result=s.points('yc',table_view=True)
            self.assertEqual([(r['pnt_no'],r['io_address'],r['type_id']) for r in result],[(1,None,None),(2,7101,13)])
        finally:s.close()

    def test_log_file_visible_during_run_and_released_at_stop(self):
        s=BacnetServerService(self.store)
        try:
            s.configure(dict(rtu_id=3,simulation=1));s.start()
            switch=s._file_log.packet_switch
            switch.set_channel(3,True)
            s._record("PACKET","报文 RX 通道=3 HEX=01",channel_id=3)
            p=next(s._file_log.directory.rglob("scdfe_s-bacnet-server-*.log"))
            self.assertIn("报文 RX 通道=3 HEX=01",p.read_text(encoding='utf8'))
            switch.set_channel(3,False)
            s._record("PACKET","报文 TX 通道=3 HEX=02",channel_id=3)
            self.assertNotIn("HEX=02",p.read_text(encoding='utf8'))
            self.assertIn("HEX=02",s.logs(1)[0]['message'])
            s.stop()
            self.assertIsNone(s._file_log.logger._stream)
            p.rename(p.with_suffix('.closed'))
        finally:s.close()

    def test_publication_failure_distinguishes_durable_commit(self):
        s=BacnetServerService(self.store)
        try:
            s.configure(dict(rtu_id=3,simulation=1));s.start()
            with patch.object(s._engine,'update_point',side_effect=OSError('offline')):
                with self.assertRaisesRegex(ProtocolServiceError,'已写入'):s.incremental_update('yc',1,75)
            self.assertEqual(self.store.get_row('fes_yc_define',[3,1])['value'],75)
        finally:s.close()

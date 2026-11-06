"""A failed multipoint update must be invisible on both the database and the wire."""
from contextlib import closing, contextmanager
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pymodbus.client import ModbusTcpClient
from scdfe_c.web.service import ScadaStore
from scdfe_s.web.protocol_service import ModbusTcpServerService, ProtocolServiceError
from tests.test_scdfe_full_interaction import free_port


class ModbusPublicationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = ScadaStore(Path(tmp.name)/'scada.sqlite3',connection_pool_size=2)
        self.addCleanup(self.store.close)
        self.store.create_row('fes_rtu_define',dict(rtu_id=7,channel_id=17,channel_type='modbus-tcp'))
        for n in (1,2):
            self.store.create_row('fes_yc_define',dict(rtu_id=7,pnt_no=n,value=0))
        port=free_port()
        self.s=ModbusTcpServerService(self.store)
        self.addCleanup(self.s.close)
        self.s.configure(dict(rtu_id=7,bind_ip='127.0.0.1',listen_port=port))
        self.s.start()
        self.peer=ModbusTcpClient('127.0.0.1',port=port,timeout=2,retries=0)
        self.addCleanup(self.peer.close)
        self.assertTrue(self.peer.connect())
        self.s.full_update({'yc':{'1':11,'2':22}})

    def values(self):
        response=self.peer.read_holding_registers(0,count=2,device_id=1)
        self.assertFalse(response.isError())
        return response.registers,[self.store.get_row('fes_yc_define',[7,n])['value'] for n in (1,2)]

    def test_late_database_rejection_leaves_both_registers_unchanged(self):
        with self.store.connection() as con:
            con.execute("CREATE TRIGGER reject_sample BEFORE UPDATE ON fes_yc_define WHEN NEW.pnt_no=2 AND NEW.value=44 BEGIN SELECT RAISE(ABORT,'sample rejected');END")
            con.commit()
        with self.assertRaises(ProtocolServiceError):
            self.s.full_update({'yc':{'1':33,'2':44}})
        self.assertEqual(self.values(),([11,22],[11,22]))
        with self.assertRaises(ProtocolServiceError):self.s.incremental_update('yc',2,44)
        self.assertEqual(self.values(),([11,22],[11,22]))

    def test_unrepresentable_values_and_duplicate_aliases_never_publish(self):
        for batch in ({'yc':{'1':32,'2':-1}},{'yc':{'1':32,'2':2.5}},{'yc':{'1':31,'01':32}}):
            with self.subTest(batch=batch):
                with self.assertRaises(ProtocolServiceError):self.s.full_update(batch)
                self.assertEqual(self.values(),([11,22],[11,22]))

    def test_shared_holding_register_rejects_contradictory_batch(self):
        self.store.create_row('fes_yt_define',dict(rtu_id=7,pnt_no=1,value=11))
        with self.assertRaises(ProtocolServiceError):
            self.s.full_update({'yc':{'1':31},'yt':{'1':32}})
        self.assertEqual(self.values(),([11,22],[11,22]))
        self.assertEqual(self.store.get_row('fes_yt_define',[7,1])['value'],11)
        self.s.full_update({'yc':{'1':33},'yt':{'1':33}})
        self.assertEqual(self.values(),([33,22],[33,22]))
        self.assertEqual(self.store.get_row('fes_yt_define',[7,1])['value'],33)

    def test_four_kind_web_read_does_not_mix_two_committed_generations(self):
        self.store.create_row('fes_yx_define',dict(rtu_id=7,pnt_no=1,value=0))
        connect=self.store.connection
        changed=[]
        def interleave(sql):
            if 'FROM fes_yx_define WHERE' in sql and not changed:
                with closing(sqlite3.connect(self.store.db_path)) as writer:
                    writer.execute('UPDATE fes_yc_define SET value=99 WHERE rtu_id=7 AND pnt_no=1')
                    writer.execute('UPDATE fes_yx_define SET value=1 WHERE rtu_id=7 AND pnt_no=1')
                    writer.commit()
                changed.append(True)
        @contextmanager
        def observed():
            with connect() as con:
                con.set_trace_callback(interleave)
                try: yield con
                finally: con.set_trace_callback(None)
        with patch.object(self.store,'connection',observed):
            rows={(r['kind'],r['pnt_no']):r['value'] for r in self.s.points()}
        self.assertTrue(changed)
        self.assertEqual((rows['yc',1],rows['yx',1]),(11,0))
        self.assertEqual(self.store.get_row('fes_yx_define',[7,1])['value'],1)

    def test_publish_failure_reports_committed_values_without_command_retry(self):
        with patch.object(self.s._engine,'publish_updates',side_effect=OSError('offline')):
            with self.assertRaisesRegex(ProtocolServiceError,'已写入'):
                self.s.full_update({'yc':{'1':39,'2':48}})
        self.assertEqual(self.values(),([11,22],[39,48]))

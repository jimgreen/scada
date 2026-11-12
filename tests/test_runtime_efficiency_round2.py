"""Correctness boundaries for batched scans and prepared protocol samples."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sqlite3
import struct
import threading
import unittest

import scdfe_c
from client.can import CanMaster, CanFrame
from scdfe_s.web.iec104_service import IEC104Service
from scdfe_t.trans import TransRuntime
from server.modbus_mapped_server import ModbusMappedServer
from tests import test_modbus_mapped_server as modbus_contracts


class MemoryStore:
    def __init__(self, con):
        self.con = con

    @contextmanager
    def connection(self):
        with self.con:
            yield self.con


class SourceReadTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.addCleanup(self.con.close)
        for kind in ('yc', 'yx'):
            self.con.execute(f'CREATE TABLE fes_{kind}_define (rtu_id INT,pnt_no INT,value REAL,'
                             'status INT,refresh_time INT,PRIMARY KEY(rtu_id,pnt_no))')
        self.runtime = object.__new__(TransRuntime)
        self.runtime.store = MemoryStore(self.con)

    @staticmethod
    def point(rtu, no, dest, kind='yc'):
        return SimpleNamespace(kind=kind, pnt_no=dest, source_rtu_id=rtu, source_pnt_no=no)

    def test_batched_station_ranges_preserve_fanout_quality_and_timestamp(self):
        self.con.executemany('INSERT INTO fes_yc_define VALUES(?,?,?,?,?)',
                             [(rtu,no,rtu*100+no,rtu,rtu*1000+no) for rtu in (1,2) for no in range(1,6)])
        self.con.commit()
        points = [self.point(rtu,no,rtu*10+no) for rtu in (1,2) for no in (1,2,5)]
        points += [self.point(1,1,99)]
        plan = self.runtime._source_plan(points)
        self.assertEqual(len(plan), 1)
        rows = list(self.con.execute(plan[0][0], plan[0][1]))
        self.assertEqual({r[1] for r in rows}, {r*100+n for r in (1,2) for n in (1,2,5)})
        result = self.runtime._read_sources(plan, True, True)
        self.assertEqual(result[('yc',11)], (101,1,1001))
        self.assertEqual(result[('yc',21)], (201,2,2001))
        self.assertEqual(result[('yc',99)], result[('yc',11)])
        self.assertEqual(len(result), 7)

    def test_many_stations_respect_variable_limit_and_nulls(self):
        self.con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        self.con.executemany('INSERT INTO fes_yc_define VALUES(?,?,?,?,?)',
                             [(r,1,None if r==1 else r,None,None) for r in range(1,702)])
        self.con.commit()
        points = [self.point(r,1,r) for r in range(1,702)]
        plan = self.runtime._source_plan(points)
        self.assertEqual(len(plan), 4)
        result = self.runtime._read_sources(plan, True, True)
        self.assertNotIn(('yc',1), result)
        self.assertEqual(result[('yc',701)], (701,0,0))
        sparse = self.runtime._source_plan([self.point(1,n,n) for n in range(1,2002,2)])
        self.assertEqual(len(sparse), 2)
        self.runtime._read_sources(sparse, False)


class DatabaseSampleTests(unittest.TestCase):
    def test_rounded_samples_quality_retry_and_removal(self):
        con = sqlite3.connect(':memory:'); self.addCleanup(con.close)
        for kind in ('yc','yx'):
            con.execute(f'CREATE TABLE fes_{kind}_define(rtu_id INT,pnt_no INT,value REAL,status INT)')
        con.execute('INSERT INTO fes_yc_define VALUES(1,1,0.1,0)')
        native = {('yc',1):(0,0)}
        seen = []
        def snapshot(keys):
            keys = list(keys); seen.append(keys)
            return {k:native[k] for k in keys}
        def update(kind,no,value,quality,transmit):
            native[(kind,no)] = (struct.unpack('f',struct.pack('f',value))[0],quality)
        server = SimpleNamespace(rtu_id=1,running=True,active_connection_count=1,
                                 prepare_value=lambda kind,no,value:value,
                                 monitoring_snapshot=Mock(side_effect=snapshot),update=Mock(side_effect=update))
        service = SimpleNamespace(_lock=threading.RLock(),_server=server,_database_samples={})
        sync = lambda: IEC104Service._sync_database_values(service,con)
        self.assertEqual(sync(),1)
        self.assertEqual(sync(),0)  # float32 rounding must not cause repeated spontaneous reports
        self.assertEqual(seen,[[('yc',1)]])
        con.execute('UPDATE fes_yc_define SET status=128')
        server.update.side_effect = OSError('offline')
        with self.assertRaises(OSError): sync()
        self.assertEqual(service._database_samples[('yc',1)],(.1,0))
        server.update.side_effect = update
        self.assertEqual(sync(),1)
        self.assertEqual(native[('yc',1)][1],128)
        con.execute('DELETE FROM fes_yc_define'); sync()
        self.assertEqual(service._database_samples,{})


class CompiledCanTests(unittest.TestCase):
    def test_decoder_equivalence_and_reconfiguration(self):
        master = CanMaster(interface='virtual',channel='round2-unused',rtu_id=7)
        self.addCleanup(master.close)
        rows = []
        for no,(typ,bits,offset,order) in enumerate([
                ('uint',13,3,'big'),('int',13,3,'little'),('bool',1,5,'big'),
                ('float',32,0,'little'),('float',64,0,'big')],1):
            rows.append(dict(kind='yx' if typ=='bool' else 'yc',pnt_no=no,frame_id=123,
                             value_type=typ,bit_length=bits,bit_offset=offset,byte_order=order,
                             scale=.25,offset=-2,quality_byte_offset=7,quality_mask=128))
        master.configure(rows)
        # Use the actual transport frame shape, including CAN FD metadata.
        frame = CanFrame(123,b'\xff\xee\x10\x41\x00\x00\x00\x80',is_fd=True,timestamp=1.0)
        expected = [master._decode_point(frame,r,7) for r in master._points]
        self.assertEqual(master._decode_frames([frame]), expected)
        with self.assertRaises(ValueError): master._decode_frames([frame._replace(data=b'\x00')])
        master.configure([])
        self.assertEqual(master._decode_frames([frame]), [frame])


class PreparedModbusTests(unittest.TestCase):
    def test_adjacent_wide_registers_loopback(self):
        from server.modbus_trans_server import ModbusTcpTransServer
        from client.modbus_tcp import ModbusTcpMaster, ModbusError
        from tests.test_protocol_interface_contracts import free_port
        port=free_port()
        server=ModbusTcpTransServer(port=port,points=[
            dict(kind='yc',pnt_no=1,address=0,data_type='float32',register_area='input'),
            dict(kind='yc',pnt_no=2,address=2,data_type='float64',register_area='input')])
        self.addCleanup(server.close)
        server.update_many([('yc',1,1.25),('yc',2,-123.5)])
        server.start()
        peer=ModbusTcpMaster('127.0.0.1',port); self.addCleanup(peer.close); peer.connect()
        expected=list(struct.unpack('>6H',struct.pack('>fd',1.25,-123.5)))
        self.assertEqual(peer.read_input_registers(0,6),expected)
        self.assertEqual(server.update_many([('yc',1,9.5),('yc',2,456.75)]),{})
        self.assertEqual(peer.read_input_registers(0,6),list(struct.unpack('>6H',struct.pack('>fd',9.5,456.75))))
        with self.assertRaises(ModbusError): peer.read_input_registers(6,1)

    # Reuse the real TCP/serial-codec contract with encode-call instrumentation.
    def test_signed_and_wide_values_and_serial_codec(self):
        original = ModbusMappedServer._encode
        calls = []
        def encode(engine,row,value):
            calls.append((row.get('pnt_no'),value))
            return original(engine,row,value)
        with patch.object(ModbusMappedServer,'_encode',encode):
            modbus_contracts.MappedModbusContracts.test_signed_and_wide_values_and_serial_codec(self)
        for value in (-321,-987654,987654):
            self.assertEqual(sum(v==value for _,v in calls),1)


class WriterCallTests(unittest.TestCase):
    def test_adjacent_groups_batch_without_changing_order_or_versions(self):
        con=sqlite3.connect(':memory:'); self.addCleanup(con.close)
        con.execute('CREATE TABLE p(id INT PRIMARY KEY,value INT,status INT,_version INT)')
        con.execute('INSERT INTO p VALUES(1,0,0,0)'); con.commit()
        calls=[]
        class Connection:
            def __getattr__(self,name): return getattr(con,name)
            def executemany(self,sql,rows): calls.append(sql); return con.executemany(sql,rows)
        messages=[dict(op='batch',_preserve_versions=True,groups=[dict(table='p',key_names=('id',),
                    value_names=(field,),rows=[(value,1)],_unique_rows=True)])
                  for field,value in [('value',1),('value',2),('status',3),('value',4)]]
        self.assertTrue(scdfe_c._flush_batch(None,Mock(),messages,Mock(),Connection()))
        self.assertEqual(len(calls),3)
        self.assertEqual(con.execute('SELECT value,status,_version FROM p').fetchone(),(4,3,4))

"""T protocol commands use SQLite only, including atomic multi-point writes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from client.iec104 import IEC104Master
from client.modbus_tcp import ModbusTcpMaster, ModbusError
from core.command_store import DatabaseCommandWriter
from scdfe_c import ChannelWorker, ChannelSpec
from scdfe_c.web.service import ScadaStore
from scdfe_t.trans import TransPoint, TransRuntime, TransSpec


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); return sock.getsockname()[1]


class DatabaseExchangeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name); self.store=ScadaStore(self.root/'scada.sqlite3')
        self.addCleanup(self.store.close)
        self.store.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='modbus-tcp'))
        for kind in ('yk','yt'):
            for no in (1,2):
                self.store.create_row(f'fes_{kind}_define',dict(rtu_id=1,pnt_no=no,value=0,cmd_time=0,cmd_handle_time=0))
        self.runtime=TransRuntime(self.store.db_path); self.addCleanup(self.runtime.store.close)
        # The default production path must not construct or contact C's IPC.
        guard=patch('scdfe_c.writer_ipc.WriterClient',side_effect=AssertionError('cross-process IPC is forbidden'))
        guard.start(); self.addCleanup(guard.stop)

    def start(self,protocol):
        points=[TransPoint.from_row(dict(channel_id=1,kind=kind,pnt_no=no,source_rtu_id=1,
                    source_pnt_no=no,register_area='coil' if kind=='yk' else 'holding',
                    address=no-1,data_type='bool' if kind=='yk' else 'uint16',type_id=45 if kind=='yk' else 50))
                for kind in ('yk','yt') for no in (1,2)]
        listen=free_port()
        server=self.runtime.build_server(TransSpec(1,protocol,'127.0.0.1',listen,1,1,.05),points)
        server.start(); self.addCleanup(server.stop if protocol=='iec104' else server.close)
        return listen

    def tcp(self):
        peer=ModbusTcpMaster('127.0.0.1',self.start('modbus-tcp'),timeout=2)
        self.addCleanup(peer.close); peer.connect(); return peer

    def test_tcp_accepts_commands_without_c_and_rejects_pending_overwrite(self):
        peer=self.tcp()
        self.assertTrue(peer.write_multiple_coils(0,[True,True]))
        before=[self.store.get_row('fes_yk_define',[1,n]) for n in (1,2)]
        self.assertTrue(all(row['value']==1 and row['cmd_time']>row['cmd_handle_time'] for row in before))
        with self.assertRaises(ModbusError) as caught: peer.write_multiple_coils(0,[False,False])
        self.assertEqual(caught.exception.exception_code,4)
        self.assertEqual(before,[self.store.get_row('fes_yk_define',[1,n]) for n in (1,2)])
        self.assertEqual(peer.read_coils(0,2),[True,True])

    def test_failed_last_coil_rolls_back_whole_request_and_wire_values(self):
        peer=self.tcp()
        self.store.update_row('fes_yk_define',[1,2],dict(man_disable=1))
        before=[self.store.get_row('fes_yk_define',[1,n]) for n in (1,2)]
        with self.assertRaises(ModbusError) as caught: peer.write_multiple_coils(0,[True,True])
        self.assertEqual(caught.exception.exception_code,4)
        self.assertEqual(before,[self.store.get_row('fes_yk_define',[1,n]) for n in (1,2)])
        self.assertEqual(peer.read_coils(0,2),[False,False])
        self.assertEqual(self.runtime._command_count,0)

    def test_failed_last_register_range_rolls_back_whole_request(self):
        peer=self.tcp()
        self.store.update_row('fes_yt_define',[1,2],dict(eng_max=5))
        with self.assertRaises(ModbusError) as caught: peer.write_multiple_registers(0,[10,20])
        self.assertEqual(caught.exception.exception_code,4)
        for n in (1,2):
            row=self.store.get_row('fes_yt_define',[1,n])
            self.assertEqual((row['value'],row['cmd_time']),(0,0))
        self.assertEqual(peer.read_holding_registers(0,2),[0,0])
        self.assertTrue(peer.write_multiple_registers(0,[10,4]))
        self.assertEqual([self.store.get_row('fes_yt_define',[1,n])['value'] for n in (1,2)],[10,4])

    def test_iec104_uses_same_database_contract_without_c(self):
        peer=IEC104Master('127.0.0.1',self.start('iec104'),init='NONE')
        self.addCleanup(peer.close); peer.add_point('yk',1); peer.connect(timeout=3)
        peer.send_yk_point(1,True)
        row=self.store.get_row('fes_yk_define',[1,1])
        self.assertEqual(row['value'],1); self.assertGreater(row['cmd_time'],row['cmd_handle_time'])

    def test_concurrent_db_submitters_cannot_overwrite_pending_command(self):
        def submit(value):
            try: DatabaseCommandWriter(self.store).submit_command(1,'yk',1,value); return True
            except ValueError: return False
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(submit,[0,1]))
        self.assertEqual(sum(results),1)

    def test_c_worker_consumes_preexisting_database_command_on_start(self):
        calls=[]
        class Master:
            def connect(self): pass
            def close(self): pass
            def collect_once(self): return []
            def send_yk_point(self,no,value): calls.append((no,value)); return True
        DatabaseCommandWriter(self.store).submit_command(1,'yk',1,1)
        worker=ChannelWorker(self.store,ChannelSpec(1,1,'modbus-tcp'),log_dir=self.root/'logs',
                             master_factory=lambda *_:Master())
        worker.run(max_cycles=1)
        self.assertEqual(calls,[(1,True)])
        row=self.store.get_row('fes_yk_define',[1,1])
        self.assertEqual(row['status'],1); self.assertGreaterEqual(row['cmd_handle_time'],row['cmd_time'])


if __name__=='__main__': unittest.main()

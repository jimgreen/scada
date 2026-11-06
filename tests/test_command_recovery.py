"""Concurrency and recovery checks using only private databases and TCP ports."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

from scdfe_c.web.service import ScadaStore
from scdfe_c.writer_ipc import WriterClient, WriterIpcServer, WriterRequestError
from scdfe_s.web.iec104_service import IEC104Service
from scdfe_s.web.protocol_service import ModbusTcpService, ProtocolServiceError
from scdfe_s.web.server import make_server
from scdfe_t.trans import TransRuntime


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'scada.sqlite3'
        self.store = ScadaStore(self.db)
        self.addCleanup(self.store.close)

    def test_writer_replays_exact_request_but_rejects_conflicting_ids_even_in_flight(self):
        queue_ = queue.Queue()
        client = WriterClient(self.db, timeout=3)
        original = dict(op='submit_command', request_id='repeat', rtu_id=1, kind='yk', point_no=1, value=0)
        with WriterIpcServer(self.db, queue_, request_timeout=.6) as server, ThreadPoolExecutor(3) as pool:
            future = pool.submit(client.request, original)
            operation = queue_.get(timeout=2)
            duplicate = pool.submit(client.request, dict(reversed(list(original.items()))))
            try:
                with self.assertRaises(WriterRequestError) as caught:
                    client.request({**original,'point_no':2})
                self.assertEqual((caught.exception.code,caught.exception.status),('request_id_conflict',409))
            finally:
                operation.response = {'ok':True, 'item':{'point_no':1,'_version':2}}
                server.complete(operation)
                operation.done.set()
            self.assertEqual(future.result(), duplicate.result())
            self.assertEqual(client.request(original), future.result())
            with self.assertRaises(WriterRequestError) as caught:
                client.request({**original,'rtu_id':True})
            self.assertEqual(caught.exception.code,'request_id_conflict')
            self.assertTrue(queue_.empty())

    def test_failed_save_does_not_disconnect_live_modbus_or_change_configuration(self):
        from pymodbus.client import ModbusTcpClient
        svc = ModbusTcpService(self.store)
        self.addCleanup(svc.close)
        port=free_port()
        svc.configure({'mode':'server','listen_port':port})
        svc.start()
        client = ModbusTcpClient('127.0.0.1',port=port,timeout=1,retries=0)
        self.addCleanup(client.close)
        self.assertTrue(client.connect())
        self.assertEqual(client.read_holding_registers(0,count=1,device_id=1).registers,[0])
        peer=client.socket
        previous=svc.config_path.read_bytes()
        with patch.object(svc,'_persist_config',side_effect=OSError('injected disk failure')):
            with self.assertRaises(ProtocolServiceError):
                svc.configure({'listen_port':free_port()})
        self.assertTrue(svc.running)
        self.assertEqual(svc.config()['listen_port'],port)
        self.assertEqual(svc.config_path.read_bytes(),previous)
        self.assertEqual(client.read_holding_registers(0,count=1,device_id=1).registers,[0])
        self.assertIs(peer,client.socket)
        with patch.object(svc,'stop',side_effect=AssertionError('unchanged settings disconnected peer')):
            svc.configure({'listen_port':port})

    def test_invalid_one_service_config_does_not_take_down_s_web(self):
        for name,data in [('iec104_server_config.json',{'port':99999}),
                          ('modbus_tcp_service_config.json',{'remote_port':99999})]:
            (self.db.parent/name).write_text(json.dumps(data))
        web=make_server('127.0.0.1',0,db_path=self.db)
        self.addCleanup(web.server_close)
        for svc in (web.iec104_service,web.modbus_tcp_service):
            self.assertFalse(svc.running)
            self.assertTrue(any(r['level']=='ERROR' and '配置' in r['message'] for r in svc.logs()))
        self.assertEqual(json.loads((self.db.parent/'iec104_server_config.json').read_text())['port'],99999)
        for svc in (web.iec104_service,web.modbus_tcp_service):
            with self.assertRaisesRegex(RuntimeError,'请先保存有效配置'):
                svc.start()
        web.iec104_service.configure({'port':free_port()})
        self.assertIsNone(web.iec104_service.status()['last_error'])

    def test_both_command_listeners_reject_ambiguous_and_unterminated_frames(self):
        from scdfe_c.command_server import CommandServer
        writer=Mock()
        with WriterIpcServer(self.db,queue.Queue()) as ipc, CommandServer(self.db,port=0,writer=writer) as tcp:
            for server in (ipc,tcp):
                data=json.dumps(dict(token=server.token,rtu_id=1,kind='yk',point_no=1,value=0,request_id='frame')).encode()
                invalid=[(data,'invalid_json'),(data[:-1]+b',"value":1}\n','invalid_json'),
                         (data.replace(server.token.encode(),b'\\u00e9')+b'\n','unauthorized')]
                for raw,code in invalid:
                    with self.subTest(port=server.address[1],code=code), socket.create_connection(server.address,timeout=2) as s:
                        s.sendall(raw)
                        s.shutdown(socket.SHUT_WR)
                        with s.makefile('rb') as stream:
                            self.assertEqual(json.loads(stream.readline())['code'],code)
        writer.submit_command.assert_not_called()

    def test_failed_iec_command_commit_does_not_leave_partial_point_value(self):
        self.store.create_row('fes_rtu_define',{'rtu_id':1,'channel_id':1,'channel_type':'iec104'})
        self.store.create_row('fes_yk_define',{'rtu_id':1,'pnt_no':1,'value':0})
        svc=IEC104Service(self.store)
        self.addCleanup(svc.close)
        previous=self.store.get_row('fes_yk_define',[1,1])
        with self.store.connection() as con:
            con.execute("CREATE TRIGGER no_ack BEFORE UPDATE OF cmd_handle_time ON fes_yk_define BEGIN SELECT RAISE(ABORT,'test failure'); END")
        event=Mock(kind='yk',point_no=1,value=True,selected=False,originator_address=0,cause=6)
        self.assertFalse(svc._on_command(event))
        self.assertEqual(self.store.get_row('fes_yk_define',[1,1]),previous)

    def test_shortening_long_scan_is_applied_without_waiting_for_old_period(self):
        from pymodbus.client import ModbusTcpClient
        port=free_port()
        self.store.create_row('fes_scdfe_t_define',dict(channel_id=1,protocol='modbus-tcp',bind_host='127.0.0.1',
                              bind_port=port,scan_interval_ms=60000,process_enable=1))
        self.store.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='modbus-tcp'))
        self.store.create_row('fes_yc_define',dict(rtu_id=1,pnt_no=1,value=7))
        self.store.create_row('fes_scdfe_t_point_define',dict(channel_id=1,pnt_no=1,kind='yc',source_rtu_id=1,
                              source_pnt_no=1,register_area='input',address=0,data_type='uint16'))
        run=TransRuntime(self.db,require_enabled=True)
        thread=threading.Thread(target=run.run)
        thread.start()
        self.addCleanup(lambda:(run.stop_event.set(),thread.join(4)))
        until=time.monotonic()+4
        while self.store.get_row('fes_scdfe_t_define',[1])['process_status']!=2:
            self.assertLess(time.monotonic(),until)
            time.sleep(.02)
        client=ModbusTcpClient('127.0.0.1',port=port,timeout=1,retries=0)
        self.addCleanup(client.close)
        self.assertTrue(client.connect())
        peer=client.socket
        def read():
            return client.read_input_registers(0,count=1,device_id=1).registers[0]
        self.assertEqual(read(),7)
        self.store.update_row('fes_yc_define',[1,1],{'value':37})
        self.store.update_row('fes_scdfe_t_define',[1],{'scan_interval_ms':50})
        until=time.monotonic()+3
        while read()!=37:
            self.assertLess(time.monotonic(),until,'still waiting for the original 60-second interval')
            time.sleep(.02)
        self.assertIs(client.socket,peer)

    def test_failed_rebind_restores_previous_modbus_listener_and_disk_settings(self):
        from pymodbus.client import ModbusTcpClient
        svc = ModbusTcpService(self.store)
        self.addCleanup(svc.close)
        port=free_port()
        svc.configure({'mode':'server','listen_port':port})
        svc.start()
        previous=json.loads(svc.config_path.read_text())
        bad_port=free_port()
        old_build=svc._build_engine
        def build():
            if svc.config()['listen_port']==bad_port:
                raise OSError("injected bind failure")
            return old_build()
        with patch.object(svc,'_build_engine',side_effect=build):
            with self.assertRaisesRegex(ProtocolServiceError,'已恢复原配置'):
                svc.configure({'listen_port':bad_port})
        self.assertEqual(json.loads(svc.config_path.read_text()),previous)
        self.assertTrue(svc.running)
        client=ModbusTcpClient('127.0.0.1',port=port,timeout=1,retries=0)
        self.addCleanup(client.close)
        self.assertTrue(client.connect())
        self.assertEqual(client.read_holding_registers(0,count=1,device_id=1).registers,[0])

    def test_active_default_iec_station_does_not_write_to_a_new_lower_numbered_rtu(self):
        from client.iec104 import IEC104Master
        port=free_port()
        def add(rtu):
            self.store.create_row('fes_rtu_define',dict(rtu_id=rtu,channel_id=rtu,channel_type='iec104'))
            self.store.create_row('fes_channel_iec104_define',dict(id=rtu,rtu_id=rtu,rtu_addr=1,
                                  remote_host='127.0.0.1',remote_port=port))
            self.store.create_row('fes_yk_define',dict(rtu_id=rtu,pnt_no=1,value=0))
            self.store.create_row('fes_channel_iec104_point_define',dict(rtu_id=rtu,kind='yk',pnt_no=1,
                                  io_address=0x6001,type_id=1))
        add(7)
        svc=IEC104Service(self.store)
        self.addCleanup(svc.close)
        svc.configure({'port':port})
        svc.start()
        client=IEC104Master('127.0.0.1',port,init='NONE')
        self.addCleanup(client.close)
        client.add_point('yk',1)
        client.connect(timeout=4)
        add(1)
        changed=self.store.get_row('fes_yk_define',[7,1])
        other=self.store.get_row('fes_yk_define',[1,1])
        self.assertEqual(svc.status()['config']['effective_rtu_id'],7)
        client.send_yk_point(1,True)
        now=self.store.get_row('fes_yk_define',[7,1])
        self.assertEqual((now['value'],now['_version'],now['cmd_handle_time']),
                         (1,changed['_version']+1,now['refresh_time']))
        self.assertEqual(self.store.get_row('fes_yk_define',[1,1]),other)

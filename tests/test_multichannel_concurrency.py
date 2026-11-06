"""Concurrent channel ownership, optimistic writes, wire values and log isolation."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from client.iec104 import IEC104Master
from pymodbus.client import ModbusTcpClient
from scdfe_c.web.server import make_server as cweb
from scdfe_s.web.server import make_server as sweb
from scdfe_t.web.server import make_server as tweb
from scdfe_s.web.iec104_service import IEC104Service
from scdfe_t.web.service import TransStore
from scdfe_t.trans import TransRuntime
from scdfe_c.web.log_query import search_logs
from core.packet_logging import packet_log_switch


class ChannelConcurrencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'scada.sqlite3'
        self.t = TransStore(self.db, connection_pool_size=2)
        self.addCleanup(self.t.store.close)

    def test_slow_control_and_heartbeat_do_not_block_other_operator_edits(self):
        for cid in (1, 10):
            self.t.create_channel(dict(channel_id=cid, code=str(cid), protocol='iec104'))
        runtime = TransRuntime(self.db, channel_id=10)
        self.addCleanup(runtime.store.close)
        runtime._heartbeat()  # initial transition into RUNNING
        row = self.t.get_channel(10)
        with ThreadPoolExecutor(2) as pool:
            with self.t._control(1):
                runtime._next_heartbeat = 1
                pool.submit(runtime._heartbeat).result(2)
                result = pool.submit(self.t.update_channel, 10, {'name':'edited'}, row['_version']).result(2)
        self.assertEqual(result['_version'], row['_version'] + 1)
        self.assertEqual(self.t.get_channel(1)['name'], '1')

    def test_old_station_callback_keeps_original_policy_and_search_identity(self):
        store = self.t.store
        callbacks = []
        for rid, channel in ((7,1), (8,10)):
            store.create_row('fes_rtu_define',dict(rtu_id=rid,channel_id=channel,channel_type='iec104'))
            store.create_row('fes_channel_iec104_define',dict(id=rid,rtu_id=rid,rtu_addr=1,remote_host='127.0.0.1',remote_port=24100+rid))
        svc = IEC104Service(store)
        self.addCleanup(svc.close)
        with patch('server.iec104_server.IEC104Server.set_raw_handlers',
                   lambda self, **kw: callbacks.append(kw)):
            for rid in (7,8):
                svc.configure(dict(rtu_id=rid))
                server = svc._build_server()
                self.addCleanup(server.stop)
        switch = packet_log_switch(self.db,'s')
        switch.set_channel(1,True)
        switch.set_channel(10,False)
        callbacks[0]['on_receive'](b'\x11')
        callbacks[1]['on_receive'](b'\x22')
        switch.set_channel(10,True)
        callbacks[1]['on_send'](b'\xaa')
        # An older, untagged packet merely mentions a channel in its payload.
        svc._file_log.write('PACKET', '报文 RX legacy 通道=1 HEX=ff',channel_id=1)
        for cid, direction, payload in [(1,'RX','11'), (10,'TX','aa')]:
            result = search_logs(self.db.parent/'logs',dict(channel_id=str(cid),record='packet',direction=direction),family='s')
            self.assertEqual([r['text'].split('HEX=')[-1] for r in result['items']], [payload])
        self.assertFalse(search_logs(self.db.parent/'logs',dict(channel_id='10',direction='RX'),family='s')['items'])
        self.assertEqual(len(search_logs(self.db.parent/'logs',dict(record='packet'),family='s')['items']),3)
        with self.assertRaises(ValueError):
            search_logs(self.db.parent/'logs',dict(channel_id='10.0'),family='s')

    def test_four_live_forwarders_parallel_writes_reads_and_channel_logs(self):
        webs = {}
        for f, factory in zip('cst',(cweb,sweb,tweb)):
            srv = factory('127.0.0.1',0,db_path=self.db.with_name('scdfe_s.sqlite3') if f=='s' else self.db)
            thread = threading.Thread(target=srv.serve_forever,daemon=True)
            thread.start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            webs[f] = srv.server_port

        def api(f, path, method='GET', data=None, headers=None, accept=(200,)):
            req = Request(f'http://127.0.0.1:{webs[f]}/api/'+path, method=method,
                          data=None if data is None else json.dumps(data).encode(),
                          headers={'Content-Type':'application/json', **(headers or {})})
            try: response = urlopen(req,timeout=20)
            except HTTPError as exc: response = exc
            with response:
                obj = json.load(response)
                self.assertIn(response.status,accept,(f,path,obj))
                return response.status,obj

        cids=(1,10,11,100)
        starts = {}
        for cid in cids:
            rid=1000+cid
            api('c','rtus','POST',dict(rtu_id=rid,channel_id=cid,channel_type='iec104'),accept=(201,))
            api('c',f'rtus/{rid}/points/yc','POST',dict(pnt_no=1,value=cid),accept=(201,))
            api('s','channels','POST',dict(rtu_id=rid,channel_id=cid,channel_type='iec104'),accept=(201,))
            api('s',f'channels/{rid}/points/yc','POST',dict(pnt_no=1,value=0),accept=(201,))
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0))
                port=probe.getsockname()[1]
            protocol='iec104' if cid in (1,11) else 'modbus-tcp'
            api('t','trans/channels','POST',dict(channel_id=cid,code=f'C{cid}',protocol=protocol,
                common_address=cid,bind_host='127.0.0.1',bind_port=port,scan_interval_ms=50),accept=(201,))
            settings=dict(io_address=0x4001,type_id=13) if protocol=='iec104' else dict(register_area='input',data_type='uint16',address=0)
            api('t',f'trans/channels/{cid}/points','POST',dict(kind='yc',pnt_no=1,source_rtu_id=rid,source_pnt_no=1,**settings),accept=(201,))
            api('t',f'packet-recording/channels/{cid}','PUT',dict(enabled=cid in (1,10)))
            starts[cid]=(port,protocol)
            self.addCleanup(api,'t',f'trans/channels/{cid}/stop','POST',{})
        with ThreadPoolExecutor(max_workers=4) as pool:
            launched=list(pool.map(lambda cid:api('t',f'trans/channels/{cid}/start','POST',{})[1]['item'],cids))
            self.assertEqual(len({r['process_pid'] for r in launched}),4)
            peers={}
            for cid,(port,proto) in starts.items():
                if proto=='iec104':
                    peer=IEC104Master('127.0.0.1',port,common_address=cid,init='NONE')
                    peer.add_point('yc',1)
                    self.addCleanup(peer.close)
                    peer.connect(timeout=5)
                else:
                    peer=ModbusTcpClient('127.0.0.1',port=port,timeout=2)
                    self.addCleanup(peer.close)
                    self.assertTrue(peer.connect())
                peers[cid]=peer

            def see(cid,target):
                peer=peers[cid]
                if starts[cid][1]=='iec104':
                    self.assertTrue(peer.general_interrogation())
                until=time.monotonic()+4
                while time.monotonic()<until:
                    if starts[cid][1]=='iec104': value=peer.value('yc',1).value
                    else:
                        result=peer.read_input_registers(0,count=1,device_id=1)
                        self.assertFalse(result.isError(),result)
                        value=result.registers[0]
                    if value==target:return
                    time.sleep(.02)
                self.fail((cid,target,value))

            list(pool.map(see,cids,cids))
            barrier=threading.Barrier(4)
            def write_read(cid):
                barrier.wait(3)
                rid=1000+cid
                path=f'rtus/{rid}/points/yc/1'
                for seq in range(1,4):
                    row=api('c',path)[1]['item']
                    value=100*cid+seq
                    api('s',f'channels/{rid}/points/yc/1','PATCH',dict(value=-value))
                    api('c',path,'PATCH',dict(value=value),{'If-Match':str(row['_version'])})
                    see(cid,value)
            list(pool.map(write_read,cids))
            # Two clients race on the same C row: exactly one commits.
            version=api('c','rtus/1001/points/yc/1')[1]['item']['_version']
            race=threading.Barrier(2)
            def contender(f):
                race.wait(3)
                path='rtus/1001/points/yc/1'
                return api('c',path,'PATCH',dict(value=f),{'If-Match':str(version)},(200,409))[0]
            statuses=list(pool.map(contender,(700,800)))
            self.assertEqual(api('s','channels/1001/points/yc/1')[1]['item']['value'],-103)
            self.assertEqual(sorted(statuses),[200,409])
            see(1,700 if statuses[0]==200 else 800)
            for cid in cids:
                for direction in ('RX','TX'):
                    rows=api('t',f'log-files/search?process=scdfe_t-channel-{cid}&direction={direction}&record=packet')[1]['items']
                    self.assertEqual(bool(rows),cid in (1,10),(cid,direction))
                    self.assertTrue(all(Path(r['filename']).name.startswith(f'scdfe_t-channel-{cid}-') for r in rows))
            api('t','trans/channels/11/stop','POST',{})
            self.assertEqual([r['channel_id'] for r in api('t','trans/processes')[1]['items'] if r['alive']],[1,10,100])
            list(pool.map(see,(10,100),(1003,10003)))

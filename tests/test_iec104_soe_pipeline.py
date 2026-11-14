"""Real TCP tests for the journal path; all databases and ports are isolated."""
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
import socket
import tempfile
import time
import unittest

import c104
from client.iec104 import IEC104Master
from core.iec104_soe import decode_events
from scdfe_c.web.service import ScadaStore
from scdfe_s.database import SimulatorStore
from scdfe_s.web.iec104_service import IEC104Service
from scdfe_t.trans import TransRuntime
from server.iec104_server import IEC104Server
from tools.verify_iec104_soe import EPOCH, cp56, fill_one, port
from tools.verify_iec104_wire_contracts import RawPeer


def wait(predicate, timeout=8):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate(): return
        time.sleep(.01)
    raise AssertionError('condition timed out')


def events(count, *, typ=30, first=0):
    result=[]
    for i in range(first,first+count):
        raw=cp56(EPOCH+timedelta(milliseconds=i),invalid=bool(i%2),
                 substituted=bool(i%3),summer=bool(i%5))
        asdu=bytes((typ,1,3,7,1,0,1,0,0,(i%(4 if typ==31 else 2))|0xb0))+raw
        result.extend(decode_events(asdu))
    return result


def stored(store, origin='C'):
    with store.connection() as con:
        return [dict(row) for row in con.execute(
            'SELECT * FROM _scada_iec104_soe WHERE origin=? ORDER BY event_id',(origin,))]


@unittest.skipUnless(hasattr(c104.Server,'submit_soe'),'requires bundled c104 scada.3')
class SOEPipelineTests(unittest.TestCase):
    def test_batch_waits_for_every_recipient_and_rejects_overlap(self):
        with ExitStack() as stack:
            server=IEC104Server(ip='127.0.0.1',port=port());stack.callback(server.stop)
            server.add_point('yx',1,data_type=30,value=False);server.start()
            self.assertFalse(server.submit_soe(events(1)))
            fast=IEC104Master('127.0.0.1',server.port,init='NONE',w=1,t2=1)
            stack.callback(fast.close);fast.add_point('yx',1,data_type=30);fast.connect(timeout=4)
            sock=socket.create_connection(('127.0.0.1',server.port),timeout=3);stack.callback(sock.close)
            raw=RawPeer(sock);raw.u(7);raw.until(lambda f:f[2]==11)
            self.assertTrue(server.submit_soe(events(128)))
            self.assertFalse(server.submit_soe(events(1)))
            time.sleep(.1)
            self.assertEqual(server.poll_soe(),0)
            count=0
            while count<128:
                frame=raw.receive()
                if not frame[2]&1:count+=len(decode_events(frame[6:]))
            wait(lambda:server.poll_soe()==1)
            self.assertEqual(count,128)
            with self.assertRaises(ValueError):server._server.submit_soe([b'bad'])

    def test_batched_repeated_ioa_and_identical_asdus_are_not_coalesced(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            store=ScadaStore(Path(root)/'c.sqlite3');stack.callback(store.close)
            server=IEC104Server(ip='127.0.0.1',port=port());stack.callback(server.stop)
            fill_one(store,server.port)
            with store.connection() as con:
                con.execute('UPDATE fes_channel_iec104_point_define SET type_id=31')
                con.execute('UPDATE fes_channel_iec104_define SET w=1,t2=1')
            server.add_point('yx',1,data_type=31,value=False);server.start()
            peer=IEC104Master.from_store(store,1,init='NONE');stack.callback(peer.close)
            peer.connect(timeout=4)
            peer.general_interrogation()
            self.assertEqual(stored(store), [])
            self.assertFalse(peer.value('yx',1).value)
            source=events(101,typ=31)
            self.assertTrue(server.submit_soe(source))
            wait(lambda:len(stored(store))==101)
            wait(lambda:server.poll_soe()==1)
            # Same content is a second event, not a duplicate frame heuristic.
            self.assertTrue(server.submit_soe(source[:1]))
            wait(lambda:len(stored(store))==102)
            rows=stored(store)
            keys=('raw_state','quality','cp56_hex','cot','originator','time_invalid','time_substituted','summer')
            self.assertEqual([tuple(r[k] for k in keys) for r in rows],
                             [tuple(r[k] for k in keys) for r in source+source[:1]])
            self.assertEqual(peer._soe_receiver.count,102)
            self.assertIsNone(peer._soe_error)

    def test_s_database_burst_and_offline_changes_recover_after_restart(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            root=Path(root)
            s=SimulatorStore(root/'s.sqlite3');stack.callback(s.close)
            c=ScadaStore(root/'c.sqlite3');stack.callback(c.close)
            listen=port();fill_one(s,listen);fill_one(c,listen)
            with c.connection() as con: con.execute('UPDATE fes_channel_iec104_define SET w=1,t2=1')
            service=IEC104Service(s,config_path=root/'s.json');stack.callback(service.close)
            service.configure(dict(rtu_id=1,port=listen));service.start()
            peer=IEC104Master.from_store(c,1,init='NONE');stack.callback(peer.close);peer.connect(timeout=4)
            with service._lock:
                for i in range(21): s.update_row('fes_yx_define',[1,1],{'value':(i+1)%2})
                s.update_row('fes_yx_define',[1,1],{'status':128})
            wait(lambda:len(stored(c))==22)
            wait(lambda:service._soe_forwarder.last_event_id==22)
            peer.close();service.stop()
            for i in range(7): s.update_row('fes_yx_define',[1,1],{'value':i%2})
            service.start()
            peer2=IEC104Master.from_store(c,1,init='NONE');stack.callback(peer2.close);peer2.connect(timeout=4)
            wait(lambda:len(stored(c))==29)
            wait(lambda:service._soe_forwarder.last_event_id==29)
            self.assertEqual([(r['raw_state'],r['quality']) for r in stored(c)],
                             [(r['raw_state'],r['quality']) for r in stored(s,'S')])

    def test_t_database_forward_preserves_bytes_and_cursor_on_disconnect(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            root=Path(root);store=ScadaStore(root/'c.sqlite3');stack.callback(store.close)
            source=IEC104Server(ip='127.0.0.1',port=port());stack.callback(source.stop)
            fill_one(store,source.port)
            with store.connection() as con:
                con.execute('UPDATE fes_channel_iec104_define SET w=1,t2=1')
                con.execute("INSERT INTO fes_scdfe_t_define(channel_id,protocol,bind_host,bind_port,common_address,scan_interval_ms,report_interval_ms) VALUES(1,'iec104','127.0.0.1',?,2,50,60000)",(port(),))
                con.execute("INSERT INTO fes_scdfe_t_point_define(channel_id,kind,pnt_no,source_rtu_id,source_pnt_no,io_address,type_id,enabled) VALUES(1,'yx',1,1,1,77,30,1)")
            source.add_point('yx',1,data_type=30,value=False);source.start()
            peer=IEC104Master.from_store(store,1,init='NONE');stack.callback(peer.close);peer.connect(timeout=4)
            expected=events(101)
            source.submit_soe(expected)
            wait(lambda:len(stored(store))==101)
            runtime=TransRuntime(store.db_path,channel_id=1,log_dir=root/'logs')
            stack.callback(runtime.store.close)
            stack.callback(lambda: runtime._soe_forwarder.close() if runtime._soe_forwarder else None)
            stack.callback(lambda: runtime.server.stop() if runtime.server else None)
            spec,points,_=runtime.load();runtime.rebuild(spec,points)
            sock=socket.create_connection(('127.0.0.1',spec.bind_port),timeout=3)
            raw=RawPeer(sock);raw.u(7);raw.until(lambda f:f[2]==11)
            runtime._pump_soe()
            # No I-frame receive/ACK yet, so a second poll must not advance.
            time.sleep(.1);runtime._pump_soe()
            self.assertEqual(runtime._soe_forwarder.last_event_id,0)
            sock.close()
            wait(lambda:runtime.server.active_connection_count==0)
            runtime._pump_soe()
            self.assertEqual(runtime._soe_forwarder.last_event_id,0)
            runtime.rebuild(spec,points)
            sock=socket.create_connection(('127.0.0.1',spec.bind_port),timeout=3);stack.callback(sock.close)
            raw=RawPeer(sock);raw.u(7);raw.until(lambda f:f[2]==11);runtime._pump_soe()
            observed=[]
            while len(observed)<101:
                frame=raw.receive()
                if not frame[2]&1: observed.extend(decode_events(frame[6:]))
            wait(lambda:(runtime._pump_soe() or runtime._soe_forwarder.last_event_id)==101)
            self.assertEqual(len(observed),101)
            self.assertEqual([r['cp56_hex'] for r in observed],[r['cp56_hex'] for r in expected])
            self.assertEqual([r['raw_state'] for r in observed],[r['raw_state'] for r in expected])
            self.assertTrue(all(r['common_address']==2 and r['io_address']==77 for r in observed))
            # Rebuild reuses durable progress: acknowledged events are not replayed.
            runtime.rebuild(spec,points);runtime._pump_soe()
            self.assertEqual(runtime._soe_forwarder.last_event_id,101)
            self.assertIsNone(runtime._soe_forwarder.pending)


if __name__=='__main__': unittest.main()

"""Isolated S DB -> TCP -> C journal -> T DB cursor -> TCP SOE acceptance."""
import argparse
from contextlib import ExitStack
from datetime import timedelta
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from client.iec104 import IEC104Master
from core.iec104_soe import SoeReceiver, _cp56, decode_events, encode_events
from scdfe_c.web.service import ScadaStore
from scdfe_s.database import SimulatorStore
from scdfe_s.web.iec104_service import IEC104Service
from scdfe_t.trans import TransRuntime
from tools.verify_iec104_soe import Capture, EPOCH, cp56, port


def fill(store, count, ports):
    with store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        for i in range(1,count+1):
            con.execute("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type,process_enable) VALUES(?,?,'iec104',0)",(i,i))
            con.execute("INSERT INTO fes_channel_iec104_define(id,channel_id,rtu_addr,remote_host,remote_port,w,t2) VALUES(?,?,1,'127.0.0.1',?,1,1)",(i,i,ports[i-1]))
            con.execute('INSERT INTO fes_yx_define(rtu_id,pnt_no,value,status) VALUES(?,1,0,0)',(i,))
            con.execute("INSERT INTO fes_channel_iec104_point_define(channel_id,kind,pnt_no,io_address,type_id) VALUES(?,'yx',1,1,30)",(i,))
        con.commit()


def pipeline(channels, events):
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root=Path(directory);s=SimulatorStore(root/'s.sqlite3');c=ScadaStore(root/'c.sqlite3')
        stack.callback(s.close);stack.callback(c.close)
        ports=[port() for _ in range(channels)];fill(s,channels,ports);fill(c,channels,ports)
        services=[];runtimes=[];captures=[]
        for i in range(1,channels+1):
            service=IEC104Service(s,config_path=root/f's-{i}.json');stack.callback(service.close)
            service.configure(dict(rtu_id=i,port=ports[i-1]));service.start();services.append(service)
            peer=IEC104Master.from_store(c,i,init='NONE');stack.callback(peer.close);peer.connect(timeout=5)
            with c.connection() as con:
                con.execute("INSERT INTO fes_scdfe_t_define(channel_id,protocol,bind_host,bind_port,common_address,scan_interval_ms,report_interval_ms) VALUES(?,'iec104','127.0.0.1',?,1,50,60000)",(i,port()))
                con.execute("INSERT INTO fes_scdfe_t_point_define(channel_id,kind,pnt_no,source_rtu_id,source_pnt_no,io_address,type_id,enabled) VALUES(?,'yx',1,?,1,1,30,1)",(i,i))
            runtime=TransRuntime(c.db_path,channel_id=i,log_dir=root/'logs');stack.callback(runtime.store.close)
            stack.callback(lambda rt=runtime: rt._soe_forwarder.close() if rt._soe_forwarder else None)
            stack.callback(lambda rt=runtime: rt.server.stop() if rt.server else None)
            spec,points,_=runtime.load();runtime.rebuild(spec,points);runtimes.append(runtime)
            down=IEC104Master('127.0.0.1',spec.bind_port,init='NONE',w=1,t2=1)
            stack.callback(down.close);down.add_point('yx',1,data_type=30)
            capture=Capture();down.set_packet_handler(capture);captures.append(capture);down.connect(timeout=5)
        # A whole burst is committed while scans are held; snapshots alone
        # cannot represent this workload (an even number returns to initial 0).
        with ExitStack() as locks:
            for service in services: locks.enter_context(service._lock)
            begin=time.perf_counter()
            with s.connection() as con:
                con.execute('BEGIN IMMEDIATE')
                for seq in range(events):
                    con.executemany('UPDATE fes_yx_define SET value=?,status=? WHERE rtu_id=? AND pnt_no=1',
                        [((seq+1)%2,(seq%2)*16,i) for i in range(1,channels+1)])
                con.commit()
            commit=time.perf_counter()
        expected=channels*events;deadline=time.monotonic()+90
        complete=False
        while time.monotonic()<deadline:
            for runtime in runtimes: runtime._pump_soe()
            with c.connection() as con:
                received=con.execute("SELECT COUNT(*) FROM _scada_iec104_soe WHERE origin='C'").fetchone()[0]
                cursors=dict(con.execute('SELECT consumer,last_event_id FROM _scada_iec104_soe_cursor'))
                maximum=dict(con.execute('SELECT rtu_id,MAX(event_id) FROM _scada_iec104_soe GROUP BY rtu_id'))
            if received==expected and all(cursors.get(f't:channel:{i}',0)==maximum.get(i) for i in range(1,channels+1)):
                complete=True;break
            time.sleep(.005)
        finish=time.perf_counter()
        # Packet trace hooks are scheduled asynchronously; give them bounded drain time.
        for _ in range(100):
            if sum(len(cap.events()) for cap in captures)>=expected: break
            time.sleep(.01)
        with s.connection() as con:
            source=[dict(r) for r in con.execute('SELECT * FROM _scada_iec104_soe ORDER BY event_id')]
        with c.connection() as con:
            incoming=[dict(r) for r in con.execute('SELECT * FROM _scada_iec104_soe ORDER BY event_id')]
        correct=complete;latencies=[];counts=[]
        for i,cap in enumerate(captures,1):
            original=[r for r in source if r['rtu_id']==i]
            persisted=[r for r in incoming if r['rtu_id']==i]
            wire=cap.events();counts.append(len(wire))
            reference=[(r['raw_state'],r['quality'],_cp56(r).hex()) for r in original]
            correct &= [(r['raw_state'],r['quality'],r['cp56_hex']) for r in persisted]==reference
            correct &= [(r['state'],r['quality'],r['cp56_hex']) for r in wire]==reference
            latencies.extend((r['observed_at']-commit)*1000 for r in wire)
        ordered=sorted(latencies)
        return dict(channels=channels,events_per_channel=events,expected=expected,
                    c_persisted=received,t_received=counts,exact=bool(correct),
                    trigger_transaction_ms=round((commit-begin)*1000,3),
                    end_to_end_ack_ms=round((finish-commit)*1000,3),
                    events_per_second=round(expected/(finish-commit),2),
                    commit_to_downstream_median_ms=round(statistics.median(ordered),3) if ordered else None,
                    commit_to_downstream_p95_ms=round(ordered[min(len(ordered)-1,int(len(ordered)*.95))],3) if ordered else None)


def write_comparison(events=512, rounds=3):
    rows=[]
    for i in range(events):
        rows.extend(decode_events(bytes((30,1,3,0,1,0,1,0,0,i%2))+cp56(EPOCH+timedelta(milliseconds=i))))
    result={}
    for name,groups in [('one_event_per_asdu',[[r] for r in rows]),('packed_asdu',[rows])]:
        samples=[]
        for _ in range(rounds):
            with tempfile.TemporaryDirectory() as directory:
                store=ScadaStore(Path(directory)/'c.sqlite3');receiver=SoeReceiver(store,1,1,{(1,30):1})
                try:
                    frames=[b'\x68'+bytes((len(a)+4,))+b'\0'*4+a for g in groups for a in encode_events(g)]
                    start=time.perf_counter()
                    for frame in frames: receiver.receive(SimpleNamespace(raw=frame))
                    elapsed=(time.perf_counter()-start)*1000
                    assert receiver.count==events
                    samples.append(elapsed)
                finally: receiver.close();store.close()
        result[name]=dict(events=events,transactions=len(frames),median_ms=round(statistics.median(samples),3),runs_ms=[round(x,3) for x in samples])
    result['speedup']=round(result['one_event_per_asdu']['median_ms']/result['packed_asdu']['median_ms'],2)
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--events',type=int,default=512)
    parser.add_argument('--channels',type=int,nargs='+',default=[1,4,8])
    parser.add_argument('--skip-db',action='store_true');args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    result=dict(scope='temporary S independent DB; C/T shared DB; real TCP; w=1 t2=1; S 50ms tick; T controlled 5ms pump; no hardware or long soak',pipelines=[])
    if not args.skip_db:
        result['database_write']=write_comparison()
        print(json.dumps(result['database_write']),flush=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    for channels in args.channels:
        item=pipeline(channels,args.events);result['pipelines'].append(item)
        print(json.dumps(item),flush=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if all(r['exact'] for r in result['pipelines']) else 1


if __name__=='__main__':sys.exit(main())

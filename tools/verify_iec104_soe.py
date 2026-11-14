"""SOE acceptance audit, distinct from latest-value/ordinary spontaneous tests.

Uses temporary databases and real loopback TCP. Observers in this file are test
instrumentation, not a production event queue. Exit 1 means SOE requirements
failed; exit 2 means the audit itself could not finish.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import c104
from client.iec104 import IEC104Master
from server.iec104_server import IEC104Server
from scdfe_c import ChannelSpec, ChannelWorker
from scdfe_c.web.service import ScadaStore
from scdfe_s.database import SimulatorStore
from scdfe_s.web.iec104_service import IEC104Service
from scdfe_t.trans import TransRuntime
from tools.verify_iec104_pipeline import port, wait_for, stats
from tools.verify_iec104_wire_contracts import RawPeer

EPOCH = datetime(2024, 2, 29, 23, 59, 59, 900000, tzinfo=timezone.utc)


def utc_text(stamp):
    # c104/pybind returns local-naive datetimes. This normalization belongs to
    # the audit oracle, not the production public snapshot API.
    return stamp.astimezone(timezone.utc).isoformat()


def cp56(stamp, *, invalid=False, summer=False, substituted=False):
    """Independent encoder; tests agree to interpret CP56 calendar as UTC."""
    return struct.pack('<HBBBBB', stamp.second*1000 + stamp.microsecond//1000,
        stamp.minute | (128 if invalid else 0) | (64 if substituted else 0),
        stamp.hour | (128 if summer else 0), stamp.day | stamp.isoweekday()<<5,
        stamp.month, stamp.year-2000)


def decode(frame):
    """Independent SP/DP decoder, including SQ, COT, QDS and raw CP56 flags."""
    if len(frame)<12 or frame[2]&1 or frame[6] not in (1,3,30,31):
        return []
    assert frame[0]==104 and frame[1]+2==len(frame)
    typ,vsq=frame[6:8]; count=vsq&127; sequential=bool(vsq&128)
    offset=12; result=[]; base=None
    for index in range(count):
        if not sequential or index==0:
            base=int.from_bytes(frame[offset:offset+3],'little');offset+=3
        ioa=base+index if sequential else base
        siq=frame[offset];offset+=1
        row=dict(type=typ,ca=int.from_bytes(frame[10:12],'little'),ioa=ioa,
                 cot=frame[8]&63,test=bool(frame[8]&128),negative=bool(frame[8]&64),
                 state=siq&(3 if typ in (3,31) else 1),quality=siq&240)
        if typ in (30,31):
            raw=frame[offset:offset+7];assert len(raw)==7;offset+=7
            ms,minute,hour,day,month,year=struct.unpack('<HBBBBB',raw)
            row.update(source_time=datetime(2000+(year&127),month&15,day&31,
                hour&31,minute&63,ms//1000,(ms%1000)*1000,tzinfo=timezone.utc).isoformat(),
                time_invalid=bool(minute&128),time_substituted=bool(minute&64),
                summer=bool(hour&128),cp56_hex=raw.hex())
        result.append(row)
    assert offset==len(frame), (offset,len(frame))
    return result


class Capture:
    def __init__(self):self.frames=[]
    def __call__(self,direction,data,detail=''):
        self.frames.append((time.perf_counter(),direction,bytes(data)))
    def events(self,mark=0,cot=3):
        return [dict(event,observed_at=stamp) for stamp,direction,frame in self.frames[mark:]
                if direction=='RX' for event in decode(frame) if event['cot']==cot]


class ObservedMaster(IEC104Master):
    """Observes callbacks while preserving the production snapshot path."""
    def __init__(self,*args,**kwargs):
        self.observed=[]
        super().__init__(*args,**kwargs)
    def _make_receive_callback(self,kind,point_no):
        original=super()._make_receive_callback(kind,point_no)
        def receive(point:c104.Point, previous_info:c104.Information,
                    message:c104.IncomingMessage)->c104.ResponseState:
            result=original(point,previous_info,message)
            with self._lock:sample=self._received_values[(kind,point_no)]
            self.observed.append((time.perf_counter(),sample))
            return result
        return receive


def point_info(typ,state,quality,stamp):
    # Explicit fixture boundary matching pybind chrono's local-naive contract.
    # The separate timezone_probe below deliberately omits this conversion.
    stamp=stamp.astimezone().replace(tzinfo=None)
    if typ==30:return c104.SingleInfo(on=bool(state),quality=c104.Quality(quality),recorded_at=stamp)
    return c104.DoubleInfo(state=c104.Double(state),quality=c104.Quality(quality),recorded_at=stamp)


def emit(point,typ,state,quality,stamp):
    point.info=point_info(typ,state,quality,stamp)
    begin=time.perf_counter()
    assert point.transmit(c104.Cot.SPONTANEOUS)
    return begin


def fixture(stack,*,ca=1,klass=ObservedMaster):
    server=IEC104Server(ip='127.0.0.1',port=port(),common_address=ca,tick_rate_ms=50)
    stack.callback(server.stop)
    points=[server.add_point('yx',no,data_type=typ,value=False) for no,typ in ((1,30),(2,31))]
    peer=klass('127.0.0.1',server.port,common_address=ca,rtu_id=ca,init='NONE')
    stack.callback(peer.close)
    peer.configure([dict(kind='yx',pnt_no=no,type_id=typ) for no,typ in ((1,30),(2,31))])
    wire=Capture();peer.set_packet_handler(wire)
    server.start();peer.connect(timeout=5)
    return server,points,peer,wire


def wire_correctness():
    with ExitStack() as stack:
        _,points,peer,wire=fixture(stack,ca=7)
        expected=[]
        # Milliseconds, leap-day rollover, same-ms changes and backwards clocks.
        stamps=[EPOCH+timedelta(milliseconds=n) for n in (0,1,100,100,99,86400101)]
        for no,(point,typ) in enumerate(zip(points,(30,31)),1):
            states=(0,1,0,1,0,1) if typ==30 else (1,2,0,3,1,2)
            for index,(stamp,state) in enumerate(zip(stamps,states)):
                quality=(0,16,32,64,128,0)[index]
                expected.append(dict(type=typ,ca=7,ioa=no,state=state,quality=quality,
                                     source_time=stamp.isoformat()))
                emit(point,typ,state,quality,stamp)
        wait_for(lambda:len(peer.observed)==len(expected),'all timed callbacks')
        wait_for(lambda:len(wire.events())==len(expected),'all timed raw observers')
        actual=wire.events()
        raw_ok=all(all(row[k]==v for k,v in target.items()) for row,target in zip(actual,expected))
        callback_ok=True
        for (_,sample),target in zip(peer.observed,expected):
            state=target['state']; typ=target['type']
            value=bool(state) if typ==30 else {0:None,1:False,2:True,3:None}[state]
            quality=target['quality'] | (128 if typ==31 and state in (0,3) else 0)
            callback_ok &= (sample.point_no,sample.value,sample.quality,utc_text(sample.recorded_at)) == (
                target['ioa'],value,quality,target['source_time'])
        snapshots=peer.collect_once()
        return dict(raw_exact=raw_ok,callback_value_quality_time_exact=bool(callback_ok),
                    sent=len(expected),raw_received=len(actual),callbacks=len(peer.observed),
                    public_snapshot_count=len(snapshots),raw_events=actual,
                    public_fields=list(snapshots[0]._fields),
                    raw_double_state_exposed='raw_state' in snapshots[0]._fields)


def timezone_probe():
    with ExitStack() as stack:
        _,points,peer,wire=fixture(stack)
        points[0].info=c104.SingleInfo(on=True,recorded_at=EPOCH)
        assert points[0].transmit(c104.Cot.SPONTANEOUS)
        wait_for(lambda:bool(wire.events()) and bool(peer.observed),'aware timestamp probe')
        actual=wire.events()[0]['source_time']
        sample=peer.observed[0][1]
        return dict(requested_utc=EPOCH.isoformat(),wire_time=actual,
            offset_seconds=(datetime.fromisoformat(actual)-EPOCH).total_seconds(),
            public_recorded_at=str(sample.recorded_at),public_has_timezone=sample.recorded_at.tzinfo is not None,
            aware_input_preserved=actual==EPOCH.isoformat(),
            scope='direct native Information constructor with UTC-aware input; no fixture conversion')


def independent_input():
    with ExitStack() as stack, ThreadPoolExecutor(1) as pool:
        listener=stack.enter_context(socket.socket());listener.bind(('127.0.0.1',0));listener.listen()
        listener.settimeout(4)
        peer=ObservedMaster('127.0.0.1',listener.getsockname()[1],init='NONE');stack.callback(peer.close)
        peer.configure([dict(kind='yx',pnt_no=1,type_id=30)])
        wire=Capture();peer.set_packet_handler(wire)
        future=pool.submit(peer.connect,4);sock,_=listener.accept();stack.callback(sock.close)
        raw=RawPeer(sock);raw.until(lambda f:f[2]==7);raw.u(11);future.result(5)
        variants=[{},dict(invalid=True),dict(summer=True),dict(substituted=True)]
        samples=[]
        for index,flags in enumerate(variants):
            before=len(peer.observed);stamp=EPOCH+timedelta(milliseconds=index)
            asdu=bytes((30,1,3,0,1,0,1,0,0,1))+cp56(stamp,**flags)
            raw.asdu(asdu);wait_for(lambda:len(peer.observed)>before,'independent CP56 input')
            sample=peer.observed[-1][1]
            samples.append(dict(flags=flags,recorded_at=str(sample.recorded_at),quality=sample.quality,
                exposed_time_flag_fields=[f for f in sample._fields if f in ('time_invalid','summer','time_substituted')]))
        before=len(peer.observed)
        raw.asdu(bytes((30,1,20,0,1,0,1,0,0,1))+cp56(EPOCH))
        wait_for(lambda:len(peer.observed)>before,'GI-cause input')
        return dict(samples=samples,raw=raw.frames,
                    time_flags_exposed=all(bool(s['exposed_time_flag_fields']) for s in samples),
                    public_cot_exposed='cot' in peer.observed[-1][1]._fields,
                    note='COT=20 is a controlled incoming ASDU, not a complete GI exchange')


def transport_benchmark(channels,events,rounds):
    with ExitStack() as stack:
        rigs=[fixture(stack,ca=n+1) for n in range(channels)]
        durations=[];latencies=[];first=None;last=None;complete=True;received=0
        for repeat in range(rounds):
            marks=[len(peer.observed) for _,_,peer,_ in rigs]
            sent={};start=time.perf_counter()
            for seq in range(events):
                no=seq%2;typ=(30,31)[no];state=(seq//2)%2+(1 if typ==31 else 0)
                for channel,(_,points,_,_) in enumerate(rigs):
                    stamp=EPOCH+timedelta(milliseconds=repeat*events+seq)
                    sent[channel,stamp.isoformat()]=(emit(points[no],typ,state,0,stamp),no+1,state,typ)
            wait_for(lambda:all(len(peer.observed)>=mark+events for (_,_,peer,_),mark in zip(rigs,marks)),
                     f'{channels} channels timed event batch',timeout=30)
            end=time.perf_counter();durations.append((end-start)*1000)
            first=start if first is None else first;last=end
            for channel,((_,_,peer,_),mark) in enumerate(zip(rigs,marks)):
                observed=peer.observed[mark:];received+=len(observed)
                complete &= len(observed)==events
                expected_times=[(EPOCH+timedelta(milliseconds=repeat*events+n)).isoformat() for n in range(events)]
                complete &= [utc_text(sample.recorded_at) for _,sample in observed]==expected_times
                for at,sample in observed:
                    begin,no,state,typ=sent[channel,utc_text(sample.recorded_at)]
                    complete &= sample.point_no==no and sample.quality==0 and sample.value==bool(state if typ==30 else state-1)
                    latencies.append((at-begin)*1000)
        expected=channels*events*rounds
        return dict(channels=channels,events_per_channel_per_batch=events,rounds=rounds,
            expected=expected,received=received,complete=bool(complete and received==expected),
            throughput_events_s=round(received/(last-first),2),batch_latency=stats(durations),
            send_to_callback_latency=stats(latencies),
            scope='S adapter -> TCP -> C receive callback; test observer, not database SOE throughput')


def overload():
    with ExitStack() as stack:
        server=IEC104Server(ip='127.0.0.1',port=port(),tick_rate_ms=50);stack.callback(server.stop)
        point=server.add_point('yx',1,data_type=30,value=False);server.start()
        sock=socket.create_connection(('127.0.0.1',server.port),timeout=3);stack.callback(sock.close)
        raw=RawPeer(sock);raw.u(7);raw.until(lambda f:f[2]==11)
        count=16384;begin=time.perf_counter()
        # Intentionally withhold I-frame acknowledgements during the entire burst.
        for seq in range(count):emit(point,30,seq%2,0,EPOCH+timedelta(milliseconds=seq))
        deadline=time.monotonic()+30
        idle=False
        while time.monotonic()<deadline:
            try:raw.receive(timeout=1)
            except socket.timeout:
                idle=True;break
        assert idle, 'overload receiver still making progress at deadline'
        observed=[e for r in raw.frames if r['direction']=='RX' for e in decode(bytes.fromhex(r['hex']))]
        times=[e['source_time'] for e in observed]
        expected={(EPOCH+timedelta(milliseconds=n)).isoformat() for n in range(count)}
        return dict(submitted=count,received=len(observed),unique_received=len(set(times)),
                    missing=len(expected-set(times)),duplicates=len(times)-len(set(times)),
                    lossless=len(times)==count and set(times)==expected,
                    elapsed_ms=round((time.perf_counter()-begin)*1000,3),raw=raw.frames,
                    scope='native queue overload with delayed ACK; not sustainable-throughput measurement')


def fill_one(store,listen):
    with store.connection() as con:
        con.execute("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type,process_enable) VALUES(1,1,'iec104',0)")
        con.execute('INSERT INTO fes_channel_iec104_define(id,channel_id,rtu_addr,remote_host,remote_port) VALUES(1,1,1,?,?)',('127.0.0.1',listen))
        con.execute('INSERT INTO fes_yx_define(rtu_id,pnt_no,value,status) VALUES(1,1,0,0)')
        con.execute("INSERT INTO fes_channel_iec104_point_define(channel_id,kind,pnt_no,io_address,type_id) VALUES(1,'yx',1,1,30)")
        con.commit()


def database_boundaries(directory):
    with ExitStack() as stack:
        root=Path(directory)
        s=SimulatorStore(root/'scdfe_s.sqlite3');stack.callback(s.close)
        c=ScadaStore(root/'scada.sqlite3');stack.callback(c.close)
        listen=port();fill_one(s,listen);fill_one(c,listen)
        service=IEC104Service(s,config_path=root/'s.json');stack.callback(service.close)
        service.configure(dict(rtu_id=1,port=listen));service.start()
        peer=ObservedMaster.from_store(c,1,init='NONE');stack.callback(peer.close)
        wire=Capture();peer.set_packet_handler(wire);peer.connect(timeout=4);peer.general_interrogation()
        wait_for(lambda:bool(peer.observed),'S initial GI');wire_mark=len(wire.frames)
        # Hold the existing scan lock, simulating one delayed scan. Each DB
        # update still commits separately; no HMI-to-protocol update is called.
        with service._lock:
            start=time.perf_counter()
            for seq in range(21):s.update_row('fes_yx_define',[1,1],dict(value=(seq+1)%2))
            commit_ms=(time.perf_counter()-start)*1000
        wait_for(lambda:peer.value('yx',1).value is True,'S final state after 21 commits')
        time.sleep(.2)
        s_changes=wire.events(wire_mark)

        worker=ChannelWorker(c,ChannelSpec(1,1,'iec104'),log_dir=root/'logs',poll_interval=.05)
        stack.callback(worker._close_command_connection)
        # Exercise actual C writeback at a controlled scan boundary. The native
        # callbacks still process every incoming event before this scan.
        initial=len(peer.observed);mark=len(wire.frames)
        point=service._server._points[('yx',1)]
        with service._lock:
            for seq in range(101):emit(point,30,seq%2,0,EPOCH+timedelta(milliseconds=seq))
            wait_for(lambda:len(peer.observed)>=initial+101,'all C ingress SOE frames')
            snapshots=peer.collect_once();worker._sync_measurements(snapshots)
        time.sleep(.1)
        c_row=c.get_row('fes_yx_define',[1,1])
        with c.connection() as con:
            tables=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            con.execute("INSERT INTO fes_scdfe_t_define(channel_id,protocol,bind_host,bind_port,common_address,scan_interval_ms,report_interval_ms) VALUES(1,'iec104','127.0.0.1',?,1,50,60000)",(port(),))
            con.execute("INSERT INTO fes_scdfe_t_point_define(channel_id,kind,pnt_no,source_rtu_id,source_pnt_no,io_address,type_id,enabled) VALUES(1,'yx',1,1,1,1,30,1)")
            con.commit()
        runtime=TransRuntime(c.db_path,channel_id=1,log_dir=root/'logs');stack.callback(runtime.store.close)
        spec,points,_=runtime.load();runtime.rebuild(spec,points);stack.callback(lambda: runtime.server.stop())
        down=ObservedMaster('127.0.0.1',spec.bind_port,init='NONE');stack.callback(down.close)
        down.configure([dict(kind='yx',pnt_no=1,type_id=30)])
        downstream=Capture();down.set_packet_handler(downstream);down.connect(timeout=4);down.general_interrogation()
        wait_for(lambda:bool(down.observed),'T initial GI')
        dmark=len(downstream.frames)
        start=time.perf_counter()
        # A known historic event time reaches C, then is written through C/T DB.
        stamp=EPOCH+timedelta(seconds=10)
        with service._lock:
            initial=len(peer.observed);emit(point,30,1,16,stamp)
            wait_for(lambda:len(peer.observed)>initial,'single source-time probe')
            sample=peer.collect_once()[0];worker._sync_measurements([sample]);runtime.refresh()
            wait_for(lambda:down.value('yx',1).quality==16,'T source-time probe')
        wait_for(lambda:bool(downstream.events(dmark)),'T raw time probe')
        c_row=c.get_row('fes_yx_define',[1,1])
        probe_ms=round((time.perf_counter()-start)*1000,3)
        forwarded=downstream.events(dmark)[0]
        # Restart T on the same DB: it can retrieve the latest state, not 101 events.
        down.close();runtime.server.stop();runtime.rebuild(spec,points)
        reconnect=ObservedMaster('127.0.0.1',spec.bind_port,init='NONE');stack.callback(reconnect.close)
        reconnect.configure([dict(kind='yx',pnt_no=1,type_id=30)])
        reconnect.connect(timeout=4);reconnect.general_interrogation()
        wait_for(lambda:bool(reconnect.observed),'T restart snapshot')
        return dict(s_commits=21,s_spontaneous_events=len(s_changes),s_commit_ms=round(commit_ms,3),
            c_incoming_events=101,c_collected_snapshots=len(snapshots),
            c_source_time=sample.recorded_at.isoformat(),c_db_refresh_time=c_row.get('refresh_time'),
            event_tables=[name for name in tables if 'soe' in name.lower() or 'event' in name.lower()],
            source_time=stamp.isoformat(),t_forwarded_time=forwarded.get('source_time'),
            source_time_preserved=forwarded.get('source_time')==stamp.isoformat(),
            t_restart_samples=len(reconnect.observed),snapshot_probe_ms=probe_ms,
            s_frames=s_changes,c_frames=wire.events(mark),t_frames=downstream.events(dmark),
            scope='controlled scan windows using real S sync, C writeback and T refresh; no event journal created')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--channels',type=int,nargs='+',default=[1,4,8])
    parser.add_argument('--events',type=int,default=512)
    parser.add_argument('--rounds',type=int,default=3)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    if min(*args.channels,args.events,args.rounds)<1:parser.error('positive counts required')
    result=dict(timestamp=datetime.now(timezone.utc).isoformat(),c104=importlib.metadata.version('c104'),
                python=sys.version,native_sha256=hashlib.sha256(Path(importlib.util.find_spec('c104._c104').origin).read_bytes()).hexdigest())
    code=2
    try:
        result['wire']=wire_correctness();print('wire',result['wire']['raw_exact'],flush=True)
        result['timezone']=timezone_probe()
        result['independent']=independent_input();print('independent input complete',flush=True)
        result['performance']=[]
        for channels in args.channels:
            row=transport_benchmark(channels,args.events,args.rounds)
            result['performance'].append(row);print(json.dumps(row),flush=True)
        result['overload']=overload();print('overload received',result['overload']['received'],flush=True)
        with tempfile.TemporaryDirectory(prefix='iec104-soe-') as directory:
            result['database']=database_boundaries(directory)
        w,d,i,o=result['wire'],result['database'],result['independent'],result['overload']
        result['requirements']=[
            dict(name='CP56单点/双点值、质量、毫秒时标与线上顺序',passed=w['raw_exact'] and w['callback_value_quality_time_exact']),
            dict(name='原生接口直接接收UTC-aware时间不偏移',passed=result['timezone']['aware_input_preserved']),
            dict(name='受控负载下逐条报文完整接收',passed=all(p['complete'] for p in result['performance'])),
            dict(name='CP56时间有效性/替代/夏令时标志向业务层暴露',passed=i['time_flags_exposed']),
            dict(name='业务样本保留COT以区分总召与变位事件',passed=i['public_cot_exposed']),
            dict(name='S逐次数据库变位均生成事件',passed=d['s_spontaneous_events']==d['s_commits']),
            dict(name='C逐条事件持久化',passed=d['c_collected_snapshots']==d['c_incoming_events'] and bool(d['event_tables'])),
            dict(name='C到T保留原始事件时标',passed=d['source_time_preserved']),
            dict(name='SOE业务记录保留双点四种原始状态',passed=w['raw_double_state_exposed']),
            dict(name='具备重启后恢复事件历史的数据基础',passed=bool(d['event_tables'])),
            dict(name='队列超载不静默丢事件',passed=o['lossless']),
        ]
        result['audit_completed']=True
        result['soe_accepted']=all(r['passed'] for r in result['requirements'])
        code=0 if result['soe_accepted'] else 1
    except Exception:
        result.update(audit_completed=False,soe_accepted=False,error=traceback.format_exc())
        print(result['error'],file=sys.stderr)
    finally:
        (args.output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('audit_completed','soe_accepted','requirements') if k in result},ensure_ascii=False))
    return code


if __name__=='__main__':sys.exit(main())

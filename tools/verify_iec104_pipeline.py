"""Real TCP IEC104 acceptance/performance with disposable C/S databases.

Run from the repository root; never opens the operator databases or ports.
Uses production S polling, C ChannelWorker + writer process, and T runtime.
Latency includes real scheduling and SQLite commit; it is not a wire limit.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from datetime import datetime
import importlib.metadata
import json
import math
import multiprocessing as mp
from pathlib import Path
import socket
import statistics
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client.iec104 import IEC104Master, IEC104Error
from core.iec104_protocol import parse_apdu
from scdfe_c import ChannelSpec, ChannelWorker, _batch_writer_entry, BATCH_WRITER_STOP
from scdfe_c.web.service import ScadaStore, SCHEMA_DIR
from scdfe_s.database import SimulatorStore
from scdfe_s.web.iec104_service import IEC104Service
from scdfe_t.trans import TransRuntime
from server.iec104_server import IEC104Server


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_for(predicate, label, timeout=8):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return time.perf_counter()
        time.sleep(.005)
    raise AssertionError(f'Timeout: {label}')


def stats(values):
    ordered = sorted(values)
    return dict(samples=len(values), median_ms=round(statistics.median(values), 3),
                p95_ms=round(ordered[math.ceil(len(values)*.95)-1], 3),
                max_ms=round(max(values), 3))


class Wire:
    def __init__(self):
        self.frames = []

    def __call__(self, direction, data, detail=''):
        self.frames.append((time.perf_counter(), direction, bytes(data)))

    def monitoring(self, since=0, cot=3):
        return [(t, b) for t, d, b in self.frames[since:]
                if d == 'RX' and len(b) > 12 and b[2] & 1 == 0
                and b[6] in (1, 13) and b[8] & 63 == cot]

    def validate(self):
        sequences = {}
        counts = {'I': 0, 'S': 0, 'U': 0}
        for _, direction, frame in self.frames:
            parse_apdu(frame)
            kind = 'I' if frame[2] & 1 == 0 else 'S' if frame[2] & 3 == 1 else 'U'
            counts[kind] += 1
            # The harness reconnects with a new transport after S restarts.
            if kind == 'U' and direction == 'TX' and frame[2] == 7:
                sequences.clear()
            if kind == 'I':
                seq = int.from_bytes(frame[2:4], 'little') >> 1
                if direction in sequences:
                    assert seq == (sequences[direction] + 1) % 32768, (direction, seq, sequences)
                sequences[direction] = seq
        return counts


def gi(peer, wire, expected, ca):
    start = len(wire.frames)
    begin = time.perf_counter()
    assert peer.general_interrogation()
    wait_for(lambda: any(d == 'RX' and len(b) > 12 and b[2] & 1 == 0
                         and b[6] == 100 and b[8] & 63 == 10
                         for _, d, b in wire.frames[start:]), 'GI termination')
    relevant = [(t, b) for t, d, b in wire.frames[start:]
                if d == 'RX' and len(b) > 12 and b[2] & 1 == 0]
    confirm = next(t for t, b in relevant if b[6] == 100 and b[8] & 63 == 7)
    termination = next(t for t, b in relevant if b[6] == 100 and b[8] & 63 == 10)
    data = [(t, b) for t, b in relevant if b[6] in (1, 13) and b[8] & 63 == 20]
    assert sum(b[7] & 127 for _, b in data) == expected
    assert all(confirm <= t <= termination for t, _ in data)
    assert all(int.from_bytes(b[10:12], 'little') == ca and not b[8] & 64 for _, b in relevant)
    return (termination - begin)*1000


def fill(store, channels, points, ports, *, initial):
    # Fixture construction in one transaction, not timed workload.
    with store.connection() as con:
        for rtu, listen in enumerate(ports, 1):
            con.execute('INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type,process_enable) VALUES(?,?,?,0)',
                        (rtu, rtu, 'iec104'))
            con.execute('INSERT INTO fes_channel_iec104_define(id,channel_id,rtu_addr,remote_host,remote_port) VALUES(?,?,?,?,?)',
                        (rtu, rtu, rtu, '127.0.0.1', listen))
            for kind, typ, base in [('yc', 13, 0x4001), ('yx', 1, 1), ('yk', 45, 0x6001), ('yt', 50, 0x6201)]:
                for no in range(1, (points if kind in ('yc', 'yx') else 2) + 1):
                    value = rtu*1000+no if initial and kind == 'yc' else 0
                    con.execute(f'INSERT INTO fes_{kind}_define(rtu_id,pnt_no,value,status) VALUES(?,?,?,0)', (rtu, no, value))
                    con.execute('INSERT INTO fes_channel_iec104_point_define(channel_id,kind,pnt_no,io_address,type_id,command_mode,report_ms) VALUES(?,?,?,?,?,?,0)',
                                (rtu, kind, no, base+no-1, typ, 'SELECT_AND_EXECUTE' if no == 2 and kind in ('yk','yt') else 'DIRECT'))
        con.commit()


def pipeline(channels, points, rounds, output):
    result = dict(channels=channels, yc_per_rtu=points, yx_per_rtu=points,
                  c_poll_ms=50, s_scan_ms=50, s_tick_ms=100, t_scan_ms=50,
                  t_tick_ms=50, t_periodic_ms=60000, writer_batch_ms=100, k=12, w=8,
                  topology='S service threads -> C ChannelWorker threads -> C writer process -> SQLite -> T runtime threads -> downstream IEC104 clients')
    wires = []
    with tempfile.TemporaryDirectory(prefix='iec104-pipeline-') as directory, ExitStack() as stack:
        root = Path(directory)
        s = SimulatorStore(root/'scdfe_s.sqlite3', connection_pool_size=2)
        c = ScadaStore(root/'scada.sqlite3', connection_pool_size=2)
        stack.callback(s.close); stack.callback(c.close)
        s_ports, t_ports = [port() for _ in range(channels)], [port() for _ in range(channels)]
        fill(s, channels, points, s_ports, initial=True)
        fill(c, channels, points, s_ports, initial=False)
        with c.connection() as con:
            for rtu, listen in enumerate(t_ports, 1):
                con.execute('INSERT INTO fes_scdfe_t_define(channel_id,protocol,bind_host,bind_port,common_address,scan_interval_ms,report_interval_ms) VALUES(?,?,?,?,?,50,60000)',
                            (rtu, 'iec104', '127.0.0.1', listen, rtu))
                for kind, typ, base in [('yc',13,0x4001),('yx',1,1),('yk',45,0x6001),('yt',50,0x6201)]:
                    con.executemany('INSERT INTO fes_scdfe_t_point_define(channel_id,kind,pnt_no,source_rtu_id,source_pnt_no,io_address,type_id,command_mode,enabled) VALUES(?,?,?,?,?,?,?,?,1)',
                                    [(rtu,kind,no,rtu,no,base+no-1,typ,'SELECT_AND_EXECUTE' if no==2 and kind in ('yk','yt') else 'DIRECT')
                                     for no in range(1,(points if kind in ('yc','yx') else 2)+1)])
            con.commit()
        ctx = mp.get_context('spawn')
        queue, ready = ctx.JoinableQueue(10000), ctx.Event()
        writer = ctx.Process(target=_batch_writer_entry, args=(str(c.db_path),str(SCHEMA_DIR),str(root/'logs'),queue,.1,500,ready))
        writer.start()
        def stop_writer():
            if writer.is_alive():
                queue.put(BATCH_WRITER_STOP, timeout=5); writer.join(15)
            if writer.is_alive():
                writer.terminate(); writer.join(5)
                raise AssertionError('private writer did not stop')
            assert writer.exitcode == 0, writer.exitcode
            queue.close(); queue.join_thread()
        stack.callback(stop_writer)
        assert ready.wait(15), 'private writer startup'
        errors, workers, services, runtimes, peers = [], [], [], [], []
        def background(fn, event):
            def run():
                try: fn()
                except Exception: errors.append(traceback.format_exc())
            thread = threading.Thread(target=run, daemon=True); thread.start()
            def stop():
                event.set(); thread.join(15)
                assert not thread.is_alive(), 'private runtime did not stop'
            stack.callback(stop)
        for rtu in range(1,channels+1):
            service = IEC104Service(s, config_path=root/f's-{rtu}.json')
            service.configure(dict(rtu_id=rtu,port=s_ports[rtu-1])); service.start()
            stack.callback(service.close); services.append(service)
            worker_store = ScadaStore(c.db_path, connection_pool_size=1)
            stack.callback(worker_store.close)
            stop = threading.Event()
            worker = ChannelWorker(worker_store,ChannelSpec(rtu,rtu,'iec104'),log_dir=root/'logs',
                                   poll_interval=.05,retry_interval=.1,stop_event=stop,writer_queue=queue)
            wire = Wire(); wires.append((f'C-{rtu}',wire))
            original = worker.logger.packet
            def record(direction,data,detail='', w=wire, original=original):
                w(direction,data,detail); original(direction,data,detail)
            worker.logger.packet = record
            workers.append(worker); background(worker.run,stop)
        def c_matches(expected):
            with c.connection() as con:
                for kind in ('yc','yx'):
                    rows = con.execute(f'SELECT rtu_id,pnt_no,value,status FROM fes_{kind}_define').fetchall()
                    if any((row[2],row[3]) != expected[(row[0],kind,row[1])] for row in rows): return False
            return True
        expected = {(r,k,n):(r*1000+n if k=='yc' else 0,0)
                    for r in range(1,channels+1) for k in ('yc','yx') for n in range(1,points+1)}
        wait_for(lambda:c_matches(expected),'C startup GI committed')
        for rtu in range(1,channels+1):
            stop = threading.Event()
            runtime = TransRuntime(c.db_path, channel_id=rtu, log_dir=root/'logs', stop_event=stop)
            runtimes.append(runtime); background(runtime.run,stop)
            wait_for(lambda: runtime.server is not None and runtime.server.running,'T listening')
            peer = IEC104Master('127.0.0.1',t_ports[rtu-1],common_address=rtu,init='NONE')
            stack.callback(peer.close)
            peer.configure([dict(kind=k,pnt_no=n,type_id=typ,command_mode='SELECT_AND_EXECUTE' if n==2 and k in ('yk','yt') else 'DIRECT')
                            for k,typ in [('yc',13),('yx',1),('yk',45),('yt',50)]
                            for n in range(1,(points if k in ('yc','yx') else 2)+1)])
            wire = Wire(); wires.append((f'T-{rtu}',wire)); peer.set_packet_handler(wire)
            peer.connect(timeout=5); peers.append((peer,wire))
        gi_times = [gi(peer,wire,points*2,r) for _ in range(5) for r,(peer,wire) in enumerate(peers,1)]
        result['t_gi'] = stats(gi_times)
        result['s_gi'] = []
        for rtu in range(1,channels+1):
            probe = IEC104Master.from_store(c,rtu,init='NONE')
            wire = Wire(); probe.set_packet_handler(wire)
            try:
                probe.connect(timeout=5)
                result['s_gi'].extend(gi(probe,wire,points*2,rtu) for _ in range(5))
                wire.validate()
            finally: probe.close()
        result['s_gi'] = stats(result['s_gi'])
        def peer_matches(target):
            return all((peer.value(k,n).value,peer.value(k,n).quality)==target[(r,k,n)]
                       for r,(peer,_) in enumerate(peers,1) for k in ('yc','yx') for n in range(1,points+1))
        assert peer_matches(expected), 'GI values and per-RTU isolation'
        result['checks'] = ['C startup GI commit','S/T GI ACTCON -> exact object count -> ACTTERM',
                            'CA/IOA mapping and RTU isolation','5 GI repeats per S/T channel']
        latencies, c_latencies, bursts = [], [], []
        workload_start = time.perf_counter()
        for step in range(1,rounds+1):
            mark = [len(w.frames) for _,w in peers]
            updates=[]
            for r,k,n in expected:
                value = r*1000+n+step*10 if k=='yc' else step%2
                expected[(r,k,n)] = value,0
                updates.append((f'fes_{k}_define',[r,n],dict(value=value,status=0)))
            s.update_rows(updates)
            begin = time.perf_counter()  # after successful commit, no HMI protocol call
            try:
                c_end = wait_for(lambda:c_matches(expected),f'C burst {step}')
                end = wait_for(lambda:peer_matches(expected),f'T burst {step}')
            except Exception:
                failure=output/f'failure-{channels}ch-burst-{step}'
                failure.mkdir(parents=True,exist_ok=True)
                mismatch=[]
                with c.connection() as con:
                    for kind in ('yc','yx'):
                        for row in con.execute(f'SELECT rtu_id,pnt_no,value,status FROM fes_{kind}_define'):
                            target=expected[(row[0],kind,row[1])]
                            if (row[2],row[3])!=target:
                                worker=workers[row[0]-1]
                                master=worker._master
                                mismatch.append(dict(rtu=row[0],kind=kind,no=row[1],database=list(row[2:]),expected=target,
                                    client=str(master.value(kind,row[1])) if master else None))
                (failure/'state.json').write_text(json.dumps(dict(mismatch=mismatch,
                    workers=[vars(w.stats) for w in workers],background_errors=errors),ensure_ascii=False,indent=2),encoding='utf-8')
                for db in (s.db_path,c.db_path):
                    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(failure/Path(db).name)) as dest:
                        source.backup(dest)
                if (root/'logs').exists(): shutil.copytree(root/'logs',failure/'logs',dirs_exist_ok=True)
                with (failure/'wire.jsonl').open('w',encoding='utf-8') as stream:
                    for name,wire in wires:
                        for stamp,direction,data in list(wire.frames):
                            stream.write(json.dumps(dict(link=name,monotonic=stamp,direction=direction,hex=data.hex()))+'\n')
                raise
            wait_for(lambda: sum(b[7]&127 for (_,w),m in zip(peers,mark)
                                  for _,b in w.monitoring(m)) >= len(expected), 'burst raw callback completion')
            c_latencies.append((c_end-begin)*1000); latencies.append((end-begin)*1000)
            frames = [b for (_,w),m in zip(peers,mark) for _,b in w.monitoring(m)]
            assert sum(b[7]&127 for b in frames)==len(expected), 'each changed point exactly once on T wire'
            bursts.append(dict(objects=len(expected),frames=len(frames),bytes=sum(map(len,frames))))
        result.update(c_commit_latency=stats(c_latencies),end_to_end_latency=stats(latencies),
                      c_commit_samples_ms=c_latencies,end_to_end_samples_ms=latencies,
                      bursts=bursts,workload_seconds=round(time.perf_counter()-workload_start,3))
        for quality in (128,16,0):
            quality_marks=[len(w.frames) for _,w in peers]
            updates=[]
            for key,(value,_) in expected.items():
                r,k,n=key; expected[key]=value,quality
                updates.append((f'fes_{k}_define',[r,n],dict(status=quality)))
            s.update_rows(updates)
            wait_for(lambda:c_matches(expected),'C quality-only')
            wait_for(lambda:peer_matches(expected),'T quality-only')
            wait_for(lambda: sum(b[7]&127 for (_,w),m in zip(peers,quality_marks)
                                  for _,b in w.monitoring(m)) >= len(expected), 'quality raw callback completion')
        result['checks'].append('quality-only IV/BL/restoration with unchanged values')
        marks=[len(w.frames) for _,w in peers]
        time.sleep(.4)
        quiet_frames = [b.hex() for (_,w),m in zip(peers,marks) for _,b in w.monitoring(m)]
        assert not quiet_frames, f'unchanged points resent spontaneously: {quiet_frames}'
        result['checks'].append('no spontaneous duplicates during 400ms quiet window')
        command_times=[]; receipt_times=[]
        for rtu,(peer,wire) in enumerate(peers,1):
            for kind in ('yk','yt'):
                for no in (1,2):
                    value = True if kind=='yk' else 42.5+no
                    begin=time.perf_counter(); mark=len(wire.frames)
                    getattr(peer,f'send_{kind}_point')(no,value)
                    end=wait_for(lambda: s.get_row(f'fes_{kind}_define',[rtu,no])['value']==value,'T -> shared DB -> C -> S command')
                    command_times.append((end-begin)*1000)
                    def acknowledged():
                        row=c.get_row(f'fes_{kind}_define',[rtu,no])
                        return (row['value']==value and row['status']==1
                                and (row.get('cmd_handle_time') or 0)>=(row.get('cmd_time') or 0)>0)
                    receipt=wait_for(acknowledged,'C command completion committed')
                    receipt_times.append((receipt-begin)*1000)
                    sent=[b for _,d,b in wire.frames[mark:] if d=='TX' and len(b)>12 and b[2]&1==0 and b[6] in (45,50)]
                    assert len(sent)==(1 if no==1 else 2)
                    assert [bool(b[-1]&128) for b in sent]==([False] if no==1 else [True,False])
                    before=s.get_row(f'fes_{kind}_define',[rtu,no])['cmd_handle_time']
                    submitted=c.get_row(f'fes_{kind}_define',[rtu,no])['cmd_time']
                    cancel_mark=len(wire.frames)
                    try:
                        getattr(peer,f'cancel_{kind}_point')(no,value)
                    except IEC104Error:
                        assert no==2, 'only an already consumed selection should reject cancel'
                    else:
                        assert no==1, 'completed SBO command must not be selected again by cancel'
                    time.sleep(.15)
                    canceled=[b for _,d,b in wire.frames[cancel_mark:] if d=='RX' and len(b)>12
                              and b[2]&1==0 and b[6] in (45,50)]
                    assert any(b[8]&127==(9 if no==1 else 9|64) for b in canceled),[b.hex() for b in canceled]
                    assert s.get_row(f'fes_{kind}_define',[rtu,no])['cmd_handle_time']==before
                    assert c.get_row(f'fes_{kind}_define',[rtu,no])['cmd_time']==submitted
        result['command_latency']=stats(command_times)
        result['command_receipt_latency']=stats(receipt_times)
        result['checks'].append('YK/YT direct and select-execute through shared DB; cancel causes no second execution')
        # Existing connection supports period edits without listener replacement.
        for runtime in runtimes: runtime.server.set_monitor_report_interval(200)
        marks=[len(w.frames) for _,w in peers]
        wait_for(lambda: all(sum(b[7]&127 for _,b in w.monitoring(m,cot=1))>=points*2
                            for (_,w),m in zip(peers,marks)), 'T cyclic COT=1')
        for runtime in runtimes: runtime.server.set_monitor_report_interval(60000)
        result['checks'].append('periodic report COT=1 on existing T connection')
        assert all(w.stats.errors==0 for w in workers), [w.stats for w in workers]
        result['errors_before_injected_disconnect']=0
        # Real upstream loss: only the private S service is stopped/restarted.
        restart_mark=len(wires[0][1].frames)
        services[0].stop()
        wait_for(lambda: workers[0]._master is None or not workers[0]._master.connected,'C sees upstream disconnect')
        updates=[]
        for key,(value,quality) in expected.items():
            r,k,n=key
            if r==1:
                value=value+123 if k=='yc' else 1-int(value)
                expected[key]=value,quality
                updates.append((f'fes_{k}_define',[r,n],dict(value=value)))
        s.update_rows(updates)
        begin=time.perf_counter(); services[0].start()
        wait_for(lambda:c_matches(expected),'C reconnect GI committed',timeout=15)
        end=wait_for(lambda:peer_matches(expected),'T receives C reconnect state',timeout=15)
        result['upstream_restart_recovery_ms']=round((end-begin)*1000,3)
        c_after=wires[0][1].frames[restart_mark:]
        assert any(d=='TX' and len(b)>12 and b[2]&1==0 and b[6]==100 for _,d,b in c_after)
        result['checks'].append('S stop/restart -> C automatic reconnect + GI -> T latest values')
        # New TCP session must retrieve the latest data by GI.
        peer,wire=peers[0]; peer.close()
        reconnect=IEC104Master('127.0.0.1',t_ports[0],common_address=1,init='NONE')
        stack.callback(reconnect.close)
        for kind in ('yc','yx'):
            for no in range(1,points+1): reconnect.add_point(kind,no)
        newwire=Wire(); reconnect.set_packet_handler(newwire); reconnect.connect(timeout=5)
        gi(reconnect,newwire,points*2,1)
        assert all((reconnect.value(k,n).value,reconnect.value(k,n).quality)==expected[(1,k,n)]
                   for k in ('yc','yx') for n in range(1,points+1))
        wires.append(('T-reconnect',newwire))
        result['checks'].append('downstream reconnect + GI retrieves latest values/qualities')
        assert not errors, errors
        result['workers']=[vars(w.stats).copy() for w in workers]
        result['frames']={name: wire.validate() for name,wire in wires}
        output.mkdir(parents=True,exist_ok=True)
        with (output/f'wire-{channels}ch.jsonl').open('w',encoding='utf-8') as stream:
            for name,wire in wires:
                for timestamp,direction,data in wire.frames:
                    stream.write(json.dumps(dict(link=name,monotonic=timestamp,direction=direction,hex=data.hex()))+'\n')
    result['passed']=True
    return result


def batch_comparison(points=512, rounds=8):
    """Compare current single-update and batch APIs over the same real socket."""
    with ExitStack() as stack:
        server=IEC104Server(ip='127.0.0.1',port=port()); stack.callback(server.stop)
        peer=IEC104Master('127.0.0.1',server.port,init='NONE'); stack.callback(peer.close)
        wire=Wire(); peer.set_packet_handler(wire)
        for no in range(1,points+1):
            server.add_point('yc',no,value=0); peer.add_point('yc',no)
        server.start(); peer.connect(timeout=5)
        result={mode:dict(elapsed=[],wire=[]) for mode in ('single','batch')}; value=0
        for _ in range(rounds):
            for mode in ('single','batch'):
                value+=1; mark=len(wire.frames); begin=time.perf_counter()
                if mode=='single':
                    for no in range(1,points+1): server.update('yc',no,float(value))
                else: server.update_monitoring([('yc',no,float(value),0) for no in range(1,points+1)])
                end=wait_for(lambda: all(peer.value('yc',no).value==value for no in range(1,points+1))
                             and sum(b[7]&127 for _,b in wire.monitoring(mark))>=points,mode)
                frames=[b for _,b in wire.monitoring(mark)]
                assert sum(b[7]&127 for b in frames)==points, (mode,len(frames),sum(b[7]&127 for b in frames))
                result[mode]['elapsed'].append((end-begin)*1000)
                result[mode]['wire'].append(dict(frames=len(frames),bytes=sum(map(len,frames))))
        for item in result.values(): item['latency']=stats(item.pop('elapsed'))
        result['points']=points; result['frame_validation']=wire.validate()
        return result


def snapshot_boundary():
    """Evidence of snapshot semantics, not an assertion of SOE retention."""
    with ExitStack() as stack:
        server=IEC104Server(ip='127.0.0.1',port=port()); stack.callback(server.stop)
        peer=IEC104Master('127.0.0.1',server.port,init='NONE'); stack.callback(peer.close)
        server.add_point('yx',1,value=False); peer.configure([dict(kind='yx',pnt_no=1)])
        wire=Wire(); peer.set_packet_handler(wire)
        server.start(); peer.connect(timeout=5)
        for no in range(21): server.update('yx',1,bool((no+1)%2))
        wait_for(lambda: sum(b[7]&127 for _,b in wire.monitoring())==21,'21 YX changes on wire')
        states=[b[15]&1 for _,b in wire.monitoring()]
        assert states==[(no+1)%2 for no in range(21)], states
        snapshot=peer.collect_once()
        assert len(snapshot)==1 and snapshot[0].value is True
        return dict(wire_updates=21,collected_samples=len(snapshot),final_value=snapshot[0].value,
                    wire_states=states,
                    conclusion='collect_once returns latest point snapshot; intermediate transitions require a separate event/SOE pipeline')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'reports/iec104_common_acceptance_20261113')
    parser.add_argument('--channels',type=int,nargs='+',default=[1,4])
    parser.add_argument('--points',type=int,default=64)
    parser.add_argument('--rounds',type=int,default=12)
    parser.add_argument('--skip-comparison',action='store_true')
    parser.add_argument('--collector-baseline',type=Path,
                        help='Preserved IEC104 client source; replace only its polling method for a controlled comparison')
    args=parser.parse_args()
    if min(*args.channels,args.points,args.rounds)<1: parser.error('counts must be positive')
    if args.collector_baseline:
        import runpy
        preserved = runpy.run_path(str(args.collector_baseline))
        IEC104Master._poll_entries = preserved['IEC104Master']._poll_entries
    result=dict(timestamp=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                python=sys.version,c104=importlib.metadata.version('c104'),pipelines=[])
    if args.collector_baseline:
        import hashlib
        result['collector_baseline_sha256'] = hashlib.sha256(args.collector_baseline.read_bytes()).hexdigest()
    args.output.mkdir(parents=True,exist_ok=True)
    try:
        for count in args.channels:
            print(f'Running {count} channels x {args.points*2} monitoring points',flush=True)
            run=pipeline(count,args.points,args.rounds,args.output); result['pipelines'].append(run)
            print(json.dumps(run,ensure_ascii=False),flush=True)
        if not args.skip_comparison: result['comparison']=batch_comparison()
        result['snapshot_boundary']=snapshot_boundary()
        result['passed']=True
    except Exception:
        result['passed']=False; result['error']=traceback.format_exc()
        raise
    finally:
        (args.output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__': main()

"""Timed local end-to-end soak. Run as python -m tools.soak_multichannel --minutes 30 --output <new directory>."""
import argparse
from collections import Counter
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import traceback

from client.iec104 import IEC104Master
from tests.test_scdfe_full_interaction import ThreeWebInteractionTests, free_port


def resources(pids):
    if os.name != 'nt':
        return {pid:(Path('/proc')/str(pid)/'status').read_text() for pid in pids if (Path('/proc')/str(pid)/'status').exists()}
    cmd=('Get-Process | Where-Object { $_.Id -in @('+','.join(map(str,pids))+') }'+
         ' | Select-Object Id,PrivateMemorySize64,WorkingSet64,HandleCount,@{n="Threads";e={$_.Threads.Count}} | ConvertTo-Json -Compress')
    data=subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-Command',cmd],
                                 creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
    return json.loads(data)


def run(minutes,out):
    out.mkdir(parents=True,exist_ok=False)
    fixture=ThreeWebInteractionTests()
    fixture.setUp()
    stop=threading.Event()
    counts=Counter(); timings={}; lock=threading.Lock()
    targets=(1,10,11,100)
    sample_records=[]; failures=[]
    started={}
    def count(key,n=1):
        with lock:counts[key]+=n
    def elapsed(key,v):
        with lock:timings[key]=max(timings.get(key,0),round(v,3))
    def note(value):
        with (out/'events.jsonl').open('a',encoding='utf8') as f:
            f.write(json.dumps({'at':datetime.now().isoformat(timespec='seconds'),**value},ensure_ascii=False)+'\n')
    def fail(exc):
        failures.append(repr(exc));stop.set()
    def api(f,path,method='GET',body=None,**kw):
        result=fixture.api(f,path,method,body,**kw)
        count(f+'_http')
        return result
    def watchdog():
        try:
            while not stop.is_set():
                for f,path,proc,on in (
                    ('c','/api/scdfe_c/logs/search',f'scdfe_c-rtu-{fixture.RTUS[0]}',True),
                    ('c','/api/scdfe_c/logs/search',f'scdfe_c-rtu-{fixture.RTUS[1]}',False),
                    ('s','/api/log-files/search','scdfe_s-modbus-tcp-server',True),
                    ('t','/api/log-files/search','scdfe_t-channel-1',True),
                    ('t','/api/log-files/search','scdfe_t-channel-10',True),
                    ('t','/api/log-files/search','scdfe_t-channel-11',False),
                    ('t','/api/log-files/search','scdfe_t-channel-100',False)):
                    for d in ('RX','TX'):
                        q=f'?record=packet&direction={d}&process={proc}&limit=3'
                        if f=='s':q+=f'&channel_id={fixture.SOURCE+1000}'
                        if f=='t':q+=f'&channel_id={proc.rsplit("-",1)[1]}'
                        result=api(f,path+q)['items']
                        assert bool(result)==on,(proc,d,len(result))
                        assert all(Path(row['filename']).name.startswith(proc+'-') and ('报文 '+d+' ') in row['text'] for row in result)
                        count('log_direction_checks')
                processes=api('t','/api/trans/processes')['items']
                assert {r['channel_id']:r['process_pid'] for r in processes if r['alive']}==started
                for r in processes:
                    age=time.time()*1000-r['process_heartbeat_time']
                    assert r['process_status']==2 and -2000<age<8000,(r['channel_id'],age,r['process_status'])
                stop.wait(2)
        except BaseException as exc:
            note({'monitor_error':traceback.format_exc()});fail(exc)
    clients={}; sockets={}; source=None
    try:
        fixture._create_source();fixture._create_acquisition();fixture._create_forwarder()
        ports={1:fixture.forward_port}
        for cid,kind,rid in ((10,'iec104',fixture.RTUS[0]),(11,'iec104',fixture.RTUS[1]),(100,'modbus-tcp',fixture.RTUS[1])):
            ports[cid]=free_port()
            api('t','/api/trans/channels','POST',dict(channel_id=cid,code=f'SOAK{cid}',protocol=kind,bind_host='127.0.0.1',
                bind_port=ports[cid],common_address=cid,scan_interval_ms=50,report_interval_ms=1000),status=201)
            opts=dict(io_address=0x4001,type_id=13) if kind=='iec104' else dict(register_area='input',address=0,data_type='uint16')
            api('t',f'/api/trans/channels/{cid}/points','POST',dict(kind='yc',pnt_no=1,source_rtu_id=rid,source_pnt_no=1,**opts),status=201)
        for f,cids in {'c':[fixture.RTUS[0]+1000],'s':[fixture.SOURCE+1000],'t':[1,10]}.items():
            for cid in cids:
                api(f,f'/api/packet-recording/channels/{cid}','PUT',{'enabled':True})
        fixture.control('main','start')
        fixture.wait_for(lambda:fixture.c_point(fixture.RTUS[0],'yc')['value']==12)
        fixture.wait_for(lambda:fixture.c_point(fixture.RTUS[1],'yc')['value']==31)
        fixture.control('command-server','start')
        with ThreadPoolExecutor(4) as pool:
            rows=list(pool.map(lambda cid:api('t',f'/api/trans/channels/{cid}/start','POST',{})['item'],targets))
        started={r['channel_id']:r['process_pid'] for r in rows}
        assert len(set(started.values()))==4
        for cid in targets:
            if cid in (10,11):
                peer=IEC104Master('127.0.0.1',ports[cid],common_address=cid,init='NONE')
                fixture.clients.append(peer);peer.add_point('yc',1);peer.connect(timeout=5)
                assert peer.general_interrogation()
            else:
                peer=fixture.client(ports[cid]);sockets[cid]=peer.socket
            clients[cid]=peer
        source=fixture.client(fixture.source_port)
        pids={os.getpid(),*started.values()}
        proc=api('c','/api/processes')['items']
        pids.update(int(r['process_pid']) for r in proc if r['process_pid'])
        # C communication workers are threads: their native thread IDs are not process IDs.
        urls={f:web.server_port for f,web in fixture.webs.items()}
        def read(cid,seq,expected):
            peer=clients[cid]
            if cid in (10,11) and seq%5==1:
                assert peer.general_interrogation();count(f'gi_{cid}')
            deadline=time.perf_counter()+5
            while time.perf_counter()<deadline:
                if cid in (10,11):
                    assert peer.connected
                    actual=[peer.value('yc',1).value]
                else:
                    assert peer.socket is sockets[cid]
                    resp=peer.read_input_registers(0,count=len(expected),device_id=1)
                    assert not resp.isError(),resp
                    actual=resp.registers
                if actual==expected:
                    count(f'wire_{cid}');return
                time.sleep(.025)
            raise AssertionError((cid,seq,expected,actual))
        db=fixture.root/'system/scada.sqlite3'
        (out/'ports.json').write_text(json.dumps(dict(web=urls,root=str(fixture.root),t_pids=started)),encoding='utf8')
        # Let the policy reach every process and prime log filters before sampling.
        clients[1].read_input_registers(0,count=2,device_id=1)
        clients[10].general_interrogation()
        fixture.wait_for(lambda:api('t','/api/log-files/search?process=scdfe_t-channel-1&direction=RX')['items'])
        start=time.perf_counter();next_sample=0.;next_command=0.;seq=0
        note(dict(start=True,duration_seconds=minutes*60,ports=urls))
        with ThreadPoolExecutor(5) as pool:
            observer=pool.submit(watchdog)
            try:
                while time.perf_counter()-start < minutes*60 and not stop.is_set():
                    tick=time.perf_counter();seq+=1
                    a=1000+(seq%12000);b=30000+(seq%12000)
                    result=api('s','/api/modbus-tcp-server/update/full','POST',{'yc':{'1':a,'2':b}})['item']
                    assert result['updated']==2
                    source_pair=source.read_holding_registers(0,count=2,device_id=1)
                    assert not source_pair.isError() and source_pair.registers==[a,b]
                    works=[pool.submit(read,cid,seq,expected) for cid,expected in zip(targets,([a,b],[a],[b],[b]))]
                    for task in works:task.result()
                    for rid,val in zip(fixture.RTUS,(a,b)):
                        row=api('c',f'/api/rtus/{rid}/points/yc/1')['item']
                        assert row['value']==val and row['status']==0
                        count('c_samples')
                    now=time.perf_counter()-start
                    if now>=next_command:
                        index=counts['commands']%2
                        desired=(counts['commands']//2)%2==0
                        reply=clients[1].write_coil(index,desired,device_id=1)
                        assert not reply.isError()
                        fixture.handled(fixture.RTUS[index],'yk',int(desired))
                        assert source.read_coils(2+index,count=1,device_id=1).bits[0] == desired
                        count('commands')
                        raw=8+(counts['commands']%100)
                        assert not clients[1].write_register(10,raw,device_id=1).isError()
                        engineering=2*raw+10
                        fixture.handled(fixture.RTUS[0],'yt',engineering)
                        assert source.read_holding_registers(4,count=1,device_id=1).registers==[engineering]
                        count('setpoints');next_command=now+35
                    count('generations');elapsed('publication_to_verified_seconds',time.perf_counter()-tick)
                    if now>=next_sample:
                        wal=Path(str(db)+'-wal')
                        record=dict(elapsed=round(now,2),counts=dict(counts),resources=resources(pids),
                                    wal_bytes=wal.stat().st_size if wal.exists() else 0)
                        note(record);sample_records.append(record);next_sample=now+60
                    stop.wait(max(0,.75-(time.perf_counter()-tick)))
            finally:
                stop.set();observer.result()
        seconds=time.perf_counter()-start
        assert not failures and seconds>=minutes*60,failures
        for family in ('s','c'):
            database = fixture.webs[family].store.db_path
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as con:
                assert con.execute('pragma integrity_check').fetchone()==('ok',)
                assert not con.execute('pragma foreign_key_check').fetchall()
        files=sorted(p.relative_to(fixture.root).as_posix() for p in fixture.root.rglob('*.log'))
        report=dict(success=True,seconds=round(seconds,2),minutes_requested=minutes,counts=dict(counts),
                    maxima=timings,errors=failures,resources=sample_records,files=files,t_pids=started)
        note({'verified_seconds':report['seconds'],'counts':dict(counts)})
    except BaseException as exc:
        note({'failure':traceback.format_exc()});raise
    finally:
        stop.set()
        try:
            trans=fixture.webs['t'].trans_store
            for row in trans.channels():
                if row['channel_id']!=1:trans.stop(row['channel_id'])
        finally:fixture._cleanup()
    report['cleanup']='complete'
    (out/'result.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf8')
    note({'finished':report['seconds'],'counts':dict(counts)})
    print('completed',report['seconds'],dict(counts),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--minutes',type=float,default=30.)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not 0<args.minutes<=1440:parser.error('--minutes must be in (0,1440]')
    run(args.minutes,args.output.resolve())

"""Published-baseline comparisons using temporary DBs and real T IEC104 TCP."""
import hashlib
import json
from pathlib import Path
import platform
import socket
import sqlite3
import statistics
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tools')]
import benchmark_efficiency_round3 as helpers
helpers.BASELINE='c1e9509'
from scdfe_t.web.service import TransStore
from scdfe_t.trans import TransRuntime
from client.iec104 import IEC104Master
from core.iec104_protocol import parse_apdu

REPORT=ROOT/'reports/scdfe_efficiency_20261112_round5'


def compare(before,after,prepare=lambda:None,repeats=21):
    times=[[],[]]
    for repeat in range(-2,repeats):
        for i in ((0,1) if repeat%2 else (1,0)):
            prepare();start=time.perf_counter_ns();(before,after)[i]()
            elapsed=(time.perf_counter_ns()-start)/1e6
            if repeat>=0:times[i].append(elapsed)
    a,b=map(statistics.median,times)
    return dict(before_ms=a,after_ms=b,reduction_percent=(1-b/a)*100,samples_ms=times)


def main():
    old_store=helpers.old_module('scdfe_t.web._round5_before','scdfe_t/web/service.py').TransStore
    old_runtime=helpers.old_module('scdfe_t._round5_before','scdfe_t/trans.py').TransRuntime
    results=[]
    with tempfile.TemporaryDirectory(prefix='scada-round5-') as tmp:
        path=Path(tmp)/'mapping.sqlite3'
        after=TransStore(path,connection_pool_size=1);before=old_store(path,connection_pool_size=1)
        try:
            after.create_channel(dict(channel_id=1,protocol='iec104',bind_host='127.0.0.1',bind_port=25444))
            with after.store.connection() as con:
                con.executemany("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,'iec104')",[(r,r) for r in range(1,201)])
                con.executemany('INSERT INTO fes_yc_define(rtu_id,pnt_no,value) VALUES(?,?,42)',[(r,n) for r in range(1,201) for n in range(1,201)])
                con.commit()
            body=dict(items=[dict(kind='yc',source_rtu_id=n%200+1,source_pnt_no=n//200+1,name=f'点{n}') for n in range(500)])
            def reset():
                with after.store.connection() as con:con.execute('DELETE FROM fes_scdfe_t_point_define');con.commit()
            calls=[lambda service=service:service.create_points_batch(1,body) for service in (before,after)]
            timing=compare(*calls,prepare=reset)
            queries=[]
            for service,call in zip((before,after),calls):
                reset();sql=[]
                with service.store.connection() as con:con.set_trace_callback(sql.append)
                try:
                    rows=call();assert len(rows)==500 and [r['source_rtu_id'] for r in rows]==[r['source_rtu_id'] for r in body['items']]
                finally:
                    with service.store.connection() as con:con.set_trace_callback(None)
                queries.append(sum(q.startswith(('SELECT','WITH')) for q in sql))
            results.append(dict(name='500 mappings from 200 RTUs',**timing,read_queries=queries))
            full=before.source_catalog('yc',100);detail=after.source_catalog('yc',100,include_rtus=False)
            assert full['items']==detail['items'] and full['rtus'] and not detail['rtus']
            results.append(dict(name='selected RTU detail from 40000 source points',
                **compare(lambda:before.source_catalog('yc',100),lambda:after.source_catalog('yc',100,include_rtus=False)),
                response_bytes=[len(json.dumps(x,ensure_ascii=False).encode()) for x in (full,detail)]))
        finally:before.store.close();after.store.close()

        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        path=Path(tmp)/'wire.sqlite3';store=TransStore(path,connection_pool_size=1)
        runtimes=[];client=None;server=None
        try:
            store.create_channel(dict(channel_id=1,protocol='iec104',bind_host='127.0.0.1',bind_port=port))
            with store.store.connection() as con:
                con.executemany("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,'iec104')",[(r,r) for r in range(1,81)])
                for kind in ('yc','yx'):
                    con.executemany(f'INSERT INTO fes_{kind}_define(rtu_id,pnt_no,value) VALUES(?,1,0)',[(r,) for r in range(1,81)])
                con.commit()
            items=[dict(kind=k,pnt_no=n+(80 if k=='yx' else 0),source_rtu_id=n,source_pnt_no=1,
                        type_id=13 if k=='yc' else 1,time_tag='CP56Time2a' if n>40 else 'NONE') for k in ('yc','yx') for n in range(1,81)]
            store.create_points_batch(1,dict(items=items))
            runtimes=[cls(path,channel_id=1) for cls in (old_runtime,TransRuntime)]
            for runtime in runtimes:runtime.store.connection_pool_size=1
            spec,points,_=runtimes[1].load();server=runtimes[1].build_server(spec,points)
            prior=runtimes[0];current=runtimes[1]
            prior.server=server;prior.points=current.points;prior._scan_plan=current._scan_plan
            frames=[];server.set_raw_handlers(on_send=lambda b:frames.append(bytes(b)))
            server.start();client=IEC104Master('127.0.0.1',port,init='NONE')
            for item in items:client.add_point(item['kind'],item['pnt_no'],data_type=item['type_id'],time_tag=item['time_tag'])
            client.connect(timeout=3)
            deadline=time.monotonic()+3
            while not server.active_connection_count and time.monotonic()<deadline:time.sleep(.01)
            assert server.active_connection_count
            wire=[];generation=0;values=current.values.copy();qualities=current.qualities.copy()
            for repeat in range(6):
                for index in ((0,1) if repeat%2 else (1,0)):
                    generation+=1
                    with store.store.connection() as con:
                        for kind in ('yc','yx'):
                            con.execute(f'UPDATE fes_{kind}_define SET value=?,status=?,refresh_time=?',
                                (generation if kind=='yc' else generation%2,128 if generation%2 else 64,generation))
                        con.commit()
                    runtime=runtimes[index];runtime.values=values.copy();runtime.qualities=qualities.copy();frames.clear()
                    start=time.perf_counter_ns();runtime.refresh();scan_ms=(time.perf_counter_ns()-start)/1e6
                    deadline=time.monotonic()+10
                    while time.monotonic()<deadline:
                        sent=[b for b in frames if len(b)>9 and b[2]&1==0 and b[8]&63==3]
                        if sum(b[7]&127 for b in sent)==160 and all(
                            client.value(k,n).value==v and client.value(k,n).quality==runtime.qualities[(k,n)] for (k,n),v in runtime.values.items()):break
                        time.sleep(.005)
                    else:raise AssertionError('T loopback did not deliver all values and qualities')
                    for b in sent:assert parse_apdu(b).asdu_length<=249
                    wire.append(dict(variant='before' if index==0 else 'after',scan_ms=scan_ms,
                        delivery_ms=(time.perf_counter_ns()-start)/1e6,frames=len(sent),apdu_bytes=sum(map(len,sent))))
                    with store.store.connection() as con:
                        assert con.execute('SELECT COUNT(*) FROM fes_scdfe_t_point_define WHERE refresh_time=? AND forward_time>0',(generation,)).fetchone()[0]==160
                    values=runtime.values.copy();qualities=runtime.qualities.copy()
            assert client.general_interrogation()
        finally:
            if client:client.close()
            if server:server.stop()
            for runtime in runtimes:runtime.store.close()
            store.store.close()
    files=['scdfe_c/web/service.py','scdfe_t/web/service.py','scdfe_t/web/server.py','scdfe_t/trans.py','server/iec104_server.py',
        'scdfe_c/web/static/app.js','scdfe_s/web/static/channels.js','scdfe_t/web/static/app.js']
    result=dict(baseline=helpers.BASELINE,python=platform.python_version(),platform=platform.platform(),results=results,wire=wire,
        source_sha256_lf={f:hashlib.sha256((ROOT/f).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for f in files},
        scope='Temporary SQLite DBs, mapping reset excluded from timing; T real TCP loopback with 80 source RTUs / 160 points. No field-device benchmark.')
    REPORT.mkdir(parents=True,exist_ok=True);(REPORT/'backend.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({**result,'source_sha256_lf':'recorded','results':[{k:v for k,v in r.items() if k!='samples_ms'} for r in results]},ensure_ascii=False),flush=True)


if __name__=='__main__':main()

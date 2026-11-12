"""Compare batch receipts and actual loopback IEC104 wire traffic to a9f0978."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import platform
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tools'))
import benchmark_efficiency_round3 as helpers
helpers.BASELINE='a9f0978'
from scdfe_c.web.service import ScadaStore
from scdfe_s.web.iec104_service import IEC104Service
from server.iec104_server import IEC104Server
from client.iec104 import IEC104Master
from core.iec104_protocol import parse_apdu

REPORT=ROOT/'reports/scdfe_efficiency_20261112_round4'


def main():
    before_store=helpers.old_module('scdfe_c.web._round4_before','scdfe_c/web/service.py').ScadaStore
    before_service=helpers.old_module('scdfe_s.web._round4_before','scdfe_s/web/iec104_service.py').IEC104Service
    results=[]
    with tempfile.TemporaryDirectory(prefix='scada-round4-') as tmp:
        path=Path(tmp)/'data.sqlite3'
        stores=[before_store(path,connection_pool_size=1),ScadaStore(path,connection_pool_size=1)]
        try:
            with stores[1].connection() as con:
                con.executemany("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,'iec104')",[(r,r) for r in range(1,201)])
                con.executemany('INSERT INTO fes_yc_define(rtu_id,pnt_no,value) VALUES(?,?,0)',[(r,n) for r in range(1,201) for n in range(1,11)])
                con.commit()
            generation=0
            def write(store,receipts=True):
                nonlocal generation
                generation+=1
                rows=store.update_rows([('fes_yc_define',[r,n],dict(value=generation,status=generation%2)) for r in range(1,201) for n in range(1,11)],return_rows=receipts)
                if receipts:
                    assert len(rows)==2000 and all(r['value']==generation for r in rows)
            for receipts in (True,False):
                timings=helpers.paired(lambda:write(stores[0],receipts),lambda:write(stores[1],receipts),repeats=21)
                queries=[]
                for store in stores:
                    statements=[]
                    with store.connection() as con: con.set_trace_callback(statements.append)
                    try: write(store,receipts)
                    finally:
                        with store.connection() as con: con.set_trace_callback(None)
                    queries.append(sum(q.startswith(('SELECT','WITH')) for q in statements))
                row=dict(name='200 RTUs x 10 points '+('with receipts' if receipts else 'without receipts control'),**timings,read_queries=queries)
                results.append(row); print(json.dumps({k:v for k,v in row.items() if k not in ('before','after')}),flush=True)
        finally:
            for store in stores: store.close()

    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=IEC104Server(ip='127.0.0.1',port=port,rtu_id=1)
    client=IEC104Master('127.0.0.1',port,init='NONE')
    frames=[]; server.set_raw_handlers(on_send=lambda data:frames.append(bytes(data)))
    con=sqlite3.connect(':memory:')
    try:
        for kind in ('yc','yx'):
            con.execute(f'CREATE TABLE fes_{kind}_define(rtu_id INT,pnt_no INT,value REAL,status INT)')
            for no in range(1,81):
                tag='CP56Time2a' if no>40 else 'NONE'
                server.add_point(kind,no,value=0,time_tag=tag)
                client.add_point(kind,no,time_tag=tag)
                con.execute(f'INSERT INTO fes_{kind}_define VALUES(1,?,0,0)',(no,))
        server.start();client.connect(timeout=3)
        services=[SimpleNamespace(_lock=threading.RLock(),_server=server,_database_samples={},_database_pending=set()) for _ in range(2)]
        wire=[]; generation=0
        for repeat in range(6):
            # Alternate before/after ordering, not progressively warming one side.
            for index in ((0,1) if repeat%2 else (1,0)):
                generation+=1
                services[index]._database_samples=server.monitoring_snapshot()
                for kind in ('yc','yx'):
                    con.execute(f'UPDATE fes_{kind}_define SET value=?,status=?',(generation if kind=='yc' else generation%2,128 if generation%2 else 64))
                frames.clear()
                start=time.perf_counter_ns()
                cls=(before_service,IEC104Service)[index]
                assert cls._sync_database_values(services[index],con)==160
                sync_ms=(time.perf_counter_ns()-start)/1e6
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    frames_now=[b for b in frames if len(b)>9 and b[2]&1==0 and b[8]&63==3]
                    if sum(b[7]&127 for b in frames_now)==160 and all(
                        client.value(k,n).value==(generation if k=='yc' else bool(generation%2)) and
                        client.value(k,n).quality==(128 if generation%2 else 64)
                        for k in ('yc','yx') for n in range(1,81)): break
                    time.sleep(.005)
                else: raise AssertionError('loopback delivery timeout')
                for b in frames_now: assert parse_apdu(b).asdu_length<=249
                wire.append(dict(variant='before' if index==0 else 'after',repeat=repeat,
                    sync_ms=sync_ms,delivery_ms=(time.perf_counter_ns()-start)/1e6,
                    frames=len(frames_now),apdu_bytes=sum(map(len,frames_now)),objects=160))
        assert client.general_interrogation()
    finally:
        client.close();server.stop();con.close()
    files=['scdfe_c/web/service.py','server/iec104_server.py','scdfe_s/web/iec104_service.py',
           'scdfe_c/web/static/app.js','scdfe_s/web/static/channels.js','scdfe_t/web/static/app.js']
    output=dict(baseline=helpers.BASELINE,python=platform.python_version(),platform=platform.platform(),results=results,wire=wire,
        source_sha256_lf={f:hashlib.sha256((ROOT/f).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for f in files},
        scope='Temporary SQLite; IEC104 localhost TCP, 160 mixed YC/YX NONE/CP56 points; APDU bytes exclude TCP headers/ACKs. No field-device throughput claim.')
    REPORT.mkdir(parents=True,exist_ok=True)
    (REPORT/'backend.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(wire),flush=True)


if __name__=='__main__': main()

"""Compare command persistence and unchanged history reads in isolated databases."""
import argparse
import http.client
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time


def stats(values):
    ordered=sorted(values)
    return dict(samples=len(values),median_ms=round(statistics.median(values),4),
                p95_ms=round(ordered[math.ceil(len(values)*.95)-1],4))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--summary',action='store_true',help='Measure the compact history view used by the current UI')
    args=parser.parse_args();sys.path.insert(0,str(args.root.resolve()))
    from scdfe_c.web.server import make_server
    from core.command_store import DatabaseCommandWriter
    from core.command_trace import claim,advance
    result=dict(root=str(args.root.resolve()),scope='temporary SQLite WAL and localhost HTTP; no equipment I/O; warm reads',history_rows=5000)
    with tempfile.TemporaryDirectory(prefix='scada-round12-') as directory:
        server=make_server('127.0.0.1',0,db_path=Path(directory)/'bench.sqlite3');store=server.store
        store.create_row('fes_rtu_define',dict(rtu_id=1,channel_type='iec104',channel_id=1,name='测试站'))
        store.create_row('fes_yk_define',dict(rtu_id=1,pnt_no=1,name='测试开关',value=0,status=0))
        with store.connection() as con:
            con.execute('BEGIN')
            con.executemany("INSERT INTO _scada_command_trace(rtu_id,kind,pnt_no,cmd_time,value,stage) VALUES(1,'yk',1,?,1,'protocol_confirmed')",[(i,) for i in range(1,5001)])
            con.commit()
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            writer=DatabaseCommandWriter(store);timings=[];submit=[]
            for cycle in range(65):
                started=time.perf_counter();row=writer.submit_command(1,'yk',1,cycle%2);submitted=time.perf_counter()
                ident=claim(store,1,'yk',1,row['cmd_time'],cycle%2);assert ident
                for stage in ('select_sent','select_confirmed','execute_sent','protocol_confirmed'):advance(store,ident,stage)
                elapsed=(time.perf_counter()-started)*1000
                with store.connection() as con:
                    assert con.execute('SELECT stage FROM _scada_command_trace WHERE command_id=?',(ident,)).fetchone()[0]=='protocol_confirmed'
                    con.execute('UPDATE fes_yk_define SET cmd_handle_time=cmd_time WHERE rtu_id=1 AND pnt_no=1')
                if cycle>=5:timings.append(elapsed);submit.append((submitted-started)*1000)
            result['command_lifecycle']=stats(timings);result['command_submit']=stats(submit)
            for mode in ('full','conditional'):
                durations=[];sizes=[];statuses={};tag=None
                for cycle in range(105):
                    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                    start=time.perf_counter()
                    connection.request('GET','/api/command-traces?limit=50'+('&view=summary' if args.summary else ''),headers={'If-None-Match':tag} if tag and mode=='conditional' else {})
                    response=connection.getresponse();body=response.read();tag=response.getheader('ETag');elapsed=(time.perf_counter()-start)*1000;connection.close()
                    assert response.status in (200,304)
                    if response.status==200:assert len(json.loads(body)['items'])==50
                    if cycle>=5:
                        durations.append(elapsed);sizes.append(len(body));statuses[str(response.status)]=statuses.get(str(response.status),0)+1
                result[mode]=dict(**stats(durations),body_bytes=int(statistics.median(sizes)),statuses=statuses)
            result['passed']=True
        finally:server.shutdown();server.server_close();thread.join(3)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()

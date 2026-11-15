"""Compare multi-channel S monitor publication with a controlled sample clock."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();sys.path.insert(0,str(args.root.resolve()))
    from scdfe_s.web.server import make_server,ScadaHandler
    from core import runtime_status
    clock=[100.0]
    runtime_status.time=SimpleNamespace(time=time.time,monotonic=lambda:clock[0])
    cases=[]
    with tempfile.TemporaryDirectory(prefix='scada-round17-') as directory:
        for size in (1,64):
            server=make_server('127.0.0.1',0,db_path=Path(directory)/f'{size}.sqlite3')
            try:
                with server.store.connection() as con:
                    con.execute('BEGIN')
                    con.executemany('INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type,name) VALUES(?,?,?,?)',
                        [(n,n,('iec104','modbus-tcp','can')[(n-1)%3],f'验收站{n}') for n in range(1,size+1)])
                    con.commit()
                handler=object.__new__(ScadaHandler);handler.server=server
                services=server.channel_services()
                for mode,step in [('burst',.051),('normal_5s',5.0)]:
                    samples=[];counts=[];poll_times=[]
                    for group in range(10):
                        # Two warmups and eight measured groups. Each group has
                        # 20 changing polls. No wall-clock sleeps are timed.
                        clock[0]+=1000
                        base=clock[0];sql=[];elapsed=[]
                        with server.store.connection() as con:con.set_trace_callback(sql.append)
                        for poll in range(20):
                            clock[0]=base+poll*step
                            count=(group+1)*100+poll
                            for service in services.values():
                                service._rx_count=count;service._tx_count=count*2
                            start=time.perf_counter()
                            statuses,_=handler._monitor_statuses(services,{})
                            elapsed.append((time.perf_counter()-start)*1000)
                            assert len(statuses)==size and all(s['rx_count']==count for _,s in statuses)
                        with server.store.connection() as con:con.set_trace_callback(None)
                        if group>=2:
                            samples.append(sum(elapsed));poll_times.extend(elapsed)
                            counts.append(dict(commits=sql.count('COMMIT'),write_locks=sql.count('BEGIN IMMEDIATE'),
                                upserts=sum(s.startswith('INSERT INTO "fes_channel_runtime_status"') for s in sql)))
                        # Outside measurement: next due call must persist the
                        # latest values for every channel (no event replay).
                        clock[0]+=2;handler._monitor_statuses(services,{})
                        with server.store.connection() as con:
                            rows=con.execute('SELECT rx_count,tx_count FROM fes_channel_runtime_status').fetchall()
                            assert len(rows)==size and all(tuple(r)==(count,count*2) for r in rows)
                    def stats(values):
                        import math
                        return dict(median_ms=round(statistics.median(values),3),p95_ms=round(sorted(values)[math.ceil(len(values)*.95)-1],3))
                    cases.append(dict(channels=size,mode=mode,polls=20,batch=stats(samples),poll=stats(poll_times),
                        sql_counts=counts,batch_samples_ms=samples))
            finally:server.server_close()
    result=dict(passed=True,root=str(args.root.resolve()),
        scope='temporary SQLite WAL; real S monitor capture+publication; simulated sampling clock; 2 warmups+8 measured groups; 20 changing polls/group; no socket or field device traffic; final latest values verified outside timing',cases=cases)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps([{k:v for k,v in c.items() if k not in ('batch_samples_ms','sql_counts')}|dict(sql=c['sql_counts'][0]) for c in cases]))


if __name__=='__main__':main()

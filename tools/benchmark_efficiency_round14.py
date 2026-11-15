"""Compare command SQL work on isolated databases; no device commands."""
import argparse
from pathlib import Path
import sys
import json
import statistics
import tempfile
import time

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=Path('.'));parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();sys.path.insert(0,str(args.root.resolve()))
    from scdfe_c.web.service import ScadaStore
    from core.command_store import DatabaseCommandWriter
    result=dict(cases=[],passed=False)
    with tempfile.TemporaryDirectory(prefix='commands-round14-') as tmp:
        store=ScadaStore(Path(tmp)/'scada.sqlite3',connection_pool_size=2)
        try:
            with store.connection() as con:
                con.execute('BEGIN')
                for rid in range(1,11):
                    con.execute("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,'modbus-tcp')",(rid,rid))
                    for kind in ('yk','yt'):
                        con.executemany(f'INSERT INTO fes_{kind}_define(rtu_id,pnt_no,value) VALUES(?,?,0)',[(rid,n) for n in range(1,101)])
                con.commit()
            writer=DatabaseCommandWriter(store)
            for size in (1,100,1000):
                commands=[(r,'yk' if n%2 else 'yt',n,1) for r in range(1,11) for n in range(1,101)][:size]
                values=[];reads=[]
                for repeat in range(15):
                    sql=[]
                    with store.connection() as con:con.set_trace_callback(sql.append)
                    start=time.perf_counter();rows=writer.submit_commands(commands);elapsed=(time.perf_counter()-start)*1000
                    with store.connection() as con:
                        con.set_trace_callback(None)
                        for kind in ('yk','yt'):con.execute(f'UPDATE fes_{kind}_define SET cmd_handle_time=cmd_time WHERE cmd_time>COALESCE(cmd_handle_time,0)')
                    assert len(rows)==size and [(x['rtu_id'],x['pnt_no']) for x in rows]==[(r,n) for r,k,n,v in commands]
                    if repeat>=3:
                        values.append(elapsed);reads.append(sum(s.upper().startswith('SELECT') for s in sql))
                result['cases'].append(dict(points=size,samples=len(values),median_ms=statistics.median(values),p95_ms=max(values),selects=statistics.median(reads)))
            result['passed']=True
        finally:store.close()
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps(result))

if __name__=='__main__':main()

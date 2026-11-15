"""Compare real FULL command commits plus coalescible HMI snapshot writes."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();sys.path.insert(0,str(args.root.resolve()))
    from scdfe_t.trans import TransRuntime
    cases=[]
    with tempfile.TemporaryDirectory(prefix='scada-round16-') as directory:
        for size in (1,25,100):
            runtime=TransRuntime(Path(directory)/f'{size}.sqlite3',channel_id=1)
            runtime.store.connection_pool_size=2
            points=[]
            try:
                with runtime.store.connection() as con:
                    con.execute('BEGIN')
                    con.execute("INSERT INTO fes_scdfe_t_define(channel_id,name) VALUES(1,'临时验收')")
                    for rid in range(1,min(size,10)+1):
                        con.execute("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,'iec104')",(rid,rid))
                    for n in range(size):
                        rid=n%min(size,10)+1;no=n//min(size,10)+1
                        con.execute('INSERT INTO fes_yk_define(rtu_id,pnt_no,value) VALUES(?,?,0)',(rid,no))
                        con.execute("INSERT INTO fes_scdfe_t_point_define(channel_id,pnt_no,kind,source_rtu_id,source_pnt_no) VALUES(1,?,'yk',?,?)",(n+1,rid,no))
                        points.append(SimpleNamespace(kind='yk',pnt_no=n+1,source_rtu_id=rid,source_pnt_no=no))
                    con.commit()
                batches=[];responses=[];commits=[]
                for iteration in range(10):
                    statements=[]
                    with runtime.store.connection() as con:
                        con.execute('UPDATE fes_yk_define SET cmd_handle_time=cmd_time,status=1')
                        con.set_trace_callback(statements.append)
                    start=time.perf_counter();samples=[]
                    for point in points:
                        stamp=time.perf_counter();assert runtime.command(point,iteration%2)
                        samples.append((time.perf_counter()-stamp)*1000)
                    assert runtime._flush_point_runtime()
                    elapsed=(time.perf_counter()-start)*1000
                    with runtime.store.connection() as con:
                        con.set_trace_callback(None)
                        assert con.execute('SELECT count(*) FROM fes_scdfe_t_point_define WHERE value=? AND command_receive_time>0',(iteration%2,)).fetchone()[0]==size
                        assert con.execute('SELECT count(*) FROM _scada_command_trace').fetchone()[0]==size*(iteration+1)
                    if iteration>=2:
                        batches.append(elapsed);responses.extend(samples)
                        commits.append(sum(s=='COMMIT' for s in statements))
                def stats(values):
                    import math
                    return dict(median_ms=round(statistics.median(values),3),
                                p95_ms=round(sorted(values)[math.ceil(len(values)*.95)-1],3))
                cases.append(dict(commands=size,rtus=min(size,10),batch=stats(batches),response=stats(responses),
                    commits=commits,batch_samples_ms=batches,diagnostics=runtime._runtime_diagnostic()))
            finally:runtime.store.close()
    result=dict(passed=True,root=str(args.root.resolve()),scope='temporary SQLite WAL; FULL commands; no device I/O; 2 warmups + 8 measured bursts; all control trace tokens verified',cases=cases)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps([{k:v for k,v in row.items() if k in ('commands','batch','response','commits')} for row in cases]))


if __name__=='__main__':main()

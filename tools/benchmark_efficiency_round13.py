"""Compare feedback idle work and writer backlog deadlines in private databases."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
import multiprocessing as mp
from types import SimpleNamespace


def backlog_benchmark():
    from scdfe_c.web.service import ScadaStore,SCHEMA_DIR
    from scdfe_c import _batch_writer_entry,BATCH_WRITER_STOP
    with tempfile.TemporaryDirectory(prefix='scada-aged-queue-') as directory:
        root=Path(directory);data=ScadaStore(root/'db.sqlite3')
        data.create_row('fes_rtu_define',dict(rtu_id=1,channel_type='iec104',channel_id=1))
        data.create_row('fes_yc_define',dict(rtu_id=1,pnt_no=1,value=0,status=0))
        ctx=mp.get_context('spawn');queue=ctx.JoinableQueue(100);ready=ctx.Event()
        writer=ctx.Process(target=_batch_writer_entry,args=(str(data.db_path),str(SCHEMA_DIR),str(root/'logs'),queue,.2,500,ready))
        writer.start();times=[]
        try:
            assert ready.wait(15)
            for cycle in range(1,13):
                start=time.perf_counter()
                queue.put(dict(op='update',table='fes_yc_define',key=dict(rtu_id=1,pnt_no=1),values=dict(value=cycle),
                               _queued_at_ns=time.perf_counter_ns()-250_000_000,source_rtu=1))
                deadline=start+5
                while time.perf_counter()<deadline:
                    with data.connection() as con:value=con.execute('SELECT value FROM fes_yc_define WHERE rtu_id=1 AND pnt_no=1').fetchone()[0]
                    if value==cycle:break
                    time.sleep(.002)
                else:raise AssertionError('private writer commit timed out')
                times.append((time.perf_counter()-start)*1000)
            return dict(samples=len(times),median_ms=statistics.median(times),p95_ms=sorted(times)[11],runs_ms=times,
                        scope='real writer process; inject 250ms queue age; batch deadline 200ms; 2ms DB observation; no device I/O')
        finally:
            queue.put(BATCH_WRITER_STOP);writer.join(15)
            if writer.is_alive():writer.terminate();writer.join(3);raise AssertionError('private writer failed to stop')
            assert writer.exitcode==0;queue.close();queue.join_thread();data.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--writer',action='store_true')
    args=parser.parse_args();sys.path.insert(0,str(args.root.resolve()))
    if args.writer:
        result=backlog_benchmark();result['root']=str(args.root.resolve());result['passed']=True
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps(result));return
    from scdfe_c.web.service import ScadaStore
    from core.command_feedback import FeedbackTracker
    result=dict(root=str(args.root.resolve()),points=100,cycles=100,scope='configured feedback targets with no pending command; temporary DB; synthetic receive timestamps')
    with tempfile.TemporaryDirectory(prefix='scada-round13-') as directory:
        data=ScadaStore(Path(directory)/'bench.sqlite3')
        try:
            data.create_row('fes_rtu_define',dict(rtu_id=1,channel_type='iec104',channel_id=1))
            with data.connection() as con:
                con.execute('BEGIN')
                con.executemany("INSERT INTO _scada_command_feedback_rule VALUES(1,'yk',?,1,'yx',?,1,0,0,5000,1,1)",[(n,n) for n in range(1,101)])
                con.commit()
            tracker=FeedbackTracker(data,1);samples=[];written=0;epoch=time.time()
            for cycle in range(100):
                start=time.perf_counter();tracker.refresh();rows=[]
                for no in range(1,101):
                    row=tracker.sample('yx',no,cycle%2,0,SimpleNamespace(received_at=epoch+cycle*.01))
                    if row is not None:rows.append(row)
                tracker.commit(rows);written+=len(rows);samples.append((time.perf_counter()-start)*1000)
            result.update(median_ms=statistics.median(samples),p95_ms=sorted(samples)[94],sample_rows_submitted=written)
            with data.connection() as con:result['sample_rows_stored']=con.execute('SELECT count(*) FROM _scada_command_feedback_sample').fetchone()[0]
        finally:data.close()
    result['passed']=True
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps(result))


if __name__=='__main__':main()

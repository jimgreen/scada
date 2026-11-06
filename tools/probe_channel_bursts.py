"""Simultaneous Web refreshes and command clients on a disposable database."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import multiprocessing as mp
from pathlib import Path
import statistics
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
from time import perf_counter
from urllib.request import urlopen

from scdfe_c import _batch_writer_entry, BATCH_WRITER_STOP
from scdfe_c.command_server import CommandServer, CommandClient
from scdfe_c.web.server import make_server as c_web
from scdfe_c.writer_ipc import WriterClient
from scdfe_s.web.server import make_server as s_web
from scdfe_t.web.server import make_server as t_web


def probe(n, rounds):
    with ExitStack() as stack:
        db = Path(stack.enter_context(TemporaryDirectory())) / "scada.sqlite3"
        calls = {}
        for name, factory in [('C', c_web), ('S', s_web), ('T', t_web)]:
            web = stack.enter_context(factory(port=0, db_path=db.with_name("scdfe_s.sqlite3") if name == "S" else db))
            if name == 'C':
                store = web.store
            Thread(target=web.serve_forever, daemon=True).start()
            stack.callback(web.shutdown)
            def health(_, port=web.server_port):
                with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3) as res:
                    if json.load(res)['status'] != 'ok':
                        raise RuntimeError('health failed')
            calls[name] = health
        store.create_row('fes_rtu_define', dict(rtu_id=1, channel_id=1, channel_type='iec104'))
        with store.connection() as dbcon:
            dbcon.executemany('INSERT INTO fes_yt_define(rtu_id,pnt_no,value) VALUES(1,?,0)',
                              [(i,) for i in range(1, 2*n*rounds+1)])
            dbcon.commit()
        ctx=mp.get_context('spawn')
        q=ctx.JoinableQueue();ready=ctx.Event()
        writer=ctx.Process(target=_batch_writer_entry, args=(str(db),str(store.catalog.directory),str(db.parent/'logs'),q,.03,100,ready))
        writer.start()
        def finish():
            q.put(BATCH_WRITER_STOP);writer.join(6)
            if writer.is_alive():
                writer.terminate();writer.join(3)
            q.close();q.join_thread()
        stack.callback(finish)
        assert ready.wait(10)
        command = CommandServer(db).start();stack.callback(command.close)
        def submit(i, cls, offset):
            no=i+offset+1
            cls(db,timeout=3).submit_command(1,'yt',no,no,request_id=f'burst-{no}')
            row=store.get_row('fes_yt_define',[1,no])
            if row['value']!=no or not row['cmd_time']:
                raise RuntimeError(f'acknowledged before commit: {no}')
        calls['writer']=lambda i:submit(i,WriterClient,0)
        calls['command']=lambda i:submit(i,CommandClient,n*rounds)
        report={}
        for key,fn in calls.items():
            times,errors=[],[]
            with ThreadPoolExecutor(n) as pool:
                for generation in range(rounds):
                    barrier=Barrier(n)
                    def attempt(i):
                        barrier.wait(10);start=perf_counter()
                        try:
                            fn(i);return (perf_counter()-start)*1000,None
                        except Exception as exc:
                            return (perf_counter()-start)*1000,repr(exc)
                    for ms,err in pool.map(attempt,range(n*generation,n*(generation+1))):
                        times.append(ms)
                        if err:errors.append(err)
            ordered=sorted(times)
            report[key]=dict(requests=len(times),errors=len(errors),examples=errors[:3],
                             p50_ms=round(statistics.median(times),2),p95_ms=round(ordered[(len(times)*95+99)//100-1],2),max_ms=round(ordered[-1],2))
        return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clients',type=int,default=48)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    if not 2<=args.clients<=128 or not 1<=args.rounds<=25:parser.error('invalid clients or rounds')
    result=probe(args.clients,args.rounds)
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf8')
    print(json.dumps(result,ensure_ascii=True))
    raise SystemExit(int(any(v['errors'] for v in result.values())))

"""Profile S monitor configuration and publication on temporary databases."""
import argparse
import cProfile
import io
import json
from pathlib import Path
import pstats
import sys
import tempfile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();sys.path.insert(0,str(args.root.resolve()))
    from scdfe_s.web.server import make_server,ScadaHandler
    with tempfile.TemporaryDirectory(prefix='scada-profile18-') as directory:
        server=make_server('127.0.0.1',0,db_path=Path(directory)/'s.sqlite3')
        try:
            with server.store.connection() as con:
                con.executemany('INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,?)',
                    [(n,n,('iec104','modbus-tcp','can')[(n-1)%3]) for n in range(1,65)])
            handler=object.__new__(ScadaHandler);handler.server=server;services=server.channel_services()
            for service in services.values():service._runtime_status.write_interval_ms=0
            handler._monitor_statuses(services,{})
            sql=[]
            with server.store.connection() as con:con.set_trace_callback(sql.append)
            try:handler._monitor_statuses(services,{})
            finally:
                with server.store.connection() as con:con.set_trace_callback(None)
            profiler=cProfile.Profile();profiler.enable()
            for _ in range(100):handler._monitor_statuses(services,{})
            profiler.disable();buffer=io.StringIO()
            stats=pstats.Stats(profiler,stream=buffer).sort_stats('cumtime');stats.print_stats(28)
            counts=dict(reads=sum(s.startswith('SELECT') for s in sql),read_transactions=sql.count('BEGIN'),
                        write_transactions=sql.count('BEGIN IMMEDIATE'),upserts=sum(s.startswith('INSERT') for s in sql))
            result=dict(root=str(args.root.resolve()),channels=64,polls=100,sql_per_poll=counts,profile=buffer.getvalue())
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
            print(json.dumps(counts));print(buffer.getvalue())
        finally:server.server_close()


if __name__=='__main__':main()

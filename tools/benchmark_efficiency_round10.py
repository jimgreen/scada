"""Same production T scan workload against two checkouts; temporary DB only."""
import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time


def summary(values):
    ordered=sorted(values)
    return dict(samples=len(values),median_ms=round(statistics.median(values),4),
                p95_ms=round(ordered[math.ceil(len(values)*.95)-1],4),
                p99_ms=round(ordered[math.ceil(len(values)*.99)-1],4))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--initialized-timestamps',action='store_true',
                        help='Seed timestamps to 0 for equal work across the NULL timestamp fix')
    args=parser.parse_args();sys.path.insert(0,str(args.root.resolve()))
    from scdfe_c.web.service import ScadaStore
    from scdfe_t.trans import TransRuntime
    result=dict(root=str(args.root.resolve()),rtus=16,points_per_rtu=1000,
                rounds=40,initialized_timestamps=args.initialized_timestamps,
                scope='T production refresh, local IEC104 cache, runtime DB; no TCP client')
    with tempfile.TemporaryDirectory(prefix='scada-round10-') as directory:
        store=ScadaStore(Path(directory)/'data.sqlite3')
        with store.connection() as con:
            con.execute('BEGIN')
            con.execute("INSERT INTO fes_scdfe_t_define(channel_id,protocol,bind_host,bind_port) VALUES(1,'iec104','127.0.0.1',2404)")
            for rid in range(1,17):
                con.execute("INSERT INTO fes_rtu_define(rtu_id,channel_type,channel_id) VALUES(?,'iec104',?)",(rid,rid))
                for kind in ('yc','yx'):
                    con.executemany(f'INSERT INTO fes_{kind}_define(rtu_id,pnt_no,value,status) VALUES(?,?,0,0)',[(rid,no) for no in range(1,501)])
                    con.executemany('INSERT INTO fes_scdfe_t_point_define(channel_id,kind,pnt_no,source_rtu_id,source_pnt_no,io_address,type_id,enabled) VALUES(1,?,?,?,?,?,?,1)',
                        [(kind,(rid-1)*500+no,rid,no,(0x4001 if kind=='yc' else 1)+(rid-1)*500+no-1,13 if kind=='yc' else 1) for no in range(1,501)])
            con.commit()
            if args.initialized_timestamps:
                for kind in ('yc','yx'):
                    con.execute(f'UPDATE fes_{kind}_define SET refresh_time=0')
        runtime=TransRuntime(store.db_path,channel_id=1,log_dir=Path(directory)/'logs')
        try:
            spec,points,_=runtime.load();runtime.build_server(spec,points);runtime.refresh()
            for mode in ('idle','sparse','dense'):
                reads,writes=[],[]
                for cycle in range(40):
                    with store.connection() as con:
                        start=time.perf_counter()
                        if mode=='sparse':
                            con.execute('UPDATE fes_yc_define SET value=value+1 WHERE rtu_id=1 AND pnt_no=1')
                        elif mode=='dense':
                            con.execute('BEGIN');con.execute('UPDATE fes_yc_define SET value=value+1');con.commit()
                        writes.append((time.perf_counter()-start)*1000)
                    start=time.perf_counter();runtime.refresh();reads.append((time.perf_counter()-start)*1000)
                    expected=cycle+1 if mode=='sparse' else 40+cycle+1 if mode=='dense' else 0
                    assert runtime.values[('yc',1)]==expected
                    assert runtime.values[('yc',8000)]==(cycle+1 if mode=='dense' else 0)
                result[mode]=dict(refresh=summary(reads),source_write=summary(writes))
            reader=getattr(runtime,'_source_reader',None)
            if reader is not None:result['source_rows_read']=reader.rows_read
            result['passed']=True
        finally:
            if runtime.server is not None:runtime.server.stop()
            runtime.store.close();store.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()

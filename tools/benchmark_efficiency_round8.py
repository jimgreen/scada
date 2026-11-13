"""Compare multi-RTU catalog queries using the same isolated SQLite database."""
from pathlib import Path
import hashlib
import json
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scdfe_t.web.service import TransStore

BASELINE = '3e36657'
REPORT = ROOT/'reports/scdfe_efficiency_20261113_round8'


def main():
    old = types.ModuleType('scdfe_t.web._round8_before')
    old.__file__ = str(ROOT/'scdfe_t/web/service.py')
    sys.modules[old.__name__] = old
    exec(compile(subprocess.check_output(['git','show',BASELINE+':scdfe_t/web/service.py'],cwd=ROOT),old.__file__,'exec'),old.__dict__)
    results = []
    for rtu_count, point_count in ((1,10), (100,1000), (1000,100)):
        with tempfile.TemporaryDirectory(prefix='scada-catalog-') as directory:
            service = TransStore(Path(directory)/'test.sqlite3',connection_pool_size=1)
            try:
                with service.store.connection() as con:
                    con.execute('BEGIN IMMEDIATE')
                    con.executemany("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type,name) VALUES(?,?,'iec104',?)",
                                    ((n,n,f'RTU{n}') for n in range(1,rtu_count+1)))
                    con.executemany('INSERT INTO fes_yc_define(rtu_id,pnt_no,name,value) VALUES(?,?,?,?)',
                                    ((r,n,f'point{n}',n/10) for r in range(1,rtu_count+1) for n in range(1,point_count+1)))
                    con.commit()
                calls = [lambda:old.TransStore.source_catalog(service,'yc'), lambda:service.source_catalog('yc')]
                assert calls[0]() == calls[1]()
                samples = [[],[]]
                for repeat in range(-3,21):
                    for index in ((0,1) if repeat%2 else (1,0)):
                        start=time.perf_counter_ns(); value=calls[index](); elapsed=(time.perf_counter_ns()-start)/1e6
                        assert len(value['rtus']) == rtu_count
                        assert all(row['point_count']==point_count for row in value['rtus'])
                        if repeat>=0: samples[index].append(elapsed)
                before,after=map(statistics.median,samples)
                results.append(dict(rtus=rtu_count,points_per_rtu=point_count,points=rtu_count*point_count,
                    before_ms=before,after_ms=after,reduction_percent=100*(1-after/before),samples_ms=samples))
            finally: service.store.close()
    files=['scdfe_t/web/service.py','scdfe_t/web/static/app.js','scdfe_t/web/static/communication.js']
    data=dict(baseline=BASELINE,results=results,python=platform.python_version(),sqlite=sqlite3.sqlite_version,
        source_sha256_lf={f:hashlib.sha256((ROOT/f).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for f in files},
        scope='TransStore.source_catalog(yc) with RTU counts, same temporary SQLite WAL database and connection pool, no field traffic. Three warmups then 21 alternating pairs; median, all samples retained. Before/after responses equal.')
    REPORT.mkdir(parents=True,exist_ok=True)
    (REPORT/'backend.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps([{k:v for k,v in r.items() if k!='samples_ms'} for r in results],ensure_ascii=False))


if __name__=='__main__': main()

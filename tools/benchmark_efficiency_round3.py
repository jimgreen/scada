"""Local, alternating comparisons against the published 1d21c03 baseline."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = ROOT / 'reports/scdfe_efficiency_20261112_round3'
BASELINE = '1d21c03'


def old_module(name, filename):
    source = subprocess.check_output(['git', 'show', f'{BASELINE}:{filename}'], cwd=ROOT)
    module = ModuleType(name)
    module.__file__ = str(ROOT / filename)
    module.__package__ = name.rpartition('.')[0]
    sys.modules[name] = module
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


def paired(before, after, repeats=31):
    functions, times = (before, after), [[], []]
    for function in functions:
        for _ in range(2):
            function()
    for repeat in range(repeats):
        for index in ((0, 1) if repeat%2 else (1, 0)):
            start = time.perf_counter_ns()
            functions[index]()
            times[index].append((time.perf_counter_ns()-start)/1e6)
    stats = [dict(median_ms=statistics.median(t), samples_ms=t) for t in times]
    return dict(before=stats[0], after=stats[1], reduction_percent=(1-stats[1]['median_ms']/stats[0]['median_ms'])*100)


def dispatches(store, operation):
    counts = dict(execute=0, executemany=0)
    original = store.connection
    class Observed:
        def __init__(self, con): self.con = con
        def __getattr__(self, name): return getattr(self.con, name)
        def execute(self, sql, params=()):
            if sql.startswith('UPDATE'): counts['execute'] += 1
            return self.con.execute(sql, params)
        def executemany(self, sql, params):
            if sql.startswith('UPDATE'): counts['executemany'] += 1
            return self.con.executemany(sql, params)
    @contextmanager
    def observed():
        with original() as con: yield Observed(con)
    store.connection = observed
    try: operation()
    finally: store.connection = original
    return counts


def main():
    from client.can import CanMaster, CanFrame
    from scdfe_c.web.service import ScadaStore
    from scdfe_t.trans import TransRuntime
    old_store = old_module('scdfe_c.web._round3_before', 'scdfe_c/web/service.py').ScadaStore
    old_can = old_module('client._round3_before', 'client/can.py').CanMaster
    old_trans = old_module('scdfe_t._round3_before', 'scdfe_t/trans.py').TransRuntime
    results = []
    def record(name, timing, **details):
        row = dict(name=name, **timing, **details); results.append(row)
        print(json.dumps(dict(name=name, before_ms=timing['before']['median_ms'], after_ms=timing['after']['median_ms'], reduction_percent=timing['reduction_percent']), ensure_ascii=False), flush=True)
    with tempfile.TemporaryDirectory(prefix='scada-round3-') as tmp:
        path = Path(tmp)/'scada.sqlite3'
        after = ScadaStore(path, connection_pool_size=2)
        before = old_store(path, connection_pool_size=2)
        try:
            with after.connection() as con:
                con.executemany("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,'iec104')", [(r,r) for r in range(1,201)])
                con.executemany('INSERT INTO fes_yc_define(rtu_id,pnt_no,value) VALUES(?,?,0)', [(r,n) for r in range(1,201) for n in range(1,11)])
                con.commit()
            reads = lambda store: [store.get_row('fes_yc_define', [r,1]) for r in range(1,201)]
            assert reads(before) == reads(after)
            record('200 RTU single-row reads', paired(lambda: reads(before), lambda: reads(after)), rtus=200, points=200)
            generation = 0
            def write(store):
                nonlocal generation
                generation += 1
                return store.update_rows([('fes_yc_define',[r,n],dict(value=generation,status=generation%2)) for r in range(1,201) for n in range(1,11)])
            timing = paired(lambda: write(before), lambda: write(after), repeats=15)
            calls = [dispatches(store, lambda: write(store)) for store in (before, after)]
            with after.connection() as con:
                assert con.execute('SELECT COUNT(*) FROM fes_yc_define WHERE value=?', (generation,)).fetchone()[0] == 2000
            record('200 RTUs x 10 points atomic write', timing, points=2000, sql_dispatch_calls=calls, commits=[1,1], connection_pool_size=2)
        finally:
            before.close(); after.close()

        con = sqlite3.connect(Path(tmp)/'runtime.sqlite3')
        con.execute('PRAGMA journal_mode=WAL'); con.execute('PRAGMA synchronous=NORMAL')
        con.execute('CREATE TABLE fes_scdfe_t_point_define(channel_id INT,pnt_no INT,kind TEXT,value REAL,status INT,refresh_time INT,PRIMARY KEY(channel_id,pnt_no,kind))')
        con.executemany("INSERT INTO fes_scdfe_t_point_define VALUES(1,?,'yc',0,0,0)", [(n,) for n in range(1,2001)]); con.commit()
        @contextmanager
        def connection():
            try:
                yield con
            finally:
                con.rollback()
        engines = []
        for cls in (old_trans, TransRuntime):
            engine = object.__new__(cls); engine.channel_id=1
            engine.store = SimpleNamespace(connection=connection)
            engine._point_runtime_state={}; engine._point_runtime_pending={}
            engines.append(engine)
        generation = 0
        def publish(engine):
            nonlocal generation
            generation += 1
            engine._point_runtime_pending = {('yc',n):dict(value=generation,status=0,refresh_time=generation) for n in range(1,2001)}
            assert engine._flush_point_runtime()
        try:
            timing = paired(lambda: publish(engines[0]), lambda: publish(engines[1]))
            calls = [dispatches(engine.store, lambda: publish(engine)) for engine in engines]
            record('T runtime persistence 2000 points', timing,
                   points=2000, sql_dispatch_calls=calls, schema='minimal runtime columns, SQLite WAL')
        finally:
            con.close()

        for count in (2, 32):
            masters = [cls(interface='virtual', channel='benchmark-round3-unused', rtu_id=1) for cls in (old_can, CanMaster)]
            try:
                points = [dict(kind='yc', pnt_no=n, frame_id=123, is_fd=True, byte_offset=n%8,
                               bit_offset=n%8, bit_length=13, byte_order='big' if n%2 else 'little',
                               value_type='int' if n%3 else 'uint', scale=.25, offset=-2)
                          for n in range(1,count+1)]
                for master in masters: master.configure(points)
                frames = [CanFrame(123, bytes(range(64)), is_fd=True, timestamp=n+1) for n in range(200)]
                assert masters[0]._decode_frames(frames) == masters[1]._decode_frames(frames)
                record(f'CAN decode 200 frames x {count} signals', paired(lambda: masters[0]._decode_frames(frames), lambda: masters[1]._decode_frames(frames)),
                       frames=200, signals_per_frame=count)
            finally:
                for master in masters: master.close()
    files = ['client/can.py','scdfe_c/web/service.py','scdfe_t/trans.py']
    output = dict(baseline=BASELINE, python=platform.python_version(), platform=platform.platform(),
                  source_sha256_lf={f:hashlib.sha256((ROOT/f).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for f in files},
                  scope='Local temporary SQLite databases and real codecs; no connected device throughput claims.', results=results)
    REPORT.mkdir(parents=True,exist_ok=True)
    (REPORT/'backend.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__ == '__main__':
    main()

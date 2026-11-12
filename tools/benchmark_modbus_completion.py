"""Compare the committed S write path with atomic multi-point request commits.

Only temporary SQLite databases and loopback listeners are used. Run without
other benchmarks/tests for meaningful timings; this is not field throughput.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASELINE = 'bd41238'
OUTPUT = ROOT / 'reports/modbus_completion_20261112/benchmark.json'


def paired(functions, repeats):
    for fn in functions:
        fn()
    samples = [[], []]
    for iteration in range(repeats):
        for index in ((0, 1) if iteration % 2 else (1, 0)):
            start = time.perf_counter_ns()
            functions[index]()
            samples[index].append((time.perf_counter_ns() - start) / 1e6)
    return [dict(median_ms=statistics.median(values), samples_ms=values) for values in samples]


def baseline():
    source = subprocess.check_output(['git', 'show', f'{BASELINE}:server/modbus_mapped_server.py'], cwd=ROOT)
    module = ModuleType('server._modbus_completion_baseline')
    module.__package__ = 'server'
    exec(compile(source, f'{BASELINE}:server/modbus_mapped_server.py', 'exec'), module.__dict__)
    return module.ModbusMappedServer, hashlib.sha256(source).hexdigest()


def main():
    from scdfe_c.web.service import ScadaStore
    from server.modbus_mapped_server import ModbusMappedServer
    from tests.test_protocol_interface_contracts import free_port
    before, before_hash = baseline()
    classes = (before, ModbusMappedServer)
    result = dict(baseline_commit=BASELINE, before_sha256=before_hash,
                  after_sha256_lf=hashlib.sha256((ROOT/'server/modbus_mapped_server.py').read_bytes().replace(b'\r\n', b'\n')).hexdigest(),
                  python=platform.python_version(), platform=platform.platform(),
                  boundary='Local temporary SQLite write path and loopback datastore publication; not hardware throughput.')
    with tempfile.TemporaryDirectory(prefix='modbus-commit-benchmark-') as directory:
        store = ScadaStore(Path(directory) / 'scdfe_s.sqlite3')
        try:
            for rtu in range(1, 17):
                store.create_row('fes_rtu_define', dict(rtu_id=rtu, channel_id=rtu, channel_type='modbus-tcp'))
            with store.connection() as con:
                con.executemany('INSERT INTO fes_yc_define(rtu_id,pnt_no,value) VALUES(?,?,0)',
                                [(rtu, no) for rtu in range(1, 17) for no in range(1, 33)])
                con.commit()
            engines = []
            for cls in classes:
                group = []
                for rtu in range(1, 17):
                    engine = object.__new__(cls)
                    engine.store, engine.rtu_id = store, rtu
                    group.append(engine)
                engines.append(group)
            generation = 0
            def write(index):
                nonlocal generation
                generation += 1
                for engine in engines[index]:
                    commands = [('yc', no, generation) for no in range(1, 33)]
                    if index == 0:
                        for command in commands:
                            engine._command(*command)
                    else:
                        engine._commands(commands)
            timings = paired([lambda: write(0), lambda: write(1)], 11)
            # Count real SQL transaction boundaries independently of timing.
            original = store.connection
            counts = []
            for index in (0, 1):
                sql_counts = dict(begin=0, commit=0, select=0)
                def trace(sql):
                    key = sql.split(None, 1)[0].lower()
                    if key in sql_counts:
                        sql_counts[key] += 1
                @contextmanager
                def observed():
                    with original() as con:
                        con.set_trace_callback(trace)
                        try:
                            yield con
                        finally:
                            con.set_trace_callback(None)
                store.connection = observed
                try:
                    write(index)
                finally:
                    store.connection = original
                counts.append(sql_counts)
            with store.connection() as con:
                assert con.execute('SELECT COUNT(*) FROM fes_yc_define WHERE value=?', (generation,)).fetchone()[0] == 512
            result['database_write'] = dict(rtus=16, points_per_request=32, points=512,
                before=timings[0], after=timings[1], sql_counts=counts,
                reduction_percent=(1-timings[1]['median_ms']/timings[0]['median_ms'])*100)

            store.create_row('fes_rtu_define', dict(rtu_id=100, channel_id=100, channel_type='modbus-tcp'))
            with store.connection() as con:
                con.executemany('INSERT INTO fes_yc_define(rtu_id,pnt_no,value) VALUES(100,?,0)', [(n,) for n in range(1, 1001)])
                con.commit()
            points = [dict(kind='yc', pnt_no=n, address=n-1, register_area='input', data_type='uint16') for n in range(1, 1001)]
            live = []
            try:
                for cls in classes:
                    engine = cls(store=store, rtu_id=100, points=points, port=free_port())
                    live.append(engine)
                    engine.start()
                    engine._sync_stop.set()
                    engine._sync_thread.join(3)
                def publish(index):
                    nonlocal generation
                    generation += 1
                    engine = live[index]
                    engine.publish_updates(engine.prepare_updates([('yc', n, generation) for n in range(1, 1001)]))
                timings = paired([lambda: publish(0), lambda: publish(1)], 31)
                for engine in live:
                    assert len(engine.snapshot()) == 1000
                result['datastore_publish'] = dict(points=1000, before=timings[0], after=timings[1],
                    reduction_percent=(1-timings[1]['median_ms']/timings[0]['median_ms'])*100)
            finally:
                for engine in reversed(live):
                    engine.close()
        finally:
            store.close()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    print(json.dumps({name: {k: v for k, v in row.items() if k not in ('before', 'after')} |
        dict(before_ms=row['before']['median_ms'], after_ms=row['after']['median_ms'])
        for name, row in result.items() if name in ('database_write', 'datastore_publish')}, indent=2))


if __name__ == '__main__':
    main()

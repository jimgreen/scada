"""Compare writer preparation and actual direct/queued SQLite interactions.

Run serially on frozen sources and fresh database copies. No protocol device
is contacted. Database setup, sample generation and final checks are excluded
from timing. benchmark_rtu_scale.py separately measures concurrent IPC/drain.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import cProfile
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pickle
import platform
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scdfe_control import InstanceLock
from tools.benchmark_inputs import DEFAULT_SEED, MeasurementStream
from tools.benchmark_rtu_scale import load_runtime
from scdfe_c.web.seed_protocol_test_data import seed_protocol_test_data
from scdfe_c.web.service import ScadaStore


class CaptureQueue:
    def __init__(self):
        self.messages = []
        self.acknowledged = 0

    def put(self, message, **_kwargs):
        self.messages.append(message)

    def task_done(self):
        self.acknowledged += 1


def database_case(runtime, template, samples, messages, root, variant, stage, cycles, profile=None):
    db = root / f'{variant}_{stage}.sqlite3'
    with template.connection() as source, closing(sqlite3.connect(db)) as destination:
        source.backup(destination)
    store = ScadaStore(db)
    spec = runtime.discover_channels(store)[0]
    worker = runtime.ChannelWorker(
        store, spec, log_dir=root / 'logs', measurement_heartbeat_interval=0,
    )
    worker._load_yc_scaling()
    # The production worker already opens this connection before entering
    # its collection loop. Prime both variants in the same way.
    worker._read_connection()
    queue = CaptureQueue()
    writer = store.open_connection()
    writer.execute('PRAGMA wal_autocheckpoint=10000')
    original_open = store.open_connection
    opened = 0

    def counted_open():
        nonlocal opened
        opened += 1
        return original_open()

    store.open_connection = counted_open
    accepted = 0
    profiler = cProfile.Profile() if profile else None
    try:
        if profiler:
            profiler.enable()
        started = time.perf_counter()
        for index in range(cycles):
            slot = index % len(samples)
            if stage == 'direct_sync':
                accepted += sum(worker._sync_measurements(samples[slot]))
            else:
                if not runtime._flush_batch(store, worker.logger, [messages[slot]], queue, writer):
                    raise AssertionError('writer failed')
                accepted += 2000
        elapsed_ms = (time.perf_counter() - started) * 1000
        if profiler:
            profiler.disable()
            profiler.dump_stats(str(profile))
        timed_opens = opened
        if accepted != 2000 * cycles or worker.stats.errors:
            raise AssertionError(f'point count or error count changed: accepted={accepted} errors={worker.stats.errors}')
        if stage != 'direct_sync' and queue.acknowledged != cycles:
            raise AssertionError('queue acknowledgement count changed')
        final = {(point.kind, point.point_no): point for point in samples[(cycles - 1) % len(samples)]}
        verified = 0
        with store.connection() as connection:
            for kind in ('yc', 'yx'):
                rows = connection.execute(
                    f'SELECT * FROM fes_{kind}_define WHERE rtu_id=?', (spec.rtu_id,),
                ).fetchall()
                for row in rows:
                    point = final[(kind, row['pnt_no'])]
                    expected = {'value': point.value, 'status': point.quality, '_version': cycles + 1}
                    if kind == 'yc':
                        expected.update(raw_value=point.value, raw_status=point.quality,
                                        value=2.0 + 1.1 * point.value, raw_time=row['refresh_time'])
                    if row['refresh_time'] <= 0 or any(row[name] != value for name, value in expected.items()):
                        raise AssertionError((kind, row['pnt_no'], expected, dict(row)))
                    verified += 1
            if verified != 2000:
                raise AssertionError('final row count changed')
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
            foreign_keys = len(connection.execute('PRAGMA foreign_key_check').fetchall())
            if integrity != 'ok' or foreign_keys:
                raise AssertionError('database integrity failure')
        return {'stage': stage, 'elapsed_ms': elapsed_ms, 'point_updates': accepted,
                'opened_connections_in_timing': timed_opens, 'final_rows_verified': verified,
                'integrity_check': integrity, 'foreign_key_violations': foreign_keys}
    finally:
        if profiler:
            profiler.disable()
        store.open_connection = original_open
        worker._close_command_connection()
        writer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-file', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=100)
    parser.add_argument('--prepare-cycles', type=int, default=2000)
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if min(args.cycles, args.prepare_cycles, args.repeat) < 1:
        parser.error('cycle and repeat counts must be positive')
    repo = Path(__file__).resolve().parents[1]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(Path(tempfile.gettempdir()) / 'scada-benchmark', role='benchmark')
    if not lock.acquire():
        raise RuntimeError('another SCADA benchmark is running')
    try:
        with tempfile.TemporaryDirectory(prefix='scada-database-io-') as folder:
            root = Path(folder)
            runtimes, hashes = {}, {}
            for variant, source in {'before': args.baseline_file.read_bytes(),
                                    'after': (repo / 'scdfe_c' / '__init__.py').read_bytes()}.items():
                path = root / f'{variant}.py'
                path.write_bytes(source)
                hashes[variant] = hashlib.sha256(source).hexdigest()
                runtimes[variant] = load_runtime(path, hashes[variant])
            template = ScadaStore(root / 'template.sqlite3')
            seed_protocol_test_data(template, replicas=1)
            spec = runtimes['before'].discover_channels(template)[0]
            stream = MeasurementStream(spec.rtu_id)
            samples = [stream.next() for _ in range(16)]
            queue = CaptureQueue()
            producer = runtimes['before'].ChannelWorker(template, spec, log_dir=root / 'logs', writer_queue=queue)
            for sample in samples:
                producer._sync_measurements(sample)
            messages = queue.messages
            payload = pickle.dumps(messages)
            for message in messages:
                if runtimes['before']._prepare_batch_updates([message]) != runtimes['after']._prepare_batch_updates([message]):
                    raise AssertionError('SQL or parameters changed')
            results = []
            for repeat in range(1, args.repeat + 1):
                for variant in (('before', 'after') if repeat % 2 else ('after', 'before')):
                    runtime = runtimes[variant]
                    runtime._prepare_batch_updates([messages[0]])  # warm SQL cache
                    started = time.perf_counter()
                    for index in range(args.prepare_cycles):
                        runtime._prepare_batch_updates([messages[index % len(messages)]])
                    results.append({'repeat': repeat, 'variant': variant, 'stage': 'prepare',
                                    'elapsed_ms': (time.perf_counter() - started) * 1000,
                                    'point_updates': args.prepare_cycles * 2000})
                    print(json.dumps(results[-1]), flush=True)
                    for stage in ('direct_sync', 'writer_flush'):
                        run_root = root / f'run_{repeat}_{variant}_{stage}'
                        run_root.mkdir()
                        result = database_case(runtime, template, samples, messages, run_root,
                                               variant, stage, args.cycles)
                        results.append({'repeat': repeat, 'variant': variant, **result})
                        print(json.dumps(results[-1]), flush=True)
            if pickle.dumps(messages) != payload:
                raise AssertionError('input messages were mutated')
            profiles = []
            if args.profile:
                for variant, runtime in runtimes.items():
                    for stage in ('direct_sync', 'writer_flush'):
                        run_root = root / f'profile_{variant}_{stage}'
                        run_root.mkdir()
                        profile = repo / 'output' / f'{args.output.stem}.{variant}.{stage}.prof'
                        profile.parent.mkdir(parents=True, exist_ok=True)
                        result = database_case(runtime, template, samples, messages, run_root,
                                               variant, stage, args.cycles, profile)
                        profiles.append({'variant': variant, 'profile': str(profile), **result})
            report = {'created_at': datetime.now(timezone.utc).isoformat(),
                      'baseline_file': str(args.baseline_file.resolve()), 'runtime_sha256': hashes,
                      'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      'workload_sha256': hashlib.sha256((repo / 'tools/benchmark_inputs.py').read_bytes()).hexdigest(),
                      'payload_sha256': hashlib.sha256(payload).hexdigest(),
                      'sql_and_parameters_equal': True, 'input_unchanged': True,
                      'platform': platform.platform(), 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
                      'cycles': args.cycles, 'prepare_cycles': args.prepare_cycles, 'repeat': args.repeat,
                      'seed': DEFAULT_SEED, 'results': results, 'profiles': profiles,
                      'median_ms': {stage: {variant: statistics.median(
                          row['elapsed_ms'] for row in results if row['variant'] == variant and row['stage'] == stage)
                          for variant in runtimes} for stage in ('prepare', 'direct_sync', 'writer_flush')},
                      'scope': 'Serial preparation CPU / direct sync and SQLite commit / writer flush and SQLite commit. Samples prepared before timing; no device I/O or actual IPC.'}
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    finally:
        lock.release()


if __name__ == '__main__':
    main()

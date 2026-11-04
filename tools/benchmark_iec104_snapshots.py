"""Compare real c104 point reads and worker/IPC/writer work on frozen APIs.

The native point objects are real; frames are injected locally without a
network connection. CPU timings exclude injection, pipeline timings include
injection, native reads, measurement conversion, spawn IPC and SQLite drain.
"""
from __future__ import annotations

import argparse
import cProfile
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import importlib.util
import json
import multiprocessing as mp
from pathlib import Path
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import c104

from core.scdfe_control import InstanceLock
from tools.benchmark_rtu_scale import load_runtime, writer_entry
from scdfe_c.web.seed_protocol_test_data import seed_protocol_test_data
from scdfe_c.web.service import ScadaStore


class CountQueue:
    def __init__(self):
        self.messages = 0

    def put(self, _message, **_kwargs):
        self.messages += 1


def load_api(root, variant, source):
    digest = hashlib.sha256(source).hexdigest()
    path = root / f'{variant}.py'
    path.write_bytes(source)
    name = '_iec104_snapshots_' + digest
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.IEC104Master, digest


def samples(rtu_id):
    rng = random.Random(f'iec104-snapshot:20261102:{rtu_id}')
    keys = [(kind, n) for kind in ('yc', 'yx') for n in range(1, 1001)]
    frames, expected, previous = [], [], []
    quality_codes = (0, 1, 16, 128)
    for cycle in range(16):
        words = rng.sample(range(-8000000, 8000000), 1000)
        if previous:
            while any(word / 8 == old[0] for word, old in zip(words, previous[:1000])):
                words = rng.sample(range(-8000000, 8000000), 1000)
        values, infos = [], []
        for index, (kind, number) in enumerate(keys):
            excluded = {previous[index][1]} if previous else set()
            if cycle == 15:
                excluded.add(expected[0][index][1])
            quality = rng.choice([q for q in quality_codes if q not in excluded])
            value = words[number - 1] / 8 if kind == 'yc' else bool(rng.randrange(2))
            info = (c104.ShortInfo(actual=value, quality=c104.Quality(quality)) if kind == 'yc'
                    else c104.SingleInfo(on=value, quality=c104.Quality(quality)))
            infos.append(info)
            values.append((value, quality))
        frames.append(infos)
        expected.append(values)
        previous = values
    if any(a[1] == b[1] for a, b in zip(expected[-1], expected[0])):
        raise AssertionError('quality must change across frame wrap')
    return keys, frames, expected


def native_master(cls, rtu_id, keys):
    master = cls('127.0.0.1', rtu_id=rtu_id, init='NONE')
    points = [master.add_point(kind, n) for kind, n in keys]
    return master, points


def inject(points, frame):
    for point, info in zip(points, frame):
        point.info = info


def verify(values, rtu_id, keys, expected):
    normalized = [(v.rtu_id, v.kind, v.point_no, v.value, v.quality, v.recorded_at) for v in values]
    reference = [(rtu_id, kind, number, value, quality, None)
                 for (kind, number), (value, quality) in zip(keys, expected)]
    if normalized != reference or any(v.received_at.tzinfo is None for v in values):
        raise AssertionError('native point values/qualities/timestamps differ')


def cpu(cls, runtime, root, rtu_id, keys, frames, expected, cycles, sync):
    master, points = native_master(cls, rtu_id, keys)
    queue = CountQueue()
    worker = runtime.ChannelWorker(None, runtime.ChannelSpec(rtu_id, rtu_id, 'iec104'),
                                   log_dir=root / 'logs', writer_queue=queue)
    worker._yc_scaling = dict.fromkeys(range(1, 1001), (2.0, 1.1))
    elapsed = 0.0
    try:
        for cycle in range(cycles):
            index = cycle % len(frames)
            inject(points, frames[index])
            started = time.perf_counter()
            values = master._poll()
            if sync and worker._sync_measurements(values) != (1000, 1000):
                raise AssertionError('worker update counts changed')
            elapsed += time.perf_counter() - started
            if cycle < len(frames) or cycle == cycles - 1:
                verify(values, rtu_id, keys, expected[index])
        if sync and (worker.stats.errors or queue.messages != cycles):
            raise AssertionError('worker queue/error counts changed')
        return elapsed * 1000
    finally:
        master.close()


def pipeline(cls, runtime, template, station, root, keys, frames, expected, cycles,
             runtime_path, runtime_hash):
    root.mkdir()
    with template.connection() as source, closing(sqlite3.connect(root / 'scada.sqlite3')) as target:
        source.backup(target)
    store = ScadaStore(root / 'scada.sqlite3')
    context = mp.get_context('spawn')
    queue = context.JoinableQueue(maxsize=64)
    ready = context.Event()
    writer = context.Process(target=writer_entry, args=(
        str(store.db_path), str(store.catalog.directory), str(root / 'logs'), queue, ready,
        str(runtime_path), runtime_hash))
    master, points = native_master(cls, station['rtu_id'], keys)
    worker = runtime.ChannelWorker(store, runtime.ChannelSpec.from_row(station), log_dir=root / 'logs',
                                   writer_queue=queue, retry_interval=30)
    worker._load_yc_scaling()
    success = False
    writer.start()
    try:
        if not ready.wait(30):
            raise RuntimeError('writer not ready')
        started = time.perf_counter()
        updated = 0
        for cycle in range(cycles):
            inject(points, frames[cycle % len(frames)])
            updated += sum(worker._sync_measurements(master._poll()))
        queue.put(runtime.BATCH_WRITER_STOP, timeout=30)
        writer.join(120)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if writer.is_alive() or writer.exitcode != 0 or updated != cycles * 2000 or worker.stats.errors:
            raise AssertionError((writer.exitcode, updated, worker.stats.errors))
        queue.join()
        reference = dict(zip(keys, expected[(cycles - 1) % len(frames)]))
        verified = 0
        with store.connection() as connection:
            for kind in ('yc', 'yx'):
                rows = connection.execute(f'SELECT * FROM fes_{kind}_define WHERE rtu_id=?',
                                          (station['rtu_id'],)).fetchall()
                if len(rows) != 1000:
                    raise AssertionError('point count mismatch')
                for row in rows:
                    raw, quality = reference[(kind, row['pnt_no'])]
                    fields = {'value': raw, 'status': quality, '_version': cycles + 1}
                    if kind == 'yc':
                        fields.update(raw_value=raw, raw_status=quality, raw_time=row['refresh_time'],
                                      value=row['raw_base'] + row['raw_scale'] * raw)
                    if row['refresh_time'] <= 0 or any(row[k] != v for k, v in fields.items()):
                        raise AssertionError((fields, dict(row)))
                    verified += 1
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
            foreign_keys = len(connection.execute('PRAGMA foreign_key_check').fetchall())
        if integrity != 'ok' or foreign_keys:
            raise AssertionError((integrity, foreign_keys))
        success = True
        return {'elapsed_ms': elapsed_ms, 'updates': updated, 'final_rows_verified': verified,
                'writer_exitcode': writer.exitcode, 'integrity': integrity,
                'foreign_key_violations': foreign_keys}
    finally:
        master.close()
        if writer.is_alive():
            writer.terminate()
            writer.join(10)
        if not success:
            queue.cancel_join_thread()
        queue.close()
        if success:
            queue.join_thread()
        writer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-dir', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=200)
    parser.add_argument('--pipeline-cycles', type=int, default=100)
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if min(args.cycles, args.pipeline_cycles, args.repeat) < 1:
        parser.error('cycle/repeat counts must be positive')
    repo = Path(__file__).resolve().parents[1]
    lock = InstanceLock(Path(tempfile.gettempdir()) / 'scada-benchmark', role='benchmark')
    if not lock.acquire():
        raise RuntimeError('another SCADA benchmark is running')
    try:
        with tempfile.TemporaryDirectory(prefix='scada-iec104-snapshots-') as directory:
            root = Path(directory)
            classes, hashes = {}, {}
            for variant, location in (('before', args.baseline_dir), ('after', repo / 'client')):
                classes[variant], hashes[variant] = load_api(root, variant, (location / 'iec104.py').read_bytes())
            runtime_path = root / 'scdfe_c' / '__init__.py'
            runtime_path.write_bytes((repo / 'scdfe_c' / '__init__.py').read_bytes())
            runtime_hash = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
            runtime = load_runtime(runtime_path, runtime_hash)
            store = ScadaStore(root / 'template.sqlite3')
            seed_protocol_test_data(store, replicas=1)
            with store.connection() as connection:
                station = dict(connection.execute(
                    "SELECT * FROM fes_rtu_define WHERE channel_type='iec104'").fetchone())
            keys, frames, expected = samples(station['rtu_id'])
            results, profiles = [], {}
            for repeat in range(1, args.repeat + 1):
                for variant in (('before', 'after') if repeat % 2 else ('after', 'before')):
                    for sync, case in ((False, 'native_point_read'), (True, 'native_read_and_sync')):
                        elapsed_ms = cpu(classes[variant], runtime, root, station['rtu_id'], keys,
                                         frames, expected, args.cycles, sync)
                        results.append({'repeat': repeat, 'variant': variant, 'case': case,
                                        'elapsed_ms': elapsed_ms})
                    result = pipeline(classes[variant], runtime, store, station, root / f'{repeat}-{variant}',
                                      keys, frames, expected, args.pipeline_cycles, runtime_path, runtime_hash)
                    results.append({'repeat': repeat, 'variant': variant, 'case': 'native_ipc_sqlite', **result})
                    print(json.dumps(results[-3:]), flush=True)
            if args.profile:
                for variant in classes:
                    master, points = native_master(classes[variant], station['rtu_id'], keys)
                    try:
                        inject(points, frames[0])
                        profiler = cProfile.Profile()
                        profiler.enable()
                        for _ in range(100):
                            master._poll()
                        profiler.disable()
                        path = repo / 'output' / f'{args.output.stem}.{variant}.prof'
                        path.parent.mkdir(parents=True, exist_ok=True)
                        profiler.dump_stats(str(path))
                        profiles[variant] = str(path)
                    finally:
                        master.close()
            report = {'created_at': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
                      'python': sys.version, 'sqlite': sqlite3.sqlite_version,
                      'c104_version': version('c104'),
                      'config': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                      'protocol_sha256': hashes, 'runtime_sha256': runtime_hash,
                      'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      'points_per_sample': 2000, 'native_values_quality_and_timestamps_verified': True,
                      'scope': 'real c104 native points; local frame injection; no IEC104 network transport',
                      'timing': 'CPU excludes injection and verification; pipeline includes injection, read, sync, IPC, writer drain',
                      'results': results, 'profiles': profiles,
                      'medians_ms': {case: {variant: statistics.median(
                          r['elapsed_ms'] for r in results if r['case'] == case and r['variant'] == variant)
                          for variant in classes} for case in sorted({r['case'] for r in results})}}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    finally:
        lock.release()


if __name__ == '__main__':
    mp.freeze_support()
    main()

"""Frozen-source Modbus CPU, TCP loopback and worker/IPC/SQLite comparisons.

All configurations and databases are temporary. RTU CPU/worker cases simulate
the transport; the wire case uses a real Modbus TCP server on 127.0.0.1.
"""
from __future__ import annotations

import argparse
import cProfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib
import json
import multiprocessing as mp
from pathlib import Path
import platform
import pstats
import random
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scdfe_control import InstanceLock
from tools.benchmark_rtu_scale import load_runtime, writer_entry
from scdfe_c.web.seed_protocol_test_data import seed_protocol_test_data
from scdfe_c.web.service import ScadaStore


class SimulatedReply:
    def __init__(self, values, bits):
        self.bits = values if bits else []
        self.registers = [] if bits else values

    def isError(self):
        return False


class SimulatedNative:
    """Deterministic inputs; no timing delays or synthetic quality codes."""
    def __init__(self, frames, serial=False, capture=False):
        self.frames, self.serial, self.capture = frames, serial, capture
        self.index = -1
        self.unit_id = 1
        self.is_open = self.connected = True
        self.requests = 0
        self.trace = []

    def open(self):
        self.is_open = self.connected = True
        return True

    connect = open

    def close(self):
        self.is_open = self.connected = False

    def __getattr__(self, name):
        if not name.startswith('read_'):
            raise AttributeError(name)

        def read(address, count, device_id=None):
            key = (name, address, count, self.unit_id if device_id is None else device_id)
            self.requests += 1
            if self.capture:
                self.trace.append(key)
            values = list(self.frames[self.index % len(self.frames)][key])
            return SimulatedReply(values, name in {'read_coils', 'read_discrete_inputs'}) if self.serial else values
        return read


def source_package(directory, variant, sources):
    package_dir = directory / variant
    package_dir.mkdir()
    hashes = {}
    for name, source in sources.items():
        (package_dir / name).write_bytes(source)
        hashes[name] = hashlib.sha256(source).hexdigest()
    package_name = '_modbus_benchmark_' + variant + '_' + hashes['modbus_tcp.py']
    package = types.ModuleType(package_name)
    package.__path__ = [str(package_dir)]
    sys.modules[package_name] = package
    return {protocol: getattr(importlib.import_module(package_name + '.' + module), cls)
            for protocol, module, cls in (
                ('modbus-tcp', 'modbus_tcp', 'ModbusTcpMaster'),
                ('modbus-rtu', 'modbus_rtu', 'ModbusRtuMaster'))}, hashes


def station_inputs(store):
    with store.connection() as connection:
        stations = [dict(row) for row in connection.execute(
            "SELECT * FROM fes_rtu_define WHERE channel_type IN ('modbus-tcp','modbus-rtu') ORDER BY rtu_id")]
        for station in stations:
            station['calls'] = [dict(row) for row in connection.execute(
                'SELECT * FROM fes_channel_modbus_define_call WHERE channel_id=? ORDER BY call_seq', (station['channel_id'],))]
            station['points'] = [dict(row) for row in connection.execute(
                'SELECT * FROM fes_channel_modbus_define_data WHERE channel_id=? ORDER BY id', (station['channel_id'],))]
            rng = random.Random(f"modbus-poll:20261102:{station['rtu_id']}")
            frames = []
            bits = [bool(rng.randrange(2)) for _ in range(1000)]
            for cycle in range(16):
                words = rng.sample(range(1, 65536), 1000)
                if frames:
                    previous = station['words']
                    while any(a == b for a, b in zip(words, previous)):
                        words = rng.sample(range(1, 65536), 1000)
                station['words'] = words
                frame = {}
                for call in station['calls']:
                    fc, address, count = call['icode'], call['iplace'], call['inum']
                    name = {1: 'read_coils', 3: 'read_holding_registers'}[fc]
                    frame[(name, address, count, call['irtuaddr'])] = (
                        words[address:address + count] if fc == 3 else
                        [bit ^ bool(cycle % 2) for bit in bits[address:address + count]])
                frames.append(frame)
            # Cyclic input reuse must still change every point at the wrap.
            for key, values in frames[0].items():
                if not all(a != b for a, b in zip(values, frames[-1][key])):
                    raise AssertionError('cyclic samples must change every point')
            station['frames'] = frames
    return stations


def simulated_master(cls, station, capture=False):
    serial = station['channel_type'] == 'modbus-rtu'
    master = cls('loop://' if serial else '127.0.0.1', rtu_id=station['rtu_id'],
                 calls=station['calls'], points=station['points'])
    native = SimulatedNative(station['frames'], serial, capture)
    master.native = native
    collect = master.collect_once

    def next_sample():
        native.index += 1
        return collect()
    master.collect_once = next_sample
    return master


def normalized(values):
    return [(v.rtu_id, v.call_seq, v.point_no, v.kind, v.value) for v in values]


def collect_cycles(master, cycles):
    for _ in range(cycles):
        values = master.collect_once()
        if len(values) != 2000:
            raise AssertionError('unexpected point count')
    return values


def pipeline(template, stations, classes, root, cycles, runtime_path, runtime_hash):
    runtime = load_runtime(runtime_path, runtime_hash)
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
    writer.start()
    release = threading.Barrier(len(stations) + 1, timeout=30)
    success = False

    def run_station(station):
        master = simulated_master(classes[station['channel_type']], station)
        spec = runtime.ChannelSpec.from_row(station)
        worker = runtime.ChannelWorker(store, spec, log_dir=root / 'logs', poll_interval=0,
                                       writer_queue=queue, master_factory=lambda *_: master)
        release.wait()
        stats = worker.run(max_cycles=cycles)
        if (stats.errors or stats.cycles != cycles or stats.sent_yk or stats.sent_yt
                or master.native.requests != cycles * len(station['calls'])):
            raise AssertionError(vars(stats))
        return stats.updated_yc + stats.updated_yx

    try:
        if not ready.wait(30):
            raise RuntimeError('writer did not become ready')
        with ThreadPoolExecutor(max_workers=len(stations)) as executor:
            futures = [executor.submit(run_station, station) for station in stations]
            started = time.perf_counter()
            release.wait()
            updates = sum(f.result(timeout=120) for f in futures)
            queue.put(runtime.BATCH_WRITER_STOP, timeout=30)
            writer.join(120)
            elapsed_ms = (time.perf_counter() - started) * 1000
        if writer.is_alive() or writer.exitcode != 0 or updates != cycles * 4000:
            raise AssertionError((writer.exitcode, updates))
        queue.join()
        verified = 0
        for station in stations:
            master = simulated_master(classes[station['channel_type']], station)
            master.native.index = (cycles - 1) % 16 - 1
            expected = {(v.kind, v.point_no): v.value for v in master.collect_once()}
            master.close()
            with store.connection() as connection:
                for kind in ('yc', 'yx'):
                    rows = connection.execute(f'SELECT * FROM fes_{kind}_define WHERE rtu_id=?',
                                              (station['rtu_id'],)).fetchall()
                    if len(rows) != 1000:
                        raise AssertionError('persisted row count mismatch')
                    for row in rows:
                        raw = expected[(kind, row['pnt_no'])]
                        fields = {'value': raw, 'status': 0, '_version': cycles + 1}
                        if kind == 'yc':
                            fields.update(raw_value=raw, raw_status=0, raw_time=row['refresh_time'],
                                          value=row['raw_base'] + row['raw_scale'] * raw)
                        if row['refresh_time'] <= 0 or any(row[k] != v for k, v in fields.items()):
                            raise AssertionError((fields, dict(row)))
                        verified += 1
        with store.connection() as connection:
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
            foreign_keys = len(connection.execute('PRAGMA foreign_key_check').fetchall())
        if integrity != 'ok' or foreign_keys:
            raise AssertionError((integrity, foreign_keys))
        success = True
        return {'elapsed_ms': elapsed_ms, 'updates': updates, 'final_rows_verified': verified,
                'writer_exitcode': writer.exitcode, 'integrity': integrity, 'foreign_key_violations': foreign_keys}
    finally:
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
    parser.add_argument('--cycles', type=int, default=300)
    parser.add_argument('--wire-cycles', type=int, default=50)
    parser.add_argument('--pipeline-cycles', type=int, default=40)
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if min(args.cycles, args.wire_cycles, args.pipeline_cycles, args.repeat) < 1:
        parser.error('cycle and repeat counts must be positive')
    repo = Path(__file__).resolve().parents[1]
    lock = InstanceLock(Path(tempfile.gettempdir()) / 'scada-benchmark', role='benchmark')
    if not lock.acquire():
        raise RuntimeError('another SCADA benchmark is running')
    try:
        with tempfile.TemporaryDirectory(prefix='scada-modbus-poll-') as directory:
            root = Path(directory)
            classes, hashes = {}, {}
            for variant, location in (('before', args.baseline_dir), ('after', repo / 'client')):
                classes[variant], hashes[variant] = source_package(
                    root, variant, {name: (location / name).read_bytes() for name in ('modbus_tcp.py', 'modbus_rtu.py')})
            runtime_path = root / 'scdfe_c' / '__init__.py'
            runtime_path.write_bytes((repo / 'scdfe_c' / '__init__.py').read_bytes())
            runtime_hash = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
            store = ScadaStore(root / 'template.sqlite3')
            seed_protocol_test_data(store, replicas=1)
            stations = station_inputs(store)
            for station in stations:
                values, traces = {}, {}
                for variant in classes:
                    master = simulated_master(classes[variant][station['channel_type']], station, capture=True)
                    values[variant] = [normalized(master.collect_once()) for _ in range(16)]
                    traces[variant] = master.native.trace
                    master.close()
                if values['before'] != values['after'] or traces['before'] != traces['after']:
                    raise AssertionError('values or request sequence differ')
            from pyModbusTCP.server import DataBank, ModbusServer
            bank = DataBank(h_regs_size=1000, coils_size=1000)
            tcp = next(s for s in stations if s['channel_type'] == 'modbus-tcp')
            for (name, address, _count, _unit), values in tcp['frames'][0].items():
                (bank.set_coils if name == 'read_coils' else bank.set_holding_registers)(address, values)
            server = ModbusServer(host='127.0.0.1', port=0, no_block=True, data_bank=bank)
            server.start()
            results, profiles = [], {}
            try:
                for repeat in range(1, args.repeat + 1):
                    order = ('before', 'after') if repeat % 2 else ('after', 'before')
                    for variant in order:
                        for station in stations:
                            protocol = station['channel_type']
                            master = simulated_master(classes[variant][protocol], station)
                            expected_master = simulated_master(classes['before'][protocol], station)
                            expected_master.native.index = (args.cycles - 1) % 16 - 1
                            expected = normalized(expected_master.collect_once())
                            started = time.perf_counter()
                            values = collect_cycles(master, args.cycles)
                            elapsed_ms = (time.perf_counter() - started) * 1000
                            if normalized(values) != expected or master.native.requests != args.cycles * 9:
                                raise AssertionError('CPU result mismatch')
                            master.close()
                            expected_master.close()
                            results.append({'repeat': repeat, 'variant': variant, 'case': 'cpu_' + protocol,
                                            'elapsed_ms': elapsed_ms})
                        master = classes[variant]['modbus-tcp'](
                            '127.0.0.1', server._service.server_address[1], rtu_id=tcp['rtu_id'],
                            calls=tcp['calls'], points=tcp['points'], timeout=2)
                        try:
                            master.connect()
                            collect_cycles(master, 1)
                            started = time.perf_counter()
                            values = collect_cycles(master, args.wire_cycles)
                            elapsed_ms = (time.perf_counter() - started) * 1000
                            expected_master = simulated_master(classes['before']['modbus-tcp'], tcp)
                            if normalized(values) != normalized(expected_master.collect_once()):
                                raise AssertionError('wire result mismatch')
                            expected_master.close()
                        finally:
                            master.close()
                        results.append({'repeat': repeat, 'variant': variant, 'case': 'tcp_loopback',
                                        'elapsed_ms': elapsed_ms})
                        result = pipeline(store, stations, classes[variant], root / f'pipeline-{repeat}-{variant}',
                                          args.pipeline_cycles, runtime_path, runtime_hash)
                        results.append({'repeat': repeat, 'variant': variant, 'case': 'worker_ipc_sqlite', **result})
                        print(json.dumps(results[-4:], ensure_ascii=False), flush=True)
                if args.profile:
                    for variant in classes:
                        master = simulated_master(classes[variant]['modbus-tcp'], tcp)
                        profiler = cProfile.Profile()
                        profiler.runcall(collect_cycles, master, 100)
                        master.close()
                        path = repo / 'output' / f'{args.output.stem}.{variant}.prof'
                        path.parent.mkdir(parents=True, exist_ok=True)
                        profiler.dump_stats(str(path))
                        stats = pstats.Stats(profiler)
                        profiles[variant] = {'path': str(path), 'total_seconds': stats.total_tt,
                            'functions': [{'name': key[2], 'calls': value[1], 'cumulative_seconds': value[3]}
                                          for key, value in stats.stats.items()
                                          if key[2] in {'field', 'decode_point', '_collect_modbus', '_build_poll_plan', '_execute_poll_plan'}]}
            finally:
                server.stop()
            report = {
                'created_at': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
                'python': sys.version, 'sqlite': sqlite3.sqlite_version,
                'config': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                'protocol_sha256': hashes, 'runtime_sha256': runtime_hash,
                'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'points_per_station': {'yc': 1000, 'yx': 1000}, 'calls_per_cycle': 9,
                'request_order_and_16_cycles_of_values_equal': True, 'seed': '20261102',
                'scope': 'CPU simulated TCP/RTU; real TCP loopback; two simulated transport workers plus real spawn writer/IPC/SQLite',
                'quality': 'Modbus has no quality field; production maps successful reads to zero; no synthetic quality injected',
                'results': results, 'profiles': profiles,
                'medians_ms': {case: {variant: statistics.median(
                    r['elapsed_ms'] for r in results if r['case'] == case and r['variant'] == variant)
                    for variant in classes} for case in sorted({r['case'] for r in results})},
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    finally:
        lock.release()


if __name__ == '__main__':
    mp.freeze_support()
    main()

"""Compare writer parameter preparation on identical random point messages.

This is a CPU microbenchmark: no SQLite commit or IPC time is included.
Use benchmark_rtu_scale.py for the actual end-to-end result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import pickle
import platform
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scdfe_control import InstanceLock
from tools.benchmark_inputs import DEFAULT_SEED, MeasurementStream
from tools.benchmark_rtu_scale import load_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', required=True)
    parser.add_argument('--cycles', type=int, default=700)
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.cycles < 1 or args.repeat < 1:
        parser.error('cycles/repeat must be positive')
    repo = Path(__file__).resolve().parents[1]
    lock = InstanceLock(Path(tempfile.gettempdir()) / 'scada-benchmark', role='benchmark')
    if not lock.acquire():
        raise RuntimeError('another SCADA benchmark is running')
    try:
        with tempfile.TemporaryDirectory(prefix='scada-prepare-') as directory:
            root = Path(directory)
            sources = {'before': subprocess.check_output(
                ['git', 'show', f'{args.baseline_ref}:scdfe_c.py'], cwd=repo),
                'after': (repo / 'scdfe_c.py').read_bytes()}
            runtimes, hashes = {}, {}
            for variant, source in sources.items():
                path = root / f'{variant}.py'
                path.write_bytes(source)
                hashes[variant] = hashlib.sha256(source).hexdigest()
                runtimes[variant] = load_runtime(path, hashes[variant])

            class CaptureQueue:
                def __init__(self):
                    self.messages = []

                def put(self, message, **_kwargs):
                    self.messages.append(message)

            # Use the production producer so the optimized internal
            # `_unique_rows` marker is present in the identical payload sent
            # to both writer implementations.
            runtime = runtimes['after']
            queue = CaptureQueue()
            worker = runtime.ChannelWorker(None, runtime.ChannelSpec(7, 7, 'custom', None, None),
                                           log_dir=root / 'logs', writer_queue=queue)
            worker._yc_scaling = dict.fromkeys(range(1, 1001), (2.0, 1.1))
            stream = MeasurementStream(7)
            for _ in range(16):
                worker._sync_measurements(stream.next())
            payload = pickle.dumps(queue.messages)
            for message in queue.messages:
                before = runtimes['before']._prepare_batch_updates([message])
                after = runtimes['after']._prepare_batch_updates([message])
                if before != after:
                    raise AssertionError('prepared SQL or parameters differ')
            inputs = [[message] for message in queue.messages]
            results = []
            for repeat in range(1, args.repeat + 1):
                for variant in (('before', 'after') if repeat % 2 else ('after', 'before')):
                    prepare = runtimes[variant]._prepare_batch_updates
                    started = time.perf_counter()
                    for index in range(args.cycles):
                        prepare(inputs[index % len(inputs)])
                    elapsed = time.perf_counter() - started
                    row = {'repeat': repeat, 'variant': variant, 'elapsed_ms': elapsed * 1000,
                           'point_updates': args.cycles * 2000,
                           'updates_per_second': args.cycles * 2000 / elapsed}
                    results.append(row)
                    print(json.dumps(row), flush=True)
            if payload != pickle.dumps(queue.messages):
                raise AssertionError('input messages were mutated')
            medians = {v: statistics.median(r['elapsed_ms'] for r in results if r['variant'] == v)
                       for v in runtimes}
            report = {'scope': 'CPU preparation only; unique canonical keys, 1000 YC + 1000 YX',
                      'baseline_ref': args.baseline_ref, 'cycles': args.cycles, 'repeat': args.repeat,
                      'seed': DEFAULT_SEED, 'platform': platform.platform(), 'python': sys.version,
                      'runtime_sha256': hashes, 'payload_sha256': hashlib.sha256(payload).hexdigest(),
                      'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      'sql_and_parameters_equal': True, 'input_unchanged': True,
                      'median_ms': medians, 'results': results}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    finally:
        lock.release()


if __name__ == '__main__':
    main()

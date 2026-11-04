"""Benchmark unchanged communication-command database polls.

The worker reads command version totals on every communication cycle but only
loads command rows when the aggregate changed. The frozen baseline and current
runtime are exercised against the same 60-RTU SQLite fixture.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scdfe_c.web.seed_protocol_test_data import seed_protocol_test_data
from scdfe_c.web.service import ScadaStore


def load_runtime(path: Path, digest: str):
    spec = importlib.util.spec_from_file_location(f'command_poll_{digest}', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def measure(runtime, db, root, cycles, *, changed):
    store = ScadaStore(db)
    workers = []
    try:
        for spec in runtime.discover_channels(store):
            worker = runtime.ChannelWorker(store, spec, log_dir=root / 'logs')
            worker._command_seen = {}
            first = worker._command_rows()
            if first is None:
                raise AssertionError('first command poll unexpectedly skipped')
            worker._sync_commands()
            workers.append(worker)
        statements = []
        for worker in workers:
            worker._command_connection.set_trace_callback(statements.append)
        started = time.perf_counter()
        skipped = 0
        for cycle in range(cycles):
            worker = workers[cycle % len(workers)]
            if changed:
                worker._command_version = -1
            if worker._command_rows() is None:
                skipped += 1
        elapsed = (time.perf_counter() - started) * 1000
        transactions = sum(statement in {'BEGIN', 'COMMIT', 'ROLLBACK'} for statement in statements)
        return dict(elapsed_ms=elapsed, cycles=cycles, workers=len(workers), skipped=skipped,
                    command_statements=len(statements), transaction_statements=transactions)
    finally:
        for worker in workers:
            worker._close_command_connection()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-file', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=10000)
    parser.add_argument('--repeat', type=int, default=7)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    before = args.baseline_file.read_bytes()
    after = (root / 'scdfe_c' / '__init__.py').read_bytes()
    hashes = {'before': hashlib.sha256(before).hexdigest(),
              'after': hashlib.sha256(after).hexdigest()}
    results = []
    with tempfile.TemporaryDirectory(prefix='scada-command-poll-') as folder:
        temp = Path(folder)
        db = temp / 'fixture.sqlite3'
        seed_protocol_test_data(ScadaStore(db), replicas=10)
        runtimes = {}
        for variant, source in (('before', before), ('after', after)):
            source_path = temp / f'{variant}.py'
            source_path.write_bytes(source)
            runtimes[variant] = load_runtime(source_path, hashes[variant])
        for repeat in range(1, args.repeat + 1):
            for variant in (('before', 'after') if repeat % 2 else ('after', 'before')):
                for changed in (False, True):
                    result = measure(runtimes[variant], db, temp, args.cycles, changed=changed)
                    result.update(repeat=repeat, variant=variant,
                                  case='changed' if changed else 'unchanged',
                                  ms_per_cycle=result['elapsed_ms'] / args.cycles)
                    results.append(result)
                    print(json.dumps(result), flush=True)
    medians = {}
    for case in ('unchanged', 'changed'):
        medians[case] = {}
        for metric in ('elapsed_ms', 'ms_per_cycle', 'command_statements', 'transaction_statements'):
            medians[case][metric] = {variant: statistics.median(row[metric] for row in results
                                                                if row['variant'] == variant and row['case'] == case)
                                     for variant in ('before', 'after')}
    report = dict(source_sha256=hashes, platform=platform.platform(), python=sys.version,
                  fixture='60 RTUs, 200 YK + 200 YT rows per RTU, unchanged command versions',
                  method='Seven alternating repeats; first changed poll excluded; each sample cycles through 60 workers',
                  cycles=args.cycles, repeat=args.repeat, results=results, medians=medians)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('MEDIANS', json.dumps(medians), flush=True)


if __name__ == '__main__':
    main()

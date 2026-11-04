"""Benchmark command-definition lookup and CAN batch reception.

The frozen baseline and current adapter are loaded independently. No field
device, formal database, or network socket is used.
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import statistics
import sys
import time
from collections import deque

import can

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load(path, module_name):
    source = path.read_bytes()
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module, hashlib.sha256(source).hexdigest()


class FakeBus:
    def __init__(self, messages):
        self.messages = deque(messages)

    def recv(self, _timeout):
        return self.messages.popleft() if self.messages else None

    def shutdown(self):
        pass


def rows(protocol, count=200):
    if protocol == 'modbus':
        return [dict(rtu_id=7, cnt=i, ifyk=kind, irtuaddr=1, reg_addr=i,
                     icode=5 if kind else 6, ytyk_type=6 if kind else 2)
                for kind in (1, 0) for i in range(1, count + 1)]
    result = []
    for kind in ('yk', 'yt'):
        for i in range(1, count + 1):
            row = dict(rtu_id=7, kind=kind, pnt_no=i)
            if protocol == 'opc':
                row['node_id'] = f'ns=2;s={kind.upper()}_{i}'
            else:
                row.update(object_type='binary-output' if kind == 'yk' else 'analog-output', instance=i)
            result.append(row)
    return result


def command_case(module, protocol, variant, cycles, profile):
    if protocol == 'modbus':
        master = module.ModbusTcpMaster('127.0.0.1', rtu_id=7)
        defs = rows(protocol)
        master.configure([], [], defs)
        def operation():
            return [master._point_definition(i, None, kind)
                    for kind in (1, 0) for i in range(1, 201)]
    elif protocol == 'opc':
        master = module.OpcUaMaster('opc.tcp://127.0.0.1:4840', rtu_id=7, backend=object())
        defs = rows(protocol)
        master.configure(defs)
        def operation():
            return [master._point_definition(kind, i) for kind in ('yk', 'yt') for i in range(1, 201)]
    else:
        master = module.BacnetMaster('127.0.0.1', rtu_id=7, backend=object())
        defs = rows(protocol)
        master.configure(defs)
        def operation():
            return [master._point_definition(kind, i) for kind in ('yk', 'yt') for i in range(1, 201)]
    profiler = cProfile.Profile() if profile else None
    start = time.perf_counter()
    if profiler:
        profiler.enable()
    for _ in range(cycles):
        operation()
    if profiler:
        profiler.disable()
    elapsed = (time.perf_counter() - start) * 1000
    result = dict(case=f'{protocol}_command_lookup', variant=variant, cycles=cycles,
                  operations=cycles * 400, ms_per_operation=elapsed / cycles / 400)
    if profiler:
        result['profile_calls'] = profiler.getstats().__len__()
    return result


def can_case(module, variant, frames, cycles, profile):
    master = module.CanMaster()
    master.bus = FakeBus([can.Message(arbitration_id=i % 2048, data=bytes((i % 256,)) * 8)
                          for i in range(frames)])
    profiler = cProfile.Profile() if profile else None
    start = time.perf_counter()
    if profiler:
        profiler.enable()
    total = 0
    for _ in range(cycles):
        master.bus.messages.extend(can.Message(arbitration_id=i % 2048, data=bytes((i % 256,)) * 8)
                                    for i in range(frames))
        total += len(master.collect_once(timeout=0, max_frames=frames))
    if profiler:
        profiler.disable()
    elapsed = (time.perf_counter() - start) * 1000
    master.close()
    result = dict(case='can_batch_receive', variant=variant, cycles=cycles, frames=frames,
                  ms_per_frame=elapsed / total, total_frames=total)
    if profiler:
        result['profile_calls'] = profiler.getstats().__len__()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-dir', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--cycles', type=int, default=1000)
    parser.add_argument('--frames', type=int, default=10000)
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    modules, hashes = {}, {}
    for protocol, filename in [('modbus', 'modbus_tcp.py'), ('opc', 'opc.py'), ('bacnet', 'bacnet.py'), ('can', 'can.py')]:
        modules[protocol], hashes[protocol] = {}, {}
        for variant, base in [('before', args.baseline_dir), ('after', root)]:
            modules[protocol][variant], hashes[protocol][variant] = load(
                base / 'client' / filename, f'round15_{protocol}_{variant}')
    results = []
    for repeat in range(args.repeat):
        for protocol in ('modbus', 'opc', 'bacnet'):
            for variant in (('before', 'after') if repeat % 2 == 0 else ('after', 'before')):
                result = command_case(modules[protocol][variant], protocol, variant, args.cycles, False)
                result['repeat'] = repeat + 1
                results.append(result)
        for variant in (('before', 'after') if repeat % 2 == 0 else ('after', 'before')):
            result = can_case(modules['can'][variant], variant, args.frames, 10, False)
            result['repeat'] = repeat + 1
            results.append(result)
    profiles = []
    if args.profile:
        for protocol in ('modbus', 'opc', 'bacnet'):
            for variant in ('before', 'after'):
                profiles.append(command_case(modules[protocol][variant], protocol, variant, 100, True))
        for variant in ('before', 'after'):
            profiles.append(can_case(modules['can'][variant], variant, args.frames, 2, True))
    medians = {}
    for case in dict.fromkeys(row['case'] for row in results):
        key = 'ms_per_frame' if case == 'can_batch_receive' else 'ms_per_operation'
        medians[case] = {variant: statistics.median(row[key] for row in results
                                                    if row['case'] == case and row['variant'] == variant)
                         for variant in ('before', 'after')}
    report = dict(source_sha256=hashes, platform=platform.platform(), python=sys.version,
                  method='Seven alternating repeats; command lookup uses 400 lookups per cycle; '
                         'CAN uses a fake python-can bus with real Message conversion; profiler separate',
                  arguments=vars(args) | {'baseline_dir': str(args.baseline_dir), 'output': str(args.output)},
                  results=results, profiles=profiles, medians_ms=medians)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(medians), flush=True)


if __name__ == '__main__':
    main()

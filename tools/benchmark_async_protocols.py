"""Compare frozen OPC UA/BACnet collection against current adapters.

CPU cases use real loop threads and deterministic in-memory protocol replies.
Wire cases use real asyncua/bacpypes3 servers bound only to 127.0.0.1.
No formal SCADA database, process controls, or field devices are used.
"""
from __future__ import annotations

import argparse
import asyncio
import cProfile
import hashlib
import importlib.util
from importlib.metadata import version
import json
from pathlib import Path
import platform
import pstats
import random
import statistics
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asyncua import ua
from tests.test_bacnet import _BacnetServer, _udp_port
from tests.test_opc import _OpcServer


def load_adapter(path, protocol, variant):
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    name = f'_async_protocol_benchmark_{protocol}_{variant}_{digest}'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return getattr(module, 'OpcUaMaster' if protocol == 'opc' else 'BacnetMaster'), digest


def rows_for(protocol, size):
    rows = []
    for index in range(size):
        kind = 'yc' if index < size // 2 else 'yx'
        number = index % (size // 2) + 1
        row = dict(rtu_id=7, kind=kind, pnt_no=number)
        if protocol == 'opc':
            row['node_id'] = f'ns=2;s=PERF_{kind.upper()}_{number}'
        else:
            row.update(object_type='analog-value' if kind == 'yc' else 'binary-value',
                       instance=index + 10001)
        rows.append(row)
    return rows


def input_values(rows, cycle):
    rng = random.Random(f'async-protocols:20261102:{len(rows)}:{cycle}')
    # Distinct analog values within each sample; each point changes over time.
    numbers = rng.sample(range(-8_000_000, 8_000_000), len(rows))
    return [numbers[i] / 8 if row['kind'] == 'yc' else bool(rng.randrange(2))
            for i, row in enumerate(rows)]


def verify(values, rows, expected, statuses=None):
    assert len(values) == len(rows)
    for i, (value, row, reference) in enumerate(zip(values, rows, expected)):
        assert (value.rtu_id, value.kind, value.point_no, value.value) == (
            7, row['kind'], row['pnt_no'], reference)
        assert value.received_at.tzinfo is not None
        if statuses is not None:
            assert value.status.value == statuses[i]


class Counters:
    def __init__(self, master):
        self.calls = self.requests = self.auxiliary_requests = 0
        original = master._loop_thread.call

        def call(*args, **kwargs):
            self.calls += 1
            return original(*args, **kwargs)
        master._loop_thread.call = call

    def reset(self):
        self.calls = self.requests = self.auxiliary_requests = 0


class MemoryOpc:
    def __init__(self, rows, counts):
        self.rows, self.counts = rows, counts
        self.data = {}
        self.previous_statuses = []

    def get_node(self, node_id):
        owner = self

        class Node:
            async def read_data_value(self):
                owner.counts.requests += 1
                await asyncio.sleep(0)
                result = owner.data[node_id]
                result.StatusCode.check()
                return result
        return SimpleNamespace(nodeid=node_id, read_data_value=Node().read_data_value)

    async def read_attributes(self, nodes):
        self.counts.requests += 1
        await asyncio.sleep(0)
        return [self.data[node.nodeid] for node in nodes]

    def update(self, cycle):
        values = input_values(self.rows, cycle)
        codes = (ua.StatusCodes.Good, ua.StatusCodes.GoodClamped,
                 ua.StatusCodes.GoodLocalOverride)
        rng = random.Random(f'opc-quality:20261102:{len(self.rows)}:{cycle}')
        statuses = [rng.choice([code for code in codes
                                if not self.previous_statuses or code != self.previous_statuses[i]])
                    for i in range(len(self.rows))]
        self.previous_statuses = statuses
        self.data = {row['node_id']: ua.DataValue(ua.Variant(value), ua.StatusCode(status))
                     for row, value, status in zip(self.rows, values, statuses)}
        return values, statuses


class MemoryBacnet:
    def __init__(self, rows, counts):
        self.rows, self.counts = rows, counts
        self.data = {}

    async def read_property(self, _address, object_id, _property, **_kwargs):
        self.counts.requests += 1
        await asyncio.sleep(0)
        return self.data[object_id]

    def update(self, cycle):
        values = input_values(self.rows, cycle)
        self.data = {f'{row["object_type"]},{row["instance"]}': value
                     for row, value in zip(self.rows, values)}
        return values, None


def cpu_case(cls, protocol, size, cycles, profile):
    from client.opc import _LoopThread as OpcLoop
    from client.bacnet import _LoopThread as BacnetLoop
    rows = rows_for(protocol, size)
    master = (cls('opc.tcp://127.0.0.1:4840', rtu_id=7) if protocol == 'opc'
              else cls('127.0.0.1', rtu_id=7))
    master.configure(rows)
    master._loop_thread = (OpcLoop if protocol == 'opc' else BacnetLoop)(f'bench-{protocol}')
    master._connected = True
    counts = Counters(master)
    memory = (MemoryOpc if protocol == 'opc' else MemoryBacnet)(rows, counts)
    if protocol == 'opc':
        master._client = memory
    else:
        master._app = memory
    try:
        expected, statuses = memory.update(0)
        verify(master.collect_once(), rows, expected, statuses)
        counts.reset()
        elapsed = 0
        profiler = cProfile.Profile() if profile else None
        for cycle in range(cycles):
            expected, statuses = memory.update(cycle + 1)
            start = time.perf_counter()
            if profiler:
                profiler.enable()
            values = master.collect_once()
            if profiler:
                profiler.disable()
            elapsed += time.perf_counter() - start
            verify(values, rows, expected, statuses)
        result = dict(ms_per_cycle=elapsed * 1000 / cycles, cycles=cycles,
                      points=size, loop_calls=counts.calls, protocol_requests=counts.requests,
                      checked_points=cycles * size)
        if profiler:
            stats = pstats.Stats(profiler)
            result['profile_total_calls'] = stats.total_calls
            result['profile_top_cumulative'] = [
                dict(function=f'{key[0]}:{key[1]}:{key[2]}', calls=value[1],
                     self_seconds=value[2], cumulative_seconds=value[3])
                for key, value in sorted(stats.stats.items(), key=lambda x: x[1][3], reverse=True)[:16]
            ]
        return result
    finally:
        master._loop_thread.close()
        master._connected = False


def wire_case(classes, protocol, size, cycles, repeats):
    rows = rows_for(protocol, size)
    server = (_OpcServer if protocol == 'opc' else _BacnetServer)()
    server.start()
    future = None
    results = []
    try:
        async def setup():
            nodes = []
            if protocol == 'opc':
                for row in rows:
                    initial = 0.0 if row['kind'] == 'yc' else False
                    node = await server.server.nodes.objects.add_variable(
                        ua.NodeId.from_string(row['node_id']), f'{row["kind"]}_{row["pnt_no"]}', initial)
                    nodes.append(node)
            else:
                from bacpypes3.local.analog import AnalogValueObject
                from bacpypes3.local.binary import BinaryValueObject
                for row in rows:
                    typ = AnalogValueObject if row['kind'] == 'yc' else BinaryValueObject
                    node = typ(objectIdentifier=(row['object_type'], row['instance']),
                               objectName=f'{row["kind"]}_{row["pnt_no"]}',
                               presentValue=0.0 if row['kind'] == 'yc' else 'inactive')
                    server.app.add_object(node)
                    nodes.append(node)
            return nodes
        future = asyncio.run_coroutine_threadsafe(setup(), server.loop)
        nodes = future.result(30)

        async def update(values):
            for node, row, value in zip(nodes, rows, values):
                if protocol == 'opc':
                    await node.write_value(value)
                else:
                    node.presentValue = value if row['kind'] == 'yc' else ('active' if value else 'inactive')

        for repeat in range(repeats):
            variants = list(classes) if repeat % 2 == 0 else list(reversed(classes))
            for variant in variants:
                cls = classes[variant]
                master = (cls(server.endpoint, rtu_id=7, timeout=5) if protocol == 'opc'
                          else cls('127.0.0.1', remote_port=server.port,
                                   local_ip='127.0.0.1', local_port=_udp_port(), rtu_id=7, timeout=3))
                master.configure(rows)
                try:
                    master.connect()
                    counts = Counters(master)
                    if protocol == 'opc':
                        original = master._client.uaclient.session._send_request
                    else:
                        original = master._app.read_property

                    async def counted(*args, **kwargs):
                        if protocol == 'opc' and not (
                                isinstance(args[0], ua.ReadRequest) and all(
                                    isinstance(item.NodeId.Identifier, str)
                                    and item.NodeId.Identifier.startswith('PERF_')
                                    for item in args[0].Parameters.NodesToRead)):
                            counts.auxiliary_requests += 1
                        else:
                            counts.requests += 1
                        return await original(*args, **kwargs)
                    if protocol == 'opc':
                        master._client.uaclient.session._send_request = counted
                    else:
                        master._app.read_property = counted
                    expected = input_values(rows, 0)
                    asyncio.run_coroutine_threadsafe(update(expected), server.loop).result(15)
                    verify(master.collect_once(), rows, expected)
                    counts.reset()
                    elapsed = 0
                    for cycle in range(cycles):
                        expected = input_values(rows, cycle + repeat * cycles + 1)
                        asyncio.run_coroutine_threadsafe(update(expected), server.loop).result(15)
                        start = time.perf_counter()
                        values = master.collect_once()
                        elapsed += time.perf_counter() - start
                        verify(values, rows, expected)
                    result = dict(case=f'{protocol}_wire_{size}', repeat=repeat + 1, variant=variant,
                                  ms_per_cycle=elapsed * 1000 / cycles, cycles=cycles, points=size,
                                  loop_calls=counts.calls, protocol_requests=counts.requests,
                                  auxiliary_requests=counts.auxiliary_requests, checked_points=size * cycles)
                    expected_requests = size * cycles if variant == 'before' or protocol == 'bacnet' else (size + master.read_batch_size - 1) // master.read_batch_size * cycles
                    assert counts.requests == expected_requests, (result, expected_requests)
                    results.append(result)
                    print(json.dumps(result), flush=True)
                finally:
                    master.close()
    finally:
        if future and not future.done():
            future.cancel()
        server.close()
        if server.thread.is_alive():
            raise RuntimeError(f'{protocol} server did not stop')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-dir', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=7)
    parser.add_argument('--cycles', type=int, default=5)
    parser.add_argument('--wire-cycles', type=int, default=2)
    parser.add_argument('--cpu-only', action='store_true')
    parser.add_argument('--baseline-only', action='store_true')
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    classes, hashes = {}, {}
    for protocol in ('opc', 'bacnet'):
        classes[protocol], hashes[protocol] = {}, {}
        for variant, base in [('before', args.baseline_dir)] + ([] if args.baseline_only else [('after', root)]):
            cls, digest = load_adapter(base / 'client' / f'{protocol}.py', protocol, variant)
            classes[protocol][variant], hashes[protocol][variant] = cls, digest
    results = []
    for repeat in range(args.repeat):
        variants = list(classes['opc']) if repeat % 2 == 0 else list(reversed(classes['opc']))
        for variant in variants:
            for protocol in classes:
                for size in (200, 2000):
                    result = cpu_case(classes[protocol][variant], protocol, size, args.cycles, False)
                    result.update(case=f'{protocol}_cpu_{size}', repeat=repeat + 1, variant=variant)
                    results.append(result)
                    print(json.dumps(result), flush=True)
    profiles = []
    if args.profile:
        for protocol in classes:
            for variant, cls in classes[protocol].items():
                result = cpu_case(cls, protocol, 2000, 3, True)
                result.update(protocol=protocol, variant=variant)
                profiles.append(result)
    if not args.cpu_only:
        results += wire_case(classes['opc'], 'opc', 2000, args.wire_cycles, args.repeat)
        results += wire_case(classes['bacnet'], 'bacnet', 200, args.wire_cycles, args.repeat)
    medians = {}
    for case in dict.fromkeys(row['case'] for row in results):
        medians[case] = {variant: statistics.median(row['ms_per_cycle'] for row in results
                                                 if row['case'] == case and row['variant'] == variant)
                         for variant in classes['opc']}
    report = dict(source_sha256=hashes, python=sys.version, platform=platform.platform(),
                  versions={lib: version(lib) for lib in ('asyncua', 'bacpypes3', 'python-can')},
                  method='Alternating repeats; timings exclude input preparation and verification; '
                         'CPU uses actual event-loop threads and simulated replies with no artificial I/O delay; '
                         'wire uses loopback servers, includes real requests and decoding; profiler timings separate',
                  arguments=vars(args) | {'baseline_dir': str(args.baseline_dir), 'output': str(args.output)},
                  results=results, profiles=profiles, medians_ms_per_cycle=medians)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('MEDIANS', json.dumps(medians), flush=True)


if __name__ == '__main__':
    main()

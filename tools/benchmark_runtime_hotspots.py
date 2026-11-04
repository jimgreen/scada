"""Compare acquisition CPU work and definition scans on frozen runtime sources.

Samples and databases are prepared before timing. This microbenchmark excludes
device I/O, IPC and commits; benchmark_rtu_scale.py covers the full write path.
"""
from __future__ import annotations

import argparse
import cProfile
from datetime import datetime, timezone
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scdfe_control import InstanceLock
from tools.benchmark_inputs import DEFAULT_SEED, MeasurementStream
from tools.benchmark_rtu_scale import load_runtime
from scdfe_c.web.seed_protocol_test_data import seed_protocol_test_data
from scdfe_c.web.service import ScadaStore


class Queue:
    def __init__(self, capture=False):
        self.capture = capture
        self.messages = []
        self.count = 0

    def put(self, message, **_kwargs):
        self.count += 1
        if self.capture:
            self.messages.append(message)


def worker_for(runtime, root, queue):
    worker = runtime.ChannelWorker(
        None, runtime.ChannelSpec(7, 7, "custom"), log_dir=root / "logs", writer_queue=queue,
        measurement_heartbeat_interval=3600,
    )
    worker._yc_scaling = dict.fromkeys(range(1, 1001), (2.0, 1.1))
    return worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--baseline-ref", help="Git revision of scdfe_c/__init__.py to compare with current source")
    baseline.add_argument("--baseline-file", type=Path,
                          help="Frozen scdfe_c/__init__.py source file to compare with current source")
    parser.add_argument("--cycles", type=int, default=1000)
    parser.add_argument("--definition-cycles", type=int, default=50)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.cycles, args.definition_cycles, args.repeat) < 1:
        parser.error("cycle and repeat counts must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[1]
    lock = InstanceLock(Path(tempfile.gettempdir()) / "scada-benchmark", role="benchmark")
    if not lock.acquire():
        raise RuntimeError("another SCADA benchmark is running")
    try:
        with tempfile.TemporaryDirectory(prefix="scada-hotspots-") as directory:
            root = Path(directory)
            if args.baseline_ref:
                baseline_source = subprocess.check_output(
                    ["git", "show", f"{args.baseline_ref}:scdfe_c/__init__.py"], cwd=repo)
                baseline_label = args.baseline_ref
            else:
                baseline_path = args.baseline_file.resolve()
                if not baseline_path.is_file():
                    raise FileNotFoundError(f"baseline source does not exist: {baseline_path}")
                baseline_source = baseline_path.read_bytes()
                baseline_label = str(baseline_path)
            sources = {"before": baseline_source,
                "after": (repo / "scdfe_c" / "__init__.py").read_bytes()}
            hashes, runtimes = {}, {}
            for variant, source in sources.items():
                path = root / f"{variant}.py"
                path.write_bytes(source)
                hashes[variant] = hashlib.sha256(source).hexdigest()
                runtimes[variant] = load_runtime(path, hashes[variant])
            stream = MeasurementStream(7, seed=DEFAULT_SEED)
            samples = [stream.next() for _ in range(16)]
            # Freeze only the wall-clock field for exact payload comparison.
            # Monotonic time still drives the production heartbeat logic.
            stamp = int(time.time() * 1000)
            payloads = {}
            for variant, runtime in runtimes.items():
                clock = runtime._now_ms
                runtime._now_ms = lambda: stamp
                try:
                    queue = Queue(capture=True)
                    worker = worker_for(runtime, root, queue)
                    for sample in samples:
                        worker._sync_measurements(sample)
                    payloads[variant] = queue.messages
                finally:
                    runtime._now_ms = clock
            if payloads["before"] != payloads["after"]:
                raise AssertionError("acquisition messages differ")
            store = ScadaStore(root / "scada.sqlite3")
            seed_protocol_test_data(store, replicas=1)
            rtu_id = runtimes["after"].discover_channels(store)[0].rtu_id
            signatures = {v: runtime.definition_signature(store, rtu_id)
                          for v, runtime in runtimes.items()}
            if len(set(signatures.values())) != 1:
                raise AssertionError("definition fingerprints differ")
            results = []
            for repeat in range(1, args.repeat + 1):
                for variant in (("before", "after") if repeat % 2 else ("after", "before")):
                    runtime = runtimes[variant]
                    queue = Queue()
                    worker = worker_for(runtime, root, queue)
                    started = time.perf_counter()
                    for index in range(args.cycles):
                        worker._sync_measurements(samples[index % len(samples)])
                    changed_ms = (time.perf_counter() - started) * 1000
                    stable_queue = Queue()
                    stable_worker = worker_for(runtime, root, stable_queue)
                    stable_worker._sync_measurements(samples[0])
                    started = time.perf_counter()
                    for _ in range(args.cycles):
                        stable_worker._sync_measurements(samples[0])
                    stable_ms = (time.perf_counter() - started) * 1000
                    with store.connection() as connection:
                        started = time.perf_counter()
                        for _ in range(args.definition_cycles):
                            signature = runtime.definition_signature(store, rtu_id, connection)
                            if signature != signatures[variant]:
                                raise AssertionError("definition fingerprint drifted")
                        definition_ms = (time.perf_counter() - started) * 1000
                    if queue.count != args.cycles or stable_queue.count != 1 or worker.stats.errors:
                        raise AssertionError("measurement counts or filtering changed")
                    result = {"repeat": repeat, "variant": variant,
                              "changed_sync_ms": changed_ms, "stable_sync_ms": stable_ms,
                              "definition_ms": definition_ms, "messages": queue.count}
                    results.append(result)
                    print(json.dumps(result), flush=True)
            profiles = {}
            if args.profile:
                for variant, runtime in runtimes.items():
                    worker = worker_for(runtime, root, Queue())
                    profiler = cProfile.Profile()
                    profiler.enable()
                    for index in range(args.cycles):
                        worker._sync_measurements(samples[index % len(samples)])
                    with store.connection() as connection:
                        for _ in range(args.definition_cycles):
                            runtime.definition_signature(store, rtu_id, connection)
                    profiler.disable()
                    path = repo / "output" / f"{args.output.stem}.{variant}.prof"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    profiler.dump_stats(str(path))
                    profiles[variant] = str(path.resolve())
            report = {"created_at": datetime.now(timezone.utc).isoformat(),
                      "baseline_ref": args.baseline_ref, "baseline_file": str(args.baseline_file.resolve())
                      if args.baseline_file else None,
                      "baseline_label": baseline_label, "cycles": args.cycles,
                      "definition_cycles": args.definition_cycles, "repeat": args.repeat,
                      "seed": DEFAULT_SEED, "points_per_sample": 2000,
                      "platform": platform.platform(), "python": sys.version,
                      "sqlite": sqlite3.sqlite_version, "runtime_sha256": hashes,
                      "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      "workload_sha256": hashlib.sha256((repo / "tools/benchmark_inputs.py").read_bytes()).hexdigest(),
                      "scope": "CPU only; pregenerated samples; isolated database; no device I/O",
                      "payloads_equal": True, "definition_fingerprints_equal": True,
                      "medians_ms": {v: {key: statistics.median(
                          r[key] for r in results if r["variant"] == v)
                          for key in ("changed_sync_ms", "stable_sync_ms", "definition_ms")}
                          for v in runtimes}, "results": results, "profiles": profiles}
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        lock.release()


if __name__ == "__main__":
    main()

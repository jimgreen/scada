"""Compare 6/60 protocol RTUs using isolated SQLite databases and real writer IPC.

No protocol socket or serial connection is opened. Stage timings are serial
microbenchmarks; the pipeline uses one thread per RTU and the production writer
in a separate spawn process. Database setup is excluded from measured work.
"""
from __future__ import annotations

import argparse
import cProfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import hashlib
import importlib.util
import multiprocessing as mp
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scdfe_control import InstanceLock
from tools.benchmark_inputs import DEFAULT_SEED, MeasurementStream
from web.seed_protocol_test_data import COUNTS, seed_protocol_test_data
from web.service import ScadaStore


def load_runtime(source_path, expected_hash):
    """Both parent and spawned writer load the same verified, frozen source."""
    if hashlib.sha256(Path(source_path).read_bytes()).hexdigest() != expected_hash:
        raise RuntimeError("benchmark runtime snapshot changed")
    name = "_scdfe_benchmark_" + expected_hash
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, source_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def unshared_binding_source(source):
    """A controlled baseline: same schema/scaling/queue/writer, expanded YC binds.

    Only the YC parameter layout changes. No old fields or old runtime APIs
    are introduced. Reject changed source patterns instead of silently timing
    two identical variants. The report records both frozen source hashes.
    """
    replacements = (
        (b'), (0, 2, 3, 1, 2, 3)),', b'), (0, 1, 2, 3, 4, 5)),'),
        (b'row = (raw_value, value, quality, now, point_no, rtu_id)',
         b'row = (raw_value, quality, now, value, quality, now, point_no, rtu_id)'),
        # The pipeline uses the queue; keep the direct-write branch correct too.
        (b'((*state, now) if kind == "yc" else (*state[1:], now))',
         b'((state[0], state[2], now, state[1], state[2], now) if kind == "yc" else (*state[1:], now))'),
    )
    for before, after in replacements:
        if source.count(before) != 1:
            raise RuntimeError('unshared binding baseline no longer matches the current source')
        source = source.replace(before, after)
    return source


class CountQueue:
    """Count accepted batches without retaining millions of Python objects."""

    def __init__(self, message_size):
        self.messages = 0
        self.points = 0
        self.message_size = message_size

    def put(self, message, **kwargs):
        self.messages += 1
        self.points += self.message_size(message)


class ReadyQueue:
    def __init__(self, queue, ready):
        self.queue, self.ready = queue, ready

    def get(self, **kwargs):
        self.ready.set()  # Writer database connection has been opened.
        return self.queue.get(**kwargs)

    def task_done(self):
        self.queue.task_done()


def writer_entry(db, schema, log_dir, queue, ready, runtime_path, runtime_hash, profile_path=None):
    runtime = load_runtime(runtime_path, runtime_hash)
    args = (db, schema, log_dir, ReadyQueue(queue, ready))
    if profile_path is None:
        runtime._batch_writer_entry(*args)
    else:
        profiler = cProfile.Profile()
        try:
            profiler.runcall(runtime._batch_writer_entry, *args)
        finally:
            profiler.dump_stats(profile_path)


def payload_metrics(store, spec, root, runtime, seed):
    """Inspect one full-change message outside the pipeline timing."""
    class SampleQueue:
        def put(self, message, **_kwargs):
            self.message = message

    queue = SampleQueue()
    worker = runtime.ChannelWorker(store, spec, log_dir=root / "payload-logs", writer_queue=queue)
    sample = MeasurementStream(spec.rtu_id, COUNTS["yc"], COUNTS["yx"], seed).next()
    worker._sync_measurements(sample)
    return {"point_updates": runtime._batch_message_size(queue.message),
            "serialized_bytes": len(mp.reduction.ForkingPickler.dumps(queue.message)),
            "yc_unique_values": len({p.value for p in sample if p.kind == "yc"}),
            "yc_unique_engineering_values": len({worker._yc_scaling[p.point_no][0] +
                                                 worker._yc_scaling[p.point_no][1] * p.value
                                                 for p in sample if p.kind == "yc"}),
            "quality_counts": {kind: {str(q): sum(p.quality == q for p in sample if p.kind == kind)
                                       for q in range(4)} for kind in ("yc", "yx")}}


def pipeline(store, specs, root, cycles, runtime_path, runtime_hash, seed, profile_path=None):
    runtime = load_runtime(runtime_path, runtime_hash)
    context = mp.get_context("spawn")
    queue = context.JoinableQueue(maxsize=64)
    ready = context.Event()
    writer = context.Process(
        target=writer_entry,
        args=(str(store.db_path), str(store.catalog.directory), str(root / "writer-logs"),
              queue, ready, str(runtime_path), runtime_hash, profile_path),
    )
    writer.start()
    success = False
    release = threading.Barrier(len(specs) + 1, timeout=60)

    def collect(spec):
        # Each worker and its connection (if used) belongs to this thread.
        worker = runtime.ChannelWorker(store, spec, log_dir=root / "worker-logs", writer_queue=queue,
                                       retry_interval=30)
        samples = MeasurementStream(spec.rtu_id, COUNTS["yc"], COUNTS["yx"], seed)
        accepted = 0
        release.wait()
        for _ in range(cycles):
            # Independent quality changes keep every point eligible for update,
            # including a binary YX point that happens to retain its value.
            accepted += sum(worker._sync_measurements(samples.next()))
        if worker.stats.errors:
            raise AssertionError(f"RTU {spec.rtu_id}: {worker.stats.errors} point errors")
        return accepted

    try:
        if not ready.wait(30):
            raise RuntimeError("writer did not become ready")
        with ThreadPoolExecutor(max_workers=len(specs)) as executor:
            futures = [executor.submit(collect, spec) for spec in specs]
            started = time.perf_counter()
            release.wait()
            accepted = sum(f.result(timeout=300) for f in futures)
            enqueue_ms = (time.perf_counter() - started) * 1000
            queue.put(runtime.BATCH_WRITER_STOP, timeout=30)
            writer.join(300)
            elapsed_ms = (time.perf_counter() - started) * 1000
        if writer.is_alive() or writer.exitcode != 0:
            raise RuntimeError(f"writer did not drain successfully: exit={writer.exitcode}")
        expected_points = len(specs) * (COUNTS["yc"] + COUNTS["yx"])
        if accepted != expected_points * cycles:
            raise AssertionError((accepted, expected_points * cycles))
        # Regenerate the same station streams outside timing and check every
        # persisted raw/engineering value, quality, timestamp and version.
        expected = {}
        for spec in specs:
            samples = MeasurementStream(spec.rtu_id, COUNTS["yc"], COUNTS["yx"], seed)
            for _ in range(cycles):
                final_sample = samples.next()
            expected.update(((spec.rtu_id, p.kind, p.point_no), p) for p in final_sample)
        with store.connection() as connection:
            for kind in ("yc", "yx"):
                rows = connection.execute(f"SELECT * FROM fes_{kind}_define").fetchall()
                if len(rows) != len(specs) * COUNTS[kind]:
                    raise AssertionError("point count mismatch")
                for row in rows:
                    point = expected[(row["rtu_id"], kind, row["pnt_no"])]
                    fields = {"value": point.value, "status": point.quality,
                              "_version": 1 + cycles}
                    if kind == "yc":
                        fields.update(raw_value=point.value, raw_status=point.quality,
                                      raw_time=row["refresh_time"],
                                      value=row["raw_base"] + row["raw_scale"] * point.value)
                        if row["raw_value"] == row["value"]:
                            raise AssertionError("test YC raw and engineering values must differ")
                    if row["refresh_time"] <= 0 or any(row[k] != v for k, v in fields.items()):
                        raise AssertionError((kind, row["rtu_id"], row["pnt_no"], fields, dict(row)))
        queue.join()  # Writer exited cleanly and acknowledged every item.
        success = True
        return {"threads": len(specs), "writer_processes": 1, "cycles_per_rtu": cycles,
                "messages": len(specs) * cycles, "point_updates": accepted,
                "producer_ms": enqueue_ms, "drained_ms": elapsed_ms,
                "updates_per_second": accepted * 1000 / elapsed_ms,
                "final_rows_verified": expected_points, "writer_exitcode": writer.exitcode}
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


def run_scale(replicas, args, repeat, variant, runtime_path, runtime_hash):
    runtime = load_runtime(runtime_path, runtime_hash)
    with tempfile.TemporaryDirectory(prefix="scada-rtu-scale-") as temp:
        root = Path(temp)
        store = ScadaStore(root / "scada.sqlite3")
        seeded = seed_protocol_test_data(store, replicas=replicas)
        specs = runtime.discover_channels(store)
        if len(specs) != 6 * replicas:
            raise AssertionError("RTU count mismatch")
        result = {"rtus": len(specs), "repeat": repeat, "variant": variant,
                  "runtime_sha256": runtime_hash,
                  "points_per_rtu": dict(COUNTS), "table_counts": seeded["totals"]}
        result["sample_payload"] = payload_metrics(store, specs[0], root, runtime, args.seed)
        print(f"{variant} repeat={repeat} RTUs={len(specs)} setup complete", flush=True)

        started = time.perf_counter()
        for _ in range(0 if args.pipeline_only else args.scan_cycles):
            if len(runtime.discover_channels(store)) != len(specs):
                raise AssertionError("discovery lost a station")
        result["discovery_ms"] = (time.perf_counter() - started) * 1000

        stable_ms = command_ms = definition_ms = 0.0
        queue = CountQueue(runtime._batch_message_size)
        for index, spec in enumerate([] if args.pipeline_only else specs, 1):
            worker = runtime.ChannelWorker(store, spec, log_dir=root / "stage-logs", writer_queue=queue)
            try:
                values = MeasurementStream(spec.rtu_id, COUNTS["yc"], COUNTS["yx"], args.seed).next()
                started = time.perf_counter()
                for _ in range(args.cycles):
                    worker._sync_measurements(values)
                stable_ms += (time.perf_counter() - started) * 1000
                worker._sync_commands()  # Initial load; zero cmd_time sends no command.
                started = time.perf_counter()
                for _ in range(args.command_cycles):
                    if worker._command_rows() is not None:
                        raise AssertionError("unchanged commands unexpectedly reloaded")
                command_ms += (time.perf_counter() - started) * 1000
                previous = runtime.definition_signature(store, spec.rtu_id, worker._read_connection())
                started = time.perf_counter()
                for _ in range(args.definition_cycles):
                    if runtime.definition_signature(store, spec.rtu_id, worker._read_connection()) != previous:
                        raise AssertionError("static definitions changed")
                definition_ms += (time.perf_counter() - started) * 1000
            finally:
                worker._close_command_connection()
            if index % 6 == 0:
                print(f"RTUs={len(specs)} stage progress {index}/{len(specs)}", flush=True)
        if args.pipeline_only:
            del result["discovery_ms"]
        else:
            result.update(stable_sync_ms=stable_ms, stable_messages=queue.messages,
                          stable_point_updates=queue.points, command_poll_ms=command_ms,
                          definition_ms=definition_ms)
        print(f"RTUs={len(specs)} concurrent pipeline starting", flush=True)
        profile_path = (str(args.output.resolve().with_suffix(f".{variant}.{len(specs)}rtus.{repeat}.prof"))
                        if args.profile_writer else None)
        result["pipeline"] = pipeline(store, specs, root, args.db_cycles,
                                      runtime_path, runtime_hash, args.seed, profile_path)
        if profile_path is not None:
            result["writer_profile"] = profile_path
        with store.connection() as connection:
            result["integrity_check"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
            result["foreign_key_violations"] = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        if result["integrity_check"] != "ok" or result["foreign_key_violations"]:
            raise AssertionError(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="reproducible random workload seed")
    parser.add_argument("--replicas", type=int, nargs="+", default=[1, 10])
    parser.add_argument("--cycles", type=int, default=1000)
    parser.add_argument("--db-cycles", type=int, default=20)
    parser.add_argument("--command-cycles", type=int, default=1000)
    parser.add_argument("--definition-cycles", type=int, default=10)
    parser.add_argument("--scan-cycles", type=int, default=1000)
    parser.add_argument("--pipeline-only", action="store_true",
                        help="skip serial microbenchmarks; measure real IPC and database commits")
    parser.add_argument("--repeat", type=int, default=1,
                        help="independent runs, each using a fresh temporary database")
    parser.add_argument("--profile-writer", action="store_true",
                        help="save writer cProfile data (adds overhead; do not compare timings)")
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument("--baseline-ref", help="Git revision of scdfe_c.py to compare with current source; "
                        "freeze both versions and alternate their sequential execution order")
    baseline.add_argument("--compare-bindings", action="store_true",
                          help="compare separate YC parameters with shared quality/time bindings; same schema and scaling")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.cycles, args.db_cycles, args.command_cycles, args.definition_cycles,
           args.scan_cycles, args.repeat) < 1:
        parser.error("cycle counts must be positive")
    if any(n < 1 or n > 10 for n in args.replicas):
        parser.error("replicas must be 1..10")
    repo = Path(__file__).resolve().parents[1]
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "config": {**vars(args), "output": str(args.output)},
        "runtimes": {},
        "workload_sha256": hashlib.sha256((repo / "tools/benchmark_inputs.py").read_bytes()).hexdigest(),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "isolation": "frozen runtime sources; parent/child SHA-256 verification; exclusive benchmark lock; "
                     "serial runs, variant order reversed on even repeats",
        "measurement_heartbeat_seconds": 5,
        "data_generation": (
            "independent per-RTU Random streams; unique YC values in [-10000,10000]; "
            "each point randomly chooses quality 0..3 excluding its previous quality; "
            "YX random 0/1 may repeat, YC changes every cycle; "
            "YC raw_value=input, value=raw_base+raw_scale*input; fixture base=2, scale=1.1"
        ),
        "baseline_method": ("current source with expanded YC quality/time parameters only"
                            if args.compare_bindings else "git revision" if args.baseline_ref else "none"),
        "scope": ("concurrent threads/spawn writer; no device I/O" if args.pipeline_only else
                  "serial stage microbenchmarks plus concurrent threads/spawn writer; no device I/O"),
        "results": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(Path(tempfile.gettempdir()) / "scada-benchmark", role="benchmark")
    if not lock.acquire():
        raise RuntimeError("another SCADA benchmark is running; run benchmarks sequentially")
    try:
        with tempfile.TemporaryDirectory(prefix="scada-runtime-snapshots-") as snapshot_dir:
            sources = {}
            if args.baseline_ref:
                sources["before"] = subprocess.check_output(
                    ["git", "show", f"{args.baseline_ref}:scdfe_c.py"], cwd=repo)
            elif args.compare_bindings:
                sources["before"] = unshared_binding_source((repo / "scdfe_c.py").read_bytes())
            sources["after"] = (repo / "scdfe_c.py").read_bytes()
            variants = []
            for variant, source in sources.items():
                snapshot_path = Path(snapshot_dir) / f"{variant}.py"
                snapshot_path.write_bytes(source)
                digest = hashlib.sha256(source).hexdigest()
                runtime = load_runtime(snapshot_path, digest)
                report["runtimes"][variant] = {
                    "sha256": digest, "interval_seconds": runtime.BATCH_WRITER_INTERVAL,
                    "max_batch": runtime.BATCH_WRITER_MAX_BATCH,
                }
                variants.append((variant, snapshot_path, digest))
            for repeat in range(1, args.repeat + 1):
                for replicas in args.replicas:
                    for variant, snapshot_path, digest in (variants if repeat % 2 else variants[::-1]):
                        report["results"].append(run_scale(replicas, args, repeat,
                                                          variant, snapshot_path, digest))
                        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    finally:
        lock.release()


if __name__ == "__main__":
    mp.freeze_support()
    main()

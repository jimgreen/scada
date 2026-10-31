"""Compare 6/60 protocol RTUs using isolated SQLite databases and real writer IPC.

No protocol socket or serial connection is opened. Stage timings are serial
microbenchmarks; the pipeline uses one thread per RTU and the production writer
in a separate spawn process. Database setup is excluded from measured work.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import multiprocessing as mp
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scdfe_c import (BATCH_WRITER_INTERVAL, BATCH_WRITER_MAX_BATCH, BATCH_WRITER_STOP,
                     ChannelWorker, _batch_writer_entry, definition_signature, discover_channels)
from web.seed_protocol_test_data import COUNTS, seed_protocol_test_data
from web.service import ScadaStore


@dataclass(frozen=True)
class Value:
    kind: str
    point_no: int
    value: float | int
    quality: int = 0


def measurements(cycle: int = 0, rtu_id: int = 0) -> list[Value]:
    return [
        *(Value("yc", p, float(rtu_id * 100000 + p + cycle)) for p in range(1, COUNTS["yc"] + 1)),
        *(Value("yx", p, (p + cycle + rtu_id) % 2) for p in range(1, COUNTS["yx"] + 1)),
    ]


class CountQueue:
    """Count accepted batches without retaining millions of Python objects."""

    def __init__(self):
        self.messages = 0
        self.points = 0

    def put(self, message, **kwargs):
        self.messages += 1
        self.points += len(message["updates"])


class ReadyQueue:
    def __init__(self, queue, ready):
        self.queue, self.ready = queue, ready

    def get(self, **kwargs):
        self.ready.set()  # Writer database connection has been opened.
        return self.queue.get(**kwargs)

    def task_done(self):
        self.queue.task_done()


def writer_entry(db, schema, log_dir, queue, ready):
    _batch_writer_entry(db, schema, log_dir, ReadyQueue(queue, ready))


def pipeline(store, specs, root, cycles):
    context = mp.get_context("spawn")
    queue = context.JoinableQueue(maxsize=64)
    ready = context.Event()
    writer = context.Process(
        target=writer_entry,
        args=(str(store.db_path), str(store.catalog.directory), str(root / "writer-logs"), queue, ready),
    )
    writer.start()
    success = False
    release = threading.Barrier(len(specs) + 1, timeout=60)

    def collect(spec):
        # Each worker and its connection (if used) belongs to this thread.
        worker = ChannelWorker(store, spec, log_dir=root / "worker-logs", writer_queue=queue,
                               retry_interval=30)
        accepted = 0
        release.wait()
        for cycle in range(cycles):
            # Both YC and YX change on every cycle: stable filtering cannot
            # suppress any update, and station-specific values detect mixing.
            accepted += sum(worker._sync_measurements(measurements(cycle, spec.rtu_id)))
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
            queue.put(BATCH_WRITER_STOP, timeout=30)
            writer.join(300)
            elapsed_ms = (time.perf_counter() - started) * 1000
        if writer.is_alive() or writer.exitcode != 0:
            raise RuntimeError(f"writer did not drain successfully: exit={writer.exitcode}")
        expected_points = len(specs) * (COUNTS["yc"] + COUNTS["yx"])
        if accepted != expected_points * cycles:
            raise AssertionError((accepted, expected_points * cycles))
        # Each message has 2000 points, exceeding max_batch=500, so it commits
        # separately. Check every row's version and final value, not a sample.
        with store.connection() as connection:
            for kind in ("yc", "yx"):
                rows = connection.execute(
                    f"SELECT rtu_id,pnt_no,value,_version,status FROM fes_{kind}_define"
                ).fetchall()
                if len(rows) != len(specs) * COUNTS[kind]:
                    raise AssertionError("point count mismatch")
                for rtu_id, number, value, version, status in rows:
                    expected = (float(rtu_id * 100000 + number + cycles - 1) if kind == "yc"
                                else (number + cycles - 1 + rtu_id) % 2)
                    if (value, version, status) != (expected, 1 + cycles, 0):
                        raise AssertionError((kind, rtu_id, number, value, version, status, expected))
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


def run_scale(replicas, args):
    with tempfile.TemporaryDirectory(prefix="scada-rtu-scale-") as temp:
        root = Path(temp)
        store = ScadaStore(root / "scada.sqlite3")
        seeded = seed_protocol_test_data(store, replicas=replicas)
        specs = discover_channels(store)
        if len(specs) != 6 * replicas:
            raise AssertionError("RTU count mismatch")
        result = {"rtus": len(specs), "points_per_rtu": dict(COUNTS), "table_counts": seeded["totals"]}
        print(f"RTUs={len(specs)} setup complete", flush=True)

        started = time.perf_counter()
        for _ in range(args.scan_cycles):
            if len(discover_channels(store)) != len(specs):
                raise AssertionError("discovery lost a station")
        result["discovery_ms"] = (time.perf_counter() - started) * 1000

        stable_ms = command_ms = definition_ms = 0.0
        queue = CountQueue()
        for index, spec in enumerate(specs, 1):
            worker = ChannelWorker(store, spec, log_dir=root / "stage-logs", writer_queue=queue)
            try:
                values = measurements(rtu_id=spec.rtu_id)
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
                previous = definition_signature(store, spec.rtu_id, worker._read_connection())
                started = time.perf_counter()
                for _ in range(args.definition_cycles):
                    if definition_signature(store, spec.rtu_id, worker._read_connection()) != previous:
                        raise AssertionError("static definitions changed")
                definition_ms += (time.perf_counter() - started) * 1000
            finally:
                worker._close_command_connection()
            if index % 6 == 0:
                print(f"RTUs={len(specs)} stage progress {index}/{len(specs)}", flush=True)
        result.update(stable_sync_ms=stable_ms, stable_messages=queue.messages,
                      stable_point_updates=queue.points, command_poll_ms=command_ms,
                      definition_ms=definition_ms)
        print(f"RTUs={len(specs)} concurrent pipeline starting", flush=True)
        result["pipeline"] = pipeline(store, specs, root, args.db_cycles)
        with store.connection() as connection:
            result["integrity_check"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
            result["foreign_key_violations"] = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        if result["integrity_check"] != "ok" or result["foreign_key_violations"]:
            raise AssertionError(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicas", type=int, nargs="+", default=[1, 10])
    parser.add_argument("--cycles", type=int, default=1000)
    parser.add_argument("--db-cycles", type=int, default=20)
    parser.add_argument("--command-cycles", type=int, default=1000)
    parser.add_argument("--definition-cycles", type=int, default=10)
    parser.add_argument("--scan-cycles", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.cycles, args.db_cycles, args.command_cycles, args.definition_cycles, args.scan_cycles) < 1:
        parser.error("cycle counts must be positive")
    if any(n < 1 or n > 10 for n in args.replicas):
        parser.error("replicas must be 1..10")
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "config": {**vars(args), "output": str(args.output)},
        "writer": {"interval_seconds": BATCH_WRITER_INTERVAL, "max_batch": BATCH_WRITER_MAX_BATCH},
        "measurement_heartbeat_seconds": 5,
        "scope": "serial stage microbenchmarks plus concurrent threads/spawn writer; no device I/O",
        "results": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for replicas in args.replicas:
        report["results"].append(run_scale(replicas, args))
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    mp.freeze_support()
    main()

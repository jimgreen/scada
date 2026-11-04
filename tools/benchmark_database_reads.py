"""Measure database-read hot paths changed in the current optimization round.

The command case exercises the real communication worker's version poll over
60 RTUs with 200 YK and 200 YT rows per RTU.  The startup case isolates the
database/schema initialization that used to happen once per worker thread by
using a no-op worker entry.  Both cases use alternating order and report
medians; protocol sockets are never opened.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.benchmark_rtu_scale import load_runtime
from scdfe_c.web.seed_protocol_test_data import seed_protocol_test_data
from scdfe_c.web.service import ScadaStore


def _load(path: Path, expected_hash: str):
    return load_runtime(path, expected_hash)


def _command_case(runtime, db: Path, root: Path, cycles: int) -> float:
    store = ScadaStore(db)
    specs = runtime.discover_channels(store)
    workers = []
    try:
        for spec in specs:
            worker = runtime.ChannelWorker(store, spec, log_dir=root / "logs")
            worker._command_seen = {}
            worker._command_version = 400
            workers.append(worker)
        started = time.perf_counter()
        for cycle in range(cycles):
            row = workers[cycle % len(workers)]._command_rows()
            if row is not None:
                raise AssertionError("unchanged command version unexpectedly returned rows")
        return (time.perf_counter() - started) * 1000
    finally:
        for worker in workers:
            worker._close_command_connection()


def _startup_case(runtime, db: Path, schema_dir: Path, root: Path, count: int,
                  shared: bool) -> float:
    # Keep the worker body out of this measurement: only the old per-thread
    # ScadaStore construction versus the shared validated catalog is timed.
    class NoopWorker:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def run(self):
            return None

    original = runtime.ChannelWorker
    runtime.ChannelWorker = NoopWorker
    try:
        shared_store = ScadaStore(db) if shared else None
        started = time.perf_counter()
        for index in range(count):
            spec = {"rtu_id": index + 1, "channel_id": index + 1,
                    "channel_type": "modbus-tcp", "name": "benchmark"}
            if shared:
                runtime._worker_thread_entry(
                    shared_store, spec, str(root / "logs"), 0.0, 0.1,
                    threading.Event(), None,
                )
            else:
                runtime._worker_thread_entry(
                    str(db), str(schema_dir), spec, str(root / "logs"),
                    0.0, 0.1, threading.Event(), None,
                )
        return (time.perf_counter() - started) * 1000
    finally:
        runtime.ChannelWorker = original


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-file", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=5000)
    parser.add_argument("--startup-counts", type=int, nargs="+", default=[6, 60])
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.cycles < 1 or args.repeat < 1 or any(count < 1 for count in args.startup_counts):
        parser.error("cycles, repeat, and startup counts must be positive")

    repo = Path(__file__).resolve().parents[1]
    before_source = args.baseline_file.read_bytes()
    after_source = (repo / "scdfe_c" / "__init__.py").read_bytes()
    hashes = {"before": hashlib.sha256(before_source).hexdigest(),
              "after": hashlib.sha256(after_source).hexdigest()}
    results = []
    with tempfile.TemporaryDirectory(prefix="scada-db-read-") as folder:
        root = Path(folder)
        db = root / "fixture.sqlite3"
        template = ScadaStore(db)
        seed_protocol_test_data(template, replicas=10)
        runtimes = {}
        for variant, source in (("before", before_source), ("after", after_source)):
            path = root / f"{variant}.py"
            path.write_bytes(source)
            runtimes[variant] = _load(path, hashes[variant])
        for repeat in range(1, args.repeat + 1):
            order = ("before", "after") if repeat % 2 else ("after", "before")
            for variant in order:
                value = _command_case(runtimes[variant], db, root, args.cycles)
                results.append({"case": "command_poll", "repeat": repeat,
                                "variant": variant, "cycles": args.cycles,
                                "elapsed_ms": value})
                print(json.dumps(results[-1]), flush=True)
            for count in args.startup_counts:
                for variant in order:
                    value = _startup_case(runtimes[variant], db, template.catalog.directory,
                                          root, count, variant == "after")
                    results.append({"case": "thread_startup_db_init", "repeat": repeat,
                                    "variant": variant, "threads": count,
                                    "elapsed_ms": value})
                    print(json.dumps(results[-1]), flush=True)

    summary = {"source_sha256": hashes, "cycles": args.cycles,
               "startup_counts": args.startup_counts, "repeat": args.repeat,
               "results": results, "medians_ms": {}}
    for case in ("command_poll", "thread_startup_db_init"):
        counts = [None] if case == "command_poll" else args.startup_counts
        for count in counts:
            key = f"{case}:{count if count is not None else args.cycles}"
            summary["medians_ms"][key] = {}
            for variant in ("before", "after"):
                values = [row["elapsed_ms"] for row in results
                          if row["case"] == case and row["variant"] == variant
                          and (count is None or row["threads"] == count)]
                summary["medians_ms"][key][variant] = statistics.median(values)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


if __name__ == "__main__":
    main()

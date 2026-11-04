"""Compare frozen log-query sources on the same temporary hourly log tree.

Times include validation, listing, reading, filtering and all cursor pages, but
exclude fixture setup and correctness checks. No live logs are read or changed.
"""
from __future__ import annotations

import argparse
import cProfile
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import pstats
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scdfe_control import InstanceLock


def load_source(path):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location("_log_benchmark_" + digest, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, digest


def fixture(root, hours, lines):
    first_hour = datetime(2026, 10, 25)
    processes = ("main", "db-writer", "rtu-7", "rtu-8", "rtu-9", "rtu-10")
    for offset in range(hours):
        hour = first_hour + timedelta(hours=offset)
        directory = root / hour.strftime("%Y/%m/%d/%H")
        directory.mkdir(parents=True)
        for process in processes:
            path = directory / f"scdfe_c-{process}-{hour:%Y%m%d-%H}.log"
            path.write_text(f"{hour:%H}:00:00 [日志] hourly normal\n", encoding="utf-8")
    selected = directory / f"scdfe_c-rtu-7-{hour:%Y%m%d-%H}.log"
    labels = ("日志", "警告", "错误", "进程")
    with selected.open("w", encoding="utf-8", newline="\n") as stream:
        for index in range(lines):
            second = (index // 4) % 3600
            clock = f"{hour:%H}:{second // 60:02d}:{second % 60:02d}"
            keyword = "命中" if index % 97 == 0 else "普通"
            stream.write(f"{clock} [{labels[index % 4]}] {keyword} 采集点={index} raw={index + 0.125} quality={index % 4}\n")
    return selected, hour, hours * len(processes)


def collect(module, root, query):
    pages = []
    cursor = None
    for _ in range(10000):
        page = module.search_logs(root, {**query, **({"cursor": cursor} if cursor else {})})
        if page["scanned_bytes"] > module.MAX_SCAN_BYTES:
            raise AssertionError("scan budget exceeded")
        pages.append(page)
        next_cursor = page["next_cursor"]
        if next_cursor is None:
            return pages
        if next_cursor == cursor:
            raise AssertionError("pagination stalled")
        cursor = next_cursor
    raise AssertionError("pagination did not finish")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--baseline-ref")
    baseline.add_argument("--baseline-file", type=Path)
    parser.add_argument("--hours", type=int, default=168)
    parser.add_argument("--lines", type=int, default=12000)
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.hours, args.lines, args.repeat) < 1:
        parser.error("hours, lines and repeat must be positive")
    repo = Path(__file__).resolve().parents[1]
    before = (subprocess.check_output(["git", "show", f"{args.baseline_ref}:web/log_query.py"], cwd=repo)
              if args.baseline_ref else args.baseline_file.read_bytes())
    after = (repo / "web/log_query.py").read_bytes()
    lock = InstanceLock(Path(tempfile.gettempdir()) / "scada-benchmark", role="benchmark")
    if not lock.acquire():
        raise RuntimeError("another SCADA benchmark is running")
    try:
        with tempfile.TemporaryDirectory(prefix="scada-log-benchmark-") as directory:
            temp = Path(directory)
            root = temp / "logs"
            runtimes, hashes = {}, {}
            for variant, source in (("before", before), ("after", after)):
                path = temp / f"{variant}.py"
                path.write_bytes(source)
                runtimes[variant], hashes[variant] = load_source(path)
            selected, hour, file_count = fixture(root, args.hours, args.lines)
            filename = selected.relative_to(root).as_posix()
            cases = {
                "selected_all_pages": {"filename": filename, "limit": "5000"},
                "selected_filtered": {
                    "filename": filename, "q": "命中", "level": "ERROR", "limit": "1000",
                    "start": hour.replace(minute=5).astimezone().isoformat(),
                    "end": hour.replace(minute=40).astimezone(timezone.utc).isoformat(),
                },
                "all_files_filtered": {"q": "命中", "level": "ERROR", "limit": "1000"},
            }
            # Warm both modules and verify exact responses, including cursor
            # tokens, file ordering, timestamps, scan counters and all pages.
            expected = {}
            checks = {}
            for name, query in cases.items():
                pages = {v: collect(module, root, query) for v, module in runtimes.items()}
                if pages["before"] != pages["after"]:
                    raise AssertionError(f"log responses differ: {name}")
                expected[name] = pages["before"]
                checks[name] = {"pages": len(pages["before"]),
                                "items": sum(len(p["items"]) for p in pages["before"]),
                                "scanned_bytes": sum(p["scanned_bytes"] for p in pages["before"]),
                                "scanned_files": sum(p["scanned_files"] for p in pages["before"]),
                                "responses_equal": True}
            results = []
            for repeat in range(1, args.repeat + 1):
                order = ("before", "after") if repeat % 2 else ("after", "before")
                for name, query in cases.items():
                    for variant in order:
                        started = time.perf_counter()
                        pages = collect(runtimes[variant], root, query)
                        elapsed_ms = (time.perf_counter() - started) * 1000
                        if pages != expected[name]:
                            raise AssertionError(f"log response drifted: {variant}, {name}")
                        result = {"repeat": repeat, "case": name, "variant": variant, "elapsed_ms": elapsed_ms}
                        results.append(result)
                        print(json.dumps(result), flush=True)
            profiles = {}
            if args.profile:
                for variant, module in runtimes.items():
                    profiler = cProfile.Profile()
                    profiler.runcall(collect, module, root, cases["selected_filtered"])
                    path = repo / "output" / f"{args.output.stem}.{variant}.prof"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    profiler.dump_stats(str(path))
                    stats = pstats.Stats(profiler)
                    top = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:20]
                    profiles[variant] = {"path": str(path), "total_seconds": stats.total_tt,
                                         "total_calls": stats.total_calls,
                                         "top_cumulative": [
                                             {"function": f"{Path(key[0]).name}:{key[1]}:{key[2]}",
                                              "calls": value[1], "self_seconds": value[2],
                                              "cumulative_seconds": value[3]} for key, value in top]}
            report = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "platform": platform.platform(), "python": sys.version,
                "baseline_ref": args.baseline_ref,
                "baseline_file": str(args.baseline_file) if args.baseline_file else None,
                "source_sha256": hashes,
                "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "hours": args.hours, "files": file_count, "selected_file_lines": args.lines,
                "selected_file_bytes": selected.stat().st_size, "repeat": args.repeat,
                "scope": "warm local filesystem; entire search including all pages; no HTTP/network; isolated logs",
                "queries": cases, "checks": checks, "results": results, "profiles": profiles,
                "medians_ms": {name: {variant: statistics.median(
                    r["elapsed_ms"] for r in results if r["case"] == name and r["variant"] == variant)
                    for variant in runtimes} for name in cases},
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        lock.release()


if __name__ == "__main__":
    main()

"""Measure WEB list payload reduction from field projections.

The baseline requests the historical full-row ``SELECT *`` response and the
candidate requests only the columns rendered by the HMI list view.  Both
variants use the same database and are alternated so cache/order effects are
visible.  No formal database is modified.
"""
from __future__ import annotations

import argparse
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


YC_FIELDS = ["rtu_id", "pnt_no", "name", "raw_value", "raw_status", "value", "status", "refresh_time"]


def payload_bytes(result):
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=200)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeat < 1 or args.rounds < 1:
        parser.error("repeat and rounds must be positive")
    results = []
    with tempfile.TemporaryDirectory(prefix="scada-web-projection-") as folder:
        store = ScadaStore(Path(folder) / "fixture.sqlite3")
        seed_protocol_test_data(store, replicas=10)
        cases = {
            "yc_common_500": lambda variant: store.list_rows(
                "fes_yc_define", limit=500, sort="pnt_no", fields=YC_FIELDS if variant == "after" else None,
            ),
            "yc_rtu_common_500": lambda variant: store.list_rows(
                "fes_yc_define", where={"rtu_id": 7}, limit=500, sort="pnt_no",
                fields=YC_FIELDS if variant == "after" else None,
            ),
        }
        reference = {name: cases[name]("after") for name in cases}
        for round_no in range(1, args.rounds + 1):
            order = ("before", "after") if round_no % 2 else ("after", "before")
            for variant in order:
                for name, fn in cases.items():
                    started = time.perf_counter()
                    result = None
                    for _ in range(args.repeat):
                        result = fn(variant)
                    elapsed = (time.perf_counter() - started) * 1000
                    if variant == "after":
                        if result != reference[name]:
                            raise AssertionError(f"{name} projection changed visible data")
                    else:
                        projected = [{key: row[key] for key in reference[name]["items"][0]}
                                     for row in result["items"]]
                        if projected != reference[name]["items"]:
                            raise AssertionError(f"{name} full response changed visible data")
                    row = {
                        "round": round_no, "variant": variant, "case": name,
                        "calls": args.repeat, "elapsed_ms": elapsed,
                        "per_call_ms": elapsed / args.repeat,
                        "payload_bytes": payload_bytes(result),
                        "fields": len(result["items"][0]) if result["items"] else 0,
                    }
                    results.append(row)
                    print(json.dumps(row), flush=True)
    medians = {}
    for name in cases:
        medians[name] = {}
        for variant in ("before", "after"):
            rows = [row for row in results if row["case"] == name and row["variant"] == variant]
            medians[name][variant] = {
                "elapsed_ms": statistics.median(row["elapsed_ms"] for row in rows),
                "per_call_ms": statistics.median(row["per_call_ms"] for row in rows),
                "payload_bytes": statistics.median(row["payload_bytes"] for row in rows),
                "fields": statistics.median(row["fields"] for row in rows),
            }
    report = {
        "platform": platform.platform(), "python": sys.version,
        "method": "Alternating repeats; 60-RTU seeded fixture; candidate requests HMI-visible YC fields",
        "arguments": {"repeat": args.repeat, "rounds": args.rounds, "output": str(args.output)},
        "results": results, "medians": medians,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("MEDIANS", json.dumps(medians, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

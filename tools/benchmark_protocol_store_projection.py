"""Benchmark protocol ``from_store`` row projections against SELECT *.

The benchmark opens the formal database read-only and never writes it.  It
measures the configuration load path used before a communication client or
server starts: SQLite fetch, ``dict(row)`` materialisation, and JSON-sized
payload.  The optimized adapter queries intentionally keep all fields needed
by the protocol mapping while omitting wide four遥 runtime columns.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import sqlite3
import time


CASES = {
    "iec104": {
        "rtu": 2,
        "mapping": ("fes_channel_iec104_point_define",
                    "rtu_id,kind,pnt_no,io_address,type_id,report_ms,command_mode,qualifier,"
                    "related_io_address,related_io_autoreturn"),
    },
    "bacnet": {
        "rtu": 6,
        "mapping": ("fes_channel_bacnet_point_define",
                    "rtu_id,kind,pnt_no,object_type,instance,property_identifier,"
                    "quality_property,priority,array_index"),
    },
    "opc": {
        "rtu": 7,
        "mapping": ("fes_channel_opc_point_define",
                    "rtu_id,kind,pnt_no,node_id,data_type,value_attribute,quality_attribute,deadband"),
    },
}
POINT_TABLES = ("fes_yc_define", "fes_yx_define", "fes_yk_define", "fes_yt_define")


def load(connection, protocol, optimized):
    spec = CASES[protocol]
    rtu_id = spec["rtu"]
    rows = []
    rtu_projection = "rtu_id,channel_type" if optimized else "*"
    rows.extend(dict(row) for row in connection.execute(
        f"SELECT {rtu_projection} FROM fes_rtu_define WHERE rtu_id=?", (rtu_id,)))
    definition_table = f"fes_channel_{protocol}_define"
    rows.extend(dict(row) for row in connection.execute(
        f"SELECT * FROM {definition_table} WHERE rtu_id=? ORDER BY id", (rtu_id,)))
    mapping_table, mapping_projection = spec["mapping"]
    rows.extend(dict(row) for row in connection.execute(
        f"SELECT {mapping_projection if optimized else '*'} FROM {mapping_table} "
        "WHERE rtu_id=? ORDER BY kind,pnt_no", (rtu_id,)))
    for table in POINT_TABLES:
        if optimized:
            projection = "pnt_no,value" if protocol == "iec104" else "pnt_no"
        else:
            projection = "*"
        rows.extend(dict(row) for row in connection.execute(
            f"SELECT {projection} FROM {table} WHERE rtu_id=? ORDER BY pnt_no", (rtu_id,)))
    return rows


def payload_bytes(rows):
    return len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("db/scada.sqlite3"))
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(args.db, uri=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    results = []
    try:
        for round_no in range(1, args.rounds + 1):
            order = (False, True) if round_no % 2 else (True, False)
            for optimized in order:
                for protocol in CASES:
                    started = time.perf_counter()
                    rows = None
                    for _ in range(args.repeat):
                        rows = load(connection, protocol, optimized)
                    elapsed = (time.perf_counter() - started) * 1000
                    row = {
                        "round": round_no, "protocol": protocol,
                        "variant": "after" if optimized else "before",
                        "calls": args.repeat, "elapsed_ms": elapsed,
                        "per_call_ms": elapsed / args.repeat,
                        "rows": len(rows), "payload_bytes": payload_bytes(rows),
                    }
                    results.append(row)
                    print(json.dumps(row), flush=True)
    finally:
        connection.close()
    medians = {}
    for protocol in CASES:
        medians[protocol] = {}
        for variant in ("before", "after"):
            selected = [r for r in results if r["protocol"] == protocol and r["variant"] == variant]
            medians[protocol][variant] = {
                "per_call_ms": statistics.median(r["per_call_ms"] for r in selected),
                "payload_bytes": statistics.median(r["payload_bytes"] for r in selected),
                "rows": statistics.median(r["rows"] for r in selected),
            }
    report = {
        "database": str(args.db.resolve()), "platform": platform.platform(),
        "method": "read-only formal DB; alternating SELECT * and protocol-required projections",
        "arguments": {"repeat": args.repeat, "rounds": args.rounds, "output": str(args.output)},
        "results": results, "medians": medians,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("MEDIANS", json.dumps(medians, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

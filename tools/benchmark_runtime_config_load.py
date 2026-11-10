"""Benchmark the scdfe_c default protocol-map load path.

The old path read wide four遥 rows and protocol maps with ``SELECT *`` on
two separate SQLite connections.  The current path uses one read transaction,
selects only mapping fields needed by the adapter, and reads only ``pnt_no``
from four遥 tables.  This script is read-only and does not modify the formal
database.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sqlite3
import sys
import time
from pathlib import Path


POINT_TABLES = ("fes_yc_define", "fes_yx_define", "fes_yk_define", "fes_yt_define")
PROJECTIONS = {
    "bacnet": (
        "fes_channel_bacnet_point_define",
        "kind,pnt_no,object_type,instance,property_identifier,"
        "quality_property,priority,array_index",
    ),
    "opc": (
        "fes_channel_opc_point_define",
        "kind,pnt_no,node_id,data_type,value_attribute,"
        "quality_attribute,deadband",
    ),
}


def old_load(connection_factory, rtu_id: int, channel_id: int, protocol: str) -> dict:
    result = {}
    with connection_factory() as connection:
        connection.row_factory = sqlite3.Row
        for table, kind in zip(POINT_TABLES, ("yc", "yx", "yk", "yt")):
            result.setdefault(kind, {})
            rows = [dict(row) for row in connection.execute(
                f'SELECT * FROM "{table}" WHERE rtu_id=? ORDER BY pnt_no', (rtu_id,)
            )]
            result[kind]["points"] = [int(row["pnt_no"]) for row in rows]
    with connection_factory() as connection:
        connection.row_factory = sqlite3.Row
        table = f"fes_channel_{protocol}_point_define"
        for row in connection.execute(
            f'SELECT * FROM "{table}" WHERE channel_id=?', (channel_id,)
        ):
            result.setdefault(row["kind"], {})
            result[row["kind"]].setdefault("maps", []).append(dict(row))
    return result


def new_load(connection: sqlite3.Connection, rtu_id: int, channel_id: int, protocol: str) -> dict:
    result = {}
    table, projection = PROJECTIONS[protocol]
    for row in connection.execute(
        f'SELECT {projection} FROM "{table}" WHERE channel_id=? ORDER BY kind,pnt_no',
        (channel_id,),
    ):
        result.setdefault(row["kind"], {})
        result[row["kind"]].setdefault("maps", []).append(dict(row))
    for table, kind in zip(POINT_TABLES, ("yc", "yx", "yk", "yt")):
        result.setdefault(kind, {})
        result[kind]["points"] = [
            int(row["pnt_no"])
            for row in connection.execute(
                f'SELECT pnt_no FROM "{table}" WHERE rtu_id=? ORDER BY pnt_no', (rtu_id,)
            )
        ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("db/scada.sqlite3"))
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeat < 1 or args.rounds < 1:
        parser.error("repeat and rounds must be positive")
    base = sqlite3.connect(args.db)
    base.row_factory = sqlite3.Row
    base.execute("PRAGMA query_only=ON")
    cases = [
        (int(row["rtu_id"]), int(row["channel_id"]), str(row["channel_type"]))
        for row in base.execute(
            "SELECT rtu_id,channel_id,channel_type FROM fes_rtu_define "
            "WHERE channel_type IN ('bacnet','opc') ORDER BY rtu_id"
        )
    ]
    base.close()
    if not cases:
        parser.error("database has no BACnet/OPC RTU fixtures")

    def connection_factory():
        connection = sqlite3.connect(args.db)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    results = []
    for round_no in range(1, args.rounds + 1):
        for variant in (("old", "new") if round_no % 2 else ("new", "old")):
            for rtu_id, channel_id, protocol in cases:
                started = time.perf_counter()
                sample = None
                for _ in range(args.repeat):
                    if variant == "old":
                        sample = old_load(connection_factory, rtu_id, channel_id, protocol)
                    else:
                        with connection_factory() as connection:
                            sample = new_load(connection, rtu_id, channel_id, protocol)
                elapsed = (time.perf_counter() - started) * 1000
                results.append({
                    "round": round_no, "variant": variant, "rtu_id": rtu_id,
                    "protocol": protocol, "calls": args.repeat,
                    "elapsed_ms": elapsed, "per_call_ms": elapsed / args.repeat,
                    "points": sum(len(value.get("points", ())) for value in sample.values()),
                    "maps": sum(len(value.get("maps", ())) for value in sample.values()),
                })
    medians = {}
    for protocol in sorted({protocol for _, _, protocol in cases}):
        selected = [row for row in results if row["protocol"] == protocol]
        medians[protocol] = {}
        for variant in ("old", "new"):
            rows = [row for row in selected if row["variant"] == variant]
            medians[protocol][variant] = {
                "per_call_ms": statistics.median(row["per_call_ms"] for row in rows),
                "points": statistics.median(row["points"] for row in rows),
                "maps": statistics.median(row["maps"] for row in rows),
            }
        old_ms = medians[protocol]["old"]["per_call_ms"]
        new_ms = medians[protocol]["new"]["per_call_ms"]
        medians[protocol]["reduction_percent"] = (old_ms - new_ms) / old_ms * 100
    report = {
        "database": str(args.db.resolve()), "platform": platform.platform(),
        "python": sys.version,
        "method": "read-only formal DB; alternating old SELECT * and one-connection narrow projection",
        "arguments": {"repeat": args.repeat, "rounds": args.rounds},
        "rtus": cases, "results": results, "medians": medians,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(medians, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

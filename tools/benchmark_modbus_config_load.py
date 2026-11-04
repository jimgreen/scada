"""Compare wide and narrow Modbus configuration reads on the formal DB.

The benchmark is read-only.  Both variants use one SQLite read transaction;
the only variable is the column projection used by ``from_store``.  This
keeps the result focused on Python row materialisation and SQLite I/O rather
than connection setup or device communication.
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import sys
import time
from pathlib import Path


TABLES = (
    ("fes_channel_modbus_define", "id"),
    ("fes_channel_modbus_define_call", "call_seq, id"),
    ("fes_channel_modbus_define_data", "call_seq, id"),
    ("fes_channel_modbus_define_ytyk", "id"),
)
PARAMETER_PROJECTIONS = {
    "modbus-tcp": (
        "fes_channel_modbus_tcp_define",
        "rtu_id,remote_host,remote_port,unit_id,connect_timeout,response_timeout,"
        "retry_count,keep_alive",
    ),
    "modbus-rtu": (
        "fes_channel_modbus_rtu_define",
        "rtu_id,serial_port,baudrate,bytesize,parity,stopbits,unit_id,"
        "response_timeout,retry_count",
    ),
}
NARROW = {
    "fes_channel_modbus_define": "rtu_id,irtuaddr",
    "fes_channel_modbus_define_call": "rtu_id,irtuaddr,call_seq,iplace,inum,icode",
    "fes_channel_modbus_define_data": (
        "rtu_id,call_seq,if_yx,cnt,trans_type,reg_addr,offset_bit,all_bit,"
        "if_reverse,twobytedata_byteseq,fourbytedata_byteseq"
    ),
    "fes_channel_modbus_define_ytyk": (
        "rtu_id,ifyk,cnt,irtuaddr,reg_addr,icode,ytyk_type,ifreverse,"
        "twobytedata_byteseq,fourbytedata_byteseq,multi_para,special_yk_yccnt,"
        "special_yk_bit,send_ff_replace_01"
    ),
}


def load(connection: sqlite3.Connection, rtu_id: int, protocol: str, narrow: bool):
    connection.execute("BEGIN")
    rtu_projection = "rtu_id,channel_type" if narrow else "*"
    rtu = connection.execute(
        f"SELECT {rtu_projection} FROM fes_rtu_define WHERE rtu_id=?", (rtu_id,)
    ).fetchone()
    if rtu is None:
        raise ValueError(f"missing RTU {rtu_id}")
    result = {"rtu": dict(rtu), "tables": {}}
    for table, order_by in TABLES:
        projection = NARROW[table] if narrow else "*"
        result["tables"][table] = [dict(row) for row in connection.execute(
            f"SELECT {projection} FROM {table} WHERE rtu_id=? ORDER BY {order_by}",
            (rtu_id,),
        )]
    table, projection = PARAMETER_PROJECTIONS[protocol]
    result["parameter"] = [dict(row) for row in connection.execute(
        f"SELECT {projection if narrow else '*'} FROM {table} WHERE rtu_id=? LIMIT 2",
        (rtu_id,),
    )]
    connection.commit()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("db/scada.sqlite3"))
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeat < 1 or args.rounds < 1:
        parser.error("repeat and rounds must be positive")

    base = sqlite3.connect(args.db)
    base.row_factory = sqlite3.Row
    base.execute("PRAGMA query_only=ON")
    cases = [
        (int(row["rtu_id"]), str(row["channel_type"]))
        for row in base.execute(
            "SELECT rtu_id,channel_type FROM fes_rtu_define "
            "WHERE channel_type IN ('modbus-tcp','modbus-rtu') ORDER BY rtu_id"
        )
    ]
    base.close()
    if not cases:
        parser.error("database has no Modbus RTU fixtures")

    def connection():
        value = sqlite3.connect(args.db)
        value.row_factory = sqlite3.Row
        value.execute("PRAGMA query_only=ON")
        return value

    results = []
    for round_no in range(1, args.rounds + 1):
        variants = (False, True) if round_no % 2 else (True, False)
        for narrow in variants:
            for rtu_id, protocol in cases:
                started = time.perf_counter()
                sample = None
                for _ in range(args.repeat):
                    with connection() as db:
                        sample = load(db, rtu_id, protocol, narrow)
                elapsed = (time.perf_counter() - started) * 1000
                results.append({
                    "round": round_no,
                    "variant": "narrow" if narrow else "wide",
                    "rtu_id": rtu_id,
                    "protocol": protocol,
                    "calls": args.repeat,
                    "elapsed_ms": elapsed,
                    "per_call_ms": elapsed / args.repeat,
                    "rows": sum(len(rows) for rows in sample["tables"].values()) + len(sample["parameter"]),
                })

    medians = {}
    for protocol in sorted({protocol for _, protocol in cases}):
        medians[protocol] = {}
        for variant in ("wide", "narrow"):
            rows = [r for r in results if r["protocol"] == protocol and r["variant"] == variant]
            medians[protocol][variant] = {
                "per_call_ms": statistics.median(r["per_call_ms"] for r in rows),
                "rows": statistics.median(r["rows"] for r in rows),
            }
        old = medians[protocol]["wide"]["per_call_ms"]
        new = medians[protocol]["narrow"]["per_call_ms"]
        medians[protocol]["reduction_percent"] = (old - new) / old * 100 if old else 0.0
    report = {
        "database": str(args.db.resolve()),
        "platform": platform.platform(),
        "python": sys.version,
        "method": "read-only formal DB; one transaction; SELECT * versus runtime narrow projection",
        "arguments": {"repeat": args.repeat, "rounds": args.rounds},
        "rtus": cases,
        "results": results,
        "medians": medians,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(medians, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

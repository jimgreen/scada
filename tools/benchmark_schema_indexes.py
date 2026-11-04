"""Benchmark explored command-history and duplicate-index schema options.

The benchmark uses temporary SQLite databases and never touches the formal
SCADA database.  The command-history comparison is exploratory; that table
is currently unused by production code, so the composite index is not
installed.  The duplicate-index comparison measures the adopted cleanup.
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path


def history_database(path: Path, composite: bool, rows: int) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute(
        "CREATE TABLE fes_ytyk_message ("
        "_version INTEGER NOT NULL DEFAULT 1, cmd_time INTEGER NOT NULL, "
        "is_yk INTEGER NOT NULL, rtu_id INTEGER NOT NULL, pnt_no INTEGER NOT NULL, "
        "name TEXT, value REAL, source INTEGER, logs TEXT, "
        "PRIMARY KEY(cmd_time,is_yk,rtu_id,pnt_no))"
    )
    batch = []
    for index in range(rows):
        batch.append((1700000000000 + index, index % 2, index % 60 + 1,
                      index % 200 + 1, f"p{index % 200}", float(index), 1, ""))
        if len(batch) == 5000:
            connection.executemany(
                "INSERT INTO fes_ytyk_message(cmd_time,is_yk,rtu_id,pnt_no,name,value,source,logs) "
                "VALUES (?,?,?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        connection.executemany(
            "INSERT INTO fes_ytyk_message(cmd_time,is_yk,rtu_id,pnt_no,name,value,source,logs) "
            "VALUES (?,?,?,?,?,?,?,?)", batch)
    if composite:
        connection.execute(
            "CREATE INDEX fes_ytyk_message_idx_1_rtu_id_cmd_time "
            "ON fes_ytyk_message(rtu_id,cmd_time)"
        )
    else:
        connection.execute(
            "CREATE INDEX fes_ytyk_message_rtu ON fes_ytyk_message(rtu_id)"
        )
    connection.commit()
    return connection


def history_benchmark(connection: sqlite3.Connection, repeats: int) -> tuple[float, list]:
    query = (
        "SELECT * FROM fes_ytyk_message WHERE rtu_id=? "
        "ORDER BY cmd_time DESC LIMIT 100"
    )
    plan = [list(row) for row in connection.execute("EXPLAIN QUERY PLAN " + query, (1,))]
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        for rtu_id in range(1, 61):
            connection.execute(query, (rtu_id,)).fetchall()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples), plan


def write_database(path: Path, duplicate: bool, rows: int) -> tuple[sqlite3.Connection, list[tuple]]:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute(
        "CREATE TABLE point ("
        "_version INTEGER, rtu_id INTEGER NOT NULL, kind TEXT NOT NULL, "
        "pnt_no INTEGER NOT NULL, io_address INTEGER NOT NULL, "
        "PRIMARY KEY(rtu_id,kind,pnt_no), UNIQUE(rtu_id,kind,io_address))"
    )
    if duplicate:
        connection.execute("CREATE UNIQUE INDEX old_uq ON point(rtu_id,kind,io_address)")
        connection.execute("CREATE INDEX old_rtu ON point(rtu_id)")
    connection.commit()
    payload = [(1, index % 60 + 1, "yc", index, 100000 + index)
               for index in range(rows)]
    return connection, payload


def write_benchmark(connection: sqlite3.Connection, payload: list[tuple], repeats: int) -> tuple[float, list]:
    samples = []
    for _ in range(repeats):
        connection.execute("DELETE FROM point")
        connection.commit()
        started = time.perf_counter()
        connection.executemany("INSERT INTO point VALUES (?,?,?,?,?)", payload)
        connection.commit()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples), samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=300_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rows < 1 or args.repeats < 1:
        parser.error("rows and repeats must be positive")
    with tempfile.TemporaryDirectory(prefix="scada-schema-index-") as folder:
        root = Path(folder)
        before_history = history_database(root / "history-before.sqlite3", False, args.rows)
        after_history = history_database(root / "history-after.sqlite3", True, args.rows)
        before_history_time, before_history_plan = history_benchmark(before_history, args.repeats)
        after_history_time, after_history_plan = history_benchmark(after_history, args.repeats)
        before_write_db, payload = write_database(root / "write-before.sqlite3", True, args.rows)
        after_write_db, _ = write_database(root / "write-after.sqlite3", False, args.rows)
        before_write, before_write_samples = write_benchmark(before_write_db, payload, args.repeats)
        after_write, after_write_samples = write_benchmark(after_write_db, payload, args.repeats)
        for connection in (before_history, after_history, before_write_db, after_write_db):
            connection.close()
    result = {
        "platform": platform.platform(),
        "python": sys.version,
        "rows": args.rows,
        "repeats": args.repeats,
        "history": {
            "before_ms": before_history_time,
            "after_ms": after_history_time,
            "reduction_percent": (before_history_time - after_history_time) / before_history_time * 100,
            "before_plan": before_history_plan,
            "after_plan": after_history_plan,
        },
        "duplicate_write": {
            "before_ms": before_write,
            "after_ms": after_write,
            "reduction_percent": (before_write - after_write) / before_write * 100,
            "before_samples_ms": before_write_samples,
            "after_samples_ms": after_write_samples,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

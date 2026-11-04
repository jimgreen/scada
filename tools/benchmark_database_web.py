"""Benchmark repeated WEB database reads and equivalent low-round-trip variants.

No production source is modified by this probe.  The candidate methods are
installed temporarily on ``ScadaStore`` and every result is compared with the
current implementation before timing.  Measurements use a fresh 60-RTU
fixture and alternating order.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import tempfile
import time
from types import MethodType
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scdfe_c.web.seed_protocol_test_data import seed_protocol_test_data
from scdfe_c.web.service import ScadaStore, quote_identifier, ApiError


def _candidate_overview(self: ScadaStore, rtu_id=None):
    if rtu_id is not None:
        self.get_row("fes_rtu_define", [rtu_id])
    names = self.catalog.names()
    parts = []
    params = []
    for name in names:
        scoped = rtu_id is not None and any(
            column["eng_name"] == "rtu_id" for column in self.schema(name)["columns"]
        )
        parts.append(
            "SELECT ? AS table_name, COUNT(*) AS row_count FROM "
            + quote_identifier(name)
            + (" WHERE rtu_id=?" if scoped else "")
        )
        params.append(name)
        if scoped:
            params.append(rtu_id)
    with self.connection() as connection:
        connection.execute("BEGIN")
        count_rows = connection.execute(" UNION ALL ".join(parts), params).fetchall()
        channel_rows = connection.execute(
            "SELECT channel_type FROM fes_rtu_define "
            "ORDER BY \"rtu_id\" ASC LIMIT 500"
        ).fetchall()
    counts = {row["table_name"]: int(row["row_count"]) for row in count_rows}
    channel_types = {}
    for row in channel_rows:
        channel_type = row["channel_type"] or "custom"
        channel_types[channel_type] = channel_types.get(channel_type, 0) + 1
    return {
        "counts": counts, "rtu_id": rtu_id, "database": str(self.db_path),
        "channel_types": channel_types,
    }


def _candidate_channel_for_rtu(self: ScadaStore, rtu_id):
    canonical, columns, _ = self._table_parts("fes_rtu_define")
    with self.connection() as connection:
        row = connection.execute(
            f"SELECT * FROM {quote_identifier(canonical)} WHERE rtu_id=?", (rtu_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "记录不存在", code="not_found")
        item = self._public_row(row, columns)
        if item.get("channel_id") is None or item.get("channel_type") is None:
            raise ApiError(404, "该 RTU 尚未配置通道", code="not_found")
        table = self.channel_parameter_table(item.get("channel_type"))
        parameter = None
        parameter_schema = None
        if table:
            parameter_schema = self.catalog.public(table)
            parameter_canonical, parameter_columns, primary_keys = self._table_parts(table)
            ordering = ", ".join(
                f"{quote_identifier(name)} ASC" for name in primary_keys
            )
            rows = connection.execute(
                f"SELECT * FROM {quote_identifier(parameter_canonical)} "
                f"WHERE rtu_id=? ORDER BY {ordering} LIMIT 2", (rtu_id,)
            ).fetchall()
            if len(rows) > 1:
                raise ApiError(
                    409, f"{table} 对同一 RTU 存在多个参数记录",
                    code="relationship_conflict",
                )
            if rows:
                parameter = self._public_row(rows[0], parameter_columns)
    return {
        "item": item, "parameter_table": table,
        "parameter": parameter, "parameter_schema": parameter_schema,
    }


def _time_call(function, repeat):
    started = time.perf_counter()
    result = None
    for _ in range(repeat):
        result = function()
    return (time.perf_counter() - started) * 1000, result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=200)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeat < 1 or args.rounds < 1:
        parser.error("repeat and rounds must be positive")
    results = []
    with tempfile.TemporaryDirectory(prefix="scada-database-web-") as folder:
        store = ScadaStore(Path(folder) / "fixture.sqlite3")
        seed_protocol_test_data(store, replicas=10)
        cases = {
            "overview_all": lambda: store.overview(),
            "overview_rtu": lambda: store.overview(1),
            "channel_for_rtu": lambda: store.channel_for_rtu(1),
        }
        baseline = {name: fn() for name, fn in cases.items()}
        original_overview = ScadaStore.overview
        original_channel = ScadaStore.channel_for_rtu
        try:
            for round_no in range(1, args.rounds + 1):
                order = ("before", "after") if round_no % 2 else ("after", "before")
                for variant in order:
                    if variant == "after":
                        store.overview = MethodType(_candidate_overview, store)
                        store.channel_for_rtu = MethodType(_candidate_channel_for_rtu, store)
                    else:
                        store.overview = MethodType(original_overview, store)
                        store.channel_for_rtu = MethodType(original_channel, store)
                    for name, fn in cases.items():
                        elapsed, value = _time_call(fn, args.repeat)
                        if value != baseline[name]:
                            raise AssertionError(f"{name} result changed for {variant}")
                        result = {"round": round_no, "variant": variant,
                                  "case": name, "calls": args.repeat,
                                  "elapsed_ms": elapsed}
                        results.append(result)
                        print(json.dumps(result), flush=True)
        finally:
            ScadaStore.overview = original_overview
            ScadaStore.channel_for_rtu = original_channel
    medians = {}
    for case in cases:
        medians[case] = {}
        for variant in ("before", "after"):
            values = [row["elapsed_ms"] for row in results
                      if row["case"] == case and row["variant"] == variant]
            medians[case][variant] = statistics.median(values)
    summary = {"repeat": args.repeat, "rounds": args.rounds,
               "results": results, "medians_ms": medians}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


if __name__ == "__main__":
    main()

"""Small repeatable benchmark for the scdfe_c runtime write path."""
from __future__ import annotations

import argparse
import pickle
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scdfe_c import ChannelSpec, ChannelWorker, _apply_batch_updates, definition_signature
from web.service import ScadaStore, quote_identifier
from tools.benchmark_inputs import MeasurementStream


POINT_COUNTS = {"yc": 1000, "yx": 1000, "yk": 200, "yt": 200}


class CountingQueue:
    def __init__(self) -> None:
        self.puts = 0
        self.messages: list[object] = []

    def put(self, message: object, **_kwargs: object) -> None:
        self.puts += 1
        self.messages.append(message)


def _bulk_create(store: ScadaStore, table: str, rows: list[dict[str, object]]) -> None:
    """Insert benchmark fixtures in one transaction, excluding setup cost."""
    if not rows:
        return
    normalised = [store._normalise(table, row) for row in rows]
    columns = [column["eng_name"] for column in store.schema(table)["columns"]
               if all(column["eng_name"] in row for row in normalised)]
    placeholders = ", ".join("?" for _ in columns)
    sql = (
        f"INSERT INTO {quote_identifier(table)} "
        f"({', '.join(quote_identifier(name) for name in columns)}) "
        f"VALUES ({placeholders})"
    )
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(sql, [[row[name] for name in columns] for row in normalised])
        connection.commit()


class LegacyWorker(ChannelWorker):
    """Use the previous one-queue-message-per-point write protocol."""

    def _queue_groups(self, groups):  # type: ignore[no-untyped-def]
        for group in groups:
            indexes = group.get("value_indexes", tuple(range(len(group["value_names"]))))
            value_count = len(set(indexes))
            for row in group["rows"]:
                self.writer_queue.put({
                    "op": "update", "table": group["table"],
                    "key": dict(zip(group["key_names"], row[value_count:])),
                    "values": {name: row[index] for name, index in zip(group["value_names"], indexes)},
                })

    def _command_rows(self):  # type: ignore[no-untyped-def]
        result = []
        with self.store.connection() as connection:
            connection.execute("BEGIN")
            for table, kind in (("fes_yk_define", "yk"), ("fes_yt_define", "yt")):
                rows = connection.execute(
                    f"SELECT pnt_no, value, cmd_time, cmd_handle_time "
                    f"FROM {quote_identifier(table)} WHERE rtu_id=? ORDER BY pnt_no",
                    (self.spec.rtu_id,),
                ).fetchall()
                result.extend((kind, dict(row)) for row in rows)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=1000)
    parser.add_argument("--db-cycles", type=int, default=20)
    parser.add_argument("--command-cycles", type=int, default=1000)
    parser.add_argument("--definition-cycles", type=int, default=50)
    args = parser.parse_args()
    if (args.cycles < 1 or args.db_cycles < 0 or args.command_cycles < 1
            or args.definition_cycles < 1):
        raise SystemExit("cycle counts must be positive and --db-cycles cannot be negative")
    # This stage intentionally holds a random sample stable to measure heartbeat filtering.
    values = MeasurementStream(1, POINT_COUNTS["yc"], POINT_COUNTS["yx"]).next()
    with tempfile.TemporaryDirectory() as log_dir:
        legacy_queue = CountingQueue()
        legacy_worker = LegacyWorker(
            None, ChannelSpec(1, 1, "custom", None, None),
            log_dir=log_dir, writer_queue=legacy_queue,
            measurement_heartbeat_interval=0,
        )
        legacy_worker._yc_scaling = dict.fromkeys(range(1, POINT_COUNTS["yc"] + 1), (2.0, 1.1))
        legacy_started = time.perf_counter()
        for _ in range(args.cycles):
            legacy_worker._sync_measurements(values)
        legacy_elapsed = time.perf_counter() - legacy_started

        queue = CountingQueue()
        worker = ChannelWorker(
            None, ChannelSpec(1, 1, "custom", None, None),
            log_dir=log_dir, writer_queue=queue,
        )
        worker._yc_scaling = dict.fromkeys(range(1, POINT_COUNTS["yc"] + 1), (2.0, 1.1))
        started = time.perf_counter()
        for _ in range(args.cycles):
            worker._sync_measurements(values)
        elapsed = time.perf_counter() - started
    legacy_payloads = legacy_queue.messages[:len(values)]
    batch_payload = queue.messages[0]
    legacy_pickle_started = time.perf_counter()
    for _ in range(args.cycles):
        for payload in legacy_payloads:
            pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    legacy_pickle_elapsed = time.perf_counter() - legacy_pickle_started
    pickle_started = time.perf_counter()
    for _ in range(args.cycles):
        pickle.dumps(batch_payload, protocol=pickle.HIGHEST_PROTOCOL)
    pickle_elapsed = time.perf_counter() - pickle_started
    actual_pickle_started = time.perf_counter()
    for payload in queue.messages:
        pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    actual_pickle_elapsed = time.perf_counter() - actual_pickle_started
    legacy_db_elapsed = db_elapsed = 0.0
    if args.db_cycles:
        with tempfile.TemporaryDirectory() as database_dir:
            store = ScadaStore(Path(database_dir) / "scada.sqlite3")
            _bulk_create(store, "fes_rtu_define", [{
                "rtu_id": 1, "channel_id": 1, "channel_type": "custom",
            }])
            _bulk_create(store, "fes_yc_define", [
                {"rtu_id": 1, "pnt_no": point_no, "name": f"YC{point_no}",
                 "raw_base": 2.0, "raw_scale": 1.1}
                for point_no in range(1, POINT_COUNTS["yc"] + 1)
            ])
            _bulk_create(store, "fes_yx_define", [
                {"rtu_id": 1, "pnt_no": point_no, "name": f"YX{point_no}"}
                for point_no in range(1, POINT_COUNTS["yx"] + 1)
            ])
            updates = [
                *legacy_payloads,
            ]

            def legacy_db_cycle() -> None:
                with store.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    for message in updates:
                        table = message["table"]
                        key = message["key"]
                        values = message["values"]
                        assignments = ", ".join(
                            f"{quote_identifier(name)}=?" for name in values
                        )
                        predicate = " AND ".join(
                            f"{quote_identifier(name)}=?" for name in key
                        )
                        connection.execute(
                            f"UPDATE {quote_identifier(table)} SET {assignments}, "
                            f"_version=_version+1 WHERE {predicate}",
                            [*values.values(), *key.values()],
                        )
                    connection.commit()

            legacy_db_cycle()
            started = time.perf_counter()
            for _ in range(args.db_cycles):
                legacy_db_cycle()
            legacy_db_elapsed = time.perf_counter() - started
            with store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                _apply_batch_updates(connection, updates)
                connection.commit()
            persistent_connection = store.open_connection()
            started = time.perf_counter()
            for _ in range(args.db_cycles):
                persistent_connection.execute("BEGIN IMMEDIATE")
                _apply_batch_updates(persistent_connection, updates)
                persistent_connection.commit()
            db_elapsed = time.perf_counter() - started
            persistent_connection.close()
    with tempfile.TemporaryDirectory() as command_dir:
        command_store = ScadaStore(Path(command_dir) / "scada.sqlite3")
        _bulk_create(command_store, "fes_rtu_define", [{
            "rtu_id": 1, "channel_id": 1, "channel_type": "custom",
        }])
        _bulk_create(command_store, "fes_yk_define", [
            {"rtu_id": 1, "pnt_no": point_no, "name": f"YK{point_no}"}
            for point_no in range(1, POINT_COUNTS["yk"] + 1)
        ])
        _bulk_create(command_store, "fes_yt_define", [
            {"rtu_id": 1, "pnt_no": point_no, "name": f"YT{point_no}"}
            for point_no in range(1, POINT_COUNTS["yt"] + 1)
        ])
        legacy_command_worker = LegacyWorker(
            command_store, ChannelSpec(1, 1, "custom", None, None), log_dir=command_dir,
        )
        command_worker = ChannelWorker(
            command_store, ChannelSpec(1, 1, "custom", None, None), log_dir=command_dir,
        )
        legacy_started = time.perf_counter()
        for _ in range(args.command_cycles):
            legacy_command_worker._command_rows()
        legacy_command_elapsed = time.perf_counter() - legacy_started
        command_worker._sync_commands()
        started = time.perf_counter()
        for _ in range(args.command_cycles):
            command_worker._command_rows()
        command_elapsed = time.perf_counter() - started
        command_worker._close_command_connection()
    with tempfile.TemporaryDirectory() as definition_dir:
        definition_store = ScadaStore(Path(definition_dir) / "scada.sqlite3")
        _bulk_create(definition_store, "fes_rtu_define", [{
            "rtu_id": 1, "channel_id": 1, "channel_type": "custom",
        }])
        _bulk_create(definition_store, "fes_yc_define", [
            {"rtu_id": 1, "pnt_no": point_no, "name": f"YC{point_no}", "value": 0.0}
            for point_no in range(1, POINT_COUNTS["yc"] + 1)
        ])
        _bulk_create(definition_store, "fes_yx_define", [
            {"rtu_id": 1, "pnt_no": point_no, "name": f"YX{point_no}", "value": 0}
            for point_no in range(1, POINT_COUNTS["yx"] + 1)
        ])
        _bulk_create(definition_store, "fes_yk_define", [
            {"rtu_id": 1, "pnt_no": point_no, "name": f"YK{point_no}",
             "value": 0, "cmd_time": 0}
            for point_no in range(1, POINT_COUNTS["yk"] + 1)
        ])
        _bulk_create(definition_store, "fes_yt_define", [
            {"rtu_id": 1, "pnt_no": point_no, "name": f"YT{point_no}",
             "value": 0.0, "cmd_time": 0}
            for point_no in range(1, POINT_COUNTS["yt"] + 1)
        ])
        started = time.perf_counter()
        for _ in range(args.definition_cycles):
            definition_signature(definition_store, 1)
        legacy_definition_elapsed = time.perf_counter() - started
        read_connection = definition_store.open_connection()
        started = time.perf_counter()
        for _ in range(args.definition_cycles):
            definition_signature(definition_store, 1, read_connection)
        definition_elapsed = time.perf_counter() - started
        read_connection.close()
    print(f"cycles={args.cycles}")
    print(f"points_per_cycle={len(values)}")
    print(f"points_per_rtu={POINT_COUNTS}")
    print(f"legacy_queue_puts={legacy_queue.puts}")
    print(f"legacy_elapsed_ms={legacy_elapsed * 1000:.3f}")
    print(f"legacy_pickle_ms={legacy_pickle_elapsed * 1000:.3f}")
    print(f"queue_puts={queue.puts}")
    print(f"elapsed_ms={elapsed * 1000:.3f}")
    print(f"pickle_ms={pickle_elapsed * 1000:.3f}")
    print(f"actual_stable_pickle_ms={actual_pickle_elapsed * 1000:.3f}")
    print(f"per_cycle_ms={elapsed * 1000 / args.cycles:.6f}")
    print(f"queue_put_reduction_pct={(1 - queue.puts / legacy_queue.puts) * 100:.4f}")
    print(f"stable_filter_reduction_pct={(1 - queue.puts / args.cycles) * 100:.2f}")
    print(f"pickle_time_reduction_pct={(1 - pickle_elapsed / legacy_pickle_elapsed) * 100:.2f}")
    print(f"stable_pickle_reduction_pct={(1 - actual_pickle_elapsed / legacy_pickle_elapsed) * 100:.2f}")
    if args.db_cycles:
        print(f"legacy_db_ms={legacy_db_elapsed * 1000:.3f}")
        print(f"db_ms={db_elapsed * 1000:.3f}")
        print(f"db_time_reduction_pct={(1 - db_elapsed / legacy_db_elapsed) * 100:.2f}")
    print(f"legacy_command_ms={legacy_command_elapsed * 1000:.3f}")
    print(f"command_ms={command_elapsed * 1000:.3f}")
    print(f"command_time_reduction_pct={(1 - command_elapsed / legacy_command_elapsed) * 100:.2f}")
    print(f"legacy_definition_ms={legacy_definition_elapsed * 1000:.3f}")
    print(f"definition_ms={definition_elapsed * 1000:.3f}")
    print(f"definition_time_reduction_pct={(1 - definition_elapsed / legacy_definition_elapsed) * 100:.2f}")


if __name__ == "__main__":
    main()

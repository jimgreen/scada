#!/usr/bin/env python3
"""Apply the measured SQLite index cleanup for the SCADA schema.

The JSON schema is the source of truth.  This migration only changes indexes;
it does not rebuild or rewrite any application table.  It is intentionally
idempotent so it can be run during deployment and reports ``backup=null`` on
an already optimized database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "db" / "scada.sqlite3"
SCHEMA = ROOT / "dbdef" / "json" / "fes_ytyk_message.json"

# These names were left by older schema revisions.  The current table-level
# UNIQUE constraints already provide the same indexes, so retaining the
# explicitly named copies only makes every point write maintain extra B-trees.
REDUNDANT_INDEXES: dict[str, tuple[str, ...]] = {
    # The command-history table is currently an unused legacy table.  Remove
    # both the old RTU-only index and the previously explored composite index;
    # the schema intentionally declares no new index for this table.
    "fes_ytyk_message": (
        "fes_ytyk_message_rtu",
        "fes_ytyk_message_idx_1_rtu_id_cmd_time",
    ),
    "fes_channel_bacnet_define": (
        "fes_bacnet_define_uq_1_rtu_id",
        "fes_bacnet_define_rtu",
    ),
    "fes_channel_iec104_define": (
        "fes_iec104_define_uq_1_rtu_id",
        "fes_iec104_define_rtu",
    ),
    "fes_channel_opc_define": (
        "fes_opc_define_uq_1_rtu_id",
        "fes_opc_define_rtu",
    ),
    "fes_channel_bacnet_point_define": (
        "fes_channel_bacnet_point_define_uq_1_rtu_id_kind_object_type_instance_property_identifier",
    ),
    "fes_channel_can_point_define": (
        "fes_channel_can_point_define_uq_1_rtu_id_kind_frame_id_byte_offset_bit_offset",
    ),
    "fes_channel_iec104_point_define": (
        "fes_channel_iec104_point_define_uq_1_rtu_id_kind_io_address",
    ),
    "fes_channel_opc_point_define": (
        "fes_channel_opc_point_define_uq_1_rtu_id_kind_node_id",
    ),
}


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def index_info(connection: sqlite3.Connection, table: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in connection.execute(f"PRAGMA index_list({quote(table)})"):
        name = str(row[1])
        columns = tuple(
            item[2]
            for item in connection.execute(f"PRAGMA index_info({quote(name)})")
        )
        result[name] = {
            "columns": columns,
            "unique": bool(row[2]),
            "origin": row[3],
        }
    return result


def schema_fingerprint(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def plan(connection: sqlite3.Connection, fingerprint: str) -> dict:
    actions = []
    for table, names in REDUNDANT_INDEXES.items():
        current = index_info(connection, table)
        for name in names:
            if name in current:
                actions.append({"action": "drop", "table": table, "index": name})
    current_fingerprint = connection.execute(
        "SELECT fingerprint FROM _scada_schema WHERE table_name='fes_ytyk_message'"
    ).fetchone()
    if current_fingerprint is None or current_fingerprint[0] != fingerprint:
        actions.append({"action": "update_fingerprint", "table": "fes_ytyk_message"})
    return {"actions": actions, "already_optimized": not actions}


def backup_database(path: Path) -> Path:
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    target = backup_dir / f"{path.stem}.before_schema_index_optimization.{stamp}.bak"
    source = sqlite3.connect(path, timeout=30)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    return target


def apply(path: Path) -> dict:
    fingerprint = schema_fingerprint(SCHEMA)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        preview = plan(connection, fingerprint)
    finally:
        connection.close()
    if preview["already_optimized"]:
        return {"database": str(path), "backup": None, **preview}

    backup = backup_database(path)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for table, names in REDUNDANT_INDEXES.items():
            current = index_info(connection, table)
            for name in names:
                if name not in current:
                    continue
                # Preserve the table-level PK/UNIQUE auto-index.  Every
                # listed historical index is redundant only when another
                # equivalent unique or primary index remains.
                target = current[name]
                equivalent = any(
                    other != name
                    and info["columns"] == target["columns"]
                    and (info["unique"] or not target["unique"])
                    for other, info in current.items()
                )
                # The composite command-history index was an explored but
                # rejected candidate: the production runtime has no reader
                # or writer for this legacy table.  Drop it explicitly so a
                # stale formal database does not retain an unused B-tree.
                stale_candidate = table == "fes_ytyk_message"
                if equivalent or stale_candidate:
                    connection.execute(f"DROP INDEX {quote(name)}")

        connection.execute(
            "UPDATE _scada_schema SET fingerprint=? WHERE table_name='fes_ytyk_message'",
            (fingerprint,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    verify = sqlite3.connect(path, timeout=30)
    try:
        integrity = verify.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = verify.execute("PRAGMA foreign_key_check").fetchall()
        after = plan(verify, fingerprint)
    finally:
        verify.close()
    if integrity != "ok" or foreign_keys or not after["already_optimized"]:
        raise RuntimeError(
            f"schema index migration verification failed: integrity={integrity!r}, "
            f"foreign_keys={len(foreign_keys)}, remaining={after['actions']}"
        )
    return {
        "database": str(path),
        "backup": str(backup),
        **preview,
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    path = args.db.resolve()
    if not path.is_file():
        parser.error(f"database does not exist: {path}")
    fingerprint = schema_fingerprint(SCHEMA)
    connection = sqlite3.connect(path, timeout=30)
    try:
        result = plan(connection, fingerprint)
    finally:
        connection.close()
    if args.dry_run:
        print(json.dumps({"database": str(path), **result}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(apply(path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

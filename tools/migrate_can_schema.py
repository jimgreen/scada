"""Upgrade the CAN schema for CAN-FD, scheduled frames and diagnostics.

The JSON definitions are authoritative, while an existing SQLite database is
strictly checked by :class:`ScadaStore`.  This command therefore adds the new
CAN columns, creates the CAN frame/protocol/diagnostic tables, refreshes their
schema fingerprints, and keeps a backup before applying changes.

It is read-only by default.  Use ``--apply`` after checking the reported plan
and ensuring that no SCADA process is writing the database.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scdfe_c.web.service import (  # noqa: E402
    DEFAULT_DB,
    SCHEMA_DIR,
    SchemaCatalog,
    ScadaStore,
    quote_identifier,
)


TABLE_COLUMNS = {
    "fes_channel_can_define": (
        ("sample_point", "REAL NOT NULL DEFAULT 87.5"),
        ("data_sample_point", "REAL NOT NULL DEFAULT 80.0"),
        ("bitrate_switch", "INTEGER NOT NULL DEFAULT 0"),
        ("restart_on_error", "INTEGER NOT NULL DEFAULT 0"),
        ("bus_off_recovery_ms", "INTEGER NOT NULL DEFAULT 0"),
        ("receive_error_frames", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "fes_channel_can_point_define": (
        ("dlc", "INTEGER"),
        ("period_ms", "INTEGER NOT NULL DEFAULT 0"),
        ("bitrate_switch", "INTEGER NOT NULL DEFAULT 0"),
        ("enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("dbc_message_name", "TEXT"),
        ("dbc_signal_name", "TEXT"),
        ("unit", "TEXT"),
        ("is_signed", "INTEGER NOT NULL DEFAULT 0"),
    ),
}

NEW_TABLES = (
    "fes_channel_can_frame_define",
    "fes_channel_canopen_service_define",
    "fes_channel_can_j1939_define",
    "fes_channel_can_uds_define",
    "fes_channel_can_diagnostic",
)

RENAMED_TABLES = {
    "fes_channel_j1939_define": "fes_channel_can_j1939_define",
    "fes_channel_uds_define": "fes_channel_can_uds_define",
}


def _fingerprint(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")
    }


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _create_table(connection: sqlite3.Connection, catalog: SchemaCatalog, table: str) -> None:
    payload = catalog.get(table)
    columns = payload["columns"]
    primary_keys = [column["eng_name"] for column in columns if column["primary_key"]]
    parts = ["_version INTEGER NOT NULL DEFAULT 1"]
    parts.extend(ScadaStore._column_definition(column) for column in columns)
    parts.append("PRIMARY KEY (" + ", ".join(quote_identifier(key) for key in primary_keys) + ")")
    for unique_key in payload.get("unique_keys", []):
        parts.append(
            "UNIQUE (" + ", ".join(quote_identifier(key) for key in unique_key) + ")"
        )
    if table != "fes_rtu_define" and any(
        column["eng_name"] == "rtu_id" for column in columns
    ):
        parts.append(
            "FOREIGN KEY (rtu_id) REFERENCES fes_rtu_define(rtu_id) "
            "ON DELETE RESTRICT ON UPDATE RESTRICT"
        )
    connection.execute(
        f"CREATE TABLE {quote_identifier(table)} ({', '.join(parts)})"
    )
    ScadaStore._ensure_indexes(connection, table, payload)


def migrate(
    db_path: Path | str,
    schema_dir: Path | str = SCHEMA_DIR,
    *,
    apply: bool = False,
) -> dict:
    db = Path(db_path).resolve(strict=True)
    schema_root = Path(schema_dir).resolve()
    catalog = SchemaCatalog(schema_root)
    report = {
        "database": str(db),
        "changed": False,
        "missing": {},
        "missing_tables": [],
        "rename_tables": [],
        "fingerprint_mismatch": {},
        "backup": None,
    }

    with closing(sqlite3.connect(db, isolation_level=None, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if not _table_exists(connection, "_scada_schema"):
            raise RuntimeError("数据库缺少 _scada_schema，不能安全迁移")

        for table, definitions in TABLE_COLUMNS.items():
            if not _table_exists(connection, table):
                raise RuntimeError(f"数据库缺少现有 CAN 表: {table}")
            actual = _columns(connection, table)
            report["missing"][table] = [
                name for name, _ in definitions if name not in actual
            ]

        for table in NEW_TABLES:
            old_table = next((old for old, new in RENAMED_TABLES.items() if new == table), None)
            if not _table_exists(connection, table) and not (old_table and _table_exists(connection, old_table)):
                report["missing_tables"].append(table)
            elif old_table and _table_exists(connection, old_table) and not _table_exists(connection, table):
                report["rename_tables"].append({"from": old_table, "to": table})
            elif old_table and _table_exists(connection, old_table) and _table_exists(connection, table):
                raise RuntimeError(f"旧表和新表同时存在，无法安全重命名: {old_table}, {table}")

        touched = tuple(TABLE_COLUMNS) + NEW_TABLES
        for table in touched:
            expected = _fingerprint(schema_root / f"{table}.json")
            current = connection.execute(
                "SELECT fingerprint FROM _scada_schema WHERE table_name=?", (table,)
            ).fetchone()
            if current is not None and current[0] != expected:
                report["fingerprint_mismatch"][table] = {
                    "current": current[0],
                    "expected": expected,
                }
            elif current is None and _table_exists(connection, table):
                # An existing table without an owned fingerprint is ambiguous;
                # never silently claim it is the table from the current schema.
                raise RuntimeError(f"_scada_schema 缺少表指纹: {table}")

        report["integrity_check_before"] = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
        if report["integrity_check_before"] != "ok":
            raise RuntimeError("迁移前数据库完整性检查失败")

        needs_change = any(report["missing"].values()) or bool(
            report["missing_tables"] or report["rename_tables"] or report["fingerprint_mismatch"]
        )
        if not needs_change:
            report["integrity_check"] = report["integrity_check_before"]
            return report
        if not apply:
            report["integrity_check"] = report["integrity_check_before"]
            return report

        backup = db.parent / "backups" / (
            db.name
            + ".before_can_schema."
            + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            + ".bak"
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        report["backup"] = str(backup)

        connection.execute("BEGIN IMMEDIATE")
        try:
            for old_table, new_table in RENAMED_TABLES.items():
                if _table_exists(connection, old_table) and not _table_exists(connection, new_table):
                    connection.execute(
                        f"ALTER TABLE {quote_identifier(old_table)} "
                        f"RENAME TO {quote_identifier(new_table)}"
                    )
                    connection.execute(
                        "UPDATE _scada_schema SET table_name=? WHERE table_name=?",
                        (new_table, old_table),
                    )

            for table, definitions in TABLE_COLUMNS.items():
                actual = _columns(connection, table)
                for name, sql_type in definitions:
                    if name not in actual:
                        connection.execute(
                            f"ALTER TABLE {quote_identifier(table)} "
                            f"ADD COLUMN {quote_identifier(name)} {sql_type}"
                        )

            for table in NEW_TABLES:
                if not _table_exists(connection, table):
                    _create_table(connection, catalog, table)

            invalid = connection.execute(
                "SELECT COUNT(*) FROM fes_channel_can_define "
                "WHERE sample_point <= 0 OR sample_point > 100 "
                "OR data_sample_point <= 0 OR data_sample_point > 100 "
                "OR bitrate_switch NOT IN (0, 1) "
                "OR restart_on_error NOT IN (0, 1) "
                "OR receive_error_frames NOT IN (0, 1) "
                "OR bus_off_recovery_ms < 0"
            ).fetchone()[0]
            if invalid:
                raise RuntimeError(f"fes_channel_can_define 存在 {invalid} 行无效 CAN 通道参数")

            invalid = connection.execute(
                "SELECT COUNT(*) FROM fes_channel_can_point_define "
                "WHERE (dlc IS NOT NULL AND (dlc < 0 OR dlc > 64)) "
                "OR period_ms < 0 OR bitrate_switch NOT IN (0, 1) "
                "OR enabled NOT IN (0, 1) OR is_signed NOT IN (0, 1)"
            ).fetchone()[0]
            if invalid:
                raise RuntimeError(f"fes_channel_can_point_define 存在 {invalid} 行无效 CAN 点参数")

            for table in touched:
                connection.execute(
                    "INSERT INTO _scada_schema(table_name, fingerprint) VALUES (?, ?) "
                    "ON CONFLICT(table_name) DO UPDATE SET fingerprint=excluded.fingerprint",
                    (table, _fingerprint(schema_root / f"{table}.json")),
                )
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("迁移后数据库完整性检查失败")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("迁移后外键检查失败")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        report["changed"] = True
        report["integrity_check"] = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, args.schema_dir, apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

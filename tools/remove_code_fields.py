"""Remove the duplicated business ``code`` columns from SCADA databases.

The migration is intentionally explicit.  The JSON catalog must already have
the ``code`` columns removed before this script is run.  It rebuilds the
affected tables from that catalog, preserves primary keys, foreign keys,
indexes, triggers and row counts, then refreshes ``_scada_schema``.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterable

from scdfe_c.web.service import DEFAULT_DB, KIND_TABLES, ScadaStore, SchemaCatalog, quote_identifier


CODE_TABLES = (
    "fes_channel_bacnet_define",
    "fes_channel_can_define",
    "fes_channel_can_frame_define",
    "fes_channel_can_j1939_define",
    "fes_channel_can_uds_define",
    "fes_channel_iec104_define",
    "fes_channel_modbus_define",
    "fes_channel_modbus_define_call",
    "fes_channel_modbus_define_data",
    "fes_channel_modbus_define_ytyk",
    "fes_channel_modbus_rtu_define",
    "fes_channel_modbus_tcp_define",
    "fes_channel_opc_define",
    "fes_rtu_define",
    "fes_scdfe_t_define",
    "fes_yc_define",
    "fes_yk_define",
    "fes_yt_define",
    "fes_yx_define",
)
RTU_UNUSED_FIELDS = ("call_all_cycle", "his_save", "refresh_trigger")
INTERNAL_TRIGGER_PREFIXES = ("_scada_defver_", "_scada_cmdver_")


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _schema_columns(catalog: SchemaCatalog, table: str) -> list[str]:
    return [column["eng_name"] for column in catalog.get(table)["columns"]]


def _create_sql(catalog: SchemaCatalog, table: str, name: str) -> str:
    payload = catalog.get(table)
    columns = payload["columns"]
    primary_keys = [column["eng_name"] for column in columns if column["primary_key"]]
    parts = ["_version INTEGER NOT NULL DEFAULT 1"]
    # Existing installations may contain intentionally unbound RTUs with a
    # NULL channel_id.  Preserve those rows during the destructive column
    # removal; the catalog still controls names and types, while the normal
    # fresh-database path continues to apply its required-field constraints.
    parts.extend(
        ScadaStore._column_definition(column, include_required=False)
        for column in columns
    )
    parts.append(
        "PRIMARY KEY (" + ", ".join(quote_identifier(key) for key in primary_keys) + ")"
    )
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
    suffix = " WITHOUT ROWID" if table in KIND_TABLES.values() else ""
    return f"CREATE TABLE {quote_identifier(name)} ({', '.join(parts)}){suffix}"


def _local_objects(connection: sqlite3.Connection, table: str) -> list[tuple[str, str, str]]:
    objects = []
    for kind, name, sql in connection.execute(
        "SELECT type,name,sql FROM sqlite_schema "
        "WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
        (table,),
    ):
        if any(name.startswith(prefix) for prefix in INTERNAL_TRIGGER_PREFIXES):
            continue
        if re.search(r"\bcode\b", sql, re.IGNORECASE):
            continue
        objects.append((kind, name, sql))
    return objects


def _rebuild_table(
    connection: sqlite3.Connection,
    catalog: SchemaCatalog,
    table: str,
) -> None:
    old_columns = {
        row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")
    }
    new_columns = ["_version", *_schema_columns(catalog, table)]
    missing = sorted(set(new_columns) - old_columns)
    if missing:
        raise RuntimeError(f"{table} 缺少待保留字段: {missing}")
    objects = _local_objects(connection, table)
    temp = f"_remove_code_{table}"
    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (temp,)
    ).fetchone():
        raise RuntimeError(f"发现未完成的临时表: {temp}")
    quoted_columns = ", ".join(quote_identifier(column) for column in new_columns)
    connection.execute(_create_sql(catalog, table, temp))
    connection.execute(
        f"INSERT INTO {quote_identifier(temp)} ({quoted_columns}) "
        f"SELECT {quoted_columns} FROM {quote_identifier(table)}"
    )
    connection.execute(f"DROP TABLE {quote_identifier(table)}")
    connection.execute(
        f"ALTER TABLE {quote_identifier(temp)} RENAME TO {quote_identifier(table)}"
    )
    for _kind, _name, sql in objects:
        connection.execute(sql)


def _backup(db: Path) -> Path:
    directory = db.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"{db.name}.before_remove_code.{stamp}.bak"
    with destination.open("xb"):
        pass
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError(f"备份完整性检查失败: {destination}")
    return destination


def _validate_catalog(catalog: SchemaCatalog) -> None:
    for table in CODE_TABLES:
        names = set(_schema_columns(catalog, table))
        if "code" in names:
            raise RuntimeError(f"schema 尚未删除 {table}.code")
        if "name" not in names:
            raise RuntimeError(f"{table} 缺少 name 字段")
    rtu_names = set(_schema_columns(catalog, "fes_rtu_define"))
    if rtu_names.intersection(RTU_UNUSED_FIELDS):
        raise RuntimeError("RTU schema 仍包含未使用字段")


def migrate(db: Path, *, write: bool = False) -> dict:
    db = db.resolve(strict=True)
    catalog = SchemaCatalog()
    _validate_catalog(catalog)
    uri = db.as_uri() + ("?mode=rw" if write else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=OFF" if write else "PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        before = {}
        for table in CODE_TABLES:
            actual = {row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")}
            if "code" not in actual:
                raise RuntimeError(f"{db.name}: {table} 没有待删除的 code 字段")
            before[table] = connection.execute(
                f"SELECT COUNT(*) FROM {quote_identifier(table)}"
            ).fetchone()[0]
        for field in RTU_UNUSED_FIELDS:
            if field not in {
                row[1] for row in connection.execute("PRAGMA table_info(fes_rtu_define)")
            }:
                raise RuntimeError(f"{db.name}: fes_rtu_define 没有待删除的 {field} 字段")
        result = {"db": str(db), "tables": list(CODE_TABLES), "changed": False, "backup": None}
        if not write:
            connection.commit()
            return result
        backup = _backup(db)
        try:
            # Foreign keys span several tables being rebuilt.  They are
            # checked again after all tables have been recreated.
            for (trigger,) in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' "
                "AND (name LIKE '_scada_defver_%' OR name LIKE '_scada_cmdver_%')"
            ).fetchall():
                connection.execute(f"DROP TRIGGER {quote_identifier(trigger)}")
            for table in CODE_TABLES:
                _rebuild_table(connection, catalog, table)
            for table, expected in before.items():
                actual = connection.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table)}"
                ).fetchone()[0]
                if actual != expected:
                    raise RuntimeError(f"{table} 行数变化: {expected} -> {actual}")
            for table in catalog.names():
                ScadaStore._ensure_indexes(connection, table, catalog.get(table))
                payload = catalog.get(table)
                connection.execute(
                    "INSERT INTO _scada_schema(table_name,fingerprint) VALUES (?,?) "
                    "ON CONFLICT(table_name) DO UPDATE SET fingerprint=excluded.fingerprint",
                    (table, _fingerprint(payload)),
                )
            ScadaStore._ensure_definition_version_triggers(connection, catalog)
            ScadaStore._ensure_command_version_triggers(connection)
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"外键检查失败: {[tuple(row) for row in violations[:10]]}")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("迁移后完整性检查失败")
            connection.commit()
            connection.execute("PRAGMA foreign_keys=ON")
        except Exception:
            connection.rollback()
            connection.execute("PRAGMA foreign_keys=ON")
            raise
        result.update(changed=True, backup=str(backup))
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="append", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    results = [migrate(path, write=args.apply) for path in args.db]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

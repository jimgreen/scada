"""Migrate protocol channel tables from rtu_id ownership to channel_id ownership.

The RTU definition remains the only table that relates an RTU to a channel.
Every ``fes_channel_*`` table stores ``channel_id`` only; this tool rebuilds
those SQLite tables so the old column, foreign keys and old primary keys are
removed together.  The default mode is a read-only plan.  ``--apply`` creates
an adjacent SQLite backup before committing the migration.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from scdfe_c.web.service import (
    DEFAULT_DB,
    RTU_VERSION_DDL,
    ScadaStore,
    SchemaCatalog,
    quote_identifier,
)


RUNTIME_TABLE = "fes_channel_runtime_status"


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'fes_channel_%' ORDER BY name"
        )
    ]


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(
        f"PRAGMA table_info({quote_identifier(table)})"
    )]


def _copy_backup(db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = db.parent / f"{db.name}.before_channel_id.{stamp}.bak"
    suffix = 1
    while backup.exists():
        backup = db.parent / f"{db.name}.before_channel_id.{stamp}_{suffix}.bak"
        suffix += 1
    with closing(sqlite3.connect(str(db))) as source, closing(sqlite3.connect(str(backup))) as destination:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"备份完整性校验失败: {backup}")
    return backup


def _plan(connection: sqlite3.Connection, catalog: SchemaCatalog) -> dict:
    channel_tables = [
        name for name in _tables(connection)
        if name != RUNTIME_TABLE and "rtu_id" in _columns(connection, name)
    ]
    mixed = [
        name for name in _tables(connection)
        if name != RUNTIME_TABLE
        and "rtu_id" in _columns(connection, name)
        and "channel_id" in _columns(connection, name)
    ]
    if mixed:
        raise RuntimeError(f"存在同时包含 rtu_id/channel_id 的半迁移表: {mixed}")
    if not channel_tables:
        return {
            "changed": False,
            "tables": [],
            "rows": {},
            "reason": "所有 fes_channel_* 表已经使用 channel_id",
        }
    rtu_columns = _columns(connection, "fes_rtu_define")
    if "channel_id" not in rtu_columns or "rtu_id" not in rtu_columns:
        raise RuntimeError("fes_rtu_define 缺少 rtu_id 或 channel_id，不能建立单向关联")
    duplicate = connection.execute(
        "SELECT channel_type, channel_id, COUNT(*) FROM fes_rtu_define "
        "WHERE channel_id IS NOT NULL GROUP BY channel_type, channel_id "
        "HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate:
        raise RuntimeError(f"fes_rtu_define 复合通道键存在重复值: {tuple(duplicate)}")
    mapping = {
        int(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT rtu_id, channel_id FROM fes_rtu_define WHERE channel_id IS NOT NULL"
        )
    }
    counts = {}
    for name in channel_tables:
        orphan = connection.execute(
            f"SELECT rtu_id, COUNT(*) FROM {quote_identifier(name)} "
            "WHERE rtu_id NOT IN (SELECT rtu_id FROM fes_rtu_define) "
            "GROUP BY rtu_id LIMIT 1"
        ).fetchone()
        if orphan:
            raise RuntimeError(f"{name} 存在孤儿 RTU 引用: {tuple(orphan)}")
        counts[name] = int(connection.execute(
            f"SELECT COUNT(*) FROM {quote_identifier(name)}"
        ).fetchone()[0])
    return {
        "changed": True,
        "tables": channel_tables,
        "rows": counts,
        "rtu_count": len(mapping),
    }


def _create_table(connection: sqlite3.Connection, table: str, payload: dict, temporary: str) -> None:
    columns = payload["columns"]
    primary_keys = [column["eng_name"] for column in columns if column["primary_key"]]
    parts = ["_version INTEGER NOT NULL DEFAULT 1"]
    parts.extend(ScadaStore._column_definition(column) for column in columns)
    parts.append(
        "PRIMARY KEY (" + ", ".join(quote_identifier(key) for key in primary_keys) + ")"
    )
    for unique_key in payload.get("unique_keys", []):
        parts.append(
            "UNIQUE (" + ", ".join(quote_identifier(key) for key in unique_key) + ")"
        )
    connection.execute(
        f"CREATE TABLE {quote_identifier(temporary)} ({', '.join(parts)})"
    )


def _rebuild_table(
    connection: sqlite3.Connection,
    table: str,
    payload: dict,
    channel_by_rtu: dict[int, int],
) -> tuple[int, int]:
    old_columns = _columns(connection, table)
    new_columns = [column["eng_name"] for column in payload["columns"]]
    if "rtu_id" not in old_columns or "channel_id" in old_columns:
        raise RuntimeError(f"{table} 不是待迁移的旧通道表")
    temporary = f"_scada_channel_rebuild_{table}"
    connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(temporary)}")
    _create_table(connection, table, payload, temporary)
    old_rows = connection.execute(f"SELECT * FROM {quote_identifier(table)}").fetchall()
    old_index = {name: index for index, name in enumerate(old_columns)}
    insert_sql = (
        f"INSERT INTO {quote_identifier(temporary)} ("
        + ", ".join(quote_identifier(name) for name in ["_version", *new_columns])
        + ") VALUES ("
        + ", ".join("?" for _ in ["_version", *new_columns])
        + ")"
    )
    for row in old_rows:
        rtu_id = int(row[old_index["rtu_id"]])
        try:
            channel_id = channel_by_rtu[rtu_id]
        except KeyError as exc:
            raise RuntimeError(f"{table} 的 rtu_id={rtu_id} 无对应 channel_id") from exc
        values = [int(row[old_index["_version"]])]
        for name in new_columns:
            if name == "channel_id":
                values.append(channel_id)
            elif name not in old_index:
                raise RuntimeError(f"{table} 缺少迁移所需字段: {name}")
            else:
                values.append(row[old_index[name]])
        connection.execute(insert_sql, values)
    connection.execute(f"DROP TABLE {quote_identifier(table)}")
    connection.execute(
        f"ALTER TABLE {quote_identifier(temporary)} RENAME TO {quote_identifier(table)}"
    )
    ScadaStore._ensure_indexes(connection, table, payload)
    return len(old_rows), int(connection.execute(
        f"SELECT COUNT(*) FROM {quote_identifier(table)}"
    ).fetchone()[0])


def migrate(db_path: Path | str = DEFAULT_DB, *, apply: bool = False) -> dict:
    db = Path(db_path).resolve(strict=True)
    catalog = SchemaCatalog()
    with closing(sqlite3.connect(str(db), timeout=30, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN")
        plan = _plan(connection, catalog)
        result = {"db": str(db), **plan, "backup": None, "row_counts": {}}
        if not apply or not plan["changed"]:
            connection.rollback()
            return result
        connection.rollback()
        backup = _copy_backup(db)
        result["backup"] = str(backup)
        connection.execute("BEGIN IMMEDIATE")
        plan = _plan(connection, catalog)
        if not plan["changed"]:
            connection.rollback()
            return {"db": str(db), **plan, "backup": str(backup), "row_counts": {}}
        channel_by_rtu = {
            int(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT rtu_id, channel_id FROM fes_rtu_define WHERE channel_id IS NOT NULL"
            )
        }
        for table in plan["tables"]:
            before, after = _rebuild_table(
                connection, table, catalog.get(table), channel_by_rtu
            )
            result["row_counts"][table] = {"before": before, "after": after}
            payload = catalog.get(table)
            connection.execute(
                "UPDATE _scada_schema SET fingerprint=? WHERE table_name=?",
                (_fingerprint(payload), table),
            )
        connection.execute(RTU_VERSION_DDL)
        connection.execute(
            "INSERT OR IGNORE INTO _scada_rtu_version(rtu_id) "
            "SELECT rtu_id FROM fes_rtu_define"
        )
        ScadaStore._ensure_definition_version_triggers(connection, catalog)
        ScadaStore._ensure_command_version_triggers(connection)
        remaining = [
            name for name in _tables(connection)
            if name != RUNTIME_TABLE and "rtu_id" in _columns(connection, name)
        ]
        if remaining:
            raise RuntimeError(f"迁移后仍存在 RTU 反向字段: {remaining}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"迁移后外键校验失败: {foreign_keys[:3]}")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("迁移后 integrity_check 失败")
        connection.commit()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="创建备份并提交迁移")
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

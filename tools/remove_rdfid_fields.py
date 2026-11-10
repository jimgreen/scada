"""Remove legacy ``rdfid`` columns from every current SCADA table.

The JSON catalog is authoritative.  This migration drops ``rdfid`` from
existing SQLite tables, preserves all rows and other columns, refreshes schema
fingerprints, and creates a backup before applying the change.  Stop the C,
S, and T Web services before applying it so no process writes during the
transaction.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scdfe_c.web.service import DEFAULT_DB, SCHEMA_DIR, SchemaCatalog, ScadaStore, quote_identifier
from scdfe_s.database import SimulatorStore


DEFAULT_S_DB = DEFAULT_DB.with_name("scdfe_s.sqlite3")


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")
    }


def _tables_with_rdfid(connection: sqlite3.Connection, catalog: SchemaCatalog) -> list[str]:
    catalog_names = set(catalog.names())
    tables = []
    for (table,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        if "rdfid" not in {name.lower() for name in _columns(connection, table)}:
            continue
        if table not in catalog_names:
            raise RuntimeError(f"数据库中的非当前业务表仍包含 rdfid: {table}")
        if "rdfid" in {column["eng_name"].lower() for column in catalog.get(table)["columns"]}:
            raise RuntimeError(f"当前 schema 仍定义 {table}.rdfid，请先更新 dbdef/json")
        tables.append(table)
    return tables


def _backup(db: Path) -> Path:
    backup_dir = db.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"{db.name}.before_remove_rdfid.{stamp}.bak"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(backup)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError(f"数据库备份完整性检查失败: {backup}")
    return backup


def _validate_schema(connection: sqlite3.Connection, catalog: SchemaCatalog) -> None:
    for table in catalog.names():
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue
        expected = {"_version", *(column["eng_name"] for column in catalog.get(table)["columns"])}
        actual = _columns(connection, table)
        if actual != expected:
            raise RuntimeError(
                f"{table} 与当前 schema 不一致；缺少={sorted(expected - actual)}，多余={sorted(actual - expected)}"
            )


def _drop_internal_triggers(connection: sqlite3.Connection) -> None:
    """Remove triggers whose old SQL may still mention a dropped column."""
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND "
            "(name LIKE '_scada_defver_%' OR name LIKE '_scada_cmdver_%' "
            "OR name LIKE '_scada_point_refresh_%')"
        )
    ]
    for name in names:
        connection.execute(f"DROP TRIGGER {quote_identifier(name)}")


def migrate(db_path: Path | str, schema_dir: Path | str = SCHEMA_DIR, *, apply: bool = False) -> dict:
    db = Path(db_path).resolve(strict=True)
    catalog = SchemaCatalog(schema_dir)
    report = {"database": str(db), "tables": [], "changed": False, "backup": None}
    with closing(sqlite3.connect(db, isolation_level=None, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("迁移前数据库完整性检查失败")
        tables = _tables_with_rdfid(connection, catalog)
        report["tables"] = tables
        report["row_counts"] = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {quote_identifier(table)}"
            ).fetchone()[0]
            for table in tables
        }
        if not tables or not apply:
            return report

        report["backup"] = str(_backup(db))
        connection.execute("BEGIN IMMEDIATE")
        try:
            _drop_internal_triggers(connection)
            for table in tables:
                connection.execute(
                    f"ALTER TABLE {quote_identifier(table)} DROP COLUMN {quote_identifier('rdfid')}"
                )
            for table in catalog.names():
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    connection.execute(
                        "UPDATE _scada_schema SET fingerprint=? WHERE table_name=?",
                        (_fingerprint(catalog.get(table)), table),
                    )
            ScadaStore._ensure_definition_version_triggers(connection, catalog)
            ScadaStore._ensure_command_version_triggers(connection)
            ScadaStore._ensure_point_refresh_triggers(connection)
            _validate_schema(connection, catalog)
            for table, count in report["row_counts"].items():
                actual = connection.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table)}"
                ).fetchone()[0]
                if actual != count:
                    raise RuntimeError(f"{table} 行数变化: {count} -> {actual}")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("迁移后外键检查失败")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("迁移后数据库完整性检查失败")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    store_class = SimulatorStore if db.name == DEFAULT_S_DB.name else ScadaStore
    validated = store_class(db, schema_dir)
    validated.close()
    report["changed"] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="append", type=Path, dest="databases")
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    databases = args.databases or [DEFAULT_DB, DEFAULT_S_DB]
    print(json.dumps(
        [migrate(path, args.schema_dir, apply=args.apply) for path in databases],
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

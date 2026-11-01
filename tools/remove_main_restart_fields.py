"""One-way migration for removing every persisted ``process_restart*`` field.

Communication recovery remains automatic in ``scdfe_c``; only the restart
request/counter/time records are removed from storage and the public schema.
Stop ``scdfe_c`` and the Web service first.  The database is backed up and
validated before and after the change.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scdfe_control import ServerInstanceLock
from web.service import DEFAULT_DB, SCHEMA_DIR, SchemaCatalog, ScadaStore, quote_identifier


REMOVED = {
    "fes_rtu_define": ("process_restart", "process_restart_time", "process_restart_count"),
    "fes_scdfe_c_define": ("process_restart", "process_restart_time", "process_restart_count"),
    "fes_process_define": ("process_restart", "process_restart_time", "process_restart_count"),
}


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(
        f"PRAGMA table_info({quote_identifier(table)})"
    )]


def _validate(connection: sqlite3.Connection, catalog: SchemaCatalog, table: str) -> None:
    expected = {"_version", *(column["eng_name"] for column in catalog.public(table)["columns"])}
    actual = set(_columns(connection, table))
    if expected != actual:
        raise RuntimeError(
            f"{table} 与当前 schema 不一致；缺少={sorted(expected - actual)}，"
            f"多余={sorted(actual - expected)}"
        )


def migrate(db_path: Path | str = DEFAULT_DB, *, schema_dir: Path | str = SCHEMA_DIR) -> dict:
    db = Path(db_path).resolve(strict=True)
    catalog = SchemaCatalog(schema_dir)
    lock = ServerInstanceLock()
    if not lock.acquire():
        raise RuntimeError("请先停止 scdfe_c 再迁移数据库")
    report = {
        "database": str(db),
        "backup": None,
        "removed": {table: list(columns) for table, columns in REMOVED.items()},
    }
    try:
        with closing(sqlite3.connect(db, isolation_level=None, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("迁移前数据库完整性检查失败")
            backup = db.parent / "backups" / (
                db.name + ".before_remove_main_restart_fields."
                + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".bak"
            )
            backup.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("备份完整性检查失败")
            report["backup"] = str(backup)

            connection.execute("BEGIN IMMEDIATE")
            try:
                for table, columns in REMOVED.items():
                    for column in columns:
                        if column not in _columns(connection, table):
                            continue
                        connection.execute(
                            f"ALTER TABLE {quote_identifier(table)} "
                            f"DROP COLUMN {quote_identifier(column)}"
                        )
                    connection.execute(
                        "UPDATE _scada_schema SET fingerprint=? WHERE table_name=?",
                        (_fingerprint(catalog.get(table)), table),
                    )

                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("迁移后数据库完整性检查失败")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("迁移后外键检查失败")
                for table in catalog.names():
                    if not connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone():
                        continue
                    _validate(connection, catalog, table)
                    row = connection.execute(
                        "SELECT fingerprint FROM _scada_schema WHERE table_name=?", (table,)
                    ).fetchone()
                    if row is None or row[0] != _fingerprint(catalog.get(table)):
                        raise RuntimeError(f"{table} schema 指纹不匹配")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        ScadaStore(db, schema_dir)
        report["integrity_check"] = "ok"
        report["foreign_key_violations"] = 0
        return report
    finally:
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = migrate(args.db)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()

"""Add and validate the Modbus address-base fields.

The runtime accepts both wire addresses and common reference addresses such
as ``40001``.  Older SQLite databases have no way to persist which convention
was used, so this migration adds ``address_base`` with a backwards-compatible
zero-based default and refreshes schema fingerprints after the JSON definitions
change.  It is read-only by default; pass ``--apply`` only after checking the
reported database and backup location.
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

from scdfe_c.web.service import DEFAULT_DB, SCHEMA_DIR, quote_identifier  # noqa: E402


ADDRESS_BASES = (0, 1, 10001, 30001, 40001)
TABLE_COLUMNS = {
    "fes_channel_modbus_define": (("address_base", "INTEGER NOT NULL DEFAULT 0"),),
    "fes_channel_modbus_define_call": (("address_base", "INTEGER NOT NULL DEFAULT 0"),),
    "fes_channel_modbus_define_data": (),
    "fes_channel_modbus_define_ytyk": (("address_base", "INTEGER NOT NULL DEFAULT 0"),),
    "fes_scdfe_t_point_define": (("address_base", "INTEGER NOT NULL DEFAULT 0"),),
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


def _invalid_count(connection: sqlite3.Connection, table: str) -> int:
    column = quote_identifier("address_base")
    allowed = ",".join("?" for _ in ADDRESS_BASES)
    return connection.execute(
        f"SELECT COUNT(*) FROM {quote_identifier(table)} "
        f"WHERE {column} IS NULL OR {column} NOT IN ({allowed})",
        ADDRESS_BASES,
    ).fetchone()[0]


def migrate(
    db_path: Path | str,
    schema_dir: Path | str = SCHEMA_DIR,
    *,
    apply: bool = False,
) -> dict:
    db = Path(db_path).resolve(strict=True)
    schema_root = Path(schema_dir).resolve()
    report = {
        "database": str(db),
        "changed": False,
        "missing": {},
        "fingerprint_mismatch": {},
        "invalid_address_base": {},
        "backup": None,
    }

    with closing(sqlite3.connect(db, isolation_level=None, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for table, definitions in TABLE_COLUMNS.items():
            if not _table_exists(connection, table):
                raise RuntimeError(f"数据库缺少表: {table}")
            actual = _columns(connection, table)
            report["missing"][table] = [
                name for name, _ in definitions if name not in actual
            ]
            expected = _fingerprint(schema_root / f"{table}.json")
            current = connection.execute(
                "SELECT fingerprint FROM _scada_schema WHERE table_name=?", (table,)
            ).fetchone()
            if current is None:
                raise RuntimeError(f"_scada_schema 缺少表指纹: {table}")
            if current[0] != expected:
                report["fingerprint_mismatch"][table] = {
                    "current": current[0],
                    "expected": expected,
                }
            if "address_base" in actual:
                report["invalid_address_base"][table] = _invalid_count(connection, table)

        invalid_tables = {
            table: count
            for table, count in report["invalid_address_base"].items()
            if count
        }
        if invalid_tables and apply:
            raise RuntimeError(
                "数据库存在非法 address_base: "
                + ", ".join(f"{table}={count}" for table, count in invalid_tables.items())
            )

        report["integrity_check_before"] = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
        if report["integrity_check_before"] != "ok":
            raise RuntimeError("迁移前数据库完整性检查失败")

        needs_change = any(report["missing"].values()) or bool(
            report["fingerprint_mismatch"]
        )
        if not needs_change:
            report["integrity_check"] = report["integrity_check_before"]
            return report
        if not apply:
            report["integrity_check"] = report["integrity_check_before"]
            return report

        backup = db.parent / "backups" / (
            db.name
            + ".before_modbus_address_base."
            + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            + ".bak"
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        report["backup"] = str(backup)

        connection.execute("BEGIN IMMEDIATE")
        try:
            for table, definitions in TABLE_COLUMNS.items():
                actual = _columns(connection, table)
                for name, sql_type in definitions:
                    if name not in actual:
                        connection.execute(
                            f"ALTER TABLE {quote_identifier(table)} "
                            f"ADD COLUMN {quote_identifier(name)} {sql_type}"
                        )
                invalid = _invalid_count(connection, table) if "address_base" in _columns(
                    connection, table
                ) else 0
                if invalid:
                    raise RuntimeError(
                        f"{table} 存在 {invalid} 行非法 address_base，"
                        f"允许值为 {ADDRESS_BASES}"
                    )
                connection.execute(
                    "UPDATE _scada_schema SET fingerprint=? WHERE table_name=?",
                    (_fingerprint(schema_root / f"{table}.json"), table),
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
    print(
        json.dumps(
            migrate(args.db, args.schema_dir, apply=args.apply),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

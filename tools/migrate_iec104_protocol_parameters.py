"""Add IEC 104 transport parameters to channel and forwarding definitions.

The application schema is strict, so an existing database needs an explicit
migration before the new ``k/w/t1/t2/t3/max_asdu_length`` fields can be used.
This command defaults to a read-only plan.  Pass ``--apply`` only after the
SCADA services have been stopped and the reported backup is acceptable.
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

from scdfe_c.web.service import DEFAULT_DB, SCHEMA_DIR, quote_identifier


TABLE_COLUMNS = {
    "fes_channel_iec104_define": (
        ("k", "INTEGER NOT NULL DEFAULT 12"),
        ("w", "INTEGER NOT NULL DEFAULT 8"),
        ("t1", "REAL NOT NULL DEFAULT 15.0"),
        ("t2", "REAL NOT NULL DEFAULT 10.0"),
        ("t3", "REAL NOT NULL DEFAULT 20.0"),
        ("max_asdu_length", "INTEGER NOT NULL DEFAULT 249"),
    ),
    "fes_scdfe_t_define": (
        ("k", "INTEGER NOT NULL DEFAULT 12"),
        ("w", "INTEGER NOT NULL DEFAULT 8"),
        ("t1", "REAL NOT NULL DEFAULT 15.0"),
        ("t2", "REAL NOT NULL DEFAULT 10.0"),
        ("t3", "REAL NOT NULL DEFAULT 20.0"),
        ("max_asdu_length", "INTEGER NOT NULL DEFAULT 249"),
    ),
    "fes_channel_iec104_point_define": (
        ("time_tag", "TEXT NOT NULL DEFAULT 'NONE'"),
    ),
    "fes_scdfe_t_point_define": (
        ("time_tag", "TEXT NOT NULL DEFAULT 'NONE'"),
    ),
}

PARAMETER_TABLES = {
    "fes_channel_iec104_define",
    "fes_scdfe_t_define",
}


def _fingerprint(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1] for row in connection.execute(
            f"PRAGMA table_info({quote_identifier(table)})"
        )
    }


def migrate(db_path: Path | str, schema_dir: Path | str = SCHEMA_DIR, *, apply: bool = False) -> dict:
    db = Path(db_path).resolve(strict=True)
    schema_root = Path(schema_dir).resolve()
    report = {"database": str(db), "changed": False, "missing": {}, "backup": None}
    with closing(sqlite3.connect(db, isolation_level=None, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for table, definitions in TABLE_COLUMNS.items():
            actual = _columns(connection, table)
            report["missing"][table] = [name for name, _ in definitions if name not in actual]

        if not any(report["missing"].values()):
            report["integrity_check"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
            return report

        if apply:
            backup = db.parent / "backups" / (
                db.name + ".before_iec104_protocol_parameters."
                + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".bak"
            )
            backup.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)
            report["backup"] = str(backup)

        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("迁移前数据库完整性检查失败")
            if apply:
                for table, definitions in TABLE_COLUMNS.items():
                    for name, sql_type in definitions:
                        if name not in _columns(connection, table):
                            connection.execute(
                                f"ALTER TABLE {quote_identifier(table)} "
                                f"ADD COLUMN {quote_identifier(name)} {sql_type}"
                            )
                    if table in PARAMETER_TABLES:
                        invalid = connection.execute(
                            f"SELECT COUNT(*) FROM {quote_identifier(table)} "
                            "WHERE k < 1 OR k > 32767 OR w < 1 OR w > k "
                            "OR t1 <= 0 OR t2 <= 0 OR t3 <= 0 "
                            "OR max_asdu_length < 1 OR max_asdu_length > 249"
                        ).fetchone()[0]
                        if invalid:
                            raise RuntimeError(f"{table} 存在 {invalid} 行无效 IEC104 协议参数")
                    connection.execute(
                        "UPDATE _scada_schema SET fingerprint=? WHERE table_name=?",
                        (_fingerprint(schema_root / f"{table}.json"), table),
                    )
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("迁移后数据库完整性检查失败")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("迁移后外键检查失败")
                report["changed"] = True
            connection.commit()
        except Exception:
            connection.rollback()
            raise
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

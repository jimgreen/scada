"""Add live forwarding fields to ``fes_scdfe_t_point_define``.

The JSON schema is authoritative and the application database is strict, so
an existing database needs an explicit migration before the scdfe_t runtime
can persist live point snapshots.  The command is read-only by default and
creates a SQLite backup before applying changes.
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

TABLE = "fes_scdfe_t_point_define"
COLUMNS = (
    ("value", "REAL NOT NULL DEFAULT 0.0"),
    ("status", "INTEGER NOT NULL DEFAULT 128"),
    ("refresh_time", "INTEGER NOT NULL DEFAULT 0"),
    ("forward_time", "INTEGER NOT NULL DEFAULT 0"),
    ("command_receive_time", "INTEGER NOT NULL DEFAULT 0"),
)


def fingerprint(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {
        row[1] for row in connection.execute(
            f"PRAGMA table_info({quote_identifier(TABLE)})"
        )
    }


def _backup(db: Path) -> Path:
    backup = db.parent / "backups" / (
        db.name + ".before_scdfe_t_point_runtime."
        + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".bak"
    )
    backup.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(backup)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"数据库备份完整性检查失败: {backup}")
    return backup


def migrate(db_path: Path | str = DEFAULT_DB, schema_dir: Path | str = SCHEMA_DIR,
            *, apply: bool = False) -> dict:
    db = Path(db_path).resolve(strict=True)
    schema_root = Path(schema_dir).resolve()
    schema_path = schema_root / f"{TABLE}.json"
    report = {"database": str(db), "table": TABLE, "missing": [],
              "fingerprint_mismatch": False, "changed": False, "backup": None}
    expected_fingerprint = fingerprint(schema_path)
    with closing(sqlite3.connect(db, isolation_level=None, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        actual = _columns(connection)
        report["missing"] = [name for name, _ in COLUMNS if name not in actual]
        current = connection.execute(
            "SELECT fingerprint FROM _scada_schema WHERE table_name=?", (TABLE,)
        ).fetchone()
        report["fingerprint_mismatch"] = current is None or current[0] != expected_fingerprint
        if not report["missing"] and not report["fingerprint_mismatch"]:
            report["integrity_check"] = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            return report

        if apply:
            report["backup"] = str(_backup(db))
        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("迁移前数据库完整性检查失败")
            if apply:
                for name, definition in COLUMNS:
                    if name not in _columns(connection):
                        connection.execute(
                            f"ALTER TABLE {quote_identifier(TABLE)} "
                            f"ADD COLUMN {quote_identifier(name)} {definition}"
                        )
                connection.execute(
                    "UPDATE _scada_schema SET fingerprint=? WHERE table_name=?",
                    (expected_fingerprint, TABLE),
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
    print(json.dumps(migrate(args.db, args.schema_dir, apply=args.apply),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

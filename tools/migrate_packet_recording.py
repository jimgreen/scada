"""Add the unified channel-level packet_recording parameter to SCDFE databases.

The application schema is intentionally strict.  This explicit migration adds
the field to the C/S RTU table and the T forwarding-channel table, refreshes
schema fingerprints, and verifies integrity before committing.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scdfe_c.web.service import DEFAULT_DB, SCHEMA_DIR, quote_identifier

TABLES = ("fes_rtu_define", "fes_scdfe_t_define")


def fingerprint(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def migrate(db_path: Path | str, schema_dir: Path | str = SCHEMA_DIR, *, apply: bool = False) -> dict:
    db = Path(db_path).resolve(strict=True)
    schema_root = Path(schema_dir).resolve()
    report = {"database": str(db), "changed": False, "tables": [], "backup": None}
    with closing(sqlite3.connect(db, timeout=30, isolation_level=None)) as con:
        con.execute("PRAGMA foreign_keys=ON")
        # Inspect first so the backup is taken before the write transaction.
        missing = []
        for table in TABLES:
            columns = {row[1] for row in con.execute(f"PRAGMA table_info({quote_identifier(table)})")}
            if "packet_recording" not in columns:
                missing.append(table)
        report["tables"] = missing
        if apply and missing:
            backup_dir = db.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"{db.name}.before_packet_recording.{datetime.now():%Y%m%d_%H%M%S}.bak"
            with closing(sqlite3.connect(backup)) as target, closing(sqlite3.connect(db, uri=False)) as source:
                source.backup(target)
            report["backup"] = str(backup)
        con.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("迁移前数据库完整性检查失败")
            if apply and report["tables"]:
                for table in report["tables"]:
                    con.execute(
                        f"ALTER TABLE {quote_identifier(table)} ADD COLUMN packet_recording INTEGER NOT NULL DEFAULT 0"
                    )
                for table in TABLES:
                    schema = schema_root / f"{table}.json"
                    con.execute(
                        "UPDATE _scada_schema SET fingerprint=? WHERE table_name=?",
                        (fingerprint(schema), table),
                    )
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("迁移后数据库完整性检查失败")
                if con.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("迁移后外键检查失败")
                report["changed"] = True
            con.commit()
        except Exception:
            con.rollback()
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

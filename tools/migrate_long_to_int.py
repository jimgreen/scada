"""Migrate legacy ``LONG`` field metadata to the canonical ``INT`` name.

The SQLite columns already use ``INTEGER`` because the JSON ``data_type`` is
``int``.  This migration updates the authoritative JSON schemas, generated
Python metadata and DBDEF exports, then refreshes the formal database schema
fingerprints.  It is read-only unless ``--apply`` is supplied and always makes
an adjacent database backup before changing the fingerprint table.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scdfe_c.web.service import DEFAULT_DB, SCHEMA_DIR, SchemaCatalog  # noqa: E402


TOKEN = re.compile(r"(?<![A-Za-z0-9_])LONG(?![A-Za-z0-9_])")


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _json_long_count(payload: dict) -> int:
    return sum(column.get("disp_type") == "LONG" for column in payload["columns"])


def _schema_plan(schema_dir: Path) -> list[dict]:
    result = []
    for path in sorted(schema_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        count = _json_long_count(payload)
        if count:
            result.append({"path": str(path), "kind": "json", "count": count})
    return result


def _text_plan(root: Path) -> list[dict]:
    result = []
    for directory, suffix in ((root / "dbdef" / "def", ".py"), (root / "dbdef", ".dbdef")):
        for path in sorted(directory.glob(f"*{suffix}")):
            count = len(TOKEN.findall(path.read_text(encoding="utf-8")))
            if count:
                result.append({"path": str(path), "kind": suffix[1:], "count": count})
    return result


def plan(root: Path = ROOT, db: Path = DEFAULT_DB) -> dict:
    schema_dir = root / "dbdef" / "json"
    files = _schema_plan(schema_dir) + _text_plan(root)
    fingerprint_mismatch = {}
    if db.is_file():
        with closing(sqlite3.connect(db)) as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_scada_schema'"
            ).fetchone():
                for path in sorted(schema_dir.glob("*.json")):
                    table = path.stem
                    expected = _fingerprint(json.loads(path.read_text(encoding="utf-8-sig")))
                    current = connection.execute(
                        "SELECT fingerprint FROM _scada_schema WHERE table_name=?", (table,)
                    ).fetchone()
                    if current and current[0] != expected:
                        fingerprint_mismatch[table] = {
                            "current": current[0], "expected": expected,
                        }
    return {
        "database": str(db.resolve()),
        "files": files,
        "file_count": len(files),
        "long_count": sum(item["count"] for item in files),
        "fingerprint_mismatch": fingerprint_mismatch,
        "changed": bool(files),
    }


def _backup_database(db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = db.parent / f"{db.name}.before_long_to_int.{stamp}.bak"
    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(backup)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"数据库备份完整性检查失败: {backup}")
    return backup


def migrate(root: Path = ROOT, db: Path = DEFAULT_DB, *, apply: bool = False) -> dict:
    root = Path(root).resolve()
    db = Path(db).resolve()
    schema_dir = root / "dbdef" / "json"
    result = plan(root, db)
    result["backup"] = None
    if not apply or not result["changed"]:
        return result

    if not db.is_file():
        raise FileNotFoundError(db)
    backup = _backup_database(db)
    result["backup"] = str(backup)

    changed = []
    for path in sorted(schema_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        count = _json_long_count(payload)
        if not count:
            continue
        for column in payload["columns"]:
            if column.get("disp_type") == "LONG":
                column["disp_type"] = "INT"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        changed.append(str(path))

    for item in _text_plan(root):
        path = Path(item["path"])
        text = path.read_text(encoding="utf-8")
        updated = TOKEN.sub("INT", text)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.append(str(path))

    # Validate the new authoritative schemas before touching the database.
    catalog = SchemaCatalog(schema_dir)
    with closing(sqlite3.connect(db, isolation_level=None, timeout=30)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = 0
            for table in catalog.names():
                fingerprint = _fingerprint(catalog.get(table))
                cursor = connection.execute(
                    "UPDATE _scada_schema SET fingerprint=? WHERE table_name=?",
                    (fingerprint, table),
                )
                rows += cursor.rowcount
            if rows != len(catalog.names()):
                raise RuntimeError(
                    f"_scada_schema 指纹数量不完整: updated={rows}, expected={len(catalog.names())}"
                )
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("迁移后数据库完整性检查失败")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("迁移后外键检查失败")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    result["changed_files"] = changed
    result["updated_fingerprints"] = len(catalog.names())
    result["integrity_check"] = "ok"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.root, args.db, apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

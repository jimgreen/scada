"""Migrate fes_rtu_define from a global channel_id key to a pair key.

The RTU-to-channel relation is identified by (channel_type, channel_id).
The default mode prints a read-only plan; --apply creates an adjacent SQLite
backup before replacing the old unique index and schema fingerprint.
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

from scdfe_c.web.service import (
    DEFAULT_DB,
    SCHEMA_DIR,
    ScadaStore,
    SchemaCatalog,
    quote_identifier,
)


TABLE = "fes_rtu_define"
OLD_INDEX = "fes_rtu_define_uq_1_channel_id"


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _copy_backup(db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = db.parent / f"{db.name}.before_rtu_channel_pair.{stamp}.bak"
    suffix = 1
    while backup.exists():
        backup = db.parent / f"{db.name}.before_rtu_channel_pair.{stamp}_{suffix}.bak"
        suffix += 1
    with closing(sqlite3.connect(str(db))) as source, closing(sqlite3.connect(str(backup))) as destination:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"备份完整性校验失败: {backup}")
    return backup


def _index_columns(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    result = {}
    for row in connection.execute(f"PRAGMA index_list({quote_identifier(TABLE)})"):
        name = row[1]
        result[name] = tuple(
            item[2]
            for item in connection.execute(f"PRAGMA index_info({quote_identifier(name)})")
        )
    return result


def migrate(db_path: Path | str = DEFAULT_DB, *, apply: bool = False) -> dict:
    db = Path(db_path).resolve(strict=True)
    catalog = SchemaCatalog(SCHEMA_DIR)
    payload = catalog.get(TABLE)
    expected = tuple(payload["unique_keys"][0])
    if expected != ("channel_type", "channel_id"):
        raise RuntimeError(f"{TABLE} schema 未定义复合唯一键: {expected}")
    with closing(sqlite3.connect(str(db), timeout=30, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        indexes = _index_columns(connection)
        pair_duplicates = connection.execute(
            f"SELECT channel_type, channel_id, COUNT(*) AS count FROM {quote_identifier(TABLE)} "
            "WHERE channel_type IS NOT NULL AND channel_id IS NOT NULL "
            "GROUP BY channel_type, channel_id HAVING COUNT(*) > 1"
        ).fetchall()
        old_present = indexes.get(OLD_INDEX) == ("channel_id",)
        pair_present = any(columns == expected for columns in indexes.values())
        result = {
            "db": str(db),
            "changed": bool(old_present or not pair_present),
            "old_index_present": old_present,
            "pair_index_present": pair_present,
            "duplicate_pairs": [tuple(row) for row in pair_duplicates],
            "backup": None,
        }
        if pair_duplicates:
            raise RuntimeError(f"{TABLE} 存在重复复合键: {result['duplicate_pairs']}")
        if not apply or (pair_present and not old_present):
            return result
        backup = _copy_backup(db)
        result["backup"] = str(backup)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"DROP INDEX IF EXISTS {quote_identifier(OLD_INDEX)}")
        ScadaStore._ensure_indexes(connection, TABLE, payload)
        connection.execute(
            "UPDATE _scada_schema SET fingerprint=? WHERE table_name=?",
            (_fingerprint(payload), TABLE),
        )
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            connection.rollback()
            raise RuntimeError("迁移后 integrity_check 失败")
        connection.commit()
        result["pair_index_present"] = any(
            columns == expected for columns in _index_columns(connection).values()
        )
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="创建备份并提交迁移")
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

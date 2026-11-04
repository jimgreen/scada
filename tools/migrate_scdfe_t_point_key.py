"""Migrate scdfe_t point mappings to a kind-aware composite key.

Older development databases keyed mappings by ``(channel_id, pnt_no)``.  That
prevented one transmission channel from exposing point 1 as YC, YX, YK and YT
at the same time.  The current JSON schema uses
``(channel_id, pnt_no, kind)``.  Run this explicit migration once against a
database that still has the two-column primary key; it does not run as an
implicit compatibility path in the application.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scdfe_c.web.service import DEFAULT_DB, SCHEMA_DIR, ScadaStore, quote_identifier

TABLE = "fes_scdfe_t_point_define"


def migrate(db_path: Path | str = DEFAULT_DB, schema_dir: Path | str = SCHEMA_DIR) -> dict:
    db = Path(db_path).resolve()
    schema = json.loads((Path(schema_dir) / f"{TABLE}.json").read_text(encoding="utf-8"))
    columns = schema["columns"]
    names = [column["eng_name"] for column in columns]
    keys = [column["eng_name"] for column in columns if column["primary_key"]]
    if keys != ["channel_id", "pnt_no", "kind"]:
        raise RuntimeError(f"unexpected current primary key: {keys}")

    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        info = connection.execute(f"PRAGMA table_info({quote_identifier(TABLE)})").fetchall()
        if not info:
            return {"changed": False, "reason": "table_missing"}
        current_pk = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        if current_pk == keys:
            return {"changed": False, "reason": "already_migrated"}
        if current_pk != ["channel_id", "pnt_no"]:
            raise RuntimeError(f"unsupported existing primary key: {current_pk}")
        existing = {row[1] for row in info}
        missing = set(names) - existing - {"_version"}
        if missing:
            raise RuntimeError(f"existing table is missing columns: {sorted(missing)}")
        temp = TABLE + "__kind_key_migration"
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(temp)}")
        parts = ["_version INTEGER NOT NULL DEFAULT 1"]
        for column in columns:
            parts.append(ScadaStore._column_definition(column))
        parts.append("PRIMARY KEY (" + ", ".join(quote_identifier(key) for key in keys) + ")")
        connection.execute(f"CREATE TABLE {quote_identifier(temp)} ({', '.join(parts)})")
        # Old rows from the first prototype did not always set source_kind;
        # derive the explicit kind from the source kind and fall back to YC for
        # the only historical rows that predated the type field.
        select_names = [name for name in names if name in existing]
        select_expr = [quote_identifier(name) for name in select_names]
        if "kind" in existing:
            select_expr[select_names.index("kind")] = 'COALESCE("kind", "source_kind", \'yc\')' if "source_kind" in existing else 'COALESCE("kind", \'yc\')'
        insert_names = [name for name in names if name in select_names]
        connection.execute(
            f"INSERT INTO {quote_identifier(temp)} ({', '.join(quote_identifier(name) for name in insert_names)}) "
            f"SELECT {', '.join(select_expr)} FROM {quote_identifier(TABLE)}"
        )
        connection.execute(f"DROP TABLE {quote_identifier(TABLE)}")
        connection.execute(f"ALTER TABLE {quote_identifier(temp)} RENAME TO {quote_identifier(TABLE)}")
        for index, fields in enumerate(schema.get("indexes", []), 1):
            name = f"{TABLE}_idx_{index}_{'_'.join(fields)}"
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS {quote_identifier(name)} ON {quote_identifier(TABLE)} "
                f"({', '.join(quote_identifier(field) for field in fields)})"
            )
        fingerprint = hashlib.sha256(json.dumps(schema, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        connection.execute("UPDATE _scada_schema SET fingerprint=? WHERE table_name=?", (fingerprint, TABLE))
        connection.commit()
        return {"changed": True, "rows": connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(TABLE)}").fetchone()[0]}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate scdfe_t mapping primary key")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, args.schema_dir), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

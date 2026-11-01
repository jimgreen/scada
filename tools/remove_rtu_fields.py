"""Migrate the RTU table after removing redundant definition fields.

The migration is intentionally explicit and one way.  It backs up the database,
copies endpoint values into protocol-specific parameter tables, drops the old
RTU columns, refreshes schema fingerprints, and validates every table before
committing.  Stop ``scdfe_c`` and the Web service before running it.
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


RTU_REMOVED = ("rdfid", "st_id", "oper_area", "rtu_104code", "chan_addr", "chan_port")
ENDPOINT_TABLES = ("fes_channel_iec104_define", "fes_channel_bacnet_define")


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")]


def _validate(connection: sqlite3.Connection, catalog: SchemaCatalog, table: str) -> None:
    expected = {"_version", *(column["eng_name"] for column in catalog.public(table)["columns"])}
    actual = set(_columns(connection, table))
    if expected != actual:
        raise RuntimeError(
            f"{table} 与当前 schema 不一致；缺少={sorted(expected - actual)}，多余={sorted(actual - expected)}"
        )


def _copy_endpoint_values(connection: sqlite3.Connection) -> dict[str, int]:
    """Move the old RTU endpoint into the matching protocol parameter row."""
    changed = {table: 0 for table in ENDPOINT_TABLES}
    rows = connection.execute(
        "SELECT rtu_id, channel_type, chan_addr, chan_port FROM fes_rtu_define"
    ).fetchall()
    for rtu_id, channel_type, address, port in rows:
        if channel_type == "iec104":
            table, default_port = "fes_channel_iec104_define", 2404
            values = (address or "127.0.0.1", port or default_port, rtu_id)
        elif channel_type == "bacnet":
            table, default_port = "fes_channel_bacnet_define", 47808
            values = (address or "127.0.0.1", port or default_port, rtu_id)
        else:
            continue
        # Fill blank protocol endpoints from the legacy RTU row. Existing
        # dedicated values remain authoritative.
        cursor = connection.execute(
            f"UPDATE {quote_identifier(table)} SET remote_host=?, remote_port=?, _version=_version+1 "
            "WHERE rtu_id=? AND (remote_host IS NULL OR remote_host='' OR remote_port IS NULL)",
            values,
        )
        changed[table] += cursor.rowcount
    # Modbus, RTU, CAN and OPC already have dedicated endpoint fields.  Fill
    # only empty values from the legacy RTU row before it is dropped.
    for rtu_id, channel_type, address, port in rows:
        if channel_type == "modbus-tcp":
            connection.execute(
                "UPDATE fes_channel_modbus_tcp_define SET remote_host=COALESCE(NULLIF(remote_host,''), ?), "
                "remote_port=COALESCE(remote_port, ?), _version=_version+1 WHERE rtu_id=?",
                (address or "127.0.0.1", port or 502, rtu_id),
            )
        elif channel_type == "modbus-rtu":
            connection.execute(
                "UPDATE fes_channel_modbus_rtu_define SET serial_port=COALESCE(NULLIF(serial_port,''), ?), "
                "_version=_version+1 WHERE rtu_id=?",
                (address or "", rtu_id),
            )
        elif channel_type == "can":
            connection.execute(
                "UPDATE fes_channel_can_define SET channel=COALESCE(NULLIF(channel,''), ?), "
                "_version=_version+1 WHERE rtu_id=?",
                (address or "", rtu_id),
            )
        elif channel_type == "opc":
            connection.execute(
                "UPDATE fes_channel_opc_define SET opc_name=COALESCE(NULLIF(opc_name,''), ?), "
                "_version=_version+1 WHERE rtu_id=?",
                (address or "", rtu_id),
            )
    return changed


def migrate(db_path: Path | str = DEFAULT_DB, *, schema_dir: Path | str = SCHEMA_DIR) -> dict:
    db = Path(db_path).resolve(strict=True)
    catalog = SchemaCatalog(schema_dir)
    lock = ServerInstanceLock()
    if not lock.acquire():
        raise RuntimeError("请先停止 scdfe_c 再迁移数据库")
    report = {"database": str(db), "backup": None, "removed": list(RTU_REMOVED), "endpoint_rows": {}}
    try:
        with closing(sqlite3.connect(db, isolation_level=None, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("迁移前数据库完整性检查失败")
                actual_rtu = set(_columns(connection, "fes_rtu_define"))
                if not set(RTU_REMOVED).issubset(actual_rtu):
                    raise RuntimeError(f"RTU 表缺少待删除字段: {sorted(set(RTU_REMOVED) - actual_rtu)}")
                backup = db.parent / "backups" / (
                    db.name + ".before_remove_rtu_fields." + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".bak"
                )
                backup.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份完整性检查失败")
                report["backup"] = str(backup)

                # New protocol endpoint columns are part of the current schema.
                for table, columns in {
                    "fes_channel_iec104_define": (("remote_host", "TEXT"), ("remote_port", "INTEGER")),
                    "fes_channel_bacnet_define": (("remote_host", "TEXT"), ("remote_port", "INTEGER")),
                }.items():
                    existing = set(_columns(connection, table))
                    for name, sql_type in columns:
                        if name not in existing:
                            connection.execute(
                                f"ALTER TABLE {quote_identifier(table)} ADD COLUMN {quote_identifier(name)} {sql_type}"
                            )

                report["endpoint_rows"] = _copy_endpoint_values(connection)
                for name in RTU_REMOVED:
                    connection.execute(f"ALTER TABLE fes_rtu_define DROP COLUMN {quote_identifier(name)}")

                for table in ("fes_rtu_define", *ENDPOINT_TABLES):
                    _validate(connection, catalog, table)
                    connection.execute(
                        "UPDATE _scada_schema SET fingerprint=? WHERE table_name=?",
                        (_fingerprint(catalog.get(table)), table),
                    )
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("迁移后数据库完整性检查失败")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("迁移后外键检查失败")
                # Validate all existing tables and fingerprints before commit.
                # A previous checkout may not yet have the optional process
                # monitor table; ScadaStore creates that current-schema table
                # immediately after this migration commits.
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

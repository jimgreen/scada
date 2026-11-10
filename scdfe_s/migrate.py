"""Create an independent S database from a consistent read-only definitions snapshot."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from scdfe_c.web.service import DEFAULT_DB as SYSTEM_DB, KIND_TABLES, quote_identifier
from scdfe_s.database import DEFAULT_DB, SimulatorStore


def copy_definitions(source, destination):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f"目标库已存在，不会覆盖：{destination}")
    if destination == SYSTEM_DB.resolve():
        raise ValueError("目标不能是 C/T 业务库")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Publish only a closed and complete SQLite file. link() fails atomically
    # if another process created the target during the copy.
    with TemporaryDirectory(dir=destination.parent) as tmp:
        staged = Path(tmp) / "scdfe_s.sqlite3"
        store = SimulatorStore(staged)
        counts = {}
        try:
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src, store.connection() as dst:
                src.execute("BEGIN")
                dst.execute("BEGIN IMMEDIATE")
                tables = ["fes_rtu_define", *KIND_TABLES.values(),
                          *(n for n in store.catalog.names() if n.startswith("fes_channel_"))]
                if not {"rtu_id", "channel_id", "channel_type"} <= {
                        r[1] for r in src.execute("PRAGMA table_info(fes_rtu_define)")}:
                    raise ValueError("源库缺少当前 RTU 通道定义")
                unbound_sql = "SELECT rtu_id FROM fes_rtu_define WHERE channel_id IS NULL AND channel_type IS NULL"
                unbound = [r[0] for r in src.execute(unbound_sql + " ORDER BY rtu_id")]
                for name in tables:
                    # Keep definitions and the last displayed value, never live PIDs,
                    # pending controls, telemetry timestamps, raw values or quality.
                    cols = [c["eng_name"] for c in store.catalog.get(name)["columns"]
                            if c["eng_name"] == "process_enable" or not c["eng_name"].startswith(
                                ("process_", "cmd_", "refresh_time", "raw_", "lock_begintime", "lock_holdtime",
                                 "man_disable_last_tm")) and c["eng_name"] != "status"]
                    columns = ",".join(map(quote_identifier, cols))
                    table = quote_identifier(name)
                    source_cols = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
                    if not set(cols) <= source_cols:
                        raise ValueError(f"源库缺少当前定义字段：{name}")
                    where = ""
                    if name == "fes_rtu_define":
                        # An RTU placeholder with neither identity nor protocol is not a channel.
                        where = " WHERE channel_id IS NOT NULL OR channel_type IS NOT NULL"
                    elif unbound and "rtu_id" in source_cols and src.execute(
                            f"SELECT 1 FROM {table} WHERE rtu_id IN ({unbound_sql}) LIMIT 1"
                    ).fetchone():
                        raise ValueError(f"无通道的 RTU 有关联数据（{name}），迁移已取消，请先在源副本补全通道定义")
                    read = src.execute(f"SELECT {columns} FROM {table}{where}")
                    counts[name] = dst.executemany(
                        f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in cols)})", read).rowcount
                dst.commit()
        finally:
            store.close()
        os.link(staged, destination)
    return {"tables": counts, "skipped_unbound_rtus": unbound}


def main():
    parser = argparse.ArgumentParser(description="从源库只读复制通道、四遥点和规约映射，以初始化 S 专库")
    parser.add_argument("--source", type=Path, default=SYSTEM_DB)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    try:
        result = copy_definitions(args.source, args.destination)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({"source": str(args.source.resolve()), "destination": str(args.destination.resolve()),
                      **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Rebuild an scdfe_d database from a model into a new staged SQLite file."""

import argparse
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

from core.web_database import DEFAULT_WORKSPACE_ROOT
from core.common import ApiError
from scdfe_d.service import DeviceStore, validate_database


def _insert_rows(connection, table, rows):
    if not rows:
        return
    # Preserve source fields supported by this version; let new/derived fields
    # take their defaults and be populated by DeviceStore._tables.
    columns = [
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table}")')
        if row[1] in rows[0]
    ]
    fields = ", ".join(f'"{name}"' for name in columns)
    parameters = ", ".join("?" for _ in columns)
    connection.executemany(
        f'INSERT INTO "{table}" ({fields}) VALUES ({parameters})',
        (tuple(row[field] for field in columns) for row in rows),
    )


def _transfer_state(source, source_path, store, model):
    with store.connection() as target:
        target.execute("BEGIN IMMEDIATE")
        model_devices = {
            (row["model_table"], row["model_idx"]): row["device_id"]
            for row in target.execute(
                "SELECT device_id,model_table,model_idx FROM device"
            )
        }
        old_devices = {
            row["device_id"]: dict(row)
            for row in source.execute("SELECT * FROM device")
        }
        id_map = {}
        for old_id, row in old_devices.items():
            new_id = model_devices.get((row["model_table"], row["model_idx"]))
            if new_id is None:
                continue
            id_map[old_id] = new_id
            target.execute(
                "UPDATE device SET active=?,scada_dev_id=? WHERE device_id=?",
                (row["active"], row.get("scada_dev_id"), new_id),
            )

        measurements = []
        for row in source.execute(
            "SELECT * FROM device_measurement ORDER BY measurement_id"
        ):
            measurement = dict(row)
            old_id = measurement["device_id"]
            if old_id not in id_map:
                device = old_devices[old_id]
                raise ValueError(
                    f"新版模型缺少 {device['model_table']}[{device['model_idx']}]，"
                    f"无法迁移其量测 {measurement['metric']}"
                )
            measurement["device_id"] = id_map[old_id]
            measurement.pop("quantity_table", None)
            measurements.append(measurement)
        _insert_rows(target, "device_measurement", measurements)

        settings = {}
        if source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='d_settings'"
        ).fetchone():
            settings = dict(source.execute("SELECT key,value FROM d_settings"))
        interval = float(settings.get("interval_seconds", "2"))
        if not math.isfinite(interval) or not 0.5 <= interval <= 3600:
            raise ValueError("旧库的采集周期不在允许范围内")
        target.execute(
            "UPDATE d_settings SET value=? WHERE key='interval_seconds'",
            (str(interval),),
        )

        mappings = [
            dict(row) for row in source.execute("SELECT * FROM scada_point_mapping")
        ]
        verified_paths = {}
        for row in mappings:
            database = row["source_database"]
            if database not in verified_paths:
                if "root_paths" not in settings:
                    # Very old D databases saved paths relative to their file.
                    database_path = (source_path.parent / database).resolve()
                    try:
                        database = database_path.relative_to(store.root).as_posix()
                    except ValueError as exc:
                        raise ValueError("旧库所引用的采集库超出工作区根目录") from exc
                store.source_path(database)
                verified_paths[row["source_database"]] = database
            row["source_database"] = verified_paths[row["source_database"]]
        _insert_rows(target, "scada_point_mapping", mappings)

        for table in ("measurement_refresh", "measurement_value", "input_change_log"):
            if table == "input_change_log":
                # The import is recorded by the rebuild entry below.
                target.execute("DELETE FROM input_change_log")
            _insert_rows(
                target,
                table,
                [dict(row) for row in source.execute(f'SELECT * FROM "{table}"')],
            )
        store._tables(target, sync_schema=True)
        result = dict(
            model=model,
            device_tables=target.execute(
                "SELECT count(*) FROM d_device_tables"
            ).fetchone()[0],
            devices=len(model_devices),
            measurements=len(measurements),
            mappings=len(mappings),
            dropped_unmapped_devices=len(old_devices) - len(id_map),
        )
        store._audit(target, "schema_rebuild", None, {}, result)
        if target.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("重建设备库存在无效的外键关联")
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("重建设备库完整性检查失败")
    return result


def rebuild(source, model, output, *, workspace_root=DEFAULT_WORKSPACE_ROOT):
    root = Path(workspace_root).resolve(strict=True)
    source = (root / source).resolve(strict=True)
    requested_output = root / output
    output = requested_output.resolve()
    if not output.is_relative_to(root) or not output.parent.is_dir():
        raise ValueError("输出文件必须位于工作区内的已有目录")
    if requested_output.is_symlink() or any(
        Path(str(output) + suffix).exists()
        for suffix in ("", "-wal", "-shm", "-journal")
    ):
        raise FileExistsError(f"不覆盖已有数据库或 SQLite 边车文件：{output}")
    validation = validate_database(source)
    if not validation["valid"]:
        raise ValueError(validation["message"])

    # Build under the destination directory using its final basename for the
    # embedded RTDB database name. An exclusive hard link publishes only the
    # fully checked database and cannot replace a file created concurrently.
    with tempfile.TemporaryDirectory(
        prefix=".scdfe-d-rebuild-", dir=output.parent
    ) as temporary:
        staged_path = Path(temporary) / output.name
        store = DeviceStore(staged_path, workspace_root=root)
        store.import_model(model)
        with closing(
            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)
        ) as old:
            old.row_factory = sqlite3.Row
            old.execute("BEGIN")
            result = _transfer_state(old, source, store, model)
            use_wal = old.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        if use_wal:
            with store.connection() as connection:
                if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise ValueError("无法启用原设备库使用的 WAL 日志模式")
        store.close()
        validation = validate_database(staged_path)
        if not validation["valid"]:
            raise ValueError(validation["message"])
        os.link(staged_path, output)
    return dict(result, output=str(output))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=DEFAULT_WORKSPACE_ROOT)
    parser.add_argument("--source", required=True, type=Path, help="旧设备库，只读")
    parser.add_argument("--model", required=True, help="设备模型库的工作区相对路径")
    parser.add_argument(
        "--output", required=True, type=Path, help="待验收的新文件，必须不存在"
    )
    args = parser.parse_args(argv)
    try:
        result = rebuild(
            args.source, args.model, args.output, workspace_root=args.workspace_root
        )
    except (ApiError, OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"重建设备库失败：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

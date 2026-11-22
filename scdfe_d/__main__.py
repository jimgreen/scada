"""Run a device collector without the Web process."""

import argparse
import json
from pathlib import Path
import time

from core.workspace_paths import (
    add_workspace_arguments,
    default_database_path,
    resolve_workspace_root,
)
from scdfe_d.service import Collector, DeviceStore, drop_legacy_tables


def main(argv=None):
    parser = argparse.ArgumentParser(description="scdfe_d 设备量测采集后台")
    parser.add_argument("--db", type=Path, default=None, help="数据库路径（相对于工作区根目录）；省略时按三参数定位")
    add_workspace_arguments(parser)
    parser.add_argument(
        "--source", type=str, default=None, help="源 SCADA 数据库相对路径，省略时默认同目录 scdfe_c.sqlite3"
    )
    parser.add_argument(
        "--interval", type=int, help="采集周期 1–60 秒，省略时沿用默认配置"
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--drop-legacy", action="store_true", help="清理旧版冗余量测表及绑定触发器后退出"
    )
    args = parser.parse_args(argv)
    workspace_root = resolve_workspace_root(args.workspace_root)
    if args.db is None:
        db_path = default_database_path("d", workspace_root, args.runtime_name, args.scene_name)
    else:
        db_path = args.db if args.db.is_absolute() else workspace_root / args.db
    if args.drop_legacy:
        print(json.dumps(drop_legacy_tables(db_path), ensure_ascii=False))
        return
    store = DeviceStore(
        db_path, workspace_root=workspace_root, source_database=args.source
    )
    collector = Collector(store)
    try:
        if args.once:
            print(json.dumps(collector.once(), ensure_ascii=False))
        else:
            collector.start(args.interval)
            print("scdfe_d 设备采集运行中", flush=True)
            while collector.running:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        collector.close()
        store.close()


if __name__ == "__main__":
    main()

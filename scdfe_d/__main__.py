"""Run a device collector without the Web process."""

import argparse
import json
from pathlib import Path
import time

from core.web_database import DEFAULT_WORKSPACE_ROOT
from scdfe_d.service import Collector, DeviceStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="scdfe_d 设备量测采集后台")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, default=DEFAULT_WORKSPACE_ROOT)
    parser.add_argument(
        "--interval", type=int, help="采集周期 1–60 秒，省略时沿用数据库配置"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    store = DeviceStore(
        args.workspace_root / args.db, workspace_root=args.workspace_root
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

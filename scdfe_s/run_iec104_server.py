"""命令行启动 IEC104 测试从站。"""
from __future__ import annotations

import argparse
from pathlib import Path
import signal
import sys
import threading

# Support both the documented module form and direct execution from the
# project checkout (``python scdfe_s/run_iec104_server.py``).  The latter is
# useful for Windows launchers and shell scripts that do not set PYTHONPATH.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.iec104_server import IEC104Server
from scdfe_c.web.service import DEFAULT_DB, SCHEMA_DIR, ScadaStore


def main() -> None:
    parser = argparse.ArgumentParser(description="IEC 60870-5-104 SCADA test server")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    parser.add_argument("--rtu-id", type=int, required=True)
    parser.add_argument("--bind-ip", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2404)
    parser.add_argument("--tick-rate-ms", type=int, default=100)
    parser.add_argument("--max-connections", type=int, default=0)
    args = parser.parse_args()
    store = ScadaStore(args.db, args.schema_dir)
    server = IEC104Server.from_store(store, args.rtu_id, ip=args.bind_ip, port=args.port,
                                     tick_rate_ms=args.tick_rate_ms,
                                     max_connections=args.max_connections)
    stopped = threading.Event()

    def stop(*_):
        server.stop()
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    server.start()
    print(f"IEC104 server listening on {args.bind_ip}:{args.port} RTU={args.rtu_id} "
          f"CA={server.common_address} points={len(server.snapshot())}", flush=True)
    try:
        stopped.wait()
    finally:
        server.stop()


if __name__ == "__main__":
    main()

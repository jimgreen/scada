"""Run one Modbus TCP master or server test channel."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scdfe_s._runner import (
    add_common_arguments,
    add_tcp_arguments,
    run_service,
    tcp_server_service,
    tcp_service,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Modbus TCP SCADA 主站/服务端测试通道")
    add_common_arguments(parser, modes=("master", "server"))
    add_tcp_arguments(parser)
    args = parser.parse_args()
    factory = tcp_server_service if args.mode == "server" else tcp_service
    return run_service(
        factory,
        args,
        label="Modbus TCP",
        override_names=(
            "bind_ip", "listen_port", "remote_host", "remote_port", "unit_id",
            "connect_timeout", "response_timeout", "retry_count", "keep_alive",
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())

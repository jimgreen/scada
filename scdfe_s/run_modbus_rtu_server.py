"""Run one Modbus RTU master or server test channel."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scdfe_s._runner import (
    add_common_arguments,
    add_rtu_arguments,
    run_service,
    rtu_server_service,
    rtu_service,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Modbus RTU SCADA 主站/服务端测试通道")
    add_common_arguments(parser, modes=("master", "server"))
    add_rtu_arguments(parser)
    args = parser.parse_args()
    factory = rtu_server_service if args.mode == "server" else rtu_service
    return run_service(
        factory,
        args,
        label="Modbus RTU",
        override_names=(
            "serial_port", "baudrate", "bytesize", "parity", "stopbits", "unit_id",
            "response_timeout", "retry_count",
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())

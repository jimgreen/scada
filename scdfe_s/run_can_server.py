"""Run one CAN master or test-server channel."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scdfe_s._runner import add_can_arguments, add_common_arguments, choose_can_service, run_service


def main() -> int:
    parser = argparse.ArgumentParser(description="CAN/CAN-FD SCADA 主站/服务端测试通道")
    add_common_arguments(parser, modes=("master", "server"))
    add_can_arguments(parser)
    parser.add_argument("--request-id", type=int, default=None, help="服务端请求帧 ID")
    parser.add_argument("--response-id", type=int, default=None, help="服务端应答帧 ID")
    parser.add_argument("--response-data", default=None, help="服务端应答数据（十六进制）")
    args = parser.parse_args()

    def factory(store):
        return choose_can_service(store, args.mode)

    return run_service(
        factory,
        args,
        label="CAN",
        override_names=(
            "interface", "channel", "bitrate", "data_bitrate", "fd",
            "receive_own_messages", "receive_timeout", "retry_count", "is_extended_id",
            "request_id", "response_id", "response_data",
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())

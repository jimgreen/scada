"""SCADA 公共异常、SQL 标识符转义与 FES 只读表名常量。"""
from __future__ import annotations

import re
from typing import Any

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

KIND_TABLES: dict[str, str] = {
    "yc": "fes_yc_define",
    "yx": "fes_yx_define",
    "yt": "fes_yt_define",
    "yk": "fes_yk_define",
}

CHANNEL_PARAMETER_TABLES: dict[str, str] = {
    "iec104": "fes_channel_iec104_define",
    "modbus-rtu": "fes_channel_modbus_rtu_define",
    "modbus-tcp": "fes_channel_modbus_tcp_define",
    "can": "fes_channel_can_define",
    "bacnet": "fes_channel_bacnet_define",
    "opc": "fes_channel_opc_define",
}


class ApiError(Exception):
    """Web API 结构化业务异常。"""

    def __init__(self, status: int, message: str, *, details: Any = None, code: str = "bad_request"):
        super().__init__(message)
        self.status, self.message, self.details, self.code = status, message, details, code


def quote_identifier(value: str) -> str:
    """校验并转义 SQLite 标识符。"""
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid SQL identifier: {value}")
    return '"' + value.replace('"', '""') + '"'

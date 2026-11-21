"""scdfe_c 运行时基础数据模型与字段提取辅助。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _field(row: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
    """兼容 dict、sqlite3.Row 与 dataclass/对象属性的字段读取。"""
    if isinstance(row, Mapping):
        value = row.get(name, default)
    elif hasattr(row, "keys") and name in row.keys():
        value = row[name]
    else:
        value = getattr(row, name, default)
    return default if value is None else value


def _jsonable(value: Any) -> Any:
    """将数据库标量值转换为 JSON 编码器可序列化的基础形态。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return repr(value)


@dataclass(frozen=True, order=True)
class ChannelKey:
    """唯一通道标识。RTU 表已合并了通道信息，但仍保留 channel_id。"""

    rtu_id: int
    channel_id: int


@dataclass(frozen=True)
class ChannelSpec:
    """从 RTU 行提取出的、可安全传给通信线程的通道配置。"""

    rtu_id: int
    channel_id: int
    channel_type: str
    name: str | None = None
    process_enable: bool = True

    @property
    def key(self) -> ChannelKey:
        return ChannelKey(self.rtu_id, self.channel_id)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ChannelSpec":
        rtu_id = int(_field(row, "rtu_id"))
        channel_id = int(_field(row, "channel_id"))
        channel_type = _field(row, "channel_type", "")
        if not isinstance(channel_type, str):
            raise ValueError(f"RTU {rtu_id} 的 channel_type 必须是字符串")
        if not channel_type:
            raise ValueError(f"RTU {rtu_id} 缺少 channel_type")
        process_enable = int(_field(row, "process_enable", 1))
        if process_enable not in (0, 1):
            raise ValueError(f"RTU {rtu_id} 的 process_enable 必须是 0 或 1")
        return cls(
            rtu_id=rtu_id,
            channel_id=channel_id,
            channel_type=channel_type,
            name=_field(row, "name"),
            process_enable=bool(process_enable),
        )


@dataclass
class WorkerStats:
    """单通道通信线程运行统计计数器。"""

    cycles: int = 0
    updated_yc: int = 0
    updated_yx: int = 0
    sent_yk: int = 0
    sent_yt: int = 0
    errors: int = 0
    alarms: int = 0


__all__ = ["ChannelKey", "ChannelSpec", "WorkerStats", "_field", "_jsonable"]

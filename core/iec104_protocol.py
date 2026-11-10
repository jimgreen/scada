"""Shared IEC 60870-5-104 transport validation and APDU inspection."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


class IEC104ProtocolError(ValueError):
    """An APDU or protocol parameter is invalid."""


@dataclass(frozen=True)
class IEC104ProtocolConfig:
    """IEC 104 transport parameters exposed by the project adapters."""

    k: int = 12
    w: int = 8
    t1: float = 15.0
    t2: float = 10.0
    t3: float = 20.0
    max_asdu_length: int = 249

    def __post_init__(self) -> None:
        _validate_parameters(self.k, self.w, self.t1, self.t2, self.t3,
                             self.max_asdu_length)

    def apply(self, parameters: Any) -> None:
        """Apply values to a c104 ProtocolParameters object."""
        parameters.send_window_size = self.k
        parameters.receive_window_size = self.w
        # c104 exposes timeout properties as integer seconds even though the
        # persisted configuration accepts finite numeric values.
        parameters.message_timeout = max(1, int(round(self.t1)))
        parameters.confirm_interval = max(1, int(round(self.t2)))
        parameters.keep_alive_interval = max(1, int(round(self.t3)))

    @classmethod
    def from_mapping(cls, values: Any, *, defaults: "IEC104ProtocolConfig | None" = None) -> "IEC104ProtocolConfig":
        """Build a validated config from a DB/config-file mapping."""
        base = defaults.as_dict() if defaults is not None else cls().as_dict()
        if isinstance(values, dict):
            for name in base:
                if values.get(name) is not None:
                    base[name] = values[name]
        return cls(
            k=int(base["k"]),
            w=int(base["w"]),
            t1=float(base["t1"]),
            t2=float(base["t2"]),
            t3=float(base["t3"]),
            max_asdu_length=int(base["max_asdu_length"]),
        )

    def as_dict(self) -> dict[str, int | float]:
        return {
            "k": self.k,
            "w": self.w,
            "t1": self.t1,
            "t2": self.t2,
            "t3": self.t3,
            "max_asdu_length": self.max_asdu_length,
        }


@dataclass(frozen=True)
class IEC104APDU:
    """Decoded APDU control fields and payload size."""

    frame_type: str
    send_sequence: int | None
    receive_sequence: int | None
    u_function: str | None
    apdu_length: int
    asdu_length: int


_U_FUNCTIONS = {
    0x07: "STARTDT_ACT",
    0x0B: "STARTDT_CON",
    0x13: "STOPDT_ACT",
    0x23: "STOPDT_CON",
    0x43: "TESTFR_ACT",
    0x83: "TESTFR_CON",
}


def _validate_parameters(k, w, t1, t2, t3, max_asdu_length) -> None:
    for name, value in (("k", k), ("w", w), ("max_asdu_length", max_asdu_length)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise IEC104ProtocolError(f"{name} 必须是整数")
    if not 1 <= k <= 32767:
        raise IEC104ProtocolError("k 必须在 1..32767")
    if not 1 <= w <= 32767 or w > k:
        raise IEC104ProtocolError("w 必须在 1..k")
    if not 1 <= max_asdu_length <= 249:
        raise IEC104ProtocolError("max_asdu_length 必须在 1..249")
    for name, value in (("t1", t1), ("t2", t2), ("t3", t3)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise IEC104ProtocolError(f"{name} 必须是正数")
        if not math.isfinite(value) or value <= 0:
            raise IEC104ProtocolError(f"{name} 必须是正数")


def parse_apdu(data: bytes | bytearray | memoryview, *, max_asdu_length: int = 249) -> IEC104APDU:
    """Decode one complete IEC 104 APDU and validate its declared length."""
    payload = bytes(data)
    if len(payload) < 6 or payload[0] != 0x68:
        raise IEC104ProtocolError("APDU 必须以 0x68 开始且至少包含 6 字节")
    apdu_length = payload[1]
    if apdu_length < 4:
        raise IEC104ProtocolError("APDU 长度不能小于 4")
    expected_length = apdu_length + 2
    if len(payload) != expected_length:
        raise IEC104ProtocolError(
            f"APDU 长度字段为 {apdu_length}，实际帧长度为 {len(payload)}"
        )
    asdu_length = apdu_length - 4
    if asdu_length > max_asdu_length:
        raise IEC104ProtocolError(
            f"ASDU 长度 {asdu_length} 超过上限 {max_asdu_length}"
        )
    control = payload[2:6]
    if control[0] & 0x01 == 0:
        return IEC104APDU(
            "I", int.from_bytes(control[0:2], "little") >> 1,
            int.from_bytes(control[2:4], "little") >> 1, None,
            apdu_length, asdu_length,
        )
    if control[0] & 0x03 == 0x01:
        # S frames reserve bit 0 of the receive sequence and carry the
        # remaining 15 bits in control octets 3 and 4. The sequence itself
        # is allowed to be non-zero.
        if control[0] != 0x01 or control[1] or control[2] & 0x01:
            raise IEC104ProtocolError("S 帧控制域无效")
        return IEC104APDU(
            "S", None, int.from_bytes(control[2:4], "little") >> 1,
            None, apdu_length, asdu_length,
        )
    if control[1:] != b"\x00\x00\x00" or control[0] not in _U_FUNCTIONS:
        raise IEC104ProtocolError("U 帧控制域无效")
    return IEC104APDU(
        "U", None, None, _U_FUNCTIONS[control[0]], apdu_length, asdu_length,
    )


def describe_apdu(data: bytes | bytearray | memoryview, *, max_asdu_length: int = 249) -> str:
    """Return a compact diagnostic label for one APDU."""
    frame = parse_apdu(data, max_asdu_length=max_asdu_length)
    if frame.frame_type == "U":
        detail = frame.u_function
    elif frame.frame_type == "S":
        detail = f"rx={frame.receive_sequence}"
    else:
        detail = f"tx={frame.send_sequence} rx={frame.receive_sequence}"
    return f"IEC104 {frame.frame_type} {detail} asdu={frame.asdu_length}"


__all__ = [
    "IEC104APDU",
    "IEC104ProtocolConfig",
    "IEC104ProtocolError",
    "describe_apdu",
    "parse_apdu",
]

"""Modbus-TCP transmission server used by :mod:`scdfe_t`.

The regular :class:`~server.modbus_server.ModbusTcpServer` is intentionally a
small register simulator.  A transmission channel needs two extra properties:
each register is mapped to a local four-remote point and writes received from
the remote client must be acknowledged only after the application has accepted
the command.  This module keeps that policy in one adapter while retaining the
same pymodbus transport and lifecycle API.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import struct
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pymodbus.constants import ExcCodes
from pymodbus.simulator import DataType, SimData, SimDevice

from client.modbus_tcp import normalize_modbus_address
from .modbus_server import ModbusAsyncServer, ModbusTcpServer

__all__ = ["ModbusTcpTransServer"]


_AREA_TO_INDEX = {"coil": 0, "discrete": 1, "holding": 2, "input": 3}
_AREA_TO_FUNCTION = {"coil": {1, 5, 15}, "discrete": {2}, "holding": {3, 6, 16}, "input": {4}}
_KIND_DEFAULT_AREA = {"yx": "discrete", "yk": "coil", "yc": "input", "yt": "holding"}


def _text(value: Any, default: str = "") -> str:
    return default if value is None else str(value).strip().lower()


def _number(value: Any, default: float = 1.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ModbusTcpTransServer(ModbusTcpServer):
    """Expose selected SCADA points through a Modbus-TCP slave.

    ``points`` is an iterable of database mapping rows.  The row names match
    ``fes_scdfe_t_point_define``: ``kind``, ``pnt_no``, ``source_pnt_no``,
    ``register_area``, ``address``, ``data_type``, ``byte_order``,
    ``word_order``, ``scale``, ``offset`` and ``address_base``.  ``pnt_no`` is
    accepted as a fallback for hand-written configurations.  ``command_handler`` is called
    for YK/YT writes as ``handler(kind, pnt_no, engineering_value)``. The
    transmission point identifies its complete source RTU/point mapping;
    returning ``False`` (or raising) produces a Modbus server exception and the
    remote write is not committed.
    """

    ALLOW_MEASUREMENT_WRITES = False

    _TYPE_SIZES = {
        "float32": 2, "single": 2, "int32": 2, "uint32": 2,
        "float64": 4, "double": 4, "int64": 4, "uint64": 4,
    }

    def __init__(self, *, host="127.0.0.1", port=1502, unit_id=1,
                 points: Sequence[Mapping[str, Any]] = (),
                 point_values: Mapping[tuple[str, int], Any] | None = None,
                 command_handler: Callable[[str, int, Any], bool] | None = None,
                 log=lambda level, message: None, tcp=True, serial=None, **kwargs):
        self.unit_id = int(unit_id)
        if not 0 <= self.unit_id <= 247:
            raise ValueError("unit_id 必须在 0..247")
        self._lock = threading.RLock()
        self._command_handler = command_handler
        self._log = log
        self._mappings: dict[tuple[str, int], dict[str, Any]] = {}
        self._by_address: dict[tuple[str, int], dict[str, Any]] = {}
        self._values: dict[tuple[str, int], Any] = {}
        for row in points:
            self._add_mapping(row, point_values or {})
        sim_device = SimDevice(id=self.unit_id, simdata=self._simdata(), action=self._action)
        # ModbusAsyncServer expects a pymodbus context, and pymodbus 3.x accepts
        # SimDevice directly.  This avoids the deprecated ModbusDeviceContext
        # wrapper and lets writes reach the async action callback.
        ModbusAsyncServer.__init__(self, sim_device, tcp=tcp, host=host,
                                   port=int(port), serial=serial, log=log, **kwargs)

    @staticmethod
    def _point_no(row: Mapping[str, Any]) -> int:
        # pnt_no belongs to the transmission protocol.  source_pnt_no belongs
        # to the source RTU and can intentionally be different.
        value = row.get("pnt_no", row.get("source_pnt_no"))
        if value is None:
            raise ValueError("转发点缺少 pnt_no")
        value = int(value)
        if value < 1:
            raise ValueError("pnt_no 必须是正整数")
        return value

    def _add_mapping(self, source: Mapping[str, Any], point_values: Mapping[tuple[str, int], Any]):
        row = dict(source)
        kind = _text(row.get("kind"))
        if kind not in {"yc", "yx", "yk", "yt"}:
            raise ValueError("转发点 kind 必须是 yc、yx、yk 或 yt")
        point_no = self._point_no(row)
        source_point_no = int(row.get("source_pnt_no", point_no))
        if source_point_no < 1:
            raise ValueError("source_pnt_no 必须是正整数")
        area = _text(row.get("register_area"), _KIND_DEFAULT_AREA[kind])
        if area not in _AREA_TO_INDEX:
            raise ValueError(f"不支持 Modbus 区域: {area}")
        address_base = int(row.get("address_base", 0))
        address = normalize_modbus_address(
            int(row.get("address", point_no - 1)), address_base, name="address"
        )
        dtype = _text(row.get("data_type"), "bool" if kind in {"yx", "yk"} else "int16")
        size = self._TYPE_SIZES.get(dtype, 1) if area in {"holding", "input"} else 1
        if address + size > 65536:
            raise ValueError("Modbus 地址范围超过 65535")
        key = (kind, point_no)
        if key in self._mappings:
            raise ValueError(f"重复转发点 {kind}/{point_no}")
        row.update(kind=kind, pnt_no=point_no, source_pnt_no=source_point_no, register_area=area,
                   address=address, address_base=address_base, data_type=dtype, size=size,
                   scale=_number(row.get("scale"), 1.0), offset=_number(row.get("offset"), 0.0),
                   byte_order=_text(row.get("byte_order"), "big"),
                   word_order=_text(row.get("word_order"), "big"))
        self._validate_mapping_overlap(row)
        self._mappings[key] = row
        for offset in range(size):
            self._by_address[(area, address + offset)] = row
        self._values[key] = point_values.get(key, row.get("value", False if kind in {"yx", "yk"} else 0))

    def _validate_mapping_overlap(self, row):
        for offset in range(row['size']):
            if (row['register_area'], row['address'] + offset) in self._by_address:
                raise ValueError(f"Modbus 地址冲突 {row['register_area']}/{row['address'] + offset}")

    def _simdata(self):
        blocks: list[list[SimData]] = [[], [], [], []]
        self._sim_blocks = {}
        # A sparse SimData list needs at least one entry.  A non-mapped default
        # point at zero leaves all other addresses invalid until explicitly
        # configured, which is safer than exposing an accidental full register
        # range.
        for area, index in _AREA_TO_INDEX.items():
            rows = [row for row in self._mappings.values() if row["register_area"] == area]
            if not rows:
                rows = [{"address": 0, "size": 1,
                         "data_type": "bool" if area in {"coil", "discrete"} else "int16",
                         "value": False if area in {"coil", "discrete"} else 0,
                         "scale": 1.0, "offset": 0.0, "byte_order": "big",
                         "word_order": "big", "_dummy": True}]
            for row in sorted(rows, key=lambda item: item["address"]):
                dtype = row["data_type"]
                if area in {"coil", "discrete"}:
                    value = bool(self._values.get((row.get("kind"), row.get("pnt_no")), False))
                    blocks[index].append(SimData(address=row["address"], count=1,
                                                 values=value, datatype=DataType.BITS))
                else:
                    raw = self._encode(row, self._values.get((row.get("kind"), row.get("pnt_no")), row.get("value", 0)))
                    # A list already contains every register. count repeats
                    # the entire list and would overlap adjacent wide points.
                    blocks[index].append(SimData(address=row["address"], count=1,
                                                 values=raw, datatype=DataType.REGISTERS,
                                                 readonly=area == "input" or
                                                 (not self.ALLOW_MEASUREMENT_WRITES and row.get("kind") in {"yc", "yx"})))
                self._sim_blocks[(area,row['address'])] = blocks[index][-1]
        return tuple(blocks)

    def _encode(self, row: Mapping[str, Any], engineering: Any) -> list[int]:
        scale = row["scale"] or 1.0
        dtype = row["data_type"]
        if dtype in {"float32", "single", "float64", "double"}:
            raw_value = (float(engineering) - row["offset"]) / scale
        else:
            # Integer wire types must not pass through float64: uint64 values
            # above 2**53 otherwise lose bits, and UINT64_MAX rounds to 2**64.
            numeric = int(engineering) if isinstance(engineering, bool) else engineering
            raw_value = ((Decimal(str(numeric)) - Decimal(str(row["offset"]))) /
                         Decimal(str(scale)))
        if dtype in {"float32", "single"}:
            payload = struct.pack(">f", raw_value)
        elif dtype in {"float64", "double"}:
            payload = struct.pack(">d", raw_value)
        elif dtype in {"int32", "uint32", "int64", "uint64"}:
            bits = self._TYPE_SIZES[dtype] * 16
            payload = int(raw_value).to_bytes(bits // 8, "big", signed=dtype.startswith("int"))
        else:
            value = int(round(raw_value))
            if dtype.startswith("uint"):
                value = max(0, value)
            payload = value.to_bytes(2, "big", signed=dtype in {"int16"})
        if row["byte_order"] == "little":
            payload = bytes(reversed(payload))
        words = [int.from_bytes(payload[i:i + 2], "big") for i in range(0, len(payload), 2)]
        if row["word_order"] == "little" and len(words) > 1:
            words.reverse()
        return words

    def _decode(self, row: Mapping[str, Any], values: Sequence[int]) -> Any:
        words = list(values[:row["size"]])
        if row["word_order"] == "little" and len(words) > 1:
            words.reverse()
        payload = b"".join(int(word).to_bytes(2, "big") for word in words)
        if row["byte_order"] == "little":
            payload = bytes(reversed(payload))
        dtype = row["data_type"]
        if dtype in {"float32", "single"}: raw = struct.unpack(">f", payload)[0]
        elif dtype in {"float64", "double"}: raw = struct.unpack(">d", payload)[0]
        elif dtype in {"int32", "uint32", "int64", "uint64"}: raw = int.from_bytes(payload, "big", signed=dtype.startswith("int"))
        elif dtype in {"bool", "boolean"}: raw = bool(words[0])
        else: raw = int.from_bytes(payload[:2], "big", signed=dtype in {"int16", "int"})
        if isinstance(raw, int) and row["scale"].is_integer() and row["offset"].is_integer():
            return raw * int(row["scale"]) + int(row["offset"])
        return raw * row["scale"] + row["offset"]

    async def _action(self, function_code, start_address, address, count, current_registers, set_values):
        if set_values is None or function_code not in {5, 6, 15, 16}:
            return None
        area = next((name for name, funcs in _AREA_TO_FUNCTION.items() if function_code in funcs), None)
        if area is None:
            return None
        # pymodbus reports packed register count for coils, whereas values
        # contains the actual written bits. Validate the entire requested span
        # before dispatching commands so a mixed command/measurement write
        # cannot partially act on equipment before reaching the read-only bit.
        # Per-bit permissions belong here because SimData readonly flags are
        # packed per 16-bit word and would also disable adjacent command coils.
        write_count = len(set_values)
        candidates = []
        for offset in range(write_count):
            row = self._by_address.get((area, int(address) + offset))
            if row is None or (not self.ALLOW_MEASUREMENT_WRITES and row["kind"] not in {"yk", "yt"}):
                return ExcCodes.ILLEGAL_ADDRESS
            if row not in candidates:
                candidates.append(row)
        decoded = []
        for row in candidates:
            begin = int(row["address"]) - int(address)
            if begin < 0 or begin + row["size"] > write_count:
                return ExcCodes.ILLEGAL_VALUE
            values = set_values[begin:begin + row["size"]]
            if area in {"coil", "discrete"}:
                value = bool(values[0])
            else:
                value = self._decode(row, values)
            decoded.append((row, value))
        for row, value in decoded:
            if self._command_handler is not None:
                try:
                    accepted = self._command_handler(row["kind"], row["pnt_no"], value)
                except Exception as exc:  # callbacks must not kill pymodbus loop
                    self._log("ERROR", f"Modbus 转发命令回调失败: {exc}")
                    return ExcCodes.DEVICE_FAILURE
                if not accepted:
                    return ExcCodes.DEVICE_FAILURE
            with self._lock:
                self._values[(row["kind"], row["pnt_no"])] = value
        return None

    def _prepare_sample(self, kind, point_no, value):
        key = (str(kind).lower(), int(point_no))
        row = self._mappings.get(key)
        if row is None:
            raise KeyError(f"转发点不存在: {kind}/{point_no}")
        if row["kind"] not in {"yc", "yx"}:
            raise ValueError("Modbus 转发 update 只允许 yc/yx")
        area = row["register_area"]
        raw = [bool(value)] if area in {"coil", "discrete"} else self._encode(row, value)
        return key, row, area, raw

    def _write_local(self, runtime, row, area, raw):
        if hasattr(runtime, "devices"):
            device = runtime.devices[self.unit_id]
            block = device.block["c" if area == "coil" else "d" if area == "discrete" else "h" if area == "holding" else "i"]
            start, _count, registers, _flags = block
            index = (row["address"] // 16 - start) if area in {"coil", "discrete"} else row["address"] - start
            if area in {"coil", "discrete"}:
                bit = row["address"] % 16
                registers[index] = (registers[index] | (1 << bit)) if raw[0] else (registers[index] & ~(1 << bit))
            else:
                registers[index:index + len(raw)] = raw
        elif hasattr(runtime, "simdata"):
            blocks = runtime.simdata[_AREA_TO_INDEX[area]]
            target = self._sim_blocks.get((area,row['address'])) if runtime is self.context else None
            if target is None:
                for candidate in blocks:
                    begin = int(candidate.address)
                    width = len(candidate.values) if isinstance(candidate.values,list) else 1
                    count = int(candidate.count) * width
                    if begin <= row["address"] and row["address"] + len(raw) <= begin + count:
                        target = candidate
                        break
            if target is None:
                raise KeyError(f"Modbus 地址未映射: {area}/{row['address']}")
            index = row["address"] - int(target.address)
            values = target.values
            if area in {"coil", "discrete"}:
                if isinstance(values, list):
                    values[index] = bool(raw[0])
                elif index == 0:
                    target.values = bool(raw[0])
            elif isinstance(values, list):
                values[index:index + len(raw)] = raw
        else:
            return False
        return True

    def update_many(self, updates):
        """Publish a scan on the datastore's own thread; return only failed keys."""
        failed, ready = {}, []
        for kind, no, value in updates:
            try:
                ready.append((*self._prepare_sample(kind, no, value), value))
            except Exception as exc:
                failed[(kind, no)] = exc
        return self._publish_ready(ready, failed)

    def _publish_ready(self, ready, failed=None):
        """Publish already validated wire values in one datastore handoff."""
        failed = {} if failed is None else failed
        if not ready:
            return failed
        runtime = getattr(self.server, "context", None)
        if runtime is None or not any(hasattr(runtime, attr) for attr in ("devices", "simdata", "async_setValues")):
            runtime = self.context
        loop = self.loop
        async def publish():
            for key, row, area, raw, value in ready:
                try:
                    if not self._write_local(runtime, row, area, raw):
                        code = {"coil": 1, "discrete": 2, "holding": 3, "input": 4}[area]
                        error = await runtime.async_setValues(self.unit_id, code, row["address"], raw)
                        if error not in (None, False):
                            raise RuntimeError(f"Modbus 数据更新失败: {error}")
                    self._values[key] = value
                except Exception as exc:
                    failed[key] = exc
        if loop is not None and loop.is_running() and threading.current_thread() is not self.thread:
            # Do not hold _lock while the event loop may be accepting a remote command.
            future = asyncio.run_coroutine_threadsafe(publish(), loop)
            try:
                future.result(timeout=2)
            except TimeoutError:
                future.cancel()
                raise
        else:
            with self._lock:
                for key, row, area, raw, value in ready:
                    try:
                        if not self._write_local(runtime, row, area, raw):
                            raise RuntimeError("Modbus 更新需要活动的服务事件循环")
                        self._values[key] = value
                    except Exception as exc:
                        failed[key] = exc
        return failed

    def update(self, kind: str, point_no: int, value: Any):
        """Update one measurement and preserve the single-point error contract."""
        key = (str(kind).lower(), int(point_no))
        failed = self.update_many([(*key, value)])
        if key in failed:
            raise failed[key]
        return value

    # Keep the same spelling used by the standalone Modbus server service.
    # It makes a transmission channel usable by the common four-remote polling
    # loop without a protocol-specific branch.
    update_point = update

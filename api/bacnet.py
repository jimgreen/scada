"""BACnet/IP master adapter backed by :mod:`bacpypes3`.

The legacy SCADA implementation used a BACnet object-name CSV and a custom
BACnet stack.  This adapter keeps the useful application boundary while using
the maintained bacpypes3 stack.  BACnet object identities are explicit
``object_type``/``instance`` pairs; a name alone is not enough to address a
BACnet object.

The public API is synchronous like the Modbus and CAN adapters.  bacpypes3 is
asyncio based, so a private event-loop thread owns the application and all
BACnet I/O.  No process-wide event loop is changed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import threading
from typing import Any

__all__ = [
    "BacnetMaster",
    "BacnetError",
    "BacnetValue",
    "BACNET_AVAILABLE",
]

try:
    from bacpypes3.ipv4.app import NormalApplication
    from bacpypes3.local.device import DeviceObject
    from bacpypes3.pdu import IPv4Address
    from bacpypes3.basetypes import BinaryPV
    BACNET_AVAILABLE = True
    _BACNET_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - dependency check
    NormalApplication = DeviceObject = IPv4Address = BinaryPV = None
    BACNET_AVAILABLE = False
    _BACNET_IMPORT_ERROR = exc


def _field(row: Any, name: str, default=None):
    value = row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)
    return default if value is None else value


def _positive_int(value, name: str, maximum: int):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} 必须是 1..{maximum} 的整数")
    return value


def _instance(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4194303:
        raise ValueError("instance 必须是 0..4194303 的整数")
    return value


def _seconds(value, name: str, *, positive: bool = False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")) or (positive and value <= 0) or value < 0:
        raise ValueError(f"{name} 必须是有限的{'正' if positive else '非负'}数")
    return value


class BacnetError(RuntimeError):
    """BACnet connection, address, or service failure."""


@dataclass(frozen=True)
class BacnetValue:
    rtu_id: int | None
    point_no: int
    kind: str
    value: Any
    object_type: str
    instance: int
    received_at: datetime


class _LoopThread:
    """Own an asyncio loop in a daemon thread and expose safe sync calls."""

    def __init__(self, name: str):
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.thread.start()
        if not self._ready.wait(5):
            raise BacnetError("启动 BACnet asyncio 线程超时")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def call(self, function, *args, timeout=10.0, **kwargs):
        if self._closed:
            raise BacnetError("BACnet asyncio 线程已关闭")
        future = asyncio.run_coroutine_threadsafe(function(*args, **kwargs), self.loop)
        try:
            return future.result(_seconds(timeout, "timeout", positive=True))
        except TimeoutError as exc:
            future.cancel()
            raise BacnetError("BACnet 操作超时") from exc
        except Exception as exc:
            if isinstance(exc, BacnetError):
                raise
            raise BacnetError(f"BACnet 操作失败: {exc}") from exc

    def close(self):
        if not self._closed:
            self._closed = True
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)


def _require_bacnet():
    if not BACNET_AVAILABLE:
        raise BacnetError("缺少依赖 bacpypes3，请执行: python -m pip install -r requirements.txt") from _BACNET_IMPORT_ERROR


class _BacnetProtocol:
    """Synchronous BACnet/IP client.

    Parameters use BACnet/IP terminology: ``local_ip``/``local_port`` bind
    this client, while ``remote_host``/``remote_port`` identify the device.
    ``device_id`` is the remote BACnet device instance and is retained for
    configuration validation and point-table metadata; object reads address
    the remote IP directly as required by BACnet/IP.
    """

    OBJECT_TYPES = {
        "analog-input", "analog-output", "analog-value",
        "binary-input", "binary-output", "binary-value",
        "multi-state-input", "multi-state-output", "multi-state-value",
        "accumulator",
    }
    KIND_OBJECT_TYPES = {
        "yc": ("analog-input", "analog-output", "analog-value", "multi-state-input", "multi-state-output", "multi-state-value", "accumulator"),
        "yx": ("binary-input", "binary-output", "binary-value"),
        "yt": ("analog-output", "analog-value", "multi-state-output", "multi-state-value"),
        "yk": ("binary-output", "binary-value"),
    }
    CHANNEL_TYPE = "bacnet"
    PROTOCOL_NAME = "BACnet/IP"

    def __init__(self, remote_host: str, *, remote_port: int = 47808,
                 local_ip: str = "0.0.0.0", local_port: int = 47808,
                 device_id: int = 0, rtu_id: int | None = None,
                 local_device_id: int = 4194302, timeout: float = 5.0,
                 backend=None):
        _require_bacnet()
        if not isinstance(remote_host, str) or not remote_host:
            raise ValueError("remote_host 必须是非空字符串")
        _positive_int(remote_port, "remote_port", 65535)
        _positive_int(local_port, "local_port", 65535)
        _instance(device_id)
        _instance(local_device_id)
        self.remote_host, self.remote_port = remote_host, int(remote_port)
        self.local_ip, self.local_port = local_ip, int(local_port)
        self.device_id, self.rtu_id = int(device_id), rtu_id
        self.local_device_id, self.timeout = int(local_device_id), _seconds(timeout, "timeout", positive=True)
        self._backend = backend
        self._loop_thread: _LoopThread | None = None
        self._app = None
        self._connected = False
        self._lock = threading.RLock()
        self._points: list[dict[str, Any]] = []

    @classmethod
    def from_channel(cls, channel, definition=None, **kwargs):
        definition = definition or {}
        kwargs.setdefault("rtu_id", _field(channel, "rtu_id", _field(definition, "rtu_id")))
        kwargs.setdefault("local_ip", _field(definition, "bind_ip", "0.0.0.0"))
        kwargs.setdefault("local_port", _field(definition, "bind_port", 47808))
        kwargs.setdefault("device_id", _field(definition, "dev_id", 0))
        remote = kwargs.pop("remote_host", None) or _field(channel, "chan_addr") or "127.0.0.1"
        return cls(remote, remote_port=_field(channel, "chan_port", 47808), **kwargs)

    @classmethod
    def from_store(cls, store, rtu_id: int, *, point_map=None, **kwargs):
        with store.connection() as connection:
            connection.execute("BEGIN")
            rtu = connection.execute("SELECT * FROM fes_rtu_define WHERE rtu_id=?", (rtu_id,)).fetchone()
            if rtu is None:
                raise ValueError(f"RTU {rtu_id} 不存在")
            channel_type = rtu["channel_type"]
            if channel_type != cls.CHANNEL_TYPE:
                raise ValueError(f"RTU {rtu_id} 不是 {cls.PROTOCOL_NAME} 通道 {cls.CHANNEL_TYPE}")
            definition_table = "fes_channel_bacnet_define"
            definition_rows = [dict(r) for r in connection.execute(
                f"SELECT * FROM {definition_table} WHERE rtu_id=? ORDER BY id", (rtu_id,))]
            channel = dict(rtu)
            points = {}
            for table, kind in (("fes_yc_define", "yc"), ("fes_yx_define", "yx"),
                                ("fes_yk_define", "yk"), ("fes_yt_define", "yt")):
                points[kind] = [dict(r) for r in connection.execute(
                    f"SELECT * FROM {table} WHERE rtu_id=? ORDER BY pnt_no", (rtu_id,))]
        if len(definition_rows) != 1:
            raise ValueError(f"RTU {rtu_id} 需要唯一 BACnet 通道定义，实际找到 {len(definition_rows)} 条")
        rows = [dict(row, kind=kind) for kind, values in points.items() for row in values]
        if point_map:
            for row in rows:
                key = (row["kind"], row.get("pnt_no"))
                mapped = point_map.get(key) if isinstance(point_map, Mapping) else None
                if mapped is None:
                    continue
                if isinstance(mapped, Mapping):
                    row.update(mapped)
                elif isinstance(mapped, (tuple, list)) and len(mapped) == 2:
                    row.update(object_type=mapped[0], instance=mapped[1])
                else:
                    raise ValueError(f"BACnet point_map[{key!r}] 必须是 object_type/instance 映射")
        client = cls.from_channel(channel, definition_rows[0], **kwargs)
        if rows and any(_field(row, "object_type") is None or _field(row, "instance") is None for row in rows):
            raise ValueError("BACnet 点表缺少 object_type/instance；请在点表配置或 from_store(point_map=...) 中提供")
        if rows:
            client.configure(rows)
        return client

    @property
    def connected(self):
        return self._connected

    async def _create_app(self):
        device = DeviceObject(objectIdentifier=("device", self.local_device_id), objectName=f"scada-bacnet-{self.local_device_id}")
        address = IPv4Address(f"{self.local_ip}:{self.local_port}")
        return NormalApplication(device, address)

    async def _connect_backend(self):
        self._app = await self._create_app()

    async def _close_backend(self):
        if self._app is not None:
            self._app.close()
            self._app = None

    def connect(self, timeout=None):
        with self._lock:
            if self._connected:
                return self
            if self._backend is not None:
                result = getattr(self._backend, "connect", lambda: None)()
                if asyncio.iscoroutine(result):
                    asyncio.run(result)
                self._connected = True
                return self
            loop_thread = _LoopThread("scada-bacnet")
            self._loop_thread = loop_thread
            try:
                loop_thread.call(self._connect_backend, timeout=timeout or self.timeout)
            except Exception:
                loop_thread.close()
                self._loop_thread = None
                raise
            self._connected = True
            return self

    def close(self):
        with self._lock:
            if self._backend is not None:
                result = getattr(self._backend, "close", lambda: None)()
                if asyncio.iscoroutine(result):
                    asyncio.run(result)
            elif self._loop_thread is not None:
                try:
                    self._loop_thread.call(self._close_backend, timeout=self.timeout)
                finally:
                    self._loop_thread.close()
                    self._loop_thread = None
            self._connected = False

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def _object(object_type, instance):
        if not isinstance(object_type, str) or object_type not in _BacnetProtocol.OBJECT_TYPES:
            raise ValueError(f"不支持的 BACnet object_type: {object_type!r}")
        _instance(instance)
        return f"{object_type},{instance}"

    def _address(self):
        return f"{self.remote_host}:{self.remote_port}"

    async def _read_backend(self, object_id, property_identifier, array_index=None):
        return await self._app.read_property(self._address(), object_id, property_identifier, array_index=array_index)

    async def _write_backend(self, object_id, property_identifier, value, priority=None, array_index=None):
        object_type = object_id.split(",", 1)[0]
        if object_type.startswith("binary-") and property_identifier == "presentValue" and isinstance(value, (bool, int)):
            value = BinaryPV.active if bool(value) else BinaryPV.inactive
        return await self._app.write_property(self._address(), object_id, property_identifier, value,
                                              array_index=array_index, priority=priority)

    def read_property(self, object_type: str, instance: int, property_identifier: str = "presentValue", *, array_index=None):
        object_id = self._object(object_type, instance)
        if not self.connected:
            raise BacnetError("未连接，请先 connect() 或使用 with")
        if self._backend is not None:
            return self._backend.read_property(object_type, instance, property_identifier, array_index=array_index)
        return self._loop_thread.call(self._read_backend, object_id, property_identifier, array_index,
                                      timeout=self.timeout)

    def write_property(self, object_type: str, instance: int, value, property_identifier: str = "presentValue", *, priority=None, array_index=None):
        object_id = self._object(object_type, instance)
        if not self.connected:
            raise BacnetError("未连接，请先 connect() 或使用 with")
        if self._backend is not None:
            return self._backend.write_property(object_type, instance, value, property_identifier, priority=priority, array_index=array_index)
        return self._loop_thread.call(self._write_backend, object_id, property_identifier, value, priority,
                                      array_index, timeout=self.timeout)

    def read_point(self, point):
        kind = _field(point, "kind")
        object_type = _field(point, "object_type")
        instance = _field(point, "instance")
        if object_type is None or instance is None:
            raise ValueError("BACnet 点必须显式提供 object_type 和 instance")
        value = self.read_property(object_type, instance, _field(point, "property_identifier", "presentValue"))
        if object_type.startswith("binary-") and _field(point, "property_identifier", "presentValue") == "presentValue":
            value = getattr(value, "attr", value)
            if value in ("active", 1, True):
                value = True
            elif value in ("inactive", 0, False):
                value = False
        return BacnetValue(self.rtu_id, int(_field(point, "pnt_no")), kind,
                           value, object_type, int(instance), datetime.now(timezone.utc))

    def write_point(self, point, value):
        object_type = _field(point, "object_type")
        instance = _field(point, "instance")
        if object_type is None or instance is None:
            raise ValueError("BACnet 点必须显式提供 object_type 和 instance")
        return self.write_property(object_type, instance, value, _field(point, "property_identifier", "presentValue"), priority=_field(point, "priority"))

    def configure(self, points):
        checked = []
        seen = set()
        for row in points:
            row = dict(row) if isinstance(row, Mapping) else vars(row).copy()
            object_type, instance = _field(row, "object_type"), _field(row, "instance")
            if object_type is None or instance is None:
                raise ValueError("每个 BACnet 点必须配置 object_type 和 instance")
            self._object(object_type, instance)
            kind = _field(row, "kind", "yc")
            if kind not in self.KIND_OBJECT_TYPES or object_type not in self.KIND_OBJECT_TYPES[kind]:
                raise ValueError(f"BACnet 点类型 {kind!r} 与对象 {object_type!r} 不匹配")
            key = (kind, int(_field(row, "pnt_no")))
            if key in seen:
                raise ValueError(f"重复 BACnet 点 {key}")
            seen.add(key)
            checked.append(row)
        self._points = checked
        return self

    def _collect_once(self, points=None):
        rows = self._points if points is None else list(points)
        return [self.read_point(row) for row in rows if self.rtu_id is None or _field(row, "rtu_id") == self.rtu_id]

    def poll_loop(self, callback: Callable[[list[BacnetValue]], object], *, interval=None, stop_event=None, max_cycles=None):
        if not callable(callback):
            raise TypeError("callback 必须可调用")
        interval = _seconds(interval if interval is not None else 1.0, "interval")
        if interval == 0 and max_cycles is None:
            raise ValueError("interval 为 0 时必须指定 max_cycles")
        event = stop_event or threading.Event()
        cycles = 0
        while not event.is_set():
            callback(self.collect_once())
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            event.wait(interval)
        return cycles

    def send_yk_point(self, point, close):
        if _field(point, "kind", "yk") != "yk":
            raise ValueError("点不是遥控点")
        return self.write_point(point, bool(close))

    def send_yt_point(self, point, value):
        if _field(point, "kind", "yt") != "yt":
            raise ValueError("点不是遥调点")
        return self.write_point(point, value)


class BacnetMaster(_BacnetProtocol):
    """BACnet/IP SCADA master facade using the common master API names."""

    CHANNEL_TYPE = "bacnet"
    PROTOCOL_NAME = "BACnet/IP"

    def _point_definition(self, kind, point_no, points=None):
        rows = self._points if points is None else list(points)
        matches = [row for row in rows if _field(row, "kind") == kind
                   and int(_field(row, "pnt_no", -1)) == int(point_no)]
        if len(matches) != 1:
            raise ValueError(f"点号 {point_no} 没有唯一的 BACnet {kind} 点定义")
        return matches[0]

    def collect_once(self, points=None):
        rows = self._points if points is None else list(points)
        if not rows:
            raise ValueError("尚未配置 BACnet 点表")
        return super()._collect_once(rows)

    def send_yk_point(self, point_no, close, points=None):
        point = point_no if isinstance(point_no, Mapping) else self._point_definition("yk", point_no, points)
        return super().send_yk_point(point, close)

    def send_yt_point(self, point_no, value, points=None):
        point = point_no if isinstance(point_no, Mapping) else self._point_definition("yt", point_no, points)
        return super().send_yt_point(point, value)


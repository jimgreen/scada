"""BACnet/IP server and in-memory simulator."""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Iterable, Mapping

from .point_store import PointStore

try:
    from bacpypes3.object import DeviceObject
    from bacpypes3.pdu import IPv4Address
    from bacpypes3.ipv4.app import NormalApplication
    from bacpypes3.local.analog import AnalogValueObject, AnalogOutputObject
    from bacpypes3.local.binary import BinaryValueObject, BinaryOutputObject
except Exception as exc:  # pragma: no cover
    DeviceObject = IPv4Address = NormalApplication = None
    AnalogValueObject = AnalogOutputObject = BinaryValueObject = BinaryOutputObject = None
    _BACNET_IMPORT_ERROR = exc
else:
    _BACNET_IMPORT_ERROR = None


class BacnetServerError(RuntimeError):
    pass


class BacnetServer:
    """BACnet/IP server exposing four-remote points as local BACnet objects."""

    def __init__(self, *, bind_ip="127.0.0.1", bind_port=47808, device_id=1001,
                 device_name="SCADA BACnet Server", points: Iterable[Mapping[str, Any]] | None = None,
                 simulation=False, on_log: Callable[[str, str], Any] | None = None):
        self.bind_ip = str(bind_ip)
        self.bind_port = int(bind_port)
        self.device_id = int(device_id)
        self.device_name = str(device_name)
        self.simulation = bool(simulation)
        self.point_store = PointStore(points)
        if not points and self.simulation:
            self.point_store.ensure_defaults()
        self._on_log = on_log
        self._app = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error = None
        self._objects: dict[tuple[str, int], Any] = {}
        self.endpoint = f"{self.bind_ip}:{self.bind_port}"

    @property
    def connected(self):
        return self._ready.is_set() and self._error is None and not self._stop.is_set()

    @property
    def running(self):
        return self.connected

    @property
    def connections(self):
        return 0

    def _log(self, level, message):
        if self._on_log:
            self._on_log(level, message)

    @staticmethod
    def _object_class(kind):
        return {
            "yc": ("analog-value", AnalogValueObject),
            "yx": ("binary-value", BinaryValueObject),
            "yk": ("binary-output", BinaryOutputObject),
            "yt": ("analog-output", AnalogOutputObject),
        }[kind]

    @staticmethod
    def _bacnet_value(kind, value):
        if kind in {"yx", "yk"}:
            return "active" if bool(value) else "inactive"
        return float(value)

    @staticmethod
    def _plain_value(kind, value):
        if kind in {"yx", "yk"}:
            return str(value) in {"active", "on", "1", "true", "True"}
        return float(value)

    async def _start_async(self):
        device = DeviceObject(objectIdentifier=("device", self.device_id), objectName=self.device_name)
        self._app = NormalApplication(device, IPv4Address(self.endpoint))
        for point in self.point_store.snapshot():
            kind, pnt_no = point["kind"], int(point["pnt_no"])
            object_type, object_class = self._object_class(kind)
            obj = object_class(
                objectIdentifier=(object_type, pnt_no),
                objectName=f"{kind.upper()}_{pnt_no}",
                presentValue=self._bacnet_value(kind, point["value"]),
            )
            self._app.add_object(obj)
            self._objects[(kind, pnt_no)] = obj
        await asyncio.sleep(0.05)

    async def _wait_async(self):
        while not self._stop.is_set():
            await asyncio.sleep(0.05)

    async def _stop_async(self):
        if self._app is not None:
            self._app.close()
            self._app = None

    def _run(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._start_async())
            self._ready.set()
            self._log("INFO", f"BACnet/IP 服务端已监听 {self.endpoint}")
            self._loop.run_until_complete(self._wait_async())
            self._loop.run_until_complete(self._stop_async())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            self._log("ERROR", f"BACnet/IP 服务端启动失败: {exc}")
        finally:
            if self._loop is not None:
                self._loop.close()
                self._loop = None

    def start(self):
        if self.connected:
            return self
        if NormalApplication is None:
            raise BacnetServerError("缺少依赖 bacpypes3") from _BACNET_IMPORT_ERROR
        self._ready.clear()
        self._stop.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="bacnet-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise BacnetServerError("BACnet 服务端启动超时")
        if self._error:
            raise BacnetServerError(str(self._error)) from self._error
        return self

    def _call(self, coroutine, timeout=5):
        if not self.connected or self._loop is None:
            raise BacnetServerError("BACnet 服务端未启动")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout)

    async def _update_async(self, key, value):
        obj = self._objects.get(key)
        if obj is not None:
            obj.presentValue = self._bacnet_value(key[0], value)

    def update_point(self, kind, pnt_no, value, *, status=None):
        item = self.point_store.update(kind, pnt_no, value, status=status)
        if self.connected and (str(kind), int(pnt_no)) in self._objects:
            self._call(self._update_async((str(kind), int(pnt_no)), value))
        return item.public()

    def define_point(self, point):
        item = self.point_store.define(point)
        if self.connected:
            raise BacnetServerError("运行中的 BACnet 点表需要重启服务端后重新建模")
        return item

    async def _snapshot_async(self):
        for key, obj in self._objects.items():
            self.point_store.update(*key, self._plain_value(key[0], obj.presentValue))

    def snapshot(self):
        if self.connected:
            self._call(self._snapshot_async())
        return self.point_store.snapshot()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        self._ready.clear()
        self._log("INFO", "BACnet/IP 服务端已停止")

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


class BacnetServerSimulator(BacnetServer):
    """In-memory BACnet simulator; no UDP socket is opened."""

    def __init__(self, **kwargs):
        kwargs.setdefault("simulation", True)
        super().__init__(**kwargs)

    def start(self):
        self._stop.clear()
        self._error = None
        self._ready.set()
        self._log("INFO", "BACnet 模拟服务端已启动（内存模式）")
        return self

    def close(self):
        self._stop.set()
        self._ready.clear()
        self._log("INFO", "BACnet 模拟服务端已停止")

    def update_point(self, kind, pnt_no, value, *, status=None):
        return self.point_store.update(kind, pnt_no, value, status=status).public()

    def snapshot(self):
        return self.point_store.snapshot()

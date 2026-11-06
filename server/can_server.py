"""CAN bus server and virtual-bus simulator.

CAN has no standard server role.  This implementation provides the useful
test-server semantics: it owns a bus endpoint, receives frames, optionally
answers configured request IDs, and exposes a four-remote value model to the
Web layer.  ``interface='virtual'`` is suitable for local client tests.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import threading
from typing import Any, Callable, Iterable, Mapping

from .point_store import PointStore
from core.packet_trace import PacketTrace

try:
    import can
except Exception as exc:  # pragma: no cover - exercised when dependency is absent
    can = None
    _CAN_IMPORT_ERROR = exc
else:
    _CAN_IMPORT_ERROR = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class CanServerError(RuntimeError):
    pass


class CanServer(PacketTrace):
    """A threaded CAN endpoint with optional request/response simulation."""

    PACKET_PROTOCOL = "CAN"

    def __init__(self, *, interface="virtual", channel="scada-can-server",
                 bitrate=500000, data_bitrate=2000000, fd=False,
                 receive_own_messages=False, receive_timeout=0.1,
                 request_id=None, response_id=None, response_data="",
                 points: Iterable[Mapping[str, Any]] | None = None,
                 simulation=False, on_log: Callable[[str, str], Any] | None = None):
        self.interface = str(interface)
        self.channel = str(channel)
        self.bitrate = int(bitrate)
        self.data_bitrate = int(data_bitrate)
        self.fd = bool(fd)
        self.receive_own_messages = bool(receive_own_messages)
        self.receive_timeout = float(receive_timeout)
        self.request_id = None if request_id in (None, "") else int(request_id)
        self.response_id = None if response_id in (None, "") else int(response_id)
        self.response_data = str(response_data or "")
        try:
            bytes.fromhex(self.response_data)
        except ValueError as exc:
            raise CanServerError("response_data 必须是十六进制字节") from exc
        self.simulation = bool(simulation)
        self.point_store = PointStore(points)
        self._on_log = on_log
        self._bus = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._error: Exception | None = None
        self._frames: deque[dict[str, Any]] = deque(maxlen=500)
        self._connections = 0

    @property
    def connected(self) -> bool:
        return (self._bus is not None and self._error is None and not self._stop.is_set()
                and self._thread is not None and self._thread.is_alive())

    @property
    def running(self) -> bool:
        return self.connected

    @property
    def connections(self) -> int:
        return self._connections

    @property
    def error(self):
        return self._error

    def _log(self, level: str, message: str) -> None:
        if self._on_log:
            try:
                self._on_log(level, message)
            except Exception:
                # Diagnostics must not terminate the transport thread or
                # prevent a failed bus from being released.
                pass

    def _release_bus(self, bus):
        with self._lock:
            if bus is None or self._bus is not bus:
                return
            self._bus = None
        try:
            bus.shutdown()
        except Exception:
            pass

    def _fail(self, exc: Exception, message: str, bus) -> None:
        self._error = exc
        self._stop.set()
        self._release_bus(bus)
        self._log("ERROR", f"{message}: {exc}")

    def start(self):
        with self._lifecycle_lock:
            if self.connected:
                return self
            if can is None:
                raise CanServerError("缺少依赖 python-can") from _CAN_IMPORT_ERROR
            if self._thread is not None or self._bus is not None:
                self.close()
                if self._thread is not None and self._thread.is_alive():
                    raise CanServerError("上一次 CAN 接收线程尚未停止")
            self._stop.clear()
            self._ready.clear()
            self._error = None
            kwargs = {
                "interface": self.interface,
                "channel": self.channel,
                "bitrate": self.bitrate,
                "receive_own_messages": self.receive_own_messages,
            }
            if self.fd:
                kwargs["fd"] = True
                kwargs["data_bitrate"] = self.data_bitrate
            try:
                try:
                    self._bus = can.Bus(**kwargs)
                except TypeError:
                    # Older python-can releases reject these bus options.
                    kwargs.pop("fd", None)
                    kwargs.pop("data_bitrate", None)
                    self._bus = can.Bus(**kwargs)
                self._thread = threading.Thread(target=self._receive_loop, name="can-server", daemon=True)
                self._thread.start()
            except Exception as exc:
                self._fail(exc, "CAN 启动失败", self._bus)
                raise CanServerError(str(exc)) from exc
            self._log("INFO", f"CAN 服务端已启动 {self.interface}/{self.channel}")
            return self

    def _receive_loop(self):
        bus = self._bus
        self._ready.set()
        try:
            while not self._stop.is_set():
                message = bus.recv(timeout=self.receive_timeout)
                if message is None:
                    continue
                self._trace_can("RX", message)
                frame = self._frame_dict(message, "RX")
                with self._lock:
                    self._frames.append(frame)
                self._log("INFO", f"CAN RX 0x{frame['arbitration_id']:X} {frame['data']}")
                if self.request_id is not None and message.arbitration_id == self.request_id:
                    if self.response_id is not None:
                        self.send_frame(self.response_id, self.response_data or frame["data"], log_direction="TX")
        except Exception as exc:
            if not self._stop.is_set():
                self._fail(exc, "CAN 接收或自动应答失败", bus)
        finally:
            self._stop.set()
            self._release_bus(bus)

    @staticmethod
    def _frame_dict(message, direction):
        return {
            "arbitration_id": int(message.arbitration_id),
            "data": bytes(message.data).hex(" "),
            "is_extended_id": bool(message.is_extended_id),
            "is_fd": bool(getattr(message, "is_fd", False)),
            "direction": direction,
            "time": _now(),
        }

    def send_frame(self, arbitration_id: int, data: bytes | bytearray | str,
                   *, is_extended_id=False, log_direction="TX"):
        if not self.connected:
            raise CanServerError("CAN 服务端未启动")
        payload = bytes.fromhex(str(data).replace(" ", "")) if isinstance(data, str) else bytes(data)
        message = can.Message(arbitration_id=int(arbitration_id), data=payload,
                              is_extended_id=bool(is_extended_id), is_fd=self.fd)
        bus = self._bus
        try:
            bus.send(message)
            self._trace_can("TX", message)
        except Exception as exc:
            self._fail(exc, "CAN 发送失败", bus)
            raise CanServerError(str(exc)) from exc
        item = self._frame_dict(message, log_direction)
        with self._lock:
            self._frames.append(item)
        self._log("INFO", f"CAN {log_direction} 0x{int(arbitration_id):X} {item['data']}")
        return item

    def receive_once(self, timeout=None):
        if not self.connected:
            raise CanServerError("CAN 服务端未启动")
        message = self._bus.recv(timeout=self.receive_timeout if timeout is None else float(timeout))
        if message is not None:
            self._trace_can("RX", message)
        if message is None:
            return None
        item = self._frame_dict(message, "RX")
        with self._lock:
            self._frames.append(item)
        return item

    def frames(self, limit=100):
        with self._lock:
            return list(self._frames)[-max(1, int(limit)):]

    def define_point(self, point):
        return self.point_store.define(point)

    def update_point(self, kind, pnt_no, value, *, status=None):
        item = self.point_store.update(kind, pnt_no, value, status=status)
        return item.public()

    def snapshot(self):
        return self.point_store.snapshot()

    def close(self):
        with self._lifecycle_lock:
            self._stop.set()
            self._release_bus(self._bus)
            thread = self._thread
            if thread is not None:
                if thread is not threading.current_thread() and thread.is_alive():
                    thread.join(timeout=2)
                if not thread.is_alive():
                    self._thread = None
            self._log("INFO", "CAN 服务端已停止")

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


class CanServerSimulator(CanServer):
    """Virtual CAN simulator used by automated tests and the HMI."""

    def __init__(self, **kwargs):
        kwargs.setdefault("interface", "virtual")
        kwargs.setdefault("receive_own_messages", True)
        kwargs.setdefault("simulation", True)
        super().__init__(**kwargs)

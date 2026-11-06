"""OPC UA server and local simulator backed by :mod:`asyncua`."""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Iterable, Mapping

from .point_store import PointStore, coerce_value
from core.packet_trace import PacketTrace, trace_stream_server_loop

try:
    from asyncua import Server as UaServer, ua
except Exception as exc:  # pragma: no cover
    UaServer = None
    ua = None
    _OPC_IMPORT_ERROR = exc
else:
    _OPC_IMPORT_ERROR = None


class OpcUaServerError(RuntimeError):
    pass


class OpcUaServer(PacketTrace):
    """A writable OPC UA endpoint exposing the configured four-remote points."""

    PACKET_PROTOCOL = "OPC-UA"

    def __init__(self, *, bind_ip="127.0.0.1", port=4840, endpoint_path="/scada/server/",
                 namespace_uri="urn:scada:server", device_name="SCADA OPC UA Server",
                 points: Iterable[Mapping[str, Any]] | None = None, simulation=False,
                 on_log: Callable[[str, str], Any] | None = None):
        self.bind_ip = str(bind_ip)
        self.port = int(port)
        self.endpoint_path = "/" + str(endpoint_path).strip("/") + "/"
        self.namespace_uri = str(namespace_uri)
        self.device_name = str(device_name)
        self.simulation = bool(simulation)
        self.point_store = PointStore(points)
        if not points and self.simulation:
            self.point_store.ensure_defaults()
        self._on_log = on_log
        self._server = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error = None
        self._nodes: dict[tuple[str, int], Any] = {}
        self.namespace_index = None
        self.endpoint = f"opc.tcp://{self.bind_ip}:{self.port}{self.endpoint_path}"

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
    def _variant_type(kind):
        if ua is None:
            return None
        return ua.VariantType.Boolean if kind in {"yx", "yk"} else ua.VariantType.Double

    async def _start_async(self):
        self._server = UaServer()
        await self._server.init()
        self._server.set_endpoint(self.endpoint)
        self.namespace_index = await self._server.register_namespace(self.namespace_uri)
        objects = self._server.nodes.objects
        for point in self.point_store.snapshot():
            kind, pnt_no = point["kind"], int(point["pnt_no"])
            identifier = f"{kind.upper()}_{pnt_no}"
            node = await objects.add_variable(
                ua.NodeId(identifier, self.namespace_index), identifier,
                coerce_value(kind, point["value"]), self._variant_type(kind),
            )
            await node.set_writable()
            self._nodes[(kind, pnt_no)] = node
        await self._server.start()

    async def _stop_async(self):
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def _wait_async(self):
        while not self._stop.is_set():
            await asyncio.sleep(0.05)

    def _run(self):
        try:
            self._loop = asyncio.new_event_loop()
            trace_stream_server_loop(self._loop, self._packet)
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._start_async())
            self._ready.set()
            self._log("INFO", f"OPC UA 服务端已监听 {self.endpoint}")
            self._loop.run_until_complete(self._wait_async())
            self._loop.run_until_complete(self._stop_async())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            self._log("ERROR", f"OPC UA 服务端启动失败: {exc}")
        finally:
            if self._loop is not None:
                self._loop.close()
                self._loop = None

    def start(self):
        if self.connected:
            return self
        if UaServer is None:
            raise OpcUaServerError("缺少依赖 asyncua") from _OPC_IMPORT_ERROR
        self._ready.clear()
        self._stop.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="opcua-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise OpcUaServerError("OPC UA 服务端启动超时")
        if self._error:
            raise OpcUaServerError(str(self._error)) from self._error
        return self

    def _call(self, coroutine, timeout=5):
        if not self.connected or self._loop is None:
            raise OpcUaServerError("OPC UA 服务端未启动")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout)

    def update_point(self, kind, pnt_no, value, *, status=None):
        item = self.point_store.update(kind, pnt_no, value, status=status)
        node = self._nodes.get((str(kind), int(pnt_no)))
        if node is not None and self.connected:
            self._call(node.write_value(coerce_value(str(kind), value)))
        return item.public()

    def define_point(self, point):
        item = self.point_store.define(point)
        if self.connected:
            raise OpcUaServerError("运行中的 OPC UA 点表需要重启服务端后重新建模")
        return item

    def snapshot(self):
        if not self.connected:
            return self.point_store.snapshot()
        for key, node in list(self._nodes.items()):
            try:
                value = self._call(node.read_value())
                self.point_store.update(*key, value)
            except Exception:
                pass
        return self.point_store.snapshot()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        self._ready.clear()
        self._log("INFO", "OPC UA 服务端已停止")

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


class OpcUaServerSimulator(OpcUaServer):
    """Named simulator entry point for local OPC UA client tests."""

    def __init__(self, **kwargs):
        kwargs.setdefault("simulation", True)
        super().__init__(**kwargs)

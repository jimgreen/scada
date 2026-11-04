"""OPC UA master adapter backed by :mod:`asyncua`.

The old ``scdfe_c`` OPC driver is Windows OPC DA/COM and stores a ProgID in
``fes_channel_opc_define.opc_name``.  This module intentionally implements modern
OPC UA.  ``opc_name`` is accepted only when it is an OPC UA endpoint such as
``opc.tcp://127.0.0.1:4840``; a DA ProgID is rejected with an actionable error
instead of being silently treated as a UA URL.

The public API is synchronous.  A private asyncio thread owns the asyncua
client, which keeps the same calling style as the Modbus, IEC104, and CAN
adapters and does not replace the application's event loop.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import threading
from typing import Any

__all__ = [
    "OpcUaMaster",
    "OpcError", "OpcValue", "OPCUA_AVAILABLE",
]

try:
    from asyncua import Client as _UaClient
    from asyncua import ua
    OPCUA_AVAILABLE = True
    _OPCUA_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - dependency check
    _UaClient = None
    OPCUA_AVAILABLE = False
    _OPCUA_IMPORT_ERROR = exc


def _field(row: Any, name: str, default=None):
    value = row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)
    return default if value is None else value


def _seconds(value, name: str, *, positive: bool = False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")) or value < 0 or (positive and value <= 0):
        raise ValueError(f"{name} 必须是有限的{'正' if positive else '非负'}数")
    return value


def _attribute_id(value):
    """Resolve the OPC UA attribute name stored in the point table."""
    if value is None or str(value).strip().lower() == "value":
        return ua.AttributeIds.Value
    text = str(value).strip()
    aliases = {name.lower(): item for name, item in ua.AttributeIds.__members__.items()}
    try:
        return aliases[text.lower()]
    except KeyError as exc:
        raise ValueError(f"不支持的 OPC UA 值属性: {value!r}") from exc


def _quality_attribute(value):
    """Validate the DataValue quality field used by OPC UA point mappings."""
    if value is None or str(value).strip().lower() in {"", "statuscode", "status_code", "status"}:
        return "StatusCode"
    raise ValueError("OPC UA 质量属性必须为 StatusCode")


def _coerce_point_value(value, data_type):
    if data_type in (None, ""):
        return value
    kind = str(data_type).strip().lower()
    if kind in {"bool", "boolean"}:
        return bool(value)
    if kind in {"int", "int8", "int16", "int32", "int64", "integer"}:
        return int(value)
    if kind in {"uint", "uint8", "uint16", "uint32", "uint64"}:
        result = int(value)
        if result < 0:
            raise ValueError("OPC UA 无符号值不能为负数")
        return result
    if kind in {"float", "double", "number"}:
        return float(value)
    if kind in {"str", "string"}:
        return str(value)
    raise ValueError(f"不支持的 OPC UA data_type: {data_type!r}")


class OpcError(RuntimeError):
    """OPC UA connection, node, or service failure."""


@dataclass(frozen=True)
class OpcValue:
    rtu_id: int | None
    point_no: int
    kind: str
    node_id: str
    value: Any
    status: Any
    received_at: datetime

    @property
    def quality(self):
        """Expose a scalar quality value for the common scdfe_c adapter path."""
        status = self.status
        if status is None:
            return 0
        value = getattr(status, "value", status)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


class _LoopThread:
    def __init__(self, name: str):
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.closed = False
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise OpcError("启动 OPC UA asyncio 线程超时")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def call(self, function, *args, timeout=10.0, **kwargs):
        if self.closed:
            raise OpcError("OPC UA asyncio 线程已关闭")
        future = asyncio.run_coroutine_threadsafe(function(*args, **kwargs), self.loop)
        try:
            return future.result(_seconds(timeout, "timeout", positive=True))
        except TimeoutError as exc:
            future.cancel()
            raise OpcError("OPC UA 操作超时") from exc
        except Exception as exc:
            if isinstance(exc, OpcError):
                raise
            raise OpcError(f"OPC UA 操作失败: {exc}") from exc

    def close(self):
        if not self.closed:
            self.closed = True
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)


def _require_opcua():
    if not OPCUA_AVAILABLE:
        raise OpcError("缺少依赖 asyncua，请执行: python -m pip install -r requirements.txt") from _OPCUA_IMPORT_ERROR


class _OpcUaProtocol:
    """Synchronous OPC UA client.

    ``endpoint`` must be an OPC UA URL.  Use ``opc.tcp://host:port`` for a
    normal server.  ``username`` and ``password`` are optional; anonymous
    servers leave them unset.
    """

    CHANNEL_TYPE = "opc"
    PROTOCOL_NAME = "OPC UA"
    _STORE_RTU_PROJECTION = "rtu_id,channel_type"
    _STORE_DEFINITION_PROJECTION = (
        "rtu_id,opc_user,opc_pwd,opc_domain,opc_name"
    )
    _STORE_POINT_QUERY = """
        SELECT p.rtu_id, p.kind, p.pnt_no, m.node_id, m.data_type, m.value_attribute,
               m.quality_attribute, m.deadband
        FROM (
            SELECT rtu_id, 'yc' AS kind, pnt_no FROM fes_yc_define WHERE rtu_id=?
            UNION ALL
            SELECT rtu_id, 'yx' AS kind, pnt_no FROM fes_yx_define WHERE rtu_id=?
            UNION ALL
            SELECT rtu_id, 'yk' AS kind, pnt_no FROM fes_yk_define WHERE rtu_id=?
            UNION ALL
            SELECT rtu_id, 'yt' AS kind, pnt_no FROM fes_yt_define WHERE rtu_id=?
        ) AS p
        LEFT JOIN fes_channel_opc_point_define AS m
          ON m.rtu_id=? AND m.kind=p.kind AND m.pnt_no=p.pnt_no
        ORDER BY CASE p.kind WHEN 'yc' THEN 1 WHEN 'yx' THEN 2
                             WHEN 'yk' THEN 3 ELSE 4 END, p.pnt_no
    """

    def __init__(self, endpoint: str, *, username: str | None = None,
                 password: str | None = None, domain: str | None = None,
                 rtu_id: int | None = None, timeout: float = 5.0,
                 read_batch_size: int = 100, backend=None):
        _require_opcua()
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("endpoint 必须是非空字符串")
        if not endpoint.lower().startswith(("opc.tcp://", "opc.https://", "https://")):
            raise ValueError(
                "OPC UA endpoint 必须以 opc.tcp://、opc.https:// 或 https:// 开头；"
                "旧 OPC DA ProgID 需要单独的 Windows COM 后端"
            )
        if isinstance(read_batch_size, bool) or not isinstance(read_batch_size, int) or read_batch_size < 1:
            raise ValueError("read_batch_size 必须是正整数")
        self.endpoint = endpoint
        self.username, self.password, self.domain = username, password, domain
        self.rtu_id, self.timeout = rtu_id, _seconds(timeout, "timeout", positive=True)
        self.read_batch_size = read_batch_size
        self._read_limit = read_batch_size
        self._backend = backend
        self._loop_thread: _LoopThread | None = None
        self._client = None
        self._connected = False
        self._lock = threading.RLock()
        self._points: list[dict[str, Any]] = []
        self._command_index: dict[tuple[str, int], dict[str, Any]] = {}

    @classmethod
    def from_channel(cls, channel, definition=None, **kwargs):
        definition = definition or {}
        endpoint = kwargs.pop("endpoint", None) or _field(definition, "opc_name")
        if not endpoint:
            raise ValueError("OPC UA 通道需要在 fes_channel_opc_define.opc_name 或 endpoint 参数中填写 UA URL")
        kwargs.setdefault("rtu_id", _field(channel, "rtu_id", _field(definition, "rtu_id")))
        kwargs.setdefault("username", _field(definition, "opc_user"))
        kwargs.setdefault("password", _field(definition, "opc_pwd"))
        kwargs.setdefault("domain", _field(definition, "opc_domain"))
        return cls(endpoint, **{k: v for k, v in kwargs.items() if v is not None})

    @classmethod
    def from_store(cls, store, rtu_id: int, *, point_map=None, **kwargs):
        with store.connection() as connection:
            connection.execute("BEGIN")
            rtu = connection.execute(
                f"SELECT {cls._STORE_RTU_PROJECTION} FROM fes_rtu_define WHERE rtu_id=?", (rtu_id,)
            ).fetchone()
            if rtu is None:
                raise ValueError(f"RTU {rtu_id} 不存在")
            channel_type = rtu["channel_type"]
            if channel_type != cls.CHANNEL_TYPE:
                raise ValueError(f"RTU {rtu_id} 不是 {cls.PROTOCOL_NAME} 通道 {cls.CHANNEL_TYPE}")
            definition_table = "fes_channel_opc_define"
            definitions = [dict(r) for r in connection.execute(
                f"SELECT {cls._STORE_DEFINITION_PROJECTION} FROM {definition_table} "
                "WHERE rtu_id=? ORDER BY id LIMIT 2", (rtu_id,))]
            channel = dict(rtu)
            rows = [dict(r) for r in connection.execute(
                cls._STORE_POINT_QUERY, (rtu_id, rtu_id, rtu_id, rtu_id, rtu_id))]
        if len(definitions) != 1:
            raise ValueError(f"RTU {rtu_id} 需要唯一 OPC 通道定义，实际找到 {len(definitions)} 条")
        if point_map:
            for row in rows:
                key = (row["kind"], row.get("pnt_no"))
                mapped = point_map.get(key) if isinstance(point_map, Mapping) else None
                if mapped is not None:
                    row["node_id"] = mapped.get("node_id", mapped) if isinstance(mapped, Mapping) else mapped
        client = cls.from_channel(channel, definitions[0], **kwargs)
        if rows and any(_field(row, "node_id") is None for row in rows):
            raise ValueError("OPC UA 点表缺少 node_id；请在点表配置或 from_store(point_map=...) 中提供")
        if rows:
            client.configure(rows)
        return client

    @property
    def connected(self):
        return self._connected

    async def _connect_backend(self):
        self._read_limit = self.read_batch_size
        self._client = _UaClient(self.endpoint, timeout=self.timeout)
        if self.username is not None:
            self._client.set_user(self.username)
        if self.password is not None:
            self._client.set_password(self.password)
        await self._client.connect()

    async def _close_backend(self):
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

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
            loop_thread = _LoopThread("scada-opcua")
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
    def _node_text(node_id):
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("node_id 必须是非空字符串，例如 ns=2;s=YC_001")
        node_id = node_id.strip()
        if not node_id.lower().startswith(("ns=", "nsu=", "i=", "s=", "g=", "b=")):
            raise ValueError("node_id 必须是 OPC UA NodeId，例如 ns=2;s=YC_001")
        return node_id

    async def _read_backend(self, node_id):
        node = self._client.get_node(node_id)
        data_value = await node.read_data_value()
        return data_value.Value.Value, data_value.StatusCode

    async def _read_attribute_backend(self, node_id, attribute):
        node = self._client.get_node(node_id)
        data_value = await node.read_attribute(_attribute_id(attribute))
        return data_value.Value.Value, data_value.StatusCode

    async def _read_data_backend(self, node_id):
        return await self._read_backend(node_id)

    async def _read_many_backend(self, node_ids):
        results = []
        offset = 0
        while offset < len(node_ids):
            chunk = node_ids[offset:offset + self._read_limit]
            nodes = [self._client.get_node(node_id) for node_id in chunk]
            try:
                async with asyncio.timeout(self.timeout):
                    data = await self._client.read_attributes(nodes)
            except ua.UaStatusCodeError as exc:
                # Only retry a read rejected at service level for its size.
                # Transport errors and individual bad statuses are never retried.
                if exc.code != ua.StatusCodes.BadTooManyOperations or len(chunk) == 1:
                    raise
                self._read_limit = max(1, len(chunk) // 2)
                continue
            if len(data) != len(chunk):
                raise OpcError("OPC UA 批量读取返回的节点数量不匹配")
            for item in data:
                if item.StatusCode is None:
                    raise OpcError("OPC UA 读取未返回状态码")
                item.StatusCode.check()
                results.append((item.Value.Value, item.StatusCode, datetime.now(timezone.utc)))
            offset += len(chunk)
        return results

    def _read_batches(self, node_ids):
        if not node_ids:
            return []
        if not self.connected:
            raise OpcError("未连接，请先 connect() 或使用 with")
        result = []
        for start in range(0, len(node_ids), self.read_batch_size):
            chunk = node_ids[start:start + self.read_batch_size]
            # Each native request has its own timeout. The outer guard also
            # allows devices that require splitting this chunk into single reads.
            result.extend(self._loop_thread.call(
                self._read_many_backend, chunk, timeout=self.timeout * len(chunk) + 1.0))
        return result

    async def _write_backend(self, node_id, value):
        node = self._client.get_node(node_id)
        await node.write_value(value)
        return value

    def read_node(self, node_id: str):
        node_id = self._node_text(node_id)
        if not self.connected:
            raise OpcError("未连接，请先 connect() 或使用 with")
        if self._backend is not None:
            return self._backend.read_node(node_id)
        value, _status = self._loop_thread.call(self._read_backend, node_id, timeout=self.timeout)
        return value

    def read_node_data(self, node_id: str):
        """Return ``(value, status_code)`` for applications needing UA quality."""
        node_id = self._node_text(node_id)
        if not self.connected:
            raise OpcError("未连接，请先 connect() 或使用 with")
        if self._backend is not None:
            reader = getattr(self._backend, "read_node_data", None)
            result = reader(node_id) if reader else self._backend.read_node(node_id)
            return result if isinstance(result, tuple) and len(result) == 2 else (result, None)
        return self._loop_thread.call(self._read_data_backend, node_id, timeout=self.timeout)

    def _read_point_data(self, point):
        node_id = self._node_for(point)
        value_attribute = _field(point, "value_attribute", "Value")
        if self._backend is not None:
            reader = getattr(self._backend, "read_point_data", None)
            if reader is not None:
                result = reader(node_id, value_attribute)
                return result if isinstance(result, tuple) and len(result) == 2 else (result, None)
            result = self._backend.read_node_data(node_id) if hasattr(self._backend, "read_node_data") else self._backend.read_node(node_id)
            return result if isinstance(result, tuple) and len(result) == 2 else (result, None)
        if str(value_attribute).strip().lower() == "value":
            return self._loop_thread.call(self._read_data_backend, node_id, timeout=self.timeout)
        return self._loop_thread.call(self._read_attribute_backend, node_id, value_attribute, timeout=self.timeout)

    def write_node(self, node_id: str, value):
        node_id = self._node_text(node_id)
        if not self.connected:
            raise OpcError("未连接，请先 connect() 或使用 with")
        if self._backend is not None:
            return self._backend.write_node(node_id, value)
        return self._loop_thread.call(self._write_backend, node_id, value, timeout=self.timeout)

    def read_nodes(self, node_ids: Iterable[str]):
        if self._backend is not None:
            return [self.read_node(node_id) for node_id in node_ids]
        nodes = [self._node_text(node_id) for node_id in node_ids]
        return [value for value, _status, _received in self._read_batches(nodes)]

    def write_nodes(self, values: Mapping[str, Any] | Iterable[tuple[str, Any]]):
        items = values.items() if isinstance(values, Mapping) else values
        return [self.write_node(node_id, value) for node_id, value in items]

    @staticmethod
    def _node_for(row):
        return _OpcUaProtocol._node_text(_field(row, "node_id"))

    def configure(self, points):
        checked, seen = [], set()
        command_index = {}
        for point in points:
            row = dict(point) if isinstance(point, Mapping) else vars(point).copy()
            node_id = self._node_for(row)
            kind = _field(row, "kind", "yc")
            if kind not in {"yc", "yx", "yk", "yt"}:
                raise ValueError("OPC UA 点 kind 必须是 yc/yx/yk/yt")
            point_no = int(_field(row, "pnt_no"))
            key = (kind, point_no)
            if key in seen:
                raise ValueError(f"重复 OPC UA 点 {key}")
            seen.add(key)
            row["node_id"] = node_id
            row["value_attribute"] = _attribute_id(_field(row, "value_attribute", "Value")).name
            row["quality_attribute"] = _quality_attribute(_field(row, "quality_attribute", "StatusCode"))
            if _field(row, "data_type") not in (None, ""):
                _coerce_point_value(0, _field(row, "data_type"))
            checked.append(row)
            if kind in ("yk", "yt"):
                command_index[key] = row
        with self._lock:
            self._points = checked
            self._command_index = command_index
        return self

    def read_point(self, point):
        node_id = self._node_for(point)
        value, status = self._read_point_data(point)
        value = _coerce_point_value(value, _field(point, "data_type"))
        return OpcValue(self.rtu_id, int(_field(point, "pnt_no")),
                        _field(point, "kind", "yc"), node_id, value, status,
                        datetime.now(timezone.utc))

    def write_point(self, point, value):
        attribute = _attribute_id(_field(point, "value_attribute", "Value"))
        if attribute is not ua.AttributeIds.Value:
            raise ValueError("OPC UA 遥控/遥调只能写入 Value 属性")
        return self.write_node(self._node_for(point),
                               _coerce_point_value(value, _field(point, "data_type")))

    def _collect_once(self, points=None):
        rows = self._points if points is None else list(points)
        selected = [row for row in rows if self.rtu_id is None or _field(row, "rtu_id") == self.rtu_id]
        if self._backend is not None:
            return [self.read_point(row) for row in selected]
        if any(str(_field(row, "value_attribute", "Value")).strip().lower() != "value"
               for row in selected):
            return [self.read_point(row) for row in selected]
        nodes = [self._node_for(row) for row in selected]
        data = self._read_batches(nodes)
        return [OpcValue(self.rtu_id, int(_field(row, "pnt_no")), _field(row, "kind", "yc"),
                         node_id, _coerce_point_value(value, _field(row, "data_type")),
                         status, received)
                for row, node_id, (value, status, received) in zip(selected, nodes, data)]

    def poll_loop(self, callback: Callable[[list[OpcValue]], object], *, interval=None, stop_event=None, max_cycles=None):
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


class OpcUaMaster(_OpcUaProtocol):
    """OPC UA SCADA master facade with the same high-level API as Modbus."""

    CHANNEL_TYPE = "opc"
    PROTOCOL_NAME = "OPC UA"

    def _point_definition(self, kind, point_no, points=None):
        if points is None:
            point = self._command_index.get((kind, int(point_no)))
            if point is None:
                raise ValueError(f"点号 {point_no} 没有唯一的 OPC UA {kind} 点定义")
            return point
        rows = list(points)
        matches = [row for row in rows if _field(row, "kind") == kind
                   and int(_field(row, "pnt_no", -1)) == int(point_no)]
        if len(matches) != 1:
            raise ValueError(f"点号 {point_no} 没有唯一的 OPC UA {kind} 点定义")
        return matches[0]

    def collect_once(self, points=None):
        rows = self._points if points is None else list(points)
        if not rows:
            raise ValueError("尚未配置 OPC UA 点表")
        return super()._collect_once(rows)

    def send_yk_point(self, point_no, close, points=None):
        with self._lock:
            point = point_no if isinstance(point_no, Mapping) else self._point_definition("yk", point_no, points)
            return super().send_yk_point(point, close)

    def send_yt_point(self, point_no, value, points=None):
        with self._lock:
            point = point_no if isinstance(point_no, Mapping) else self._point_definition("yt", point_no, points)
            return super().send_yt_point(point, value)


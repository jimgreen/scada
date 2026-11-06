"""Web-managed Modbus TCP/RTU and CAN test services.

The managers in this module deliberately keep protocol transport state out of
SQLite.  SQLite remains the source of four-remote definitions; the service
configuration, short diagnostic queues, and live transport objects belong to
the Web process and are safe to reload independently.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from typing import Any
from core.packet_trace import service_packet_recorder
from core.service_log import ServiceFileLog
from scdfe_s.web.configuration import apply_config, write_config

from client.can import CanMaster
from client.modbus_rtu import ModbusRtuMaster
from client.modbus_tcp import ModbusTcpMaster
from server.bacnet_server import BacnetServer, BacnetServerSimulator
from server.can_server import CanServer, CanServerSimulator
from server.modbus_server import (
    ModbusAsyncServer,
    ModbusRtuServer,
    ModbusRtuServerSimulator,
    ModbusTcpServer,
)
from server.opc_server import OpcUaServer, OpcUaServerSimulator


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _boolean(value, name):
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"0", "false"}:
            return 0
        if value in {"1", "true"}:
            return 1
    elif isinstance(value, (bool, int)) and value in (0, 1):
        return int(value)
    raise ProtocolServiceError(f"{name} 必须是布尔值或 0/1")


def _json_value(value):
    """Convert nested protocol snapshots, including frames, to JSON values."""
    if hasattr(value, "_asdict"):
        return _json_value(value._asdict())
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex(" ")
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class ProtocolServiceError(RuntimeError):
    """User-correctable protocol service error."""


class ProtocolService:
    """Common lifecycle and Web diagnostics for one protocol service."""

    PROTOCOL = "protocol"
    LABEL = "Protocol"
    MODES = ("master",)
    DEFAULT_CONFIG: dict[str, Any] = {"rtu_id": None, "mode": "master"}
    CONFIG_NAME: str | None = None
    LOG_LIMIT = 500
    ALERT_LIMIT = 200

    def __init__(self, store, *, config_path: Path | str | None = None, startup_catalog=None):
        self.store = store
        self.config_path = Path(config_path) if config_path else store.db_path.with_name(
            self.CONFIG_NAME or f"{self.PROTOCOL.replace('-', '_')}_service_config.json"
        )
        self._lock = threading.RLock()
        self._log_lock = threading.RLock()
        self._configured_fields: set[str] = set()
        self._config_error = None
        self._config = self._load_config()
        self._engine = None
        self._binding_active = False
        self._bound_rtu_id = None
        self._latest_values: dict[tuple[str, int], dict[str, Any]] = {}
        self._started_at: int | None = None
        self._stopped_at: int | None = None
        self._last_error: str | None = self._config_error
        slug = self.PROTOCOL
        if self.CONFIG_NAME and "_server_" in self.CONFIG_NAME and not slug.endswith("-server"):
            slug += "-server"
        self._file_log = ServiceFileLog(store, slug, startup_catalog=startup_catalog)
        self._logs: deque[dict[str, Any]] = deque(self._file_log.recent(self.LOG_LIMIT), maxlen=self.LOG_LIMIT)
        self._alerts: deque[dict[str, Any]] = deque(maxlen=self.ALERT_LIMIT)
        self._connections: deque[dict[str, Any]] = deque(maxlen=100)
        if self._config_error:
            self._record("ERROR", self._config_error, alert=True)
        self._record("INFO", f"{self.LABEL} 服务管理器已就绪")

    def _load_config(self):
        config = dict(self.DEFAULT_CONFIG)
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("配置必须是对象")
            explicit = payload.pop("_overrides", None)
            if isinstance(explicit, list):
                self._configured_fields = set(explicit) & set(self.DEFAULT_CONFIG)
            else:
                # Legacy files persisted every default.  Preserve their
                # historical fallback to the database for untouched values.
                self._configured_fields = {
                    key for key, value in payload.items()
                    if key in self.DEFAULT_CONFIG and value != self.DEFAULT_CONFIG[key]
                }
            config.update(payload)
            return self._validate_config(config)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, OverflowError, RecursionError, ProtocolServiceError) as exc:
            self._config_error = f"读取配置失败，使用默认配置，保留原文件: {exc}"
            self._configured_fields.clear()
        return self._validate_config(self.DEFAULT_CONFIG)

    def _validate_config(self, values):
        config = dict(self.DEFAULT_CONFIG)
        config.update(values)
        mode = str(config.get("mode", "master"))
        if mode not in self.MODES:
            raise ProtocolServiceError(f"{self.LABEL} mode 必须是: {', '.join(self.MODES)}")
        rtu_id = config.get("rtu_id")
        if rtu_id in (None, "", 0, "0"):
            rtu_id = None
        else:
            try:
                rtu_id = int(rtu_id)
            except (TypeError, ValueError) as exc:
                raise ProtocolServiceError("rtu_id 必须是正整数或为空") from exc
            if rtu_id < 1:
                raise ProtocolServiceError("rtu_id 必须是正整数或为空")
        config["mode"] = mode
        config["rtu_id"] = rtu_id
        return config

    @property
    def running(self):
        with self._lock:
            engine = self._engine
            if engine is None:
                return False
            if getattr(engine, "keep_alive", True) is False:
                return bool(getattr(engine, "_session_started", False) and
                            not getattr(engine, "_manual_close", True))
            return bool(getattr(engine, "connected", True))

    def _record(self, level, message, *, alert=False, details=None, channel_id=None):
        item = {"time": _iso_now(), "level": level, "message": str(message)}
        if details is not None:
            item["details"] = details
        # Transport callbacks must never wait for the lifecycle lock while a
        # caller holds that lock and joins the transport's receiver thread.
        with self._log_lock:
            self._logs.append(item)
            self._file_log.write(level, message, channel_id=channel_id)
            if alert:
                self._alerts.append({**item, "acknowledged": False})

    def _persist_config(self, payload):
        write_config(self.config_path, payload)

    def _adopt_config(self, payload):
        self._configured_fields = set(payload["_overrides"])
        self._config = {key: value for key, value in payload.items() if key != "_overrides"}

    def _default_rtu_id(self):
        with self.store.connection() as con:
            row = con.execute(
                "SELECT rtu_id FROM fes_rtu_define WHERE channel_type=? ORDER BY rtu_id LIMIT 1",
                (self.PROTOCOL.removesuffix("-server"),),
            ).fetchone()
        return int(row[0]) if row else None

    def config(self):
        with self._lock:
            table = getattr(self, "PARAMETER_TABLE", None)
            item = self._transport_config(table) if table else dict(self._config)
            item["effective_rtu_id"] = self._configured_rtu()
        return item

    def _configured_rtu(self):
        return self._bound_rtu_id if self._binding_active else self._config.get("rtu_id") or self._default_rtu_id()

    def configure(self, values):
        if not isinstance(values, dict):
            raise ProtocolServiceError("配置必须是对象")
        allowed = set(self.DEFAULT_CONFIG)
        unknown = set(values) - allowed
        if unknown:
            raise ProtocolServiceError(f"不支持的 {self.LABEL} 配置字段: {', '.join(sorted(unknown))}")
        with self._lock:
            updated = dict(self._config)
            updated.update(values)
            updated = self._validate_config(updated)
            previous = {**self._config, "_overrides": sorted(self._configured_fields)}
            candidate = {**updated, "_overrides": sorted(self._configured_fields | values.keys())}
            result = apply_config(self, previous, candidate, self._adopt_config, ProtocolServiceError,
                                  active=self._engine is not None)
            if candidate != previous:
                self._latest_values.clear()
            return result

    def _effective_rtu(self):
        with self._lock:
            rtu_id = self._configured_rtu()
        if rtu_id is None:
            raise ProtocolServiceError(f"没有可用的 {self.LABEL} RTU，请先配置通道")
        return int(rtu_id)

    def _parameter_row(self, table):
        rtu_id = self._configured_rtu()
        if rtu_id is None:
            return {}
        try:
            with self.store.connection() as con:
                row = con.execute(f"SELECT * FROM {table} WHERE rtu_id=? LIMIT 1", (rtu_id,)).fetchone()
            return dict(row) if row else {}
        except Exception:
            return {}

    def _transport_config(self, table):
        """Combine persisted parameters with only explicitly saved overrides."""
        parameter = self._parameter_row(table)
        config = dict(self._config)
        for name, value in parameter.items():
            if name in self.DEFAULT_CONFIG and name not in self._configured_fields and value is not None:
                config[name] = value
        return self._validate_config(config)

    def _build_engine(self):
        raise NotImplementedError

    def start(self):
        with self._lock:
            if self._config_error:
                raise ProtocolServiceError("请先保存有效配置: " + self._config_error)
            if self.running:
                return self.status()
            try:
                if self._engine is not None:
                    self._stop_engine(self._engine)
                self._latest_values.clear()
                self._bound_rtu_id = self._config.get("rtu_id") or self._default_rtu_id()
                self._binding_active = True
                self._engine = self._build_engine()
                if hasattr(self._engine, "set_packet_handler"):
                    from core.packet_logging import rtu_channel_id
                    channel = rtu_channel_id(self.store, self._bound_rtu_id)
                    self._engine.set_packet_handler(service_packet_recorder(self._record, self._bound_rtu_id, channel))
                self._start_engine(self._engine)
            except Exception as exc:
                if self._engine is not None:
                    try:
                        self._stop_engine(self._engine)
                    except Exception:
                        pass
                self._engine = None
                self._binding_active = False
                self._last_error = str(exc)
                self._record("ERROR", f"{self.LABEL} 服务启动失败: {exc}", alert=True)
                raise ProtocolServiceError(str(exc)) from exc
            self._started_at = _now_ms()
            self._stopped_at = None
            self._last_error = None
            self._record("INFO", f"{self.LABEL} 服务已启动 ({self._config.get('mode')})")
            return self.status()

    def _start_engine(self, engine):
        engine.connect()

    def stop(self):
        with self._lock:
            engine = self._engine
            if engine is not None:
                try:
                    self._stop_engine(engine)
                except Exception as exc:
                    self._last_error = str(exc)
                    self._record("ERROR", f"{self.LABEL} 服务停止失败: {exc}", alert=True)
                    raise ProtocolServiceError(str(exc)) from exc
                self._record("INFO", f"{self.LABEL} 服务已停止")
            self._engine = None
            self._binding_active = False
            self._stopped_at = _now_ms()
            return self.status()

    def _stop_engine(self, engine):
        engine.close()

    def close(self):
        try:
            self.stop()
        except ProtocolServiceError:
            pass

    def status(self):
        with self._lock:
            engine = self._engine
            connection_count = int(getattr(engine, "connections", 0)) if engine else 0
            config = self.config()
            rtu_id = config.get("effective_rtu_id")
            return {
                "protocol": self.PROTOCOL,
                "label": self.LABEL,
                "running": self.running,
                "config": config,
                "rtu_id": rtu_id,
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "last_error": str(engine.error) if engine is not None and
                isinstance(getattr(engine, "error", None), BaseException) else self._last_error,
                "connections": list(self._connections),
                "open_connections": connection_count,
                # Status is polled frequently by the Web page.  Counting four
                # point tables must not materialise every point row just to
                # render one integer; use one aggregate query instead.
                "point_count": self._point_count(rtu_id),
            }

    def _point_count(self, rtu_id) -> int:
        if rtu_id is None:
            return 0
        with self.store.connection() as con:
            row = con.execute(
                "SELECT (SELECT COUNT(*) FROM fes_yc_define WHERE rtu_id=?)"
                " + (SELECT COUNT(*) FROM fes_yx_define WHERE rtu_id=?)"
                " + (SELECT COUNT(*) FROM fes_yk_define WHERE rtu_id=?)"
                " + (SELECT COUNT(*) FROM fes_yt_define WHERE rtu_id=?)",
                (int(rtu_id), int(rtu_id), int(rtu_id), int(rtu_id)),
            ).fetchone()
        return int(row[0] or 0)

    def logs(self, limit=200):
        with self._log_lock:
            return list(self._logs)[-max(1, min(int(limit), self.LOG_LIMIT)):]

    def alerts(self, limit=100):
        with self._log_lock:
            return list(self._alerts)[-max(1, min(int(limit), self.ALERT_LIMIT)):]

    def connections(self):
        return list(self._connections)

    def points(self, kind=None):
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yt": "fes_yt_define", "yk": "fes_yk_define"}
        if kind is not None and kind not in tables:
            raise ProtocolServiceError("kind 必须是 yc、yx、yk 或 yt")
        with self._lock:
            rtu_id = self.config().get("effective_rtu_id")
            latest = {key: dict(value) for key, value in self._latest_values.items()}
        if rtu_id is None:
            return []
        rows = []
        with self.store.connection() as con:
            if kind is None:
                con.execute("BEGIN")  # all four point kinds must belong to one committed snapshot
            for name, table in tables.items():
                if kind is not None and kind != name:
                    continue
                for row in con.execute(f"SELECT * FROM {table} WHERE rtu_id=? ORDER BY pnt_no", (rtu_id,)):
                    item = dict(row)
                    item["kind"] = name
                    item.update(latest.get((name, int(item["pnt_no"])), {}))
                    rows.append(item)
        return rows

    def define_point(self, body):
        if not isinstance(body, dict):
            raise ProtocolServiceError("点定义必须是对象")
        kind = str(body.get("kind", ""))
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yt": "fes_yt_define", "yk": "fes_yk_define"}
        if kind not in tables:
            raise ProtocolServiceError("kind 必须是 yc、yx、yk 或 yt")
        payload = dict(body)
        payload.pop("kind", None)
        with self._lock:
            payload["rtu_id"] = self._effective_rtu()
            row = self.store.create_row(tables[kind], payload)
            self._latest_values.pop((kind, int(row["pnt_no"])), None)
            return row

    def update_point(self, kind, point_no, values):
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yt": "fes_yt_define", "yk": "fes_yk_define"}
        if kind not in tables:
            raise ProtocolServiceError("kind 必须是 yc、yx、yk 或 yt")
        body = dict(values)
        body.pop("kind", None)
        body.pop("pnt_no", None)
        with self._lock:
            row = self.store.update_row(tables[kind], [self._effective_rtu(), int(point_no)], body)
            self._latest_values.pop((kind, int(point_no)), None)
            return row

    def delete_point(self, kind, point_no):
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yt": "fes_yt_define", "yk": "fes_yk_define"}
        if kind not in tables:
            raise ProtocolServiceError("kind 必须是 yc、yx、yk 或 yt")
        with self._lock:
            self.store.delete_row(tables[kind], [self._effective_rtu(), int(point_no)])
            self._latest_values.pop((kind, int(point_no)), None)

    def _persist_value(self, kind, point_no, value):
        table = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yt": "fes_yt_define", "yk": "fes_yk_define"}.get(kind)
        if table is None:
            raise ProtocolServiceError("kind 必须是 yc、yx、yk 或 yt")
        if kind in {"yx", "yk"}:
            value = int(bool(value))
        elif kind in {"yc", "yt"}:
            value = float(value)
        row = self.store.update_row(table, [self._effective_rtu(), int(point_no)], {"value": value, "status": 0, "refresh_time": _now_ms()})
        self._latest_values.pop((kind, int(point_no)), None)
        return row

    def _engine_command(self, kind, point_no, value):
        raise ProtocolServiceError(f"{self.LABEL} 尚未启动")

    def command(self, kind, point_no, value):
        with self._lock:
            if kind not in {"yk", "yt"}:
                raise ProtocolServiceError("只支持 yk/yt 命令")
            if not self.running:
                raise ProtocolServiceError(f"{self.LABEL} 服务未启动")
            if kind == "yk":
                value = bool(_boolean(value, "遥控值"))
            else:
                value = float(value)
                if not math.isfinite(value):
                    raise ProtocolServiceError("遥调值必须是有限数")
            try:
                result = self._engine_command(kind, int(point_no), value)
                row = self._persist_value(kind, point_no, value)
            except Exception as exc:
                self._last_error = str(exc)
                self._record("ERROR", f"{self.LABEL} {kind}/{point_no} 指令失败: {exc}", alert=True)
                raise ProtocolServiceError(str(exc)) from exc
            self._record("INFO", f"{self.LABEL} {kind}/{point_no}={value} 指令已发送")
            return {"kind": kind, "pnt_no": int(point_no), "value": value,
                    "command_status": "sent", "result": _json_value(result), "item": row}

    def _publish_modbus(self, updates):
        try:
            batch = self._engine.prepare_updates(updates)
            rtu = self._effective_rtu()
            now = _now_ms()
            rows = self.store.update_rows(((f"fes_{kind}_define", [rtu, no],
                {"value": int(value), "status": 0, "refresh_time": now}) for kind, no, value in batch), return_rows=True)
        except Exception as exc:
            self._record("ERROR", f"模拟点更新未提交: {exc}", alert=True)
            raise ProtocolServiceError(str(exc)) from exc
        for kind, no, _ in batch:
            self._latest_values.pop((kind, no), None)
        try:
            self._engine.publish_updates(batch)
        except Exception as exc:
            message = f"数据库已写入，但模拟寄存器更新未确认: {exc}"
            self._record("ERROR", message, alert=True)
            raise ProtocolServiceError(message) from exc
        return [{"kind": kind, "pnt_no": no, "value": value, "item": row}
                for (kind, no, value), row in zip(batch, rows)]

    def incremental_update(self, kind, point_no, value):
        with self._lock:
            if not self.running:
                raise ProtocolServiceError(f"{self.LABEL} 服务未启动")
            if isinstance(self._engine, ModbusAsyncServer):
                item, = self._publish_modbus([(kind, point_no, value)])
                self._record("INFO", f"增量更新 {kind}/{point_no}={item['value']}")
                return item
            if self._config["mode"] == "master" and kind in {"yk", "yt"}:
                return self.command(kind, point_no, value)
            try:
                result = self._engine_update(kind, int(point_no), value)
                row = self._persist_value(kind, point_no, result)
            except Exception as exc:
                self._record("ERROR", f"增量更新失败: {exc}", alert=True)
                raise ProtocolServiceError(str(exc)) from exc
            self._record("INFO", f"增量更新 {kind}/{point_no}={result}")
            return {"kind": kind, "pnt_no": int(point_no), "value": result, "item": row}

    def _engine_update(self, kind, point_no, value):
        updater = getattr(self._engine, "update_point", None)
        if updater:
            updater(kind, point_no, value)
        return value

    def full_update(self, values=None):
        values = values or {}
        if not isinstance(values, dict) or any(not isinstance(items, dict) for items in values.values()):
            raise ProtocolServiceError("全量更新必须是按类型、点号组织的对象")
        with self._lock:
            if isinstance(self._engine, ModbusAsyncServer):
                if not self.running:
                    raise ProtocolServiceError(f"{self.LABEL} 服务未启动")
                items = self._publish_modbus((kind, no, value) for kind, group in values.items() for no, value in group.items())
                self._record("INFO", f"全量更新完成: {len(items)} 个点")
                return {"updated": len(items), "items": items}
        updated = []
        for kind, items in values.items():
            if not isinstance(items, dict):
                raise ProtocolServiceError(f"{kind} 必须是点号到值的对象")
            for point_no, value in items.items():
                updated.append(self.incremental_update(kind, int(point_no), value))
        self._record("INFO", f"全量更新完成: {len(updated)} 个点")
        return {"updated": len(updated), "items": updated}

    def poll(self):
        with self._lock:
            if not self.running:
                raise ProtocolServiceError(f"{self.LABEL} 服务未启动")
            try:
                values = self._poll_engine()
            except Exception as exc:
                self._record("ERROR", f"采集失败: {exc}", alert=True)
                raise ProtocolServiceError(str(exc)) from exc
            items = []
            for value in values or ():
                item = _json_value(value)
                if not isinstance(item, dict):
                    item = {"value": item}
                point = item.get("pnt_no", item.get("point_no"))
                kind = item.get("kind")
                if kind in {"yc", "yx", "yk", "yt"} and point is not None and "value" in item:
                    item["pnt_no"] = int(point)
                    # Keep diagnostic samples private to this service.  A test
                    # master must not overwrite scdfe_c's acquisition database.
                    self._latest_values[(kind, int(point))] = {
                        "value": item["value"], "status": item.get("status", item.get("quality", 0)),
                        "refresh_time": _now_ms(),
                    }
                items.append(item)
            self._record("INFO", f"单次采集完成: {len(items)} 个值")
            return {"items": items, "count": len(items)}

    def _poll_engine(self):
        if hasattr(self._engine, "collect_once"):
            return self._engine.collect_once()
        snapshot = getattr(self._engine, "snapshot", None)
        return snapshot() if snapshot else []


class ModbusTcpService(ProtocolService):
    PROTOCOL = "modbus-tcp"
    PARAMETER_TABLE = "fes_channel_modbus_tcp_define"
    LABEL = "Modbus-TCP"
    MODES = ("master", "server")
    DEFAULT_CONFIG = {
        "rtu_id": None, "mode": "master", "bind_ip": "127.0.0.1", "listen_port": 1502,
        "remote_host": "127.0.0.1", "remote_port": 502, "unit_id": 1, "response_timeout": 3.0,
        "connect_timeout": 3.0, "retry_count": 0, "keep_alive": 1,
    }

    def _validate_config(self, values):
        config = super()._validate_config(values)
        for key in ("listen_port", "remote_port"):
            try: value = int(config.get(key))
            except (TypeError, ValueError) as exc: raise ProtocolServiceError(f"{key} 必须是整数") from exc
            if not 1 <= value <= 65535: raise ProtocolServiceError(f"{key} 必须在 1..65535")
            config[key] = value
        for key in ("unit_id",):
            config[key] = int(config.get(key))
            if not 1 <= config[key] <= 247: raise ProtocolServiceError("unit_id 必须在 1..247")
        for key in ("response_timeout", "connect_timeout"):
            config[key] = float(config.get(key))
            if config[key] <= 0: raise ProtocolServiceError(f"{key} 必须大于 0")
        config["retry_count"] = max(0, int(config.get("retry_count", 0)))
        config["keep_alive"] = _boolean(config.get("keep_alive", 1), "keep_alive")
        config["bind_ip"] = str(config.get("bind_ip", "127.0.0.1"))
        config["remote_host"] = str(config.get("remote_host", "127.0.0.1"))
        return config

    def _server_context(self, unit_id):
        from pymodbus.datastore import ModbusDeviceContext, ModbusServerContext, ModbusSequentialDataBlock
        context = ModbusDeviceContext(
            co=ModbusSequentialDataBlock(1, [False] * 1000),
            di=ModbusSequentialDataBlock(1, [False] * 1000),
            hr=ModbusSequentialDataBlock(1, [0] * 1000),
            ir=ModbusSequentialDataBlock(1, [0] * 1000),
        )
        return ModbusServerContext({int(unit_id): context}, single=False)

    def _build_engine(self):
        config = self._transport_config("fes_channel_modbus_tcp_define")
        if config["mode"] == "server":
            return ModbusTcpServer(host=config["bind_ip"], port=config["listen_port"], unit_id=config["unit_id"], context=self._server_context(config["unit_id"]), points=self.points(), log=self._record)
        rtu_id = self.config().get("effective_rtu_id")
        options = dict(unit_id=config["unit_id"], timeout=config["response_timeout"],
                       connect_timeout=config["connect_timeout"], retry_count=config["retry_count"],
                       keep_alive=bool(config["keep_alive"]))
        if rtu_id is not None:
            return ModbusTcpMaster.from_store(self.store, int(rtu_id), transport_overrides={
                "remote_host": config["remote_host"], "remote_port": config["remote_port"],
            }, **options)
        return ModbusTcpMaster(config["remote_host"], config["remote_port"], **options)

    def _start_engine(self, engine):
        if isinstance(engine, ModbusAsyncServer): engine.start()
        else: engine.connect()

    def _stop_engine(self, engine):
        if isinstance(engine, ModbusAsyncServer): engine.close()
        else: engine.close()

    def _engine_update(self, kind, point_no, value):
        updater = getattr(self._engine, "update_point", None)
        if updater:
            updater(kind, point_no, value)
        return value

    def _engine_command(self, kind, point_no, value):
        if self._config["mode"] != "master":
            return value
        engine = self._engine
        if kind == "yk": return engine.send_yk_point(point_no, bool(value))
        if kind == "yt": return engine.send_yt_point(point_no, float(value))
        raise ProtocolServiceError("只支持 yk/yt 命令")


class ModbusTcpServerService(ModbusTcpService):
    """Dedicated Modbus-TCP server service; it cannot switch to master mode."""

    LABEL = "Modbus-TCP 服务端"
    MODES = ("server",)
    CONFIG_NAME = "modbus_tcp_server_service_config.json"
    DEFAULT_CONFIG = {
        "rtu_id": None, "mode": "server", "bind_ip": "127.0.0.1", "listen_port": 1502,
        "remote_host": "127.0.0.1", "remote_port": 502, "unit_id": 1, "response_timeout": 3.0,
        "connect_timeout": 3.0, "retry_count": 0, "keep_alive": 1,
    }


class ModbusRtuService(ProtocolService):
    PROTOCOL = "modbus-rtu"
    PARAMETER_TABLE = "fes_channel_modbus_rtu_define"
    LABEL = "Modbus-RTU"
    MODES = ("master", "server")
    DEFAULT_CONFIG = {
        "rtu_id": None, "mode": "master", "serial_port": "COM1", "baudrate": 9600,
        "bytesize": 8, "parity": "E", "stopbits": 1, "unit_id": 1, "response_timeout": 3.0,
        "retry_count": 0,
    }

    def _validate_config(self, values):
        config = super()._validate_config(values)
        config["serial_port"] = str(config.get("serial_port", "COM1"))
        config["baudrate"] = int(config.get("baudrate", 9600))
        config["bytesize"] = int(config.get("bytesize", 8))
        config["stopbits"] = int(config.get("stopbits", 1))
        config["unit_id"] = int(config.get("unit_id", 1))
        config["parity"] = str(config.get("parity", "E")).upper()
        if config["baudrate"] <= 0 or config["bytesize"] != 8 or config["stopbits"] not in (1, 2) or config["parity"] not in {"N", "E", "O"}:
            raise ProtocolServiceError("RTU 参数必须为 8 数据位、N/E/O 校验、1/2 停止位")
        if not 1 <= config["unit_id"] <= 247: raise ProtocolServiceError("unit_id 必须在 1..247")
        config["response_timeout"] = float(config.get("response_timeout", 3.0))
        if config["response_timeout"] <= 0: raise ProtocolServiceError("response_timeout 必须大于 0")
        config["retry_count"] = max(0, int(config.get("retry_count", 0)))
        config["simulation"] = _boolean(config.get("simulation", 0), "simulation")
        return config

    def _server_context(self, unit_id):
        from pymodbus.datastore import ModbusDeviceContext, ModbusServerContext, ModbusSequentialDataBlock
        context = ModbusDeviceContext(co=ModbusSequentialDataBlock(1, [False] * 1000), di=ModbusSequentialDataBlock(1, [False] * 1000), hr=ModbusSequentialDataBlock(1, [0] * 1000), ir=ModbusSequentialDataBlock(1, [0] * 1000))
        return ModbusServerContext({int(unit_id): context}, single=False)

    def _build_engine(self):
        config = self._transport_config("fes_channel_modbus_rtu_define")
        if config["mode"] == "server":
            if config.get("simulation"):
                rows = self.points() or [
                    {"kind": "yc", "pnt_no": 1, "name": "YC_1", "value": 0},
                    {"kind": "yx", "pnt_no": 1, "name": "YX_1", "value": 0},
                    {"kind": "yk", "pnt_no": 1, "name": "YK_1", "value": 0},
                    {"kind": "yt", "pnt_no": 1, "name": "YT_1", "value": 0},
                ]
                return ModbusRtuServerSimulator(unit_id=config["unit_id"], points=rows, log=self._record)
            return ModbusRtuServer(serial=config["serial_port"], unit_id=config["unit_id"], context=self._server_context(config["unit_id"]), points=self.points(), baudrate=config["baudrate"], bytesize=config["bytesize"], parity=config["parity"], stopbits=config["stopbits"], timeout=config["response_timeout"], log=self._record)
        rtu_id = self.config().get("effective_rtu_id")
        options = dict(baudrate=config["baudrate"], bytesize=config["bytesize"],
                       parity=config["parity"], stopbits=config["stopbits"], unit_id=config["unit_id"],
                       timeout=config["response_timeout"], retry_count=config["retry_count"])
        if rtu_id is not None:
            return ModbusRtuMaster.from_store(self.store, int(rtu_id), transport_overrides={
                "serial_port": config["serial_port"],
            }, **options)
        return ModbusRtuMaster(config["serial_port"], **options)

    def _start_engine(self, engine):
        if isinstance(engine, ModbusAsyncServer): engine.start()
        else: engine.connect()

    def _stop_engine(self, engine): engine.close()

    def _engine_command(self, kind, point_no, value):
        if self._config["mode"] != "master": return value
        if kind == "yk": return self._engine.send_yk_point(point_no, bool(value))
        if kind == "yt": return self._engine.send_yt_point(point_no, float(value))
        raise ProtocolServiceError("只支持 yk/yt 命令")


class ModbusRtuServerService(ModbusRtuService):
    """Dedicated Modbus-RTU server service; it cannot switch to master mode."""

    LABEL = "Modbus-RTU 服务端"
    MODES = ("server",)
    CONFIG_NAME = "modbus_rtu_server_service_config.json"
    DEFAULT_CONFIG = {
        "rtu_id": None, "mode": "server", "serial_port": "COM1", "baudrate": 9600,
        "bytesize": 8, "parity": "E", "stopbits": 1, "unit_id": 1, "response_timeout": 3.0,
        "retry_count": 0, "simulation": 1,
    }


class StandaloneServerService(ProtocolService):
    """Web lifecycle adapter for a server implementation in ``server``."""

    MODES = ("server",)
    DEFAULT_CONFIG = {"rtu_id": None, "mode": "server", "simulation": 1}

    def __init__(self, store, **kwargs):
        self._standalone_points: dict[tuple[str, int], dict[str, Any]] = {}
        super().__init__(store, **kwargs)

    def _has_database_rtu(self):
        return self.config().get("effective_rtu_id") is not None

    def _server_points(self):
        rows = self.points()
        if rows:
            return rows
        return [
            {"kind": "yc", "pnt_no": 1, "name": "YC_1", "value": 0},
            {"kind": "yx", "pnt_no": 1, "name": "YX_1", "value": 0},
            {"kind": "yk", "pnt_no": 1, "name": "YK_1", "value": 0},
            {"kind": "yt", "pnt_no": 1, "name": "YT_1", "value": 0},
        ]

    def points(self, kind=None):
        rows = super().points(kind)
        if rows or self._has_database_rtu():
            return rows
        if self.running and hasattr(self._engine, "snapshot"):
            rows = self._engine.snapshot()
            self._standalone_points = {(row["kind"], int(row["pnt_no"])): dict(row) for row in rows}
        rows = list(self._standalone_points.values())
        return [row for row in rows if kind is None or row.get("kind") == kind]

    def _persist_value(self, kind, point_no, value):
        if self._has_database_rtu():
            return super()._persist_value(kind, point_no, value)
        key = (str(kind), int(point_no))
        row = dict(self._standalone_points.get(key, {
            "kind": key[0], "pnt_no": key[1], "name": f"{key[0].upper()}_{key[1]}", "status": 0,
        }))
        row["value"] = bool(value) if kind in {"yx", "yk"} else float(value)
        row["status"] = 0
        self._standalone_points[key] = row
        return row

    def define_point(self, body):
        if self._has_database_rtu():
            return super().define_point(body)
        if not isinstance(body, dict):
            raise ProtocolServiceError("点定义必须是对象")
        kind, pnt_no = str(body.get("kind", "")), int(body.get("pnt_no", 0))
        if kind not in {"yc", "yx", "yk", "yt"} or pnt_no < 1:
            raise ProtocolServiceError("点定义必须包含有效的 kind 和正整数 pnt_no")
        row = {"kind": kind, "pnt_no": pnt_no, "name": str(body.get("name", "") or ""),
               "value": body.get("value", 0), "status": int(body.get("status", 0) or 0)}
        self._standalone_points[(kind, pnt_no)] = row
        if self.running and hasattr(self._engine, "define_point"):
            self._engine.define_point(row)
        return row

    def update_point(self, kind, point_no, values):
        if self._has_database_rtu():
            return super().update_point(kind, point_no, values)
        row = self._persist_value(kind, point_no, values.get("value", 0))
        row.update({key: value for key, value in values.items() if key not in {"kind", "pnt_no"}})
        self._standalone_points[(str(kind), int(point_no))] = row
        return row

    def delete_point(self, kind, point_no):
        if self._has_database_rtu():
            return super().delete_point(kind, point_no)
        self._standalone_points.pop((str(kind), int(point_no)), None)

    def _build_server(self, config, points):
        raise NotImplementedError

    def _build_engine(self):
        config = self._transport_config(self.PARAMETER_TABLE) if getattr(self, "PARAMETER_TABLE", None) else self.config()
        return self._build_server(config, self._server_points())

    def _start_engine(self, engine):
        engine.start()

    def _stop_engine(self, engine):
        engine.close()

    def _engine_update(self, kind, point_no, value):
        updater = getattr(self._engine, "update_point", None)
        if updater:
            updater(kind, point_no, value)
        return value

    def _engine_command(self, kind, point_no, value):
        return self._engine_update(kind, point_no, value)

    def _poll_engine(self):
        snapshot = getattr(self._engine, "snapshot", lambda: [])()
        return snapshot

class CanServerService(StandaloneServerService):
    PROTOCOL = "can-server"
    LABEL = "CAN 服务端"
    CONFIG_NAME = "can_server_service_config.json"
    PARAMETER_TABLE = "fes_channel_can_define"
    DEFAULT_CONFIG = {
        "rtu_id": None, "mode": "server", "simulation": 1, "interface": "virtual",
        "channel": "scada-can-server", "bitrate": 500000, "data_bitrate": 2000000,
        "fd": 0, "receive_own_messages": 1, "receive_timeout": 0.1,
        "request_id": 256, "response_id": 257, "response_data": "01 00",
    }

    def _validate_config(self, values):
        config = super()._validate_config(values)
        config["interface"] = str(config.get("interface", "virtual"))
        config["channel"] = str(config.get("channel", "scada-can-server"))
        for key in ("bitrate", "data_bitrate"):
            config[key] = int(config.get(key, 500000))
            if config[key] <= 0:
                raise ProtocolServiceError(f"{key} 必须大于 0")
        config["fd"] = _boolean(config.get("fd", 0), "fd")
        config["receive_own_messages"] = _boolean(config.get("receive_own_messages", 1), "receive_own_messages")
        config["receive_timeout"] = float(config.get("receive_timeout", 0.1))
        if config["receive_timeout"] <= 0:
            raise ProtocolServiceError("receive_timeout 必须大于 0")
        for key in ("request_id", "response_id"):
            value = config.get(key)
            config[key] = None if value in (None, "") else int(value)
            if config[key] is not None and not 0 <= config[key] <= 0x1FFFFFFF:
                raise ProtocolServiceError(f"{key} 超出 CAN 地址范围")
        config["response_data"] = str(config.get("response_data", ""))
        try:
            response = bytes.fromhex(config["response_data"])
        except ValueError as exc:
            raise ProtocolServiceError("response_data 必须是十六进制字节") from exc
        if len(response) > (64 if config["fd"] else 8):
            raise ProtocolServiceError("response_data 超过 CAN/CAN-FD 帧长度")
        config["simulation"] = _boolean(config.get("simulation", 1), "simulation")
        return config

    def _build_server(self, config, points):
        cls = CanServerSimulator if config.get("simulation") else CanServer
        return cls(interface=config["interface"], channel=config["channel"], bitrate=config["bitrate"],
                   data_bitrate=config["data_bitrate"], fd=bool(config["fd"]),
                   receive_own_messages=bool(config["receive_own_messages"]),
                   receive_timeout=config["receive_timeout"], request_id=config["request_id"],
                   response_id=config["response_id"], response_data=config["response_data"],
                   points=points, on_log=self._record)

    def send_frame(self, arbitration_id, data, *, extended=None):
        if not self.running:
            raise ProtocolServiceError("CAN 服务端未启动")
        return self._engine.send_frame(arbitration_id, data, is_extended_id=extended)


class OpcServerService(StandaloneServerService):
    PROTOCOL = "opc-server"
    LABEL = "OPC UA 服务端"
    CONFIG_NAME = "opc_server_service_config.json"
    PARAMETER_TABLE = "fes_channel_opc_define"
    DEFAULT_CONFIG = {
        "rtu_id": None, "mode": "server", "simulation": 1, "bind_ip": "127.0.0.1",
        "port": 4840, "endpoint_path": "/scada/server/", "namespace_uri": "urn:scada:server",
        "device_name": "SCADA OPC UA Server",
    }

    def _validate_config(self, values):
        config = super()._validate_config(values)
        config["bind_ip"] = str(config.get("bind_ip", "127.0.0.1"))
        config["port"] = int(config.get("port", 4840))
        if not 1 <= config["port"] <= 65535:
            raise ProtocolServiceError("port 必须在 1..65535")
        for key in ("endpoint_path", "namespace_uri", "device_name"):
            config[key] = str(config.get(key, self.DEFAULT_CONFIG[key]))
        config["simulation"] = _boolean(config.get("simulation", 1), "simulation")
        return config

    def _build_server(self, config, points):
        cls = OpcUaServerSimulator if config.get("simulation") else OpcUaServer
        return cls(bind_ip=config["bind_ip"], port=config["port"], endpoint_path=config["endpoint_path"],
                   namespace_uri=config["namespace_uri"], device_name=config["device_name"],
                   points=points, simulation=bool(config.get("simulation")), on_log=self._record)


class BacnetServerService(StandaloneServerService):
    PROTOCOL = "bacnet-server"
    LABEL = "BACnet/IP 服务端"
    CONFIG_NAME = "bacnet_server_service_config.json"
    PARAMETER_TABLE = "fes_channel_bacnet_define"
    DEFAULT_CONFIG = {
        "rtu_id": None, "mode": "server", "simulation": 1, "bind_ip": "127.0.0.1",
        "bind_port": 47808, "device_id": 1001, "device_name": "SCADA BACnet Server",
    }

    def _validate_config(self, values):
        config = super()._validate_config(values)
        config["bind_ip"] = str(config.get("bind_ip", "127.0.0.1"))
        config["bind_port"] = int(config.get("bind_port", 47808))
        if not 1 <= config["bind_port"] <= 65535:
            raise ProtocolServiceError("bind_port 必须在 1..65535")
        config["device_id"] = int(config.get("device_id", 1001))
        if not 0 <= config["device_id"] <= 4194303:
            raise ProtocolServiceError("device_id 超出 BACnet 范围")
        config["device_name"] = str(config.get("device_name", self.DEFAULT_CONFIG["device_name"]))
        config["simulation"] = _boolean(config.get("simulation", 1), "simulation")
        return config

    def _build_server(self, config, points):
        cls = BacnetServerSimulator if config.get("simulation") else BacnetServer
        return cls(bind_ip=config["bind_ip"], bind_port=config["bind_port"], device_id=config["device_id"],
                   device_name=config["device_name"], points=points, simulation=bool(config.get("simulation")),
                   on_log=self._record)


class CanService(ProtocolService):
    PROTOCOL = "can"
    PARAMETER_TABLE = "fes_channel_can_define"
    LABEL = "CAN 总线主站"
    MODES = ("master",)
    DEFAULT_CONFIG = {
        "rtu_id": None, "mode": "master", "interface": "virtual", "channel": "scada-can",
        "bitrate": 500000, "data_bitrate": 2000000, "fd": 0, "receive_own_messages": 1,
        "receive_timeout": 0.1, "retry_count": 0, "is_extended_id": 0,
    }

    def _validate_config(self, values):
        config = super()._validate_config(values)
        config["interface"] = str(config.get("interface", "virtual"))
        config["channel"] = str(config.get("channel", "scada-can"))
        for key in ("bitrate", "data_bitrate"):
            config[key] = int(config.get(key, 0))
            if config[key] <= 0: raise ProtocolServiceError(f"{key} 必须大于 0")
        config["fd"] = _boolean(config.get("fd", 0), "fd")
        config["receive_own_messages"] = _boolean(config.get("receive_own_messages", 1), "receive_own_messages")
        config["is_extended_id"] = _boolean(config.get("is_extended_id", 0), "is_extended_id")
        config["receive_timeout"] = float(config.get("receive_timeout", 0.1))
        if config["receive_timeout"] <= 0: raise ProtocolServiceError("receive_timeout 必须大于 0")
        config["retry_count"] = max(0, int(config.get("retry_count", 0)))
        return config

    def _build_engine(self):
        config = self._transport_config("fes_channel_can_define")
        rtu_id = self.config().get("effective_rtu_id")
        options = dict(interface=config["interface"], channel=config["channel"],
                       bitrate=config["bitrate"], data_bitrate=config["data_bitrate"],
                       fd=bool(config["fd"]), receive_own_messages=bool(config["receive_own_messages"]),
                       auto_start=False)
        if rtu_id is not None:
            return CanMaster.from_store(self.store, int(rtu_id), **options)
        return CanMaster(**options)

    def _engine_command(self, kind, point_no, value):
        if kind == "yk": return self._engine.send_yk_point(point_no, bool(value))
        if kind == "yt": return self._engine.send_yt_point(point_no, float(value))
        raise ProtocolServiceError("只支持 yk/yt 命令")

    def send_frame(self, arbitration_id, data, *, extended=None):
        with self._lock:
            if not self.running: raise ProtocolServiceError("CAN 服务未启动")
            try:
                payload = bytes.fromhex(str(data).replace(" ", ""))
                effective = self.config()
                self._engine.send(int(arbitration_id), payload, is_extended_id=bool(
                    effective["is_extended_id"] if extended is None else _boolean(extended, "is_extended_id")))
            except Exception as exc:
                self._record("ERROR", f"CAN 发帧失败: {exc}", alert=True)
                raise ProtocolServiceError(str(exc)) from exc
            item = {"arbitration_id": int(arbitration_id), "data": payload.hex(" "), "time": _iso_now(), "direction": "TX"}
            self._record("INFO", f"CAN TX 0x{int(arbitration_id):X} {payload.hex(' ')}", details=item)
            return item

    def _poll_engine(self):
        return self._engine.collect_once(timeout=self.config()["receive_timeout"])

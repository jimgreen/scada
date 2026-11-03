"""Web-managed Modbus TCP/RTU and CAN test services.

The managers in this module deliberately keep protocol transport state out of
SQLite.  SQLite remains the source of four-remote definitions; the service
configuration, short diagnostic queues, and live transport objects belong to
the Web process and are safe to reload independently.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any

from api.can import CanMaster
from api.modbus_rtu import ModbusRtuMaster
from api.modbus_tcp import ModbusError, ModbusTcpMaster


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ProtocolServiceError(RuntimeError):
    """User-correctable protocol service error."""


class ProtocolService:
    """Common lifecycle and Web diagnostics for one protocol service."""

    PROTOCOL = "protocol"
    LABEL = "Protocol"
    MODES = ("master",)
    DEFAULT_CONFIG: dict[str, Any] = {"rtu_id": None, "mode": "master"}
    LOG_LIMIT = 500
    ALERT_LIMIT = 200

    def __init__(self, store, *, config_path: Path | str | None = None):
        self.store = store
        self.config_path = Path(config_path) if config_path else store.db_path.with_name(
            f"{self.PROTOCOL.replace('-', '_')}_service_config.json"
        )
        self._lock = threading.RLock()
        self._config = self._load_config()
        self._engine = None
        self._started_at: int | None = None
        self._stopped_at: int | None = None
        self._last_error: str | None = None
        self._logs: deque[dict[str, Any]] = deque(maxlen=self.LOG_LIMIT)
        self._alerts: deque[dict[str, Any]] = deque(maxlen=self.ALERT_LIMIT)
        self._connections: deque[dict[str, Any]] = deque(maxlen=100)
        self._record("INFO", f"{self.LABEL} 服务管理器已就绪")

    def _load_config(self):
        config = dict(self.DEFAULT_CONFIG)
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                config.update(payload)
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._safe_boot_log(f"读取配置失败，使用默认配置: {exc}")
        return self._validate_config(config)

    def _safe_boot_log(self, message):
        try:
            self._logs.append({"time": _iso_now(), "level": "ERROR", "message": message})
        except AttributeError:
            pass

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
            return self._engine is not None and bool(getattr(self._engine, "connected", True))

    def _record(self, level, message, *, alert=False, details=None):
        item = {"time": _iso_now(), "level": level, "message": str(message)}
        if details is not None:
            item["details"] = details
        with self._lock:
            self._logs.append(item)
            if alert:
                self._alerts.append({**item, "acknowledged": False})

    def _persist_config(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temp.write_text(json.dumps(self._config, ensure_ascii=False, indent=2) + "", encoding="utf-8")
        temp.replace(self.config_path)

    def _default_rtu_id(self):
        with self.store.connection() as con:
            row = con.execute(
                "SELECT rtu_id FROM fes_rtu_define WHERE channel_type=? ORDER BY rtu_id LIMIT 1",
                (self.PROTOCOL,),
            ).fetchone()
        return int(row[0]) if row else None

    def config(self):
        with self._lock:
            item = dict(self._config)
        item["effective_rtu_id"] = item.get("rtu_id") or self._default_rtu_id()
        return item

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
            was_running = self.running
            if was_running:
                self.stop()
            self._config = updated
            try:
                self._persist_config()
            except OSError as exc:
                raise ProtocolServiceError(f"保存配置失败: {exc}") from exc
            self._record("INFO", f"{self.LABEL} 配置已更新")
            return self.start() if was_running else self.status()

    def _effective_rtu(self):
        rtu_id = self.config().get("effective_rtu_id")
        if rtu_id is None:
            raise ProtocolServiceError(f"没有可用的 {self.LABEL} RTU，请先配置通道")
        return int(rtu_id)

    def _parameter_row(self, table):
        rtu_id = self.config().get("effective_rtu_id")
        if rtu_id is None:
            return {}
        try:
            with self.store.connection() as con:
                row = con.execute(f"SELECT * FROM {table} WHERE rtu_id=? LIMIT 1", (rtu_id,)).fetchone()
            return dict(row) if row else {}
        except Exception:
            return {}

    def _transport_config(self, table):
        """Use persisted channel parameters when service config is untouched."""
        parameter = self._parameter_row(table)
        config = dict(self._config)
        for name, value in parameter.items():
            if name in config and name in self.DEFAULT_CONFIG and config[name] == self.DEFAULT_CONFIG[name] and value is not None:
                config[name] = value
        return config

    def _build_engine(self):
        raise NotImplementedError

    def start(self):
        with self._lock:
            if self.running:
                return self.status()
            try:
                self._engine = self._build_engine()
                self._start_engine(self._engine)
            except Exception as exc:
                self._engine = None
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
            return {
                "protocol": self.PROTOCOL,
                "label": self.LABEL,
                "running": bool(engine is not None and getattr(engine, "connected", True)),
                "config": self.config(),
                "rtu_id": self.config().get("effective_rtu_id"),
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "last_error": self._last_error,
                "connections": list(self._connections),
                "open_connections": connection_count,
                "point_count": len(self.points()),
            }

    def logs(self, limit=200):
        return list(self._logs)[-max(1, min(int(limit), self.LOG_LIMIT)):]

    def alerts(self, limit=100):
        return list(self._alerts)[-max(1, min(int(limit), self.ALERT_LIMIT)):]

    def connections(self):
        return list(self._connections)

    def points(self, kind=None):
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yt": "fes_yt_define", "yk": "fes_yk_define"}
        if kind is not None and kind not in tables:
            raise ProtocolServiceError("kind 必须是 yc、yx、yk 或 yt")
        rtu_id = self.config().get("effective_rtu_id")
        if rtu_id is None:
            return []
        rows = []
        with self.store.connection() as con:
            for name, table in tables.items():
                if kind is not None and kind != name:
                    continue
                for row in con.execute(f"SELECT * FROM {table} WHERE rtu_id=? ORDER BY pnt_no", (rtu_id,)):
                    item = dict(row)
                    item["kind"] = name
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
        payload["rtu_id"] = self._effective_rtu()
        return self.store.create_row(tables[kind], payload)

    def update_point(self, kind, point_no, values):
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yt": "fes_yt_define", "yk": "fes_yk_define"}
        if kind not in tables:
            raise ProtocolServiceError("kind 必须是 yc、yx、yk 或 yt")
        body = dict(values)
        body.pop("kind", None)
        body.pop("pnt_no", None)
        return self.store.update_row(tables[kind], [self._effective_rtu(), int(point_no)], body)

    def delete_point(self, kind, point_no):
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yt": "fes_yt_define", "yk": "fes_yk_define"}
        if kind not in tables:
            raise ProtocolServiceError("kind 必须是 yc、yx、yk 或 yt")
        self.store.delete_row(tables[kind], [self._effective_rtu(), int(point_no)])

    def _persist_value(self, kind, point_no, value):
        table = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yt": "fes_yt_define", "yk": "fes_yk_define"}.get(kind)
        if table is None:
            raise ProtocolServiceError("kind 必须是 yc、yx、yk 或 yt")
        if kind in {"yx", "yk"}:
            value = int(bool(value))
        elif kind in {"yc", "yt"}:
            value = float(value)
        row = self.store.update_row(table, [self._effective_rtu(), int(point_no)], {"value": value, "status": 0, "refresh_time": _now_ms()})
        return row

    def _engine_command(self, kind, point_no, value):
        raise ProtocolServiceError(f"{self.LABEL} 尚未启动")

    def command(self, kind, point_no, value):
        with self._lock:
            try:
                result = self._engine_command(kind, int(point_no), value)
                row = self._persist_value(kind, point_no, value)
            except Exception as exc:
                self._last_error = str(exc)
                self._record("ERROR", f"{self.LABEL} {kind}/{point_no} 指令失败: {exc}", alert=True)
                raise ProtocolServiceError(str(exc)) from exc
            self._record("INFO", f"{self.LABEL} {kind}/{point_no}={value} 指令已发送")
            return {"kind": kind, "pnt_no": int(point_no), "value": value, "result": result, "item": row}

    def incremental_update(self, kind, point_no, value):
        with self._lock:
            if not self.running:
                raise ProtocolServiceError(f"{self.LABEL} 服务未启动")
            try:
                if kind in {"yc", "yx"}:
                    result = self._engine_update(kind, int(point_no), value)
                else:
                    result = value
                row = self._persist_value(kind, point_no, result)
            except Exception as exc:
                self._record("ERROR", f"增量更新失败: {exc}", alert=True)
                raise ProtocolServiceError(str(exc)) from exc
            self._record("INFO", f"增量更新 {kind}/{point_no}={result}")
            return {"kind": kind, "pnt_no": int(point_no), "value": result, "item": row}

    def _engine_update(self, kind, point_no, value):
        return value

    def full_update(self, values=None):
        values = values or {}
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
                if hasattr(value, "_asdict"):
                    items.append(value._asdict())
                elif isinstance(value, dict):
                    items.append(value)
                else:
                    items.append({"value": value})
            self._record("INFO", f"单次采集完成: {len(items)} 个值")
            return {"items": items, "count": len(items)}

    def _poll_engine(self):
        if hasattr(self._engine, "collect_once"):
            return self._engine.collect_once()
        return []


class ModbusTcpService(ProtocolService):
    PROTOCOL = "modbus-tcp"
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
        config["keep_alive"] = int(bool(config.get("keep_alive", 1)))
        config["bind_ip"] = str(config.get("bind_ip", "127.0.0.1"))
        config["remote_host"] = str(config.get("remote_host", "127.0.0.1"))
        return config

    def _server_context(self):
        from pymodbus.datastore import ModbusDeviceContext, ModbusServerContext, ModbusSequentialDataBlock
        context = ModbusDeviceContext(
            co=ModbusSequentialDataBlock(1, [False] * 1000),
            di=ModbusSequentialDataBlock(1, [False] * 1000),
            hr=ModbusSequentialDataBlock(1, [0] * 1000),
            ir=ModbusSequentialDataBlock(1, [0] * 1000),
        )
        return ModbusServerContext({int(self._config["unit_id"]): context}, single=False)

    def _build_engine(self):
        config = self._transport_config("fes_channel_modbus_tcp_define")
        if config["mode"] == "server":
            return _ModbusAsyncServer(self._server_context(), tcp=True, host=config["bind_ip"], port=config["listen_port"], log=self._record)
        master = ModbusTcpMaster(config["remote_host"], config["remote_port"], unit_id=config["unit_id"], timeout=config["response_timeout"], connect_timeout=config["connect_timeout"], rtu_id=config.get("rtu_id") or self.config().get("effective_rtu_id"), retry_count=config["retry_count"], keep_alive=bool(config["keep_alive"]))
        return master

    def _start_engine(self, engine):
        if isinstance(engine, _ModbusAsyncServer): engine.start()
        else: engine.connect()

    def _stop_engine(self, engine):
        if isinstance(engine, _ModbusAsyncServer): engine.close()
        else: engine.close()

    def _engine_update(self, kind, point_no, value):
        return value

    def _engine_command(self, kind, point_no, value):
        if self._config["mode"] != "master":
            return value
        engine = self._engine
        if kind == "yk": return engine.write_single_coil(point_no, bool(value))
        if kind == "yt": return engine.write_single_register(point_no, int(float(value)))
        raise ProtocolServiceError("只支持 yk/yt 命令")


class ModbusRtuService(ProtocolService):
    PROTOCOL = "modbus-rtu"
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
        return config

    def _server_context(self):
        from pymodbus.datastore import ModbusDeviceContext, ModbusServerContext, ModbusSequentialDataBlock
        context = ModbusDeviceContext(co=ModbusSequentialDataBlock(1, [False] * 1000), di=ModbusSequentialDataBlock(1, [False] * 1000), hr=ModbusSequentialDataBlock(1, [0] * 1000), ir=ModbusSequentialDataBlock(1, [0] * 1000))
        return ModbusServerContext({int(self._config["unit_id"]): context}, single=False)

    def _build_engine(self):
        config = self._transport_config("fes_channel_modbus_rtu_define")
        if config["mode"] == "server":
            return _ModbusAsyncServer(self._server_context(), tcp=False, serial=config["serial_port"], baudrate=config["baudrate"], bytesize=config["bytesize"], parity=config["parity"], stopbits=config["stopbits"], timeout=config["response_timeout"], log=self._record)
        return ModbusRtuMaster(config["serial_port"], baudrate=config["baudrate"], bytesize=config["bytesize"], parity=config["parity"], stopbits=config["stopbits"], unit_id=config["unit_id"], timeout=config["response_timeout"], rtu_id=config.get("rtu_id") or self.config().get("effective_rtu_id"), retry_count=config["retry_count"])

    def _start_engine(self, engine):
        if isinstance(engine, _ModbusAsyncServer): engine.start()
        else: engine.connect()

    def _stop_engine(self, engine): engine.close()

    def _engine_command(self, kind, point_no, value):
        if self._config["mode"] != "master": return value
        if kind == "yk": return self._engine.write_single_coil(point_no, bool(value))
        if kind == "yt": return self._engine.write_single_register(point_no, int(float(value)))
        raise ProtocolServiceError("只支持 yk/yt 命令")


class CanService(ProtocolService):
    PROTOCOL = "can"
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
        config["fd"] = int(bool(config.get("fd", 0)))
        config["receive_own_messages"] = int(bool(config.get("receive_own_messages", 1)))
        config["is_extended_id"] = int(bool(config.get("is_extended_id", 0)))
        config["receive_timeout"] = float(config.get("receive_timeout", 0.1))
        if config["receive_timeout"] <= 0: raise ProtocolServiceError("receive_timeout 必须大于 0")
        config["retry_count"] = max(0, int(config.get("retry_count", 0)))
        return config

    def _build_engine(self):
        config = self._transport_config("fes_channel_can_define")
        return CanMaster(interface=config["interface"], channel=config["channel"], bitrate=config["bitrate"], data_bitrate=config["data_bitrate"], fd=bool(config["fd"]), receive_own_messages=bool(config["receive_own_messages"]), rtu_id=config.get("rtu_id") or self.config().get("effective_rtu_id"), auto_start=False)

    def _engine_command(self, kind, point_no, value):
        if kind == "yk": return self._engine.send_yk_point(point_no, bool(value))
        if kind == "yt": return self._engine.send_yt_point(point_no, float(value))
        raise ProtocolServiceError("只支持 yk/yt 命令")

    def send_frame(self, arbitration_id, data, *, extended=None):
        with self._lock:
            if not self.running: raise ProtocolServiceError("CAN 服务未启动")
            try:
                payload = bytes.fromhex(str(data).replace(" ", ""))
                self._engine.send(int(arbitration_id), payload, is_extended_id=bool(self._config["is_extended_id"] if extended is None else extended))
            except Exception as exc:
                self._record("ERROR", f"CAN 发帧失败: {exc}", alert=True)
                raise ProtocolServiceError(str(exc)) from exc
            item = {"arbitration_id": int(arbitration_id), "data": payload.hex(" "), "time": _iso_now(), "direction": "TX"}
            self._record("INFO", f"CAN TX 0x{int(arbitration_id):X} {payload.hex(' ')}", details=item)
            return item

    def _poll_engine(self):
        return self._engine.collect_once(timeout=self._config["receive_timeout"])


class _ModbusAsyncServer:
    """Own a pymodbus 3.x async server on a small dedicated event loop."""

    def __init__(self, context, *, tcp, log, host=None, port=None, serial=None, **serial_options):
        self.context, self.tcp, self.log = context, tcp, log
        self.host, self.port, self.serial, self.serial_options = host, port, serial, serial_options
        self.thread = None
        self.loop = None
        self.server = None
        self.ready = threading.Event()
        self.error = None
        self.connections = 0

    @property
    def connected(self):
        return self.server is not None and self.error is None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="modbus-server", daemon=True)
        self.thread.start()
        if not self.ready.wait(5): raise TimeoutError("Modbus 服务启动超时")
        if self.error: raise self.error

    def _run(self):
        try: asyncio.run(self._serve())
        except BaseException as exc:
            self.error = exc
            self.ready.set()

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        from pymodbus.server import ModbusSerialServer, ModbusTcpServer
        from pymodbus import FramerType
        kwargs = {"context": self.context, "framer": FramerType.SOCKET if self.tcp else FramerType.RTU,
                  "trace_connect": self._trace_connect}
        if self.tcp:
            kwargs["address"] = (self.host, self.port)
            self.server = ModbusTcpServer(**kwargs)
        else:
            kwargs.update({"port": self.serial, **self.serial_options})
            self.server = ModbusSerialServer(**kwargs)
        await self.server.serve_forever(background=True)
        self.ready.set()
        self.log("INFO", "Modbus 从站已监听")
        await self.server.serving

    def _trace_connect(self, connected):
        self.connections = max(0, self.connections + (1 if connected else -1))

    def close(self):
        if self.server is not None and self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.server.shutdown(), self.loop).result(5)
        elif self.server is not None:
            self.server.close()
        if self.thread is not None: self.thread.join(5)

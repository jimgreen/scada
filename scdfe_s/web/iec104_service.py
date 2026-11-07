"""可由 Web 管理的 IEC 60870-5-104 测试服务。

服务端只维护运行状态和测试日志，四遥点定义仍来自当前 SCADA SQLite
数据库。这样修改 RTU、通道参数或四遥点后，重新加载即可用同一份定义启动
IEC 104 从站，适合客户端联调和回环测试。
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any

from server.iec104_server import IEC104Command, IEC104Server
from functools import partial
from core.packet_trace import service_packet_recorder
from core.service_log import ServiceFileLog
from scdfe_s.web.configuration import apply_config, write_config


class IEC104ServiceError(RuntimeError):
    """IEC 104 Web 服务状态或配置错误。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class IEC104Service:
    """在 Web 进程内托管一个可重载的 IEC 104 服务端实例。"""

    DEFAULT_CONFIG = {
        "bind_ip": "127.0.0.1",
        "port": 2404,
        "rtu_id": None,
        "tick_rate_ms": 100,
        "select_timeout_ms": 10000,
        "max_connections": 0,
    }
    LOG_LIMIT = 500
    ALERT_LIMIT = 200
    CONNECTION_LIMIT = 100

    def __init__(self, store, *, config_path: Path | str | None = None, startup_catalog=None):
        self.store = store
        self.config_path = Path(config_path) if config_path else store.db_path.with_name(
            "iec104_server_config.json"
        )
        self._lock = threading.RLock()
        self._log_lock = threading.RLock()
        self._packet_sink = service_packet_recorder(self._record, None, None)
        self._server: IEC104Server | None = None
        self._config_error = None
        self._config = self._load_config()
        self._file_log = ServiceFileLog(store, "iec104", startup_catalog=startup_catalog)
        initial_logs = self._file_log.recent(self.LOG_LIMIT, record="runtime")
        initial_logs.extend(self._file_log.recent(self.LOG_LIMIT, record="packet"))
        initial_logs.sort(key=lambda item: item.get("time", ""))
        self._logs: deque[dict[str, Any]] = deque(initial_logs[-self.LOG_LIMIT:], maxlen=self.LOG_LIMIT)
        self._alerts: deque[dict[str, Any]] = deque(maxlen=self.ALERT_LIMIT)
        self._connections: deque[dict[str, Any]] = deque(maxlen=self.CONNECTION_LIMIT)
        self._started_at: int | None = None
        self._stopped_at: int | None = None
        self._last_error: str | None = self._config_error
        if self._config_error:
            self._record("ERROR", self._config_error, alert=True)
        self._record("INFO", "服务管理器已就绪")

    def _load_config(self) -> dict[str, Any]:
        config = dict(self.DEFAULT_CONFIG)
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("配置必须是对象")
            config.update(payload)
            return self._validate_config(config)
        except FileNotFoundError:
            pass
        except (OSError, TypeError, ValueError, OverflowError, RecursionError, IEC104ServiceError) as exc:
            self._config_error = f"读取配置失败，使用默认配置，保留原文件: {exc}"
        return self._validate_config(self.DEFAULT_CONFIG)

    @staticmethod
    def _validate_config(values: dict[str, Any]) -> dict[str, Any]:
        config = dict(IEC104Service.DEFAULT_CONFIG)
        config.update(values)
        bind_ip = config.get("bind_ip")
        if not isinstance(bind_ip, str) or not bind_ip.strip():
            raise IEC104ServiceError("bind_ip 必须是非空字符串")
        try:
            port = int(config.get("port"))
            tick_rate_ms = int(config.get("tick_rate_ms"))
            select_timeout_ms = int(config.get("select_timeout_ms"))
            max_connections = int(config.get("max_connections"))
        except (TypeError, ValueError) as exc:
            raise IEC104ServiceError("端口和 IEC 104 时间参数必须是整数") from exc
        if not 1 <= port <= 65535:
            raise IEC104ServiceError("port 必须在 1..65535")
        if tick_rate_ms < 1 or select_timeout_ms < 1:
            raise IEC104ServiceError("tick_rate_ms 和 select_timeout_ms 必须为正数")
        if max_connections < 0:
            raise IEC104ServiceError("max_connections 不能为负数")
        rtu_id = config.get("rtu_id")
        if rtu_id in (None, "", 0, "0"):
            rtu_id = None
        else:
            try:
                rtu_id = int(rtu_id)
            except (TypeError, ValueError) as exc:
                raise IEC104ServiceError("rtu_id 必须是正整数或为空") from exc
            if rtu_id < 1:
                raise IEC104ServiceError("rtu_id 必须是正整数或为空")
        return {
            "bind_ip": bind_ip.strip(),
            "port": port,
            "rtu_id": rtu_id,
            "tick_rate_ms": tick_rate_ms,
            "select_timeout_ms": select_timeout_ms,
            "max_connections": max_connections,
        }

    def _record(self, level: str, message: str, *, alert: bool = False, details: Any = None, channel_id=None) -> None:
        item = {"time": _iso_now(), "level": level, "message": str(message)}
        if details is not None:
            item["details"] = details
        with self._log_lock:
            self._logs.append(item)
            self._file_log.write(level, message, channel_id=channel_id)
            if alert:
                self._alerts.append({**item, "acknowledged": False})

    @property
    def server(self) -> IEC104Server | None:
        with self._lock:
            return self._server

    @property
    def running(self) -> bool:
        server = self.server
        return bool(server and server.running)

    def _persist_config(self, config) -> None:
        write_config(self.config_path, config)

    def _default_rtu_id(self) -> int | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT rtu_id FROM fes_rtu_define WHERE channel_type='iec104' ORDER BY rtu_id LIMIT 1"
            ).fetchone()
        return int(row[0]) if row else None

    def _effective_config(self) -> dict[str, Any]:
        with self._lock:
            config = dict(self._config)
            if self._server is not None:
                config["rtu_id"] = self._server.rtu_id
            elif config.get("rtu_id") is None:
                config["rtu_id"] = self._default_rtu_id()
            return config

    def config(self) -> dict[str, Any]:
        with self._lock:
            config = dict(self._config)
            config["effective_rtu_id"] = self._effective_config()["rtu_id"]
        return config

    def _build_server(self) -> IEC104Server:
        config = self._effective_config()
        if config.get("rtu_id") is None:
            raise IEC104ServiceError("没有可用的 IEC104 RTU，请先在数据与定义中配置通道")
        rtu_id = int(config["rtu_id"])
        from core.packet_logging import rtu_channel_id
        sink = service_packet_recorder(self._record, rtu_id, rtu_channel_id(self.store, rtu_id))
        self._packet_sink = sink
        with self.store.connection() as connection:
            rows = []
            for table, kind in (("fes_yk_define", "yk"), ("fes_yt_define", "yt")):
                rows.extend(
                    (kind, int(row[0]))
                    for row in connection.execute(
                        f"SELECT pnt_no FROM {table} WHERE rtu_id=? ORDER BY pnt_no", (rtu_id,)
                    )
                )
        def command(event):
            return self._on_command(event, rtu_id)
        handlers = {(kind, point_no): command for kind, point_no in rows}
        server = IEC104Server.from_store(
            self.store,
            rtu_id,
            ip=config["bind_ip"],
            port=config["port"],
            tick_rate_ms=config["tick_rate_ms"],
            select_timeout_ms=config["select_timeout_ms"],
            max_connections=config["max_connections"],
            command_handlers=handlers,
        )
        server.set_connect_handler(self._on_connect)
        server.set_raw_handlers(on_receive=partial(sink, 'RX', detail='IEC104 APDU'),
                                on_send=partial(sink, 'TX', detail='IEC104 APDU'))
        return server

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._config_error:
                raise IEC104ServiceError("请先保存有效配置: " + self._config_error)
            if self._server and self._server.running:
                return self.status()
            try:
                self._file_log.start_session()
                server = self._build_server()
                server.start()
            except Exception as exc:
                self._last_error = str(exc)
                self._record("ERROR", f"IEC104 服务启动失败: {exc}", alert=True)
                self._file_log.end_session()
                raise IEC104ServiceError(str(exc)) from exc
            self._server = server
            self._started_at = _now_ms()
            self._stopped_at = None
            self._last_error = None
            self._record(
                "INFO",
                f"IEC104 服务已启动 {server.ip}:{server.port}，RTU {server.rtu_id}",
            )
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            server = self._server
            if server is not None:
                try:
                    server.stop()
                except Exception as exc:
                    self._last_error = str(exc)
                    self._record("ERROR", f"IEC104 服务停止失败: {exc}", alert=True)
                    raise IEC104ServiceError(str(exc)) from exc
                self._record("INFO", "IEC104 服务已停止")
            self._server = None
            self._stopped_at = _now_ms()
            self._file_log.end_session()
            return self.status()

    def close(self) -> None:
        try:
            self.stop()
        except IEC104ServiceError:
            pass
        finally:
            with self._lock:
                self._file_log.end_session()

    def configure(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(self.DEFAULT_CONFIG)
        unknown = set(values) - allowed
        if unknown:
            raise IEC104ServiceError(f"不支持的 IEC104 配置字段: {', '.join(sorted(unknown))}")
        with self._lock:
            updated = dict(self._config)
            updated.update(values)
            updated = self._validate_config(updated)
            return apply_config(self, self._config, updated, self._adopt_config, IEC104ServiceError,
                                active=self._server is not None)

    def _adopt_config(self, config):
        self._config = config

    def _row_for_point(self, kind: str, point_no: int, rtu_id=None) -> tuple[str, list[int]]:
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yk": "fes_yk_define", "yt": "fes_yt_define"}
        if kind not in tables:
            raise IEC104ServiceError("kind 必须是 yc、yx、yk 或 yt")
        if rtu_id is None:
            rtu_id = self._effective_config().get('rtu_id')
        if rtu_id is None:
            raise IEC104ServiceError("没有配置 IEC104 RTU")
        return tables[kind], [int(rtu_id), int(point_no)]

    def _persist_point(self, kind: str, point_no: int, value: Any) -> dict[str, Any]:
        return self.store.update_row(*self._point_update(kind, point_no, value))

    def _point_update(self, kind, point_no, value, rtu_id=None):
        table, key = self._row_for_point(kind, point_no, rtu_id)
        now = _now_ms()
        if value is None and kind in {"yx", "yk"}:
            if kind == "yk":
                raise IEC104ServiceError("中间态或不确定双点状态不能作为遥控命令")
            # An unknown double-point monitoring state is not an OFF update.
            # Keep the last business value and explicitly invalidate quality.
            values = {"status": 128, "refresh_time": now}
            columns = {c["eng_name"] for c in self.store.schema(table)["columns"]}
            if "raw_status" in columns:
                values.update(raw_status=128, raw_time=now)
            return table, key, values
        if kind in {"yx", "yk"}:
            value = int(bool(value))
        elif kind in {"yc", "yt"}:
            value = float(value)
        values = {"value": value, "status": 0, "refresh_time": now}
        if "raw_value" in {c["eng_name"] for c in self.store.schema(table)["columns"]}:
            values["raw_value"] = value
            values["raw_time"] = now
            values["raw_status"] = 0
        return table, key, values

    @staticmethod
    def _updates(payload: Any) -> list[tuple[str, int, Any]]:
        if not isinstance(payload, dict):
            raise IEC104ServiceError("values 必须是对象")
        result: list[tuple[str, int, Any]] = []
        for kind, values in payload.items():
            if kind not in {"yc", "yx", "yk", "yt"}:
                raise IEC104ServiceError(f"未知四遥类型: {kind}")
            if not isinstance(values, dict):
                raise IEC104ServiceError(f"{kind} 必须是点号到值的对象")
            seen = set()
            for point_no, value in values.items():
                try:
                    point = int(point_no)
                except (TypeError, ValueError) as exc:
                    raise IEC104ServiceError(f"{kind} 点号必须是整数") from exc
                if isinstance(point_no, (bool, float)) or point < 1:
                    raise IEC104ServiceError("点号必须是正整数")
                if point in seen:
                    raise IEC104ServiceError(f"{kind} 重复点号 {point}")
                seen.add(point)
                result.append((kind, point, value))
        return result

    def incremental_update(self, kind: str, point_no: int, value: Any) -> dict[str, Any]:
        with self._lock:
            server = self._server
            if server is None or not server.running:
                raise IEC104ServiceError("IEC104 服务未启动")
            try:
                actual = server.prepare_value(kind, point_no, value)
                item = self.store.update_row(*self._point_update(kind, int(point_no), actual, server.rtu_id))
            except Exception as exc:
                self._last_error = str(exc)
                self._record("ERROR", f"增量更新 {kind}/{point_no} 未写入: {exc}", alert=True)
                raise IEC104ServiceError(str(exc)) from exc
            try:
                if kind in {"yc", "yx"}:
                    sent = bool(server.active_connection_count)
                    actual = server.update(kind, int(point_no), value, transmit=sent)
                else:
                    actual = server.set_value(kind, int(point_no), value)
                    sent = False
            except Exception as exc:
                self._last_error = str(exc)
                message = f"点值已写入，IEC104 发布失败: {exc}"
                self._record("ERROR", message, alert=True)
                raise IEC104ServiceError(message) from exc
            self._record("INFO", f"增量更新 {kind}/{point_no}={actual} ({'已上送' if sent else '站内值'})")
            return {"kind": kind, "pnt_no": int(point_no), "value": actual, "sent": sent, "item": item}

    def full_update(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            server = self._server
            if server is None or not server.running:
                raise IEC104ServiceError("IEC104 服务未启动")
            updates = self._updates({} if values is None else values)
            try:
                supplied = {kind: {} for kind in ("yc", "yx", "yk", "yt")}
                for kind, point_no, value in updates:
                    supplied[kind][point_no] = value
                persisted = self.store.update_rows(
                    self._point_update(kind, no, server.prepare_value(kind, no, value), server.rtu_id)
                    for kind, no, value in updates)
            except Exception as exc:
                self._last_error = str(exc)
                self._record("ERROR", f"全量更新失败: {exc}", alert=True)
                raise IEC104ServiceError(str(exc)) from exc
            try:
                result = server.transmit_full(supplied, transmit=bool(server.active_connection_count))
            except Exception as exc:
                self._last_error = str(exc)
                message = f"数据库更新已完成，IEC104 发布失败: {exc}"
                self._record("ERROR", message, alert=True)
                raise IEC104ServiceError(message) from exc
            self._record("INFO", f"全量更新完成: {len(updates)} 个显式点，{result['sent_batches']} 个上送批次")
            return {**result, "requested": len(updates), "persisted": persisted}

    def simulate_command(self, kind: str, point_no: int, value: Any) -> dict[str, Any]:
        if kind not in {"yk", "yt"}:
            raise IEC104ServiceError("模拟指令只允许 yk 或 yt")
        with self._lock:
            server = self._server
            if server is None:
                raise IEC104ServiceError("IEC104 服务未启动")
            try:
                actual = server.prepare_value(kind, point_no, value)
                item = self.store.update_row(*self._point_update(kind, int(point_no), actual, server.rtu_id))
            except Exception as exc:
                raise IEC104ServiceError(str(exc)) from exc
            try:
                server.set_value(kind, int(point_no), value)
            except Exception as exc:
                raise IEC104ServiceError(f"点值已写入，IEC104 站内值更新失败: {exc}") from exc
            self._record("WARNING", f"Web 模拟{kind}指令 {point_no}={actual}，未发送控制报文")
            return {"kind": kind, "pnt_no": int(point_no), "value": actual, "sent": False, "item": item}

    def _on_connect(self, ip: str) -> bool:
        item = {"ip": ip, "time": _iso_now(), "accepted": True}
        with self._log_lock:
            self._connections.appendleft(item)
        self._record("INFO", f"IEC104 客户端接入: {ip}")
        return True

    def _on_receive_raw(self, data: bytes) -> None:
        self._packet_sink("RX", data, "IEC104 APDU")

    def _on_send_raw(self, data: bytes) -> None:
        self._packet_sink("TX", data, "IEC104 APDU")

    def _on_command(self, event: IEC104Command, rtu_id=None) -> bool:
        value = event.value
        try:
            table, key, fields = self._point_update(event.kind, event.point_no, value, rtu_id)
            fields["cmd_handle_time"] = fields["refresh_time"]
            item = self.store.update_row(table, key, fields)
            self._record(
                "INFO",
                f"收到{event.kind}命令 点号={event.point_no} 值={value} "
                f"选择={event.selected} OA={event.originator_address}",
                details={"item": item, "cause": str(event.cause)},
            )
            return True
        except Exception as exc:
            self._last_error = str(exc)
            self._record("ERROR", f"处理 {event.kind}/{event.point_no} 命令失败: {exc}", alert=True)
            return False

    def status(self) -> dict[str, Any]:
        with self._lock:
            server = self._server
            config = self.config()
            with self._log_lock:
                connections = list(self._connections)
            return {
                "running": bool(server and server.running),
                "config": config,
                "rtu_id": getattr(server, "rtu_id", None) if server else config.get("effective_rtu_id"),
                "common_address": getattr(server, "common_address", None) if server else None,
                "open_connections": int(server.open_connection_count) if server else 0,
                "active_connections": int(server.active_connection_count) if server else 0,
                "point_count": server.point_count if server else 0,
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "last_error": self._last_error,
                "connections": connections,
            }

    def points(self, kind: str | None = None, *, table_view=False) -> list[dict[str, Any]]:
        config = self._effective_config()
        rtu_id = config.get("rtu_id")
        if rtu_id is None:
            return []
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yk": "fes_yk_define", "yt": "fes_yt_define"}
        kinds = [kind] if kind else list(tables)
        if any(item not in tables for item in kinds):
            raise IEC104ServiceError("kind 必须是 yc、yx、yk 或 yt")
        rows: list[dict[str, Any]] = []
        with self.store.connection() as connection:
            connection.execute("BEGIN")
            if table_view:
                for item in kinds:
                    rows.extend(dict(row) for row in connection.execute(
                        f"SELECT ? AS kind,p.rtu_id,p.pnt_no,p.value,p.status,m.io_address,m.type_id "
                        f"FROM {tables[item]} p LEFT JOIN fes_channel_iec104_point_define m "
                        "ON m.rtu_id=p.rtu_id AND m.kind=? AND m.pnt_no=p.pnt_no "
                        "WHERE p.rtu_id=? ORDER BY p.pnt_no", (item, item, rtu_id)))
                return rows
            sql = "SELECT * FROM fes_channel_iec104_point_define WHERE rtu_id=?"
            params = (rtu_id,)
            if kind:
                sql += " AND kind=?"
                params += (kind,)
            mappings = {
                (row["kind"], row["pnt_no"]): dict(row)
                for row in connection.execute(sql, params)
            }
            for item in kinds:
                for row in connection.execute(
                    f"SELECT * FROM {tables[item]} WHERE rtu_id=? ORDER BY pnt_no", (rtu_id,)
                ):
                    value = dict(row)
                    value["kind"] = item
                    value.update({
                        key: mapped_value for key, mapped_value in mappings.get((item, int(value["pnt_no"])), {}).items()
                        if key not in {"_version", "rtu_id", "kind", "pnt_no"}
                    })
                    rows.append(value)
        return rows

    def define_point(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise IEC104ServiceError("点定义必须是对象")
        kind = str(body.get("kind", ""))
        if kind not in {"yc", "yx", "yk", "yt"}:
            raise IEC104ServiceError("kind 必须是 yc、yx、yk 或 yt")
        point_no = body.get("pnt_no")
        try:
            point_no = int(point_no)
        except (TypeError, ValueError) as exc:
            raise IEC104ServiceError("pnt_no 必须是正整数") from exc
        if point_no < 1:
            raise IEC104ServiceError("pnt_no 必须是正整数")
        config = self._effective_config()
        if config.get("rtu_id") is None:
            raise IEC104ServiceError("请先配置 IEC104 RTU")
        values = dict(body)
        values.pop("kind", None)
        values["rtu_id"] = int(config["rtu_id"])
        values["pnt_no"] = point_no
        table, _ = self._row_for_point(kind, point_no)
        try:
            item = self.store.create_row(table, values)
        except Exception as exc:
            raise IEC104ServiceError(str(exc)) from exc
        self._record("INFO", f"新增 IEC104 {kind}/{point_no} 定义，重启服务后生效")
        return item

    def update_point(self, kind: str, point_no: int, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict) or "kind" in body or "pnt_no" in body:
            raise IEC104ServiceError("修改点定义时不能修改 kind 或 pnt_no")
        table, key = self._row_for_point(kind, point_no)
        try:
            item = self.store.update_row(table, key, body, body.pop("_if_match", None))
        except Exception as exc:
            raise IEC104ServiceError(str(exc)) from exc
        self._record("INFO", f"修改 IEC104 {kind}/{point_no} 定义，重启服务后生效")
        return item

    def delete_point(self, kind: str, point_no: int, if_match: int | None = None) -> None:
        table, key = self._row_for_point(kind, point_no)
        try:
            self.store.delete_row(table, key, if_match)
        except Exception as exc:
            raise IEC104ServiceError(str(exc)) from exc
        self._record("INFO", f"删除 IEC104 {kind}/{point_no} 定义，重启服务后生效")

    def commands(self) -> list[dict[str, Any]]:
        server = self.server
        if server is None:
            return []
        result = []
        for (kind, point_no), event in list(server._commands.items()):
            result.append({
                "kind": kind,
                "pnt_no": point_no,
                "value": event.value,
                "selected": event.selected,
                "cause": str(event.cause),
                "originator_address": event.originator_address,
                "received_at": event.received_at.isoformat(),
            })
        return result

    def logs(self, limit: int = 200, record: str = "runtime") -> list[dict[str, Any]]:
        if record not in {"runtime", "packet"}:
            raise ValueError("记录类型无效")
        with self._log_lock:
            rows = [item for item in self._logs
                    if (item.get("level") == "PACKET") == (record == "packet")]
            return rows[-max(1, min(int(limit), self.LOG_LIMIT)):][::-1]

    def alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._log_lock:
            return list(self._alerts)[-max(1, min(int(limit), self.ALERT_LIMIT)):][::-1]


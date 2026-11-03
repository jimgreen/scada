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

from api.iec104_server import IEC104Command, IEC104Server


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

    def __init__(self, store, *, config_path: Path | str | None = None):
        self.store = store
        self.config_path = Path(config_path) if config_path else store.db_path.with_name(
            "iec104_server_config.json"
        )
        self._lock = threading.RLock()
        self._server: IEC104Server | None = None
        self._config = self._load_config()
        self._logs: deque[dict[str, Any]] = deque(maxlen=self.LOG_LIMIT)
        self._alerts: deque[dict[str, Any]] = deque(maxlen=self.ALERT_LIMIT)
        self._connections: deque[dict[str, Any]] = deque(maxlen=self.CONNECTION_LIMIT)
        self._started_at: int | None = None
        self._stopped_at: int | None = None
        self._last_error: str | None = None
        self._record("INFO", "服务管理器已就绪")

    def _load_config(self) -> dict[str, Any]:
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

    def _safe_boot_log(self, message: str) -> None:
        # 配置文件损坏时仍允许对象构造完成，正式日志队列尚未建立。
        try:
            self._logs.append({"time": _iso_now(), "level": "ERROR", "message": message})
        except AttributeError:
            pass

    def _record(self, level: str, message: str, *, alert: bool = False, details: Any = None) -> None:
        item = {"time": _iso_now(), "level": level, "message": str(message)}
        if details is not None:
            item["details"] = details
        with self._lock:
            self._logs.append(item)
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

    def _persist_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.config_path)

    def _default_rtu_id(self) -> int | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT rtu_id FROM fes_rtu_define WHERE channel_type='iec104' ORDER BY rtu_id LIMIT 1"
            ).fetchone()
        return int(row[0]) if row else None

    def _effective_config(self) -> dict[str, Any]:
        config = dict(self._config)
        if config.get("rtu_id") is None:
            config["rtu_id"] = self._default_rtu_id()
        return config

    def config(self) -> dict[str, Any]:
        with self._lock:
            config = dict(self._config)
        effective = self._effective_config()
        config["effective_rtu_id"] = effective.get("rtu_id")
        return config

    def _build_server(self) -> IEC104Server:
        config = self._effective_config()
        if config.get("rtu_id") is None:
            raise IEC104ServiceError("没有可用的 IEC104 RTU，请先在数据与定义中配置通道")
        rtu_id = int(config["rtu_id"])
        with self.store.connection() as connection:
            rows = []
            for table, kind in (("fes_yk_define", "yk"), ("fes_yt_define", "yt")):
                rows.extend(
                    (kind, int(row[0]))
                    for row in connection.execute(
                        f"SELECT pnt_no FROM {table} WHERE rtu_id=? ORDER BY pnt_no", (rtu_id,)
                    )
                )
        handlers = {(kind, point_no): self._on_command for kind, point_no in rows}
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
        server.set_raw_handlers(on_receive=self._on_receive_raw, on_send=self._on_send_raw)
        return server

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._server and self._server.running:
                return self.status()
            try:
                server = self._build_server()
                server.start()
            except Exception as exc:
                self._last_error = str(exc)
                self._record("ERROR", f"IEC104 服务启动失败: {exc}", alert=True)
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
            return self.status()

    def close(self) -> None:
        try:
            self.stop()
        except IEC104ServiceError:
            pass

    def configure(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(self.DEFAULT_CONFIG)
        unknown = set(values) - allowed
        if unknown:
            raise IEC104ServiceError(f"不支持的 IEC104 配置字段: {', '.join(sorted(unknown))}")
        with self._lock:
            updated = dict(self._config)
            updated.update(values)
            updated = self._validate_config(updated)
            was_running = bool(self._server and self._server.running)
            if was_running:
                self.stop()
            self._config = updated
            try:
                self._persist_config()
            except OSError as exc:
                self._record("ERROR", f"保存 IEC104 配置失败: {exc}", alert=True)
                raise IEC104ServiceError(str(exc)) from exc
            self._record("INFO", f"IEC104 配置已更新: {updated}")
            if was_running:
                return self.start()
            return self.status()

    def _row_for_point(self, kind: str, point_no: int) -> tuple[str, list[int]]:
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yk": "fes_yk_define", "yt": "fes_yt_define"}
        if kind not in tables:
            raise IEC104ServiceError("kind 必须是 yc、yx、yk 或 yt")
        config = self._effective_config()
        if config.get("rtu_id") is None:
            raise IEC104ServiceError("没有配置 IEC104 RTU")
        return tables[kind], [int(config["rtu_id"]), int(point_no)]

    def _persist_point(self, kind: str, point_no: int, value: Any) -> dict[str, Any]:
        table, key = self._row_for_point(kind, point_no)
        now = _now_ms()
        if kind in {"yx", "yk"}:
            value = int(bool(value))
        elif kind in {"yc", "yt"}:
            value = float(value)
        values = {"value": value, "status": 0, "refresh_time": now}
        if "raw_value" in {c["eng_name"] for c in self.store.schema(table)["columns"]}:
            values["raw_value"] = value
            values["raw_time"] = now
            values["raw_status"] = 0
        return self.store.update_row(table, key, values)

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
            for point_no, value in values.items():
                try:
                    point = int(point_no)
                except (TypeError, ValueError) as exc:
                    raise IEC104ServiceError(f"{kind} 点号必须是整数") from exc
                if point < 1:
                    raise IEC104ServiceError("点号必须是正整数")
                result.append((kind, point, value))
        return result

    def incremental_update(self, kind: str, point_no: int, value: Any) -> dict[str, Any]:
        with self._lock:
            server = self._server
            if server is None or not server.running:
                raise IEC104ServiceError("IEC104 服务未启动")
            try:
                if kind in {"yc", "yx"}:
                    sent = bool(server.active_connection_count)
                    actual = server.update(kind, int(point_no), value, transmit=sent)
                else:
                    actual = server.set_value(kind, int(point_no), value)
                    sent = False
                item = self._persist_point(kind, int(point_no), actual)
            except Exception as exc:
                self._last_error = str(exc)
                self._record("ERROR", f"增量更新 {kind}/{point_no} 失败: {exc}", alert=True)
                raise IEC104ServiceError(str(exc)) from exc
            self._record("INFO", f"增量更新 {kind}/{point_no}={actual} ({'已上送' if sent else '站内值'})")
            return {"kind": kind, "pnt_no": int(point_no), "value": actual, "sent": sent, "item": item}

    def full_update(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            server = self._server
            if server is None or not server.running:
                raise IEC104ServiceError("IEC104 服务未启动")
            updates = self._updates(values or {})
            try:
                supplied = {kind: {} for kind in ("yc", "yx", "yk", "yt")}
                for kind, point_no, value in updates:
                    supplied[kind][point_no] = value
                result = server.transmit_full(supplied, transmit=bool(server.active_connection_count))
                persisted = []
                for kind, point_no, value in updates:
                    actual = server.snapshot()[(kind, point_no)]
                    persisted.append(self._persist_point(kind, point_no, actual))
            except Exception as exc:
                self._last_error = str(exc)
                self._record("ERROR", f"全量更新失败: {exc}", alert=True)
                raise IEC104ServiceError(str(exc)) from exc
            self._record("INFO", f"全量更新完成: {len(updates)} 个显式点，{result['sent_batches']} 个上送批次")
            return {**result, "requested": len(updates), "persisted": len(persisted)}

    def simulate_command(self, kind: str, point_no: int, value: Any) -> dict[str, Any]:
        if kind not in {"yk", "yt"}:
            raise IEC104ServiceError("模拟指令只允许 yk 或 yt")
        with self._lock:
            server = self._server
            if server is None:
                raise IEC104ServiceError("IEC104 服务未启动")
            try:
                actual = server.set_value(kind, int(point_no), value)
                item = self._persist_point(kind, int(point_no), actual)
            except Exception as exc:
                raise IEC104ServiceError(str(exc)) from exc
            self._record("WARNING", f"Web 模拟{kind}指令 {point_no}={actual}，未发送控制报文")
            return {"kind": kind, "pnt_no": int(point_no), "value": actual, "sent": False, "item": item}

    def _on_connect(self, ip: str) -> bool:
        item = {"ip": ip, "time": _iso_now(), "accepted": True}
        with self._lock:
            self._connections.appendleft(item)
        self._record("INFO", f"IEC104 客户端接入: {ip}")
        return True

    def _on_receive_raw(self, data: bytes) -> None:
        self._record("DEBUG", f"RX APDU {bytes(data).hex(' ')}")

    def _on_send_raw(self, data: bytes) -> None:
        self._record("DEBUG", f"TX APDU {bytes(data).hex(' ')}")

    def _on_command(self, event: IEC104Command) -> bool:
        value = event.value
        try:
            item = self._persist_point(event.kind, event.point_no, value)
            now = _now_ms()
            table, key = self._row_for_point(event.kind, event.point_no)
            # The command callback has already passed c104's select/execute
            # validation. Keep the command completion timestamp for the Web
            # page and the existing scdfe_c command monitor.
            item = self.store.update_row(table, key, {"cmd_handle_time": now, "status": 0})
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
            return {
                "running": bool(server and server.running),
                "config": config,
                "rtu_id": getattr(server, "rtu_id", None) if server else config.get("effective_rtu_id"),
                "common_address": getattr(server, "common_address", None) if server else None,
                "open_connections": int(server.open_connection_count) if server else 0,
                "active_connections": int(server.active_connection_count) if server else 0,
                "point_count": len(server.snapshot()) if server else 0,
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "last_error": self._last_error,
                "connections": list(self._connections),
            }

    def points(self, kind: str | None = None) -> list[dict[str, Any]]:
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
            mappings = {
                (str(row[0]), int(row[1])): dict(row)
                for row in connection.execute(
                    "SELECT * FROM fes_channel_iec104_point_define WHERE rtu_id=?",
                    (rtu_id,),
                )
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

    def logs(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._logs)[-max(1, min(int(limit), self.LOG_LIMIT)):][::-1]

    def alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._alerts)[-max(1, min(int(limit), self.ALERT_LIMIT)):][::-1]


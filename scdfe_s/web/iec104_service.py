"""可由 Web 管理的 IEC 60870-5-104 测试服务。

服务端只维护运行状态和测试日志，四遥点定义仍来自当前 SCADA SQLite
数据库。这样修改 RTU、通道参数或四遥点后，重新加载即可用同一份定义启动
IEC 104 从站，适合客户端联调和回环测试。
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from server.iec104_server import IEC104Command, IEC104Server
from functools import partial
from core.iec104_protocol import IEC104ProtocolConfig, IEC104ProtocolError
from core.packet_trace import service_packet_recorder
from core.runtime_status import RuntimeStatusStore, now_ms, state_for
from core.service_log import ServiceFileLog
from core.scdfe_control import log_message_text
from scdfe_s.web.configuration import apply_config, write_config
from scdfe_s.web.lifecycle import traced_lifecycle


LOG = logging.getLogger(__name__)


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
        "k": 12,
        "w": 8,
        "t1": 15.0,
        "t2": 10.0,
        "t3": 20.0,
        "max_asdu_length": 249,
    }
    LOG_LIMIT = 500
    ALERT_LIMIT = 200
    CONNECTION_LIMIT = 100
    DATABASE_SYNC_INTERVAL_S = 0.05

    def __init__(self, store, *, config_path: Path | str | None = None, startup_catalog=None):
        self.store = store
        self.config_path = Path(config_path) if config_path else store.db_path.with_name(
            "iec104_server_config.json"
        )
        self._lock = threading.RLock()
        self._log_lock = threading.RLock()
        self._packet_sink = service_packet_recorder(self._record, None, None)
        self._server: IEC104Server | None = None
        self._database_samples = {}
        self._database_pending = set()
        self._soe_forwarder = None
        self._database_sync_stop = threading.Event()
        self._database_sync_thread: threading.Thread | None = None
        self._config_error = None
        self._config = self._load_config()
        self._file_log = ServiceFileLog(store, "iec104", startup_catalog=startup_catalog, channel_required=True)
        initial_logs = self._file_log.recent(self.LOG_LIMIT, record="runtime")
        initial_logs.extend(self._file_log.recent(self.LOG_LIMIT, record="packet"))
        initial_logs.sort(key=lambda item: item.get("time", ""))
        self._logs: deque[dict[str, Any]] = deque(initial_logs[-self.LOG_LIMIT:], maxlen=self.LOG_LIMIT)
        self._alerts: deque[dict[str, Any]] = deque(maxlen=self.ALERT_LIMIT)
        self._connections: deque[dict[str, Any]] = deque(maxlen=self.CONNECTION_LIMIT)
        self._started_at: int | None = None
        self._stopped_at: int | None = None
        self._last_error: str | None = self._config_error
        self._rx_count = 0
        self._tx_count = 0
        self._error_count = 0
        self._reconnect_count = 0
        self._last_rx_time = 0
        self._last_tx_time = 0
        self._last_error_time = 0
        self._runtime_status = RuntimeStatusStore(store, "scdfe_s")
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
        try:
            protocol = IEC104ProtocolConfig.from_mapping(config)
        except (TypeError, ValueError, IEC104ProtocolError) as exc:
            raise IEC104ServiceError(f"IEC104 协议参数无效: {exc}") from exc
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
            **protocol.as_dict(),
        }

    def _record(self, level: str, message: str, *, alert: bool = False, details: Any = None, channel_id=None) -> None:
        message = log_message_text(message)
        item = {"time": _iso_now(), "level": level, "message": message}
        if details is not None:
            item["details"] = details
        sampled_at = now_ms()
        with self._log_lock:
            if level == "PACKET":
                if "报文 RX" in str(message):
                    self._rx_count += 1
                    self._last_rx_time = sampled_at
                elif "报文 TX" in str(message):
                    self._tx_count += 1
                    self._last_tx_time = sampled_at
            elif level == "ERROR":
                self._error_count += 1
                self._last_error_time = sampled_at
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
            k=config["k"],
            w=config["w"],
            t1=config["t1"],
            t2=config["t2"],
            t3=config["t3"],
            max_asdu_length=config["max_asdu_length"],
            command_handlers=handlers,
        )
        server.set_connect_handler(self._on_connect)
        sink.business.bind_points(server)
        server.set_raw_handlers(on_receive=partial(sink, 'RX', detail='IEC104 APDU'),
                                on_send=partial(sink, 'TX', detail='IEC104 APDU'))
        return server

    @traced_lifecycle
    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._config_error:
                raise IEC104ServiceError("请先保存有效配置: " + self._config_error)
            if self._server and self._server.running:
                return self.status()
            try:
                from core.packet_logging import rtu_channel_id
                effective_rtu = self._effective_config().get("rtu_id")
                self._file_log.bind_channel(rtu_channel_id(self.store, effective_rtu))
                self._file_log.start_session()
                server = self._build_server()
                if getattr(server, 'soe_point_keys', set()) and self.store.application_id:
                    from core.iec104_soe import SoeForwarder
                    server.require_soe_support()
                    self._soe_forwarder = SoeForwarder(
                        self.store, f's:rtu:{server.rtu_id}', 'S',
                        lambda row: self._route_soe(server, row), rtu_id=int(server.rtu_id),
                        on_event=self._packet_sink.business.soe)
                server.start()
            except Exception as exc:
                self._last_error = str(exc)
                self._record("ERROR", f"IEC104 服务启动失败: {exc}", alert=True)
                self._file_log.end_session()
                raise IEC104ServiceError(str(exc)) from exc
            self._server = server
            self._database_samples.clear()
            self._database_pending.clear()
            self._database_sync_stop.clear()
            self._database_sync_thread = threading.Thread(
                target=self._database_sync_loop,
                name=f"iec104-db-sync-{server.rtu_id}",
                daemon=True,
            )
            self._database_sync_thread.start()
            self._started_at = _now_ms()
            self._stopped_at = None
            self._reconnect_count += 1
            self._last_error = None
            self._record(
                "INFO",
                f"IEC104 服务已启动 {server.ip}:{server.port}，RTU {server.rtu_id}",
            )
            return self.status()

    @traced_lifecycle
    def stop(self) -> dict[str, Any]:
        from .lifecycle import stop_cause
        sync_thread = None
        with self._lock:
            server = self._server
            self._database_sync_stop.set()
            if server is not None:
                detail = (f'停止原因={stop_cause()}；RTU编号={server.rtu_id} '
                          f'通道编号={self._file_log.channel_id} 监听={server.ip}:{server.port}')
                self._record('INFO', 'IEC104 服务开始停止 ' + detail)
                try:
                    server.stop()
                except Exception as exc:
                    self._last_error = str(exc)
                    self._record("ERROR", f"IEC104 服务停止失败: {exc}；{detail}", alert=True)
                    raise IEC104ServiceError(str(exc)) from exc
                self._record("INFO", "IEC104 服务已停止 " + detail)
            self._server = None
            if self._soe_forwarder is not None:
                self._soe_forwarder.close()
                self._soe_forwarder = None
            sync_thread = self._database_sync_thread
            self._database_sync_thread = None
            self._stopped_at = _now_ms()
            self._file_log.end_session()
            result = self.status()
        if sync_thread is not None and sync_thread is not threading.current_thread():
            sync_thread.join(timeout=2)
        return result

    def close(self) -> None:
        from .lifecycle import lifecycle_cause
        try:
            with lifecycle_cause('Web服务关闭或资源释放'):
                self.stop()
        except IEC104ServiceError:
            pass
        finally:
            with self._lock:
                self._file_log.end_session()

    @traced_lifecycle
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

    def _sync_database_values(self, connection=None) -> int:
        """Publish database YC/YX changes independently of Web requests.

        The S point editor is deliberately database-only.  This worker polls
        the committed S database and reconciles the running IEC104 station's
        in-memory monitoring points, so edits from the UI, scripts, or another
        process all follow the same IEC104 spontaneous-report path.
        """
        if connection is None:
            with self.store.connection() as owned_connection:
                return self._sync_database_values(owned_connection)
        with self._lock:
            server = self._server
            if server is None or not server.running:
                return 0
            rtu_id = int(server.rtu_id)
            transmit = bool(server.active_connection_count)
            changed = []
            candidates = []
            source_times = {}
            present = set()
            for table, kind in (("fes_yc_define", "yc"), ("fes_yx_define", "yx")):
                rows = connection.execute(
                    f"SELECT pnt_no, value, status, refresh_time FROM {table} WHERE rtu_id=? ORDER BY pnt_no",
                    (rtu_id,),
                )
                for row in rows:
                    point_no, value, quality = int(row[0]), row[1], int(row[2] or 0)
                    key = (kind, point_no)
                    present.add(key)
                    sample = (value, quality)
                    if key not in self._database_pending and self._database_samples.get(key) == sample:
                        continue
                    try:
                        actual = server.prepare_value(kind, point_no, value)
                    except Exception as exc:
                        LOG.warning("IEC104 数据库值无法转换 %s/%s: %s", kind, point_no, exc)
                        continue
                    candidates.append((key, sample, actual))
                    source_times[key] = row[3]
            snapshot = server.monitoring_snapshot(key for key, _, _ in candidates) if candidates else {}
            for key, sample, actual in candidates:
                value, quality = sample
                if key in self._database_pending or snapshot.get(key) != (actual, quality):
                    changed.append((*key, value, quality))
            if changed:
                # Updating native memory is not proof of successful delivery.
                # Retain pending keys until every type group is accepted, even
                # when a retry sees the same value already in native memory.
                self._database_pending.update((k, n) for k, n, _, _ in changed)
                soe_keys = server.soe_point_keys if self._soe_forwarder else set()
                current_only = [item for item in changed if item[:2] in soe_keys]
                ordinary = [item for item in changed if item[:2] not in soe_keys]
                details = [{'四遥': kind.upper(), '点号': no,
                            '旧值': snapshot.get((kind, no), ('未知', '未知'))[0],
                            '旧质量': snapshot.get((kind, no), ('未知', '未知'))[1],
                            '新值': value, '新质量': quality,
                            '源刷新时刻': source_times.get((kind, no)),
                            '发布方式': '当前值缓存，SOE另行投递' if (kind, no) in soe_keys else
                                '突发传送提交' if transmit else '仅缓存，无活动连接'}
                           for kind, no, value, quality in changed]
                self._packet_sink.business.rows('检测到数据库实时变化', details, RTU编号=rtu_id)
                try:
                    if current_only:
                        server.update_monitoring(current_only, transmit=False)
                    if ordinary:
                        server.update_monitoring(ordinary, transmit=transmit)
                except Exception as exc:
                    self._packet_sink.business.rows('实时变化批次未完成，保留重试', details,
                                                    RTU编号=rtu_id, 错误=str(exc))
                    raise
                self._packet_sink.business.rows('实时变化已同步到协议服务', details, RTU编号=rtu_id)
                self._database_pending.difference_update((k, n) for k, n, _, _ in changed)
            for key, sample, _ in candidates:
                # Store source values, not float32 wire-rounding artifacts.
                # Failed native updates never advance the source snapshot.
                self._database_samples[key] = sample
            for key in self._database_samples.keys() - present:
                del self._database_samples[key]
            self._database_pending.intersection_update(present)
            if changed:
                LOG.debug(
                    "IEC104 数据库值同步 RTU=%s points=%s transmit=%s",
                    rtu_id, len(changed), transmit,
                )
            return len(changed)

    @staticmethod
    def _route_soe(server, row):
        key = ('yx', row['pnt_no'])
        if key not in server.soe_point_keys:
            return []
        point = server._points[key]
        if point.type.value != row['type_id']:
            raise ValueError(f'SOE 历史事件类型与当前点类型不同: {key}')
        return [dict(row, common_address=server.common_address, io_address=point.io_address)]

    def _database_sync_loop(self) -> None:
        connection = None
        try:
            # PRAGMA data_version lets the worker avoid scanning all YC/YX
            # rows on every 50 ms tick.  Keep this connection open because
            # data_version is scoped to a connection and must be compared with
            # its own previous value.
            connection = sqlite3.connect(
                str(self.store.db_path), timeout=2.0, isolation_level=None
            )
            previous_data_version = None
            while not self._database_sync_stop.wait(self.DATABASE_SYNC_INTERVAL_S):
                try:
                    current_data_version = int(
                        connection.execute("PRAGMA data_version").fetchone()[0]
                    )
                    if previous_data_version is None or current_data_version != previous_data_version:
                        self._sync_database_values(connection)
                    previous_data_version = current_data_version
                    with self._lock:
                        if self._soe_forwarder is not None and self._server is not None:
                            self._soe_forwarder.tick(self._server)
                            if self._soe_forwarder.error:
                                self._last_error = self._soe_forwarder.error
                except Exception as exc:  # keep the transport alive if a poll fails
                    self._last_error = str(exc)
                    LOG.warning("IEC104 数据库值同步失败: %s", exc)
        except Exception as exc:
            LOG.warning("IEC104 数据库同步线程启动失败: %s", exc)
        finally:
            if connection is not None:
                connection.close()

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
                    sent = bool(server.active_connection_count) and not (
                        self._soe_forwarder and (kind,int(point_no)) in server.soe_point_keys)
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
            table, key = self._row_for_point(event.kind, event.point_no, rtu_id)
            cause_name = str(getattr(event.cause, "name", event.cause)).upper()
            if event.selected:
                item = self.store.get_row(table, key)
                self._record(
                    "INFO",
                    f"收到{event.kind}命令选择 点号={event.point_no} 值={value} OA={event.originator_address}",
                    details={"item": item, "cause": str(event.cause)},
                )
                return True
            if cause_name.endswith("DEACTIVATION"):
                item = self.store.get_row(table, key)
                self._record(
                    "INFO",
                    f"收到{event.kind}命令撤销 点号={event.point_no} 值={value} OA={event.originator_address}",
                    details={"item": item, "cause": str(event.cause)},
                )
                return True
            if not cause_name.endswith('ACTIVATION'):
                return False
            fields = self._point_update(event.kind, event.point_no, value, rtu_id)[2]
            fields["cmd_handle_time"] = fields["refresh_time"]
            item = self.store.update_row(table, key, fields)
            self._record(
                "INFO",
                f"收到{event.kind}命令 点号={event.point_no} 值={value} "
                f"选择={event.selected} OA={event.originator_address} RTU编号={key[0]} "
                f"数据库已提交 质量={item.get('status')} "
                f"指令接收时刻={item.get('cmd_handle_time')} 刷新时刻={item.get('refresh_time')}",
                details={"item": item, "cause": str(event.cause)},
            )
            return True
        except Exception as exc:
            self._last_error = str(exc)
            self._record("ERROR", f"处理 {event.kind}/{event.point_no} 命令失败: {exc}", alert=True)
            return False

    def _persist_runtime_status(self, item: dict[str, Any], *, force: bool = False,
                                runtime_batch=None, channel_info=None) -> None:
        rtu_id = item.get("rtu_id")
        if rtu_id is None:
            return
        try:
            if channel_info is not None:
                row = channel_info.get(int(rtu_id))
            else:
                with self.store.connection() as connection:
                    row = connection.execute(
                        "SELECT channel_id, name FROM fes_rtu_define WHERE rtu_id=?",
                        (int(rtu_id),),
                    ).fetchone()
            channel_id = int(row[0] or rtu_id) if row else int(rtu_id)
            channel_name = str(row[1] or "") if row else ""
            error = str(item.get("last_error") or "")
            diagnostic = {
                "label": "IEC104 服务端",
                "config": item.get("config", {}),
                "protocol_config": item.get("protocol_config", {}),
                "open_connections": item.get("open_connections", 0),
                "active_connections": item.get("active_connections", 0),
                "point_count": item.get("point_count", 0),
                "protocol_errors": item.get("protocol_errors", []),
                "unexpected_messages": item.get("unexpected_messages", []),
            }
            values = {
                "protocol": "iec104",
                "channel_name": channel_name,
                "state": state_for(2 if item.get("running") else 0, item.get("running"), error),
                "running": int(bool(item.get("running"))),
                "connection_state": "listening" if item.get("running") else "disconnected",
                "process_status": 2 if item.get("running") else 0,
                "process_pid": os.getpid() if item.get("running") else 0,
                "process_start_time": int(item.get("started_at") or 0),
                "process_stop_time": int(item.get("stopped_at") or 0),
                "process_heartbeat_time": now_ms(),
                "process_error": error,
                "rx_count": self._rx_count,
                "tx_count": self._tx_count,
                "error_count": self._error_count,
                "reconnect_count": self._reconnect_count,
                "last_rx_time": self._last_rx_time,
                "last_tx_time": self._last_tx_time,
                "last_error_time": self._last_error_time,
                "last_error": error,
                "diagnostic_json": diagnostic,
            }
            if runtime_batch is not None:
                runtime_batch.append((self._runtime_status, channel_id, values))
            else:
                self._runtime_status.upsert(channel_id, values, force=force)
        except Exception:
            LOG.exception("写入 IEC104 通道运行状态失败: rtu_id=%s", rtu_id)

    def status(self, *, point_counts: dict[int, int] | None = None,
               runtime_batch=None, channel_info=None) -> dict[str, Any]:
        with self._lock:
            server = self._server
            config = self.config()
            with self._log_lock:
                connections = list(self._connections)
            item = {
                "running": bool(server and server.running),
                "config": config,
                "rtu_id": getattr(server, "rtu_id", None) if server else config.get("effective_rtu_id"),
                "common_address": getattr(server, "common_address", None) if server else None,
                "open_connections": int(server.open_connection_count) if server else 0,
                "active_connections": int(server.active_connection_count) if server else 0,
                # A running IEC104 server owns its in-memory point snapshot.
                # When stopped, use the monitor's shared database aggregate so
                # the page still shows the configured point count.
                "point_count": (
                    server.point_count if server
                    else int(point_counts.get(int(config["effective_rtu_id"]), 0))
                    if point_counts is not None and config.get("effective_rtu_id") is not None
                    else 0
                ),
                "protocol_config": (server.protocol_config if server else {
                    key: config[key] for key in ("k", "w", "t1", "t2", "t3", "max_asdu_length")
                }),
                "protocol_errors": ([str(item) for item in getattr(server, "protocol_errors", [])]
                                    if server else []),
                "unexpected_messages": (server.unexpected_messages if server else []),
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "last_error": self._last_error,
                "soe_cursor": self._soe_forwarder.last_event_id if self._soe_forwarder else None,
                "soe_pending": self._soe_forwarder.pending if self._soe_forwarder else None,
                "connections": connections,
                "rx_count": self._rx_count,
                "tx_count": self._tx_count,
                "error_count": self._error_count,
                "reconnect_count": self._reconnect_count,
                "last_rx_time": self._last_rx_time,
                "last_tx_time": self._last_tx_time,
                "last_error_time": self._last_error_time,
            }
            self._persist_runtime_status(item, runtime_batch=runtime_batch, channel_info=channel_info)
            return item

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
            channel_row = connection.execute(
                "SELECT channel_id FROM fes_rtu_define WHERE rtu_id=?", (rtu_id,)
            ).fetchone()
            channel_id = channel_row[0] if channel_row else None
            if table_view:
                for item in kinds:
                    rows.extend(dict(row) for row in connection.execute(
                        f"SELECT ? AS kind,p._version,p.rtu_id,p.pnt_no,p.value,p.status,m.io_address,m.type_id "
                        f"FROM {tables[item]} p LEFT JOIN fes_channel_iec104_point_define m "
                        "ON m.channel_id=? AND m.kind=? AND m.pnt_no=p.pnt_no "
                        "WHERE p.rtu_id=? ORDER BY p.pnt_no", (item, channel_id, item, rtu_id)))
                return rows
            sql = "SELECT * FROM fes_channel_iec104_point_define WHERE channel_id=?"
            params = (channel_id,)
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
                        if key not in {"_version", "channel_id", "kind", "pnt_no"}
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

    def logs(self, limit: int = 200, record: str | None = None) -> list[dict[str, Any]]:
        if record is None:
            with self._log_lock:
                return list(self._logs)[-max(1, min(int(limit), self.LOG_LIMIT)):]
        if record not in {"runtime", "packet"}:
            raise ValueError("记录类型无效")
        with self._log_lock:
            rows = [item for item in self._logs
                    if (item.get("level") == "PACKET") == (record == "packet")]
            return rows[-max(1, min(int(limit), self.LOG_LIMIT)):][::-1]

    def alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._log_lock:
            return list(self._alerts)[-max(1, min(int(limit), self.ALERT_LIMIT)):][::-1]


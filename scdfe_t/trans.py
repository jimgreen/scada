"""Runtime engine for scdfe_t four-remote transmission channels.

Each channel is run by one process.  A channel publishes selected YC/YX rows
through IEC104 or Modbus-TCP and accepts YK/YT writes.  Writes are submitted to
the scdfe_c writer IPC endpoint, so the transmission process never competes for
the SQLite write lock.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import logging
import math
import os
from pathlib import Path
import signal
import sqlite3
import stat
import threading
import time
from collections.abc import Mapping
from typing import Any

from scdfe_c.web.service import DEFAULT_DB, SCHEMA_DIR, ScadaStore
from core.scdfe_control import InstanceLock
from core.iec104_protocol import IEC104ProtocolConfig
from core.log_paths import packet_log_directory
from scdfe_c import ScdfeLogger
from core.runtime_status import RuntimeStatusStore, now_ms, state_for
from client.modbus_tcp import normalize_modbus_address

CHANNEL_TABLE = "fes_scdfe_t_define"
POINT_TABLE = "fes_scdfe_t_point_define"
KINDS = {"yc", "yx", "yk", "yt"}
_SOURCE_FIELDS = ("channel_id", "pnt_no", "kind", "source_rtu_id", "source_pnt_no")
_WIRE_FIELDS = {
    "iec104": _SOURCE_FIELDS + ("io_address", "type_id", "time_tag", "command_mode"),
    "modbus-tcp": _SOURCE_FIELDS + ("register_area", "address", "data_type", "byte_order",
                                    "word_order", "scale", "offset", "address_base"),
}
_POINT_RUNTIME_FIELDS = (
    "value", "status", "refresh_time", "forward_time", "command_receive_time",
)


def _get(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = row.get(key)
    return default if value is None else value


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class TransSpec:
    channel_id: int
    protocol: str
    bind_host: str
    bind_port: int
    common_address: int
    unit_id: int
    scan_interval: float
    report_interval_ms: int = 1000
    log_retention_hours: int = 720
    protocol_config: IEC104ProtocolConfig = field(default_factory=IEC104ProtocolConfig)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TransSpec":
        protocol = str(_get(row, "protocol", "")).lower()
        if protocol not in {"iec104", "modbus-tcp"}:
            raise ValueError("转发规约只能是 iec104 或 modbus-tcp")
        port = _get(row, "bind_port", 2404 if protocol == "iec104" else 1502)
        protocol_config = IEC104ProtocolConfig.from_mapping(row)
        return cls(int(row["channel_id"]), protocol,
                   str(_get(row, "bind_host", "0.0.0.0")), int(port),
                   int(_get(row, "common_address", 1)), int(_get(row, "unit_id", 1)),
                   max(.05, _float(_get(row, "scan_interval_ms", 200), 200) / 1000),
                   max(50, int(_get(row, "report_interval_ms", 1000))),
                   max(1, int(_get(row, "log_retention_hours", 720))),
                   protocol_config)


@dataclass(frozen=True)
class TransPoint:
    channel_id: int
    pnt_no: int
    kind: str
    source_rtu_id: int
    source_pnt_no: int
    enabled: bool
    io_address: int | None
    type_id: int | None
    time_tag: str
    command_mode: str
    register_area: str
    address: int
    address_base: int
    data_type: str
    byte_order: str
    word_order: str
    scale: float
    offset: float

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TransPoint":
        kind = str(_get(row, "kind", "")).lower()
        if kind not in KINDS:
            raise ValueError("转发点类型必须是 yc/yx/yk/yt")
        io = _get(row, "io_address")
        typ = _get(row, "type_id")
        address = int(_get(row, "address", int(row["pnt_no"]) - 1))
        address_base = int(_get(row, "address_base", 0))
        normalize_modbus_address(address, address_base, name="address")
        return cls(int(row["channel_id"]), int(row["pnt_no"]), kind,
                   int(_get(row, "source_rtu_id", 0)),
                   int(_get(row, "source_pnt_no", row["pnt_no"])),
                   bool(_get(row, "enabled", 1)),
                    None if io in (None, 0) else int(io), None if typ in (None, 0) else int(typ),
                    str(_get(row, "time_tag", "NONE")),
                   str(_get(row, "command_mode", "DIRECT")),
                   str(_get(row, "register_area", "holding")).lower(),
                   address, address_base,
                   str(_get(row, "data_type", "float32")).lower(),
                   str(_get(row, "byte_order", "big")).lower(),
                   str(_get(row, "word_order", "big")).lower(),
                   _float(_get(row, "scale", 1), 1), _float(_get(row, "offset", 0), 0))


class TransRuntimeError(RuntimeError):
    pass


class TransRuntime:
    def __init__(self, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR, channel_id=1,
                 *, log_dir=None, stop_event=None, require_enabled=False):
        self.db_path = Path(db_path).resolve()
        self.schema_dir = Path(schema_dir).resolve()
        self.channel_id = int(channel_id)
        self.store = ScadaStore(self.db_path, self.schema_dir, connection_pool_size=1 if require_enabled else 0)
        self.stop_event = stop_event or threading.Event()
        # Managed CLI/Web processes cooperate with the persisted stop flag.
        # Direct in-process callers can still use run(max_cycles=...) without
        # modifying the operator's enable setting.
        self.require_enabled = bool(require_enabled)
        from core.packet_logging import packet_log_switch
        runtime_log_dir = Path(log_dir).resolve() if log_dir else self.db_path.parent / "logs"
        self.logger = ScdfeLogger(runtime_log_dir,
                                  process_name=f"scdfe_t-channel-{self.channel_id}",
                                  packet_directory=packet_log_directory(runtime_log_dir),
                                  runtime_packet_compat=self.require_enabled,
                                  packet_switch=packet_log_switch(self.db_path, 't').for_channel(self.channel_id))
        self.server = None
        self.points: dict[tuple[str, int], TransPoint] = {}
        self._scan_plan = ()
        self.values: dict[tuple[str, int], Any] = {}
        self.qualities: dict[tuple[str, int], int] = {}
        self._sample_seen: dict[tuple[str, int], tuple[Any, int, int]] = {}
        self._point_runtime_state: dict[tuple[str, int], dict[str, Any]] = {}
        self._point_runtime_pending: dict[tuple[str, int], dict[str, Any]] = {}
        self.signature = None
        self._definition_cache = None
        self.writer = None
        self.failed = False
        # Definition files are operator configuration, not live samples.
        # Avoid reloading every point on every fast scan; a one
        # second check keeps online edits responsive while removing repeated
        # SQLite/materialisation work from the hot path.
        self.definition_check_interval = 1.0
        self._next_definition_check = 0.0
        self._next_heartbeat = 0.0
        self._log_retention = None
        self._next_cleanup = 0.0
        self._runtime_status = RuntimeStatusStore(self.store, "scdfe_t")
        self._runtime_protocol = ""
        self._runtime_channel_name = ""
        self._cycles = 0
        self._rx_count = 0
        self._tx_count = 0
        self._error_count = 0
        self._reconnect_count = 0
        self._last_rx_time = 0
        self._last_tx_time = 0
        self._last_error_time = 0
        self._last_error = ""
        self._command_count = 0
        self._runtime_state = {
            "process_status": 0, "process_pid": 0,
            "process_start_time": 0, "process_stop_time": 0,
            "process_heartbeat_time": 0, "process_exit_code": 0,
            "process_error": "",
        }

    def _runtime_metadata(self) -> tuple[str, str]:
        if self._runtime_protocol and self._runtime_channel_name:
            return self._runtime_protocol, self._runtime_channel_name
        try:
            with self.store.connection() as connection:
                row = connection.execute(
                    f"SELECT protocol, name FROM {CHANNEL_TABLE} WHERE channel_id=?",
                    (self.channel_id,),
                ).fetchone()
            if row is not None:
                self._runtime_protocol = str(row[0] or "")
                self._runtime_channel_name = str(row[1] or "")
        except Exception:
            pass
        return self._runtime_protocol, self._runtime_channel_name

    def _runtime_diagnostic(self) -> dict[str, Any]:
        server = self.server
        diagnostic: dict[str, Any] = {
            "cycles": self._cycles,
            "source_reads": self._rx_count,
            "published_updates": self._tx_count,
            "command_count": self._command_count,
            "reconnect_count": self._reconnect_count,
            "server_running": bool(server and getattr(server, "running", False)),
            "open_connections": int(getattr(server, "open_connection_count", 0) or 0),
        }
        if server is not None:
            for name in ("unit_id", "host", "port"):
                value = getattr(server, name, None)
                if value is not None:
                    diagnostic[name] = value
            errors = getattr(server, "protocol_errors", None)
            if errors is not None:
                diagnostic["protocol_errors"] = [str(item) for item in errors[-20:]]
            unexpected = getattr(server, "unexpected_messages", None)
            if unexpected is not None:
                diagnostic["unexpected_messages"] = list(unexpected[-20:])
            protocol = getattr(server, "protocol_config", None)
            if protocol is not None and hasattr(protocol, "as_dict"):
                diagnostic["protocol_config"] = protocol.as_dict()
        return diagnostic

    def _persist_runtime_status(self, updates: Mapping[str, Any] | None = None,
                                *, force: bool = False) -> None:
        protocol, channel_name = self._runtime_metadata()
        values = dict(updates or {})
        self._runtime_state.update(values)
        status = int(self._runtime_state.get("process_status") or 0)
        error = str(self._runtime_state.get("process_error") or self._last_error or "")
        running = status == 2
        try:
            self._runtime_status.upsert(self.channel_id, {
                **self._runtime_state,
                "protocol": protocol,
                "channel_name": channel_name,
                "state": state_for(status, running, error),
                "running": int(running),
                "connection_state": "listening" if running and self.server is not None else (
                    "retrying" if status == 5 else "disconnected"
                ),
                "rx_count": self._rx_count,
                "tx_count": self._tx_count,
                "error_count": self._error_count,
                "reconnect_count": self._reconnect_count,
                "last_rx_time": self._last_rx_time,
                "last_tx_time": self._last_tx_time,
                "last_error_time": self._last_error_time,
                "last_error": error,
                "diagnostic_json": self._runtime_diagnostic(),
            }, force=force)
        except Exception as exc:
            self.logger.error(f"写入转发通道运行状态失败: {exc}")

    def _runtime_error(self, message: Any) -> None:
        self._error_count += 1
        self._last_error = str(message)
        self._last_error_time = now_ms()

    def _update_state(self, **values):
        self._persist_runtime_status(values, force="process_status" in values and values["process_status"] != 2)
        try:
            if values.keys() == {'process_heartbeat_time'}:
                # A periodic pulse is not an operator edit or a state transition.
                # Update just this channel in one statement; keep its edit version.
                with self.store.connection() as con:
                    updated = con.execute(f'UPDATE {CHANNEL_TABLE} SET process_heartbeat_time=? WHERE channel_id=?',
                                          (values['process_heartbeat_time'], self.channel_id))
                    if not updated.rowcount:
                        raise TransRuntimeError(f'转发通道 {self.channel_id} 不存在')
                    con.commit()
            else:
                self.store.update_row(CHANNEL_TABLE, [self.channel_id], values)
            return True
        except Exception as exc:
            self.logger.error(f"写入转发进程状态失败: {exc}")
            return False

    def _heartbeat(self):
        now = time.monotonic()
        if now >= self._next_heartbeat:
            values = {'process_heartbeat_time': int(time.time() * 1000)}
            if not self._next_heartbeat:
                values.update(process_status=2, process_pid=os.getpid())
            if self._update_state(**values):
                self._next_heartbeat = now + 1.0

    def load(self):
        with self.store.connection() as con:
            con.execute('BEGIN')
            channel = con.execute(f"SELECT * FROM {CHANNEL_TABLE} WHERE channel_id=?",
                                  (self.channel_id,)).fetchone()
            if channel is None:
                raise TransRuntimeError(f"转发通道 {self.channel_id} 不存在")
            spec = TransSpec.from_row(dict(channel))
            fields = _WIRE_FIELDS[spec.protocol]
            cur = con.cursor()
            cur.row_factory = None
            rows = tuple(cur.execute(f"SELECT {','.join(fields)} FROM {POINT_TABLE} "
                                     "WHERE channel_id=? AND enabled=1 ORDER BY pnt_no,kind", (self.channel_id,)))
        # Compare the effective wire definition directly. Display names, the
        # other protocol's settings and runtime heartbeats do not close peers.
        address = spec.common_address if spec.protocol == 'iec104' else spec.unit_id
        signature = (spec.protocol, spec.bind_host, spec.bind_port, address,
                     tuple(spec.protocol_config.as_dict().items()), rows)
        if self._definition_cache is None or self._definition_cache[0] != signature:
            points = tuple(TransPoint.from_row(dict(zip(fields, row))) for row in rows)
            self._definition_cache = (signature, points)
        signature, points = self._definition_cache
        return spec, list(points), signature

    @staticmethod
    def _source_plan(points):
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yk": "fes_yk_define", "yt": "fes_yt_define"}
        grouped = {}
        for p in points:
            targets = grouped.setdefault((tables[p.kind], p.source_rtu_id), {})
            targets.setdefault(p.source_pnt_no, []).append((p.kind, p.pnt_no))
        plan = []
        for (table, rtu), targets in grouped.items():
            numbers = sorted(targets)
            # Sequential point numbers form one bounded primary-key range.
            # Sparse mappings still use exact lookups; never read intervening rows.
            projection = f"SELECT pnt_no,value,status,refresh_time FROM {table} WHERE rtu_id=? AND "
            if len(numbers) > 1 and numbers[-1] - numbers[0] + 1 == len(numbers):
                plan.append((projection + "pnt_no BETWEEN ? AND ?", (rtu, numbers[0], numbers[-1]), targets))
                continue
            # Leave room for the RTU parameter on SQLite builds with a 999
            # variable limit. Multiple destinations may share a source.
            for start in range(0, len(numbers), 900):
                batch = numbers[start:start+900]
                sql = projection + f"pnt_no IN ({','.join('?' for _ in batch)})"
                plan.append((sql, (rtu, *batch), targets))
        return tuple(plan)

    def _read_sources(self, plan, include_quality, include_timestamp=False):
        result = {}
        if not plan:
            return result
        with self.store.connection() as con:
            if len(plan) > 1:
                con.execute('BEGIN')  # all groups/chunks see the same committed scan
            cursor = con.cursor()
            cursor.row_factory = None
            for sql, args, targets in plan:
                for no, value, quality, refresh_time in cursor.execute(sql, args):
                    if value is not None:
                        sample = (value, int(quality or 0)) if include_quality else value
                        if include_timestamp:
                            sample = (*sample, int(refresh_time or 0)) if isinstance(sample, tuple) else (
                                sample, int(refresh_time or 0)
                            )
                        for key in targets[no]:
                            result[key] = sample
        return result

    def source_values(self, points, *, include_quality=False, include_timestamp=False):
        return self._read_sources(self._source_plan(points), include_quality, include_timestamp)

    @staticmethod
    def _sample_parts(sample, default=(0, 128, 0)):
        if sample is None:
            return default
        values = tuple(sample) if isinstance(sample, tuple) else (sample,)
        return (
            values[0] if len(values) > 0 else default[0],
            int(values[1]) if len(values) > 1 else default[1],
            int(values[2] or 0) if len(values) > 2 else default[2],
        )

    def _queue_point_runtime(self, key, **updates):
        allowed = set(_POINT_RUNTIME_FIELDS)
        updates = {name: value for name, value in updates.items() if name in allowed}
        if not updates:
            return
        current = dict(self._point_runtime_state.get(key, {}))
        current.update(self._point_runtime_pending.get(key, {}))
        changed = {name: value for name, value in updates.items()
                   if current.get(name) != value}
        if changed:
            self._point_runtime_pending.setdefault(key, {}).update(changed)

    def _flush_point_runtime(self):
        if not self._point_runtime_pending:
            return True
        pending = {key: dict(values) for key, values in self._point_runtime_pending.items()}
        try:
            with self.store.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                for (kind, point_no), values in pending.items():
                    assignments = ", ".join(f'"{name}"=?' for name in values)
                    con.execute(
                        f'UPDATE {POINT_TABLE} SET {assignments} '
                        'WHERE channel_id=? AND pnt_no=? AND kind=?',
                        [*values.values(), self.channel_id, point_no, kind],
                    )
                con.commit()
        except Exception as exc:
            self._runtime_error(exc)
            self.logger.error(f"写入转发点实时状态失败: {exc}")
            return False
        for key, values in pending.items():
            self._point_runtime_state.setdefault(key, {}).update(values)
            current = self._point_runtime_pending.get(key)
            if current is not None:
                for name, value in values.items():
                    if current.get(name) == value:
                        current.pop(name, None)
                if not current:
                    self._point_runtime_pending.pop(key, None)
        return True

    def _record_sample(self, key, value, quality, source_time=0):
        sample = (value, int(quality), int(source_time or 0))
        if self._sample_seen.get(key) == sample:
            return False
        self._sample_seen[key] = sample
        self._queue_point_runtime(
            key, value=value, status=int(quality),
            refresh_time=sample[2] or now_ms(),
        )
        return True

    def _writer_client(self):
        if self.writer is None:
            try:
                from scdfe_c.writer_ipc import WriterClient
            except ImportError as exc:
                raise TransRuntimeError("scdfe_c 写库 IPC 不可用") from exc
            self.writer = WriterClient(self.db_path)
        return self.writer

    def command(self, p: TransPoint, engineering_value):
        """Submit an already decoded business value from either transport.

        Modbus owns its register scale/offset conversion. IEC104 callbacks
        already contain engineering values and normalized single/double states.
        """
        received_at = now_ms()
        try:
            value = float(engineering_value)
            if not math.isfinite(value):
                raise ValueError("命令值必须是有限数字")
            if p.kind == "yk":
                if value not in (0, 1):
                    raise ValueError("遥控值只能为 0 或 1")
                value = int(value)
            self._writer_client().submit_command(p.source_rtu_id, p.kind,
                                                 p.source_pnt_no, value, timeout=5)
        except Exception as exc:
            self._runtime_error(exc)
            try:
                self.logger.error(f"转发命令写库失败: {exc}")
            except OSError:
                logging.getLogger(__name__).exception("记录转发命令失败日志时出错")
            return False
        # The writer has committed. A logging failure must not turn its
        # confirmation into a protocol rejection and invite a duplicate command.
        try:
            self.logger.normal("收到命令并提交写库")
        except OSError:
            logging.getLogger(__name__).exception("已提交转发命令，但未写入日志")
        self._command_count += 1
        self.values[(p.kind, p.pnt_no)] = value
        self.qualities[(p.kind, p.pnt_no)] = 0
        self._queue_point_runtime(
            (p.kind, p.pnt_no), value=value, status=0,
            refresh_time=received_at, command_receive_time=received_at,
        )
        self._flush_point_runtime()
        return True

    def build_server(self, spec, points):
        self._flush_point_runtime()
        self.values.clear()
        self.qualities.clear()
        self._sample_seen.clear()
        self._point_runtime_state.clear()
        self._point_runtime_pending.clear()
        self.points = {(p.kind, p.pnt_no): p for p in points}
        self._scan_plan = self._source_plan(p for p in points if p.kind in {'yc', 'yx'})
        initial_samples = self.source_values(points, include_quality=True, include_timestamp=True)
        initial = {key: self._sample_parts(sample)[0] for key, sample in initial_samples.items()}
        if spec.protocol == "iec104":
            from server.iec104_server import IEC104Server
            server = IEC104Server(ip=spec.bind_host, port=spec.bind_port,
                                  common_address=spec.common_address, tick_rate_ms=50,
                                  **spec.protocol_config.as_dict())
            server.set_packet_handler(self.logger.packet)
            for key, p in self.points.items():
                handler = (lambda event, point=p: self.command(point, event.value)) if p.kind in {"yk", "yt"} else None
                value, quality, source_time = self._sample_parts(initial_samples.get(key))
                try:
                    server.add_point(p.kind, p.pnt_no, data_type=p.type_id, value=value,
                                     io_address=p.io_address, time_tag=p.time_tag,
                                     command_mode=p.command_mode,
                                     command_handler=handler)
                except (TypeError, ValueError, OverflowError, IndexError) as exc:
                    # A malformed source value must not take down unrelated
                    # transmission channels. Keep the configured type and expose
                    # a neutral value until scdfe_c publishes a valid sample.
                    self.logger.error(f"IEC 初值无效 {p.kind}/{p.pnt_no}: {exc}")
                    value, quality = 0, quality | 128
                    server.add_point(p.kind, p.pnt_no, data_type=p.type_id, value=0,
                                     io_address=p.io_address, command_mode=p.command_mode,
                                     command_handler=handler)
                if p.kind in {"yc", "yx"}:
                    server.set_value(p.kind, p.pnt_no, value, quality=quality)
                    self.values[key], self.qualities[key] = value, quality
                self._record_sample(key, value, quality, source_time)
            server.set_monitor_report_interval(spec.report_interval_ms)
            # Keep the constructed server as the active target as well.  This
            # makes the method useful for callers that build a server for
            # diagnostics before invoking ``rebuild``.
            self.server = server
            self._flush_point_runtime()
            return server
        from server.modbus_trans_server import ModbusTcpTransServer
        point_index = self.points
        def on_command(kind, no, value):
            point = point_index.get((str(kind).lower(), int(no)))
            return False if point is None else self.command(point, value)
        server = ModbusTcpTransServer(
            host=spec.bind_host, port=spec.bind_port, unit_id=spec.unit_id,
            points=[p.__dict__ for p in points], point_values=initial,
            command_handler=on_command,
            log=lambda level, message: self.logger.emit("normal", message, level=level))
        server.set_packet_handler(self.logger.packet)
        # The constructor already encoded the initial samples into the datastore.
        for key, sample in initial_samples.items():
            value, quality, source_time = self._sample_parts(sample)
            if key[0] in {"yc", "yx"}:
                self.values[key], self.qualities[key] = value, quality
            self._record_sample(key, value, quality, source_time)
        self._flush_point_runtime()
        self.server = server
        return server

    def rebuild(self, spec, points, signature=None):
        if self.server is not None:
            try:
                (self.server.stop() if hasattr(self.server, "stop") else self.server.close())
            except Exception as exc:
                self.logger.error(f"停止转发服务失败: {exc}")
        self.server = self.build_server(spec, points)
        try:
            self.server.start()
        except Exception:
            if hasattr(self.server, "close"):
                self.server.close()
            raise
        # Direct callers retain the historical behaviour and get an exact
        # database signature. The run loop passes the already loaded definition so
        # it does not issue a second full definition query after rebuilding.
        self.signature = signature if signature is not None else self.load()[2]
        self._runtime_protocol = spec.protocol
        self._reconnect_count += 1
        self.logger.process(f"转发通道启动，规约={spec.protocol}")

    def refresh(self):
        current = self._read_sources(self._scan_plan, True, True)
        if current:
            self._rx_count += len(current)
            self._last_rx_time = now_ms()
        if getattr(type(self.server), "update_many", None) is not None:
            changes = []
            for key, sample in current.items():
                value, quality, source_time = self._sample_parts(sample)
                self._record_sample(key, value, quality, source_time)
                if self.values.get(key) != value:
                    changes.append((*key, value))
                else:
                    self.qualities[key] = quality  # Modbus does not transmit quality bits.
            for key, point in self.points.items():
                if point.kind not in {"yc", "yx"} or key in current:
                    continue
                value = self.values.get(key, 0)
                quality = self.qualities.get(key, 0) | 128
                self.qualities[key] = quality
                self._record_sample(key, value, quality, 0)
            if changes:
                try:
                    errors = self.server.update_many(changes)
                except Exception as exc:
                    self._runtime_error(exc)
                    self.logger.error(f"Modbus 扫描发布未确认: {exc}")
                    self._flush_point_runtime()
                    return  # Retry unconfirmed values on the next scan.
                for kind, no, value in changes:
                    key = (kind, no)
                    if key in errors:
                        self._runtime_error(errors[key])
                        self.logger.error(f"转发点 {kind}/{no} 更新失败: {errors[key]}")
                    else:
                        sample = self._sample_parts(current[key])
                        self.values[key], self.qualities[key] = sample[0], sample[1]
                        self._queue_point_runtime(key, forward_time=now_ms())
                        self._tx_count += 1
                        self._last_tx_time = now_ms()
            self._flush_point_runtime()
            return
        for key, point in self.points.items():
            if point.kind not in {"yc", "yx"}:
                continue
            if key in current:
                value, quality, source_time = self._sample_parts(current[key])
            elif hasattr(self.server, "set_value"):
                # Missing/NULL source: hold the last published value and mark
                # it invalid. Modbus registers have no protocol quality bit.
                value, quality = self.values.get(key, 0), self.qualities.get(key, 0) | 128
                source_time = 0
            else:
                continue
            self._record_sample(key, value, quality, source_time)
            # Avoid sending an IEC spontaneous frame on every scan when the
            # source value is unchanged.  Modbus still receives the changed
            # snapshot in its datastore and is cheap, but this guard keeps the
            # two transports consistent and reduces CPU/network traffic.
            if self.values.get(key) == value and self.qualities.get(key) == quality:
                continue
            try:
                if hasattr(self.server, "set_value"):
                    # Retained values may be unchanged while invalid/blocked
                    # quality changes. IEC104 must send those changes too.
                    # Without an active peer, only update the station cache.
                    # A later general interrogation reads that latest value;
                    # the lack of a recipient is not a transmission failure.
                    self.server.set_value(point.kind, point.pnt_no, value,
                                          quality=quality,
                                          transmit=self.server.active_connection_count > 0)
                else:
                    # Modbus has no quality field. Keep it in the local sample
                    # metadata without inventing additional wire registers.
                    if self.values.get(key) != value:
                        self.server.update(point.kind, point.pnt_no, value)
                self.values[key] = value
                self.qualities[key] = quality
                self._queue_point_runtime(key, forward_time=now_ms())
                self._tx_count += 1
                self._last_tx_time = now_ms()
            except Exception as exc:
                self._runtime_error(exc)
                self.logger.error(f"转发点更新失败: {exc}")
        self._flush_point_runtime()

    def _enabled(self):
        if not self.require_enabled:
            return True
        with self.store.connection() as con:
            row = con.execute(f"SELECT process_enable FROM {CHANNEL_TABLE} WHERE channel_id=?",
                              (self.channel_id,)).fetchone()
        return row is not None and bool(row[0])

    def _cleanup_logs(self, hours):
        now = time.monotonic()
        if hours == self._log_retention and now < self._next_cleanup:
            return
        from scdfe_c.web.log_query import LOG_PATTERNS, _iter_log_files
        self._log_retention, self._next_cleanup = hours, now + 3600
        root = self.logger.directory.resolve()
        prefix = f"scdfe_t-channel-{self.channel_id}-"
        cutoff = time.time() - hours * 3600
        for path, _, _, _, _ in _iter_log_files(root, None, LOG_PATTERNS['t'], prefix):
            try:
                info = os.stat(path, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff and Path(path).resolve().is_relative_to(root):
                    os.unlink(path)
            except OSError:
                continue  # rotation or an open reader may win; retry next hour

    def run(self, max_cycles=None):
        # The running loop owns the store until its finally block below. Keep
        # one idle connection during this lifetime, including for embedded
        # callers; closing the last WAL connection on each heartbeat can make
        # the stop path wait for a checkpoint. Standalone load/refresh calls
        # retain their original short-lived connections.
        self.store.connection_pool_size = 1
        try:
            with self.logger.session():
                return self._run(max_cycles)
        finally:
            self.store.close()

    def _run(self, max_cycles):
        cycles = 0
        self._next_heartbeat = 0.0
        # A stop may arrive after the Web spawned us but before initialization.
        # Never turn that stop into another start or resurrect the stopped row.
        if self.stop_event.is_set() or not self._enabled():
            return cycles
        try:
            now = int(time.time() * 1000)
            self._update_state(process_status=1, process_pid=os.getpid(), process_start_time=now,
                               process_heartbeat_time=now, process_error="")
            spec, points, sig = self.load()
            self.rebuild(spec, points, signature=sig)
            self._next_definition_check = time.monotonic() + self.definition_check_interval
            last_scan = None
            next_control = 0.0
            while not self.stop_event.is_set():
                monotonic_now = time.monotonic()
                if monotonic_now >= next_control:
                    if not self._enabled():
                        break
                    next_control = monotonic_now + .2
                if monotonic_now >= self._next_definition_check:
                    current_spec, current_points, current_sig = self.load()
                    self._next_definition_check = time.monotonic() + self.definition_check_interval
                    if current_sig != self.signature:
                        self.logger.process("检测到转发定义变化，重新加载")
                        self.rebuild(current_spec, current_points, signature=current_sig)
                        last_scan = None
                    elif current_spec.protocol == "iec104" and current_spec.report_interval_ms != spec.report_interval_ms:
                        self.server.set_monitor_report_interval(current_spec.report_interval_ms)
                    spec = current_spec  # interval edits affect scheduling, not the socket
                self._cleanup_logs(spec.log_retention_hours)
                if last_scan is None or time.monotonic() >= last_scan + spec.scan_interval:
                    self.refresh()
                    last_scan = time.monotonic()
                    cycles += 1
                    self._cycles = cycles
                self._heartbeat()
                if max_cycles is not None and cycles >= max_cycles:
                    break
                # Definition polling and cooperative stop/heartbeat continue
                # between scans. A new shorter period uses the last real scan
                # time rather than waiting out the former period or rescanning
                # on every configuration poll.
                wake = min(next_control, self._next_definition_check, last_scan + spec.scan_interval)
                self.stop_event.wait(max(0., wake - time.monotonic()))
            return cycles
        except Exception as exc:
            self.failed = True
            self._runtime_error(exc)
            self._update_state(process_status=5, process_pid=0, process_stop_time=int(time.time() * 1000),
                               process_exit_code=-1, process_error=str(exc))
            self.logger.error(f"转发通道异常退出: {exc}")
            raise
        finally:
            if self.server is not None:
                try:
                    (self.server.stop() if hasattr(self.server, "stop") else self.server.close())
                except Exception as exc:
                    self.logger.error(f"停止转发服务失败: {exc}")
            if not self.failed:
                self._update_state(process_status=0, process_pid=0, process_stop_time=int(time.time() * 1000),
                                   process_heartbeat_time=int(time.time() * 1000), process_exit_code=0)
            self.logger.process("转发通道停止")
            if self.writer is not None and hasattr(self.writer, "close"):
                self.writer.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="scdfe_t 四遥转发通道")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--channel-id", dest="channel_id", type=int, required=True)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--managed-start", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.channel_id <= 0 or args.max_cycles is not None and args.max_cycles <= 0:
        parser.error("channel-id/max-cycles 必须为正整数")
    event = threading.Event()
    def stop(*_): event.set()
    lock = InstanceLock(args.db, role=f"trans-{args.channel_id}")
    control = InstanceLock(args.db, role=f"trans-control-{args.channel_id}")
    control_owned = False
    runtime = None
    old = {}
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            old[sig] = signal.signal(sig, stop)
    try:
        # Explicit CLI starts serialize their enable write with Web controls.
        # A Web child never writes enable and must not wait on the control lock:
        # stop holds that lock while waiting for this child to exit.
        deadline = time.monotonic() + 5.0
        while not args.managed_start and not event.is_set():
            if control.acquire():
                control_owned = True
                break
            if time.monotonic() >= deadline:
                raise TransRuntimeError("转发通道正在执行其他进程操作")
            event.wait(.05)
        if not args.managed_start and not control_owned:
            return 0
        try:
            if not lock.acquire():
                raise TransRuntimeError(f"scdfe_t 通道 {args.channel_id} 已有运行实例")
            runtime = TransRuntime(args.db, args.schema_dir, args.channel_id, log_dir=args.log_dir,
                                   stop_event=event, require_enabled=True)
            with runtime.store.connection() as con:
                row = con.execute(f"SELECT process_enable FROM {CHANNEL_TABLE} WHERE channel_id=?",
                                  (args.channel_id,)).fetchone()
            if row is None:
                raise TransRuntimeError(f"转发通道 {args.channel_id} 不存在")
            if args.managed_start:
                if not row[0]:
                    return 0
            else:
                runtime.store.update_row(CHANNEL_TABLE, [args.channel_id], {"process_enable": 1})
        finally:
            if control_owned:
                control.release()
                control_owned = False
        runtime.run(args.max_cycles)
        return 0
    except (OSError, ValueError, sqlite3.Error, TransRuntimeError) as exc:
        print(f"scdfe_t: {exc}", flush=True)
        return 2
    finally:
        if runtime is not None:
            runtime.store.close()
        if control_owned:
            control.release()
        for sig, handler in old.items(): signal.signal(sig, handler)
        lock.release()


__all__ = ["TransRuntime", "TransRuntimeError", "TransPoint", "TransSpec", "main"]

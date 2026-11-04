"""Runtime engine for scdfe_t four-remote transmission channels.

Each channel is run by one process.  A channel publishes selected YC/YX rows
through IEC104 or Modbus-TCP and accepts YK/YT writes.  Writes are submitted to
the scdfe_c writer IPC endpoint, so the transmission process never competes for
the SQLite write lock.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time
from collections.abc import Mapping
from typing import Any

from scdfe_c.web.service import DEFAULT_DB, SCHEMA_DIR, ScadaStore
from core.scdfe_control import InstanceLock
from scdfe_c import ScdfeLogger

CHANNEL_TABLE = "fes_scdfe_t_define"
POINT_TABLE = "fes_scdfe_t_point_define"
KINDS = {"yc", "yx", "yk", "yt"}


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

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TransSpec":
        protocol = str(_get(row, "protocol", "")).lower()
        if protocol not in {"iec104", "modbus-tcp"}:
            raise ValueError("转发规约只能是 iec104 或 modbus-tcp")
        port = _get(row, "bind_port", 2404 if protocol == "iec104" else 1502)
        return cls(int(row["channel_id"]), protocol,
                   str(_get(row, "bind_host", "0.0.0.0")), int(port),
                   int(_get(row, "common_address", 1)), int(_get(row, "unit_id", 1)),
                   max(.05, _float(_get(row, "scan_interval_ms", 200), 200) / 1000))


@dataclass(frozen=True)
class TransPoint:
    channel_id: int
    pnt_no: int
    kind: str
    source_kind: str
    source_rtu_id: int
    source_pnt_no: int
    enabled: bool
    io_address: int | None
    type_id: int | None
    command_mode: str
    register_area: str
    address: int
    data_type: str
    byte_order: str
    word_order: str
    scale: float
    offset: float

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TransPoint":
        kind = str(_get(row, "kind", "")).lower()
        source_kind = str(_get(row, "source_kind", kind)).lower()
        if kind not in KINDS or source_kind not in KINDS:
            raise ValueError("转发点类型必须是 yc/yx/yk/yt")
        io = _get(row, "io_address")
        typ = _get(row, "type_id")
        return cls(int(row["channel_id"]), int(row["pnt_no"]), kind, source_kind,
                   int(_get(row, "source_rtu_id", 0)),
                   int(_get(row, "source_pnt_no", row["pnt_no"])),
                   bool(_get(row, "enabled", 1)),
                   None if io in (None, 0) else int(io), None if typ in (None, 0) else int(typ),
                   str(_get(row, "command_mode", "DIRECT")),
                   str(_get(row, "register_area", "holding")).lower(),
                   int(_get(row, "address", int(row["pnt_no"]) - 1)),
                   str(_get(row, "data_type", "float32")).lower(),
                   str(_get(row, "byte_order", "big")).lower(),
                   str(_get(row, "word_order", "big")).lower(),
                   _float(_get(row, "scale", 1), 1), _float(_get(row, "offset", 0), 0))


class TransRuntimeError(RuntimeError):
    pass


class TransRuntime:
    def __init__(self, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR, channel_id=1,
                 *, log_dir=None, stop_event=None):
        self.db_path = Path(db_path).resolve()
        self.schema_dir = Path(schema_dir).resolve()
        self.channel_id = int(channel_id)
        self.store = ScadaStore(self.db_path, self.schema_dir)
        self.stop_event = stop_event or threading.Event()
        self.logger = ScdfeLogger(Path(log_dir).resolve() if log_dir else self.db_path.parent / "logs",
                                  process_name=f"scdfe_t-channel-{self.channel_id}")
        self.server = None
        self.points: dict[tuple[str, int], TransPoint] = {}
        self.values: dict[tuple[str, int], Any] = {}
        self.signature = ""
        self.writer = None
        self.failed = False
        # Definition files are operator configuration, not live samples.
        # Avoid reloading and hashing every point on every fast scan; a one
        # second check keeps online edits responsive while removing repeated
        # SQLite/materialisation work from the hot path.
        self.definition_check_interval = 1.0
        self._next_definition_check = 0.0

    def _update_state(self, **values):
        try:
            self.store.update_row(CHANNEL_TABLE, [self.channel_id], values)
        except Exception as exc:
            self.logger.error(f"写入转发进程状态失败: {exc}")

    def load(self):
        with self.store.connection() as con:
            channel = con.execute(f"SELECT * FROM {CHANNEL_TABLE} WHERE channel_id=?", (self.channel_id,)).fetchone()
            if channel is None:
                raise TransRuntimeError(f"转发通道 {self.channel_id} 不存在")
            channel = dict(channel)
            rows = [dict(r) for r in con.execute(
                f"SELECT * FROM {POINT_TABLE} WHERE channel_id=? AND enabled=1 ORDER BY pnt_no", (self.channel_id,))]
        spec = TransSpec.from_row(channel)
        points = [TransPoint.from_row(r) for r in rows]
        sig = self._definition_signature(channel, rows)
        return spec, points, sig

    def source_values(self, points):
        tables = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yk": "fes_yk_define", "yt": "fes_yt_define"}
        # Read all mapped points in one query per source table/RTU.  A
        # per-point SELECT makes a large transmission channel spend most of its
        # scan period in SQLite cursor setup and parsing.
        result = {}
        grouped: dict[tuple[str, int], list[TransPoint]] = {}
        for point in points:
            grouped.setdefault((tables[point.source_kind], point.source_rtu_id), []).append(point)
        with self.store.connection() as con:
            for (table, rtu_id), rows in grouped.items():
                point_nos = sorted({int(p.source_pnt_no) for p in rows})
                if not point_nos:
                    continue
                placeholders = ",".join("?" for _ in point_nos)
                fetched = con.execute(
                    f"SELECT pnt_no, value FROM {table} WHERE rtu_id=? AND pnt_no IN ({placeholders})",
                    [rtu_id, *point_nos],
                ).fetchall()
                values = {int(row["pnt_no"]): row["value"] for row in fetched}
                for p in rows:
                    value = values.get(int(p.source_pnt_no))
                    if value is not None:
                        result[(p.kind, p.pnt_no)] = value
        return result

    @staticmethod
    def _definition_signature(channel, rows):
        """Hash only operator definitions, never process heartbeat fields.

        The worker updates ``process_heartbeat_time`` on every scan.  Hashing
        those runtime columns causes an unnecessary protocol-server rebuild on
        every heartbeat and disconnects remote clients.
        """
        runtime = {
            "process_enable", "process_status", "process_pid", "process_start_time",
            "process_stop_time", "process_heartbeat_time", "process_exit_code",
            "process_error",
        }
        stable_channel = {key: value for key, value in channel.items() if key not in runtime}
        return hashlib.sha256(
            json.dumps([stable_channel, rows], sort_keys=True, default=str).encode()
        ).hexdigest()

    def _writer_client(self):
        if self.writer is None:
            try:
                from scdfe_c.writer_ipc import WriterClient
            except ImportError as exc:
                raise TransRuntimeError("scdfe_c 写库 IPC 不可用") from exc
            self.writer = WriterClient(self.db_path)
        return self.writer

    def command(self, p: TransPoint, wire_value):
        try:
            value = (float(wire_value) - p.offset) / p.scale if p.scale else float(wire_value)
            if p.source_kind == "yk":
                value = int(bool(wire_value))
            self._writer_client().submit_command(p.source_rtu_id, p.source_kind,
                                                 p.source_pnt_no, value, timeout=5)
            self.logger.normal("收到命令并提交写库")
            return True
        except Exception as exc:
            self.logger.error(f"转发命令写库失败: {exc}")
            return False

    def build_server(self, spec, points):
        self.points = {(p.kind, p.pnt_no): p for p in points}
        initial = self.source_values(points)
        if spec.protocol == "iec104":
            from server.iec104_server import IEC104Server
            server = IEC104Server(ip=spec.bind_host, port=spec.bind_port,
                                  common_address=spec.common_address)
            for key, p in self.points.items():
                handler = (lambda event, point=p: self.command(point, event.value)) if p.kind in {"yk", "yt"} else None
                try:
                    server.add_point(p.kind, p.pnt_no, data_type=p.type_id, value=initial.get(key, 0),
                                     io_address=p.io_address, command_mode=p.command_mode,
                                     command_handler=handler)
                except (TypeError, ValueError, OverflowError, IndexError) as exc:
                    # A malformed source value must not take down unrelated
                    # transmission channels. Keep the configured type and expose
                    # a neutral value until scdfe_c publishes a valid sample.
                    self.logger.error(f"IEC 初值无效 {p.kind}/{p.pnt_no}: {exc}")
                    server.add_point(p.kind, p.pnt_no, data_type=p.type_id, value=0,
                                     io_address=p.io_address, command_mode=p.command_mode,
                                     command_handler=handler)
            # Keep the constructed server as the active target as well.  This
            # makes the method useful for callers that build a server for
            # diagnostics before invoking ``rebuild``.
            self.server = server
            return server
        from server.modbus_trans_server import ModbusTcpTransServer
        source_index = {(p.kind, p.source_pnt_no): p for p in points}
        def on_command(kind, no, value):
            point = source_index.get((str(kind).lower(), int(no)))
            return False if point is None else self.command(point, value)
        server = ModbusTcpTransServer(
            host=spec.bind_host, port=spec.bind_port, unit_id=spec.unit_id,
            points=[p.__dict__ for p in points], point_values=initial,
            command_handler=on_command,
            log=lambda level, message: self.logger.emit("normal", message, level=level))
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
        # database signature. The run loop passes the already loaded digest so
        # it does not issue a second full definition query after rebuilding.
        self.signature = signature if signature is not None else self.load()[2]
        self.logger.process(f"转发通道启动，规约={spec.protocol}")

    def refresh(self):
        current = self.source_values(list(self.points.values()))
        for key, point in self.points.items():
            if point.kind not in {"yc", "yx"} or key not in current:
                continue
            value = current[key]
            # Avoid sending an IEC spontaneous frame on every scan when the
            # source value is unchanged.  Modbus still receives the changed
            # snapshot in its datastore and is cheap, but this guard keeps the
            # two transports consistent and reduces CPU/network traffic.
            if self.values.get(key) == value:
                continue
            try:
                if hasattr(self.server, "update"):
                    self.server.update(point.kind, point.pnt_no, value)
                else:
                    self.server.set_value(point.kind, point.pnt_no, value, transmit=True)
                self.values[key] = value
            except Exception as exc:
                self.logger.error(f"转发点更新失败: {exc}")

    def run(self, max_cycles=None):
        cycles = 0
        try:
            now = int(time.time() * 1000)
            self._update_state(process_status=1, process_pid=os.getpid(), process_start_time=now,
                               process_heartbeat_time=now, process_error="")
            spec, points, sig = self.load()
            self.rebuild(spec, points, signature=sig)
            self._next_definition_check = time.monotonic() + self.definition_check_interval
            while not self.stop_event.is_set():
                # Definition reloads are deliberately rate limited. Source
                # values remain read every scan, while the larger channel/
                # point query and JSON digest run at most once per second.
                monotonic_now = time.monotonic()
                if monotonic_now >= self._next_definition_check:
                    current_spec, current_points, current_sig = self.load()
                    self._next_definition_check = monotonic_now + self.definition_check_interval
                    if current_sig != self.signature:
                        self.logger.process("检测到转发定义变化，重新加载")
                        self.rebuild(current_spec, current_points, signature=current_sig)
                        spec, points = current_spec, current_points
                self.refresh()
                self._update_state(process_status=2, process_pid=os.getpid(),
                                   process_heartbeat_time=int(time.time() * 1000))
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                self.stop_event.wait(spec.scan_interval)
            return cycles
        except Exception as exc:
            self.failed = True
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
    args = parser.parse_args(argv)
    if args.channel_id <= 0 or args.max_cycles is not None and args.max_cycles <= 0:
        parser.error("channel-id/max-cycles 必须为正整数")
    event = threading.Event()
    def stop(*_): event.set()
    lock = InstanceLock(args.db, role=f"trans-{args.channel_id}")
    if not lock.acquire():
        print(f"scdfe_t 通道 {args.channel_id} 已有运行实例", flush=True)
        return 2
    old = {}
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            old[sig] = signal.signal(sig, stop)
    try:
        TransRuntime(args.db, args.schema_dir, args.channel_id, log_dir=args.log_dir, stop_event=event).run(args.max_cycles)
        return 0
    except (ValueError, sqlite3.Error, TransRuntimeError) as exc:
        print(f"scdfe_t: {exc}", flush=True)
        return 2
    finally:
        for sig, handler in old.items(): signal.signal(sig, handler)
        lock.release()


__all__ = ["TransRuntime", "TransRuntimeError", "TransPoint", "TransSpec", "main"]

"""SCADA ``scdfe_c`` 通道管理进程。

该模块是旧版 ``scdfe_c`` 的 Python 运行时替代层。父进程扫描
``fes_rtu_define``，每个 ``(rtu_id, channel_id)`` 启动一个独立通信线程；线程
加载当前协议主站，周期采集遥测/遥信并写回 SQLite，同时检测遥控/遥调命令
的变化并下发。主调度线程监视各通信线程和通道定义指纹，定义变化时平滑停止
旧线程并重新加载点表。数据库批量写入仍由独立进程负责，避免多个通信线程争抢
SQLite 写锁。

默认启动方式：

    python -m scdfe_c --db D:\\codex\\scada\\db\\scada.sqlite3

每个通信线程独占自己的协议主站和 SQLite 连接。自动通信交给独立写库进程提交；
单 RTU 调试复用线程连接执行短事务。SQLite 保持 WAL 和 busy_timeout，连接不跨线程共享。
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import logging
import math
import multiprocessing as mp
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
from typing import Any
from queue import Empty, Full, Queue as ThreadQueue

from core.scdfe_control import (
    CONTROL_TABLE,
    ServerInstanceLock,
    LOG_PATTERN as LOG_FILE_RE,
    LOG_LEVEL_LABELS,
    sanitize_log_message,
    ensure_control_row,
    log_directory,
)
from core.log_paths import log_entries, packet_log_directory
from core.process_registry import PROCESS_TABLE, ProcessLease, ensure_process
from core.runtime_status import RuntimeStatusStore, now_ms, state_for
from .web.service import CHANNEL_PARAMETER_TABLES, DEFAULT_DB, SCHEMA_DIR, ScadaStore, quote_identifier
from .log_context import channel_context, channel_identity
from .writer_ipc import (
    WriterClient, WriterIpcServer, WriterRequest, WriterRequestError, WriterUnavailable,
)

__all__ = [
    "ChannelKey",
    "ChannelSpec",
    "ChannelWorker",
    "ScdfeLogger",
    "ScdfeSupervisor",
    "build_master",
    "definition_signature",
    "definition_version_token",
    "command_version_token",
    "discover_channels",
    "run_channel_debug",
    "RTU_AUTO_COMMUNICATION_FIELD", "RTU_AUTO_COMMUNICATION_ON",
    "RTU_AUTO_COMMUNICATION_OFF",
    "main",
    "WriterClient", "WriterIpcServer", "WriterRequestError", "WriterUnavailable",
    "CommandClient",
    "PROCESS_STOPPED", "PROCESS_STARTING", "PROCESS_RUNNING",
    "PROCESS_STOPPING", "PROCESS_RECOVERING", "PROCESS_ERROR",
]


SUPPORTED_CHANNEL_TYPES = frozenset(CHANNEL_PARAMETER_TABLES)
# ``fes_rtu_define.process_enable`` is the RTU-level automatic communication
# flag.  It is deliberately reused instead of adding a second enable column:
# 1 = scdfe_c owns and guards this RTU, 0 = scdfe_c leaves it alone and an
# operator may run ``--rtu-id`` debug mode.
RTU_AUTO_COMMUNICATION_FIELD = "process_enable"
RTU_AUTO_COMMUNICATION_ON = 1
RTU_AUTO_COMMUNICATION_OFF = 0
LOG_CATEGORIES = ("normal", "alarm", "error", "process")
BATCH_WRITER_STOP = "__scdfe_c_batch_writer_stop__"
BATCH_WRITER_DRAIN = "__scdfe_c_batch_writer_drain__"
BATCH_WRITER_INTERVAL = 0.1
BATCH_WRITER_MAX_BATCH = 500
BATCH_WRITER_WAL_AUTOCHECKPOINT = 10000
# SQLite has one WAL writer. Web edits, scdfe_t and the command service can
# briefly hold that lock; a transient busy result must not kill the dedicated
# writer process or lose the batch that is waiting to be committed.
BATCH_WRITER_LOCK_RETRIES = 8
BATCH_WRITER_LOCK_BACKOFF = 0.2
BATCH_WRITER_BUSY_TIMEOUT_MS = 1000
PROCESS_LOG_INTERVAL = 30.0
LOG_CLEANUP_INTERVAL = 60.0
WORKER_HEARTBEAT_INTERVAL = 5.0
WORKER_DEFINITION_CHECK_INTERVAL = 2.0
MEASUREMENT_HEARTBEAT_INTERVAL = 5.0
# Third-party protocol ``connect()`` calls can take longer than the normal
# retry interval (for example a TCP connect timeout).  Never shorten the
# supervisor's join window merely because a live configuration changed the
# retry interval to a small value.
WORKER_STOP_TIMEOUT = 5.0
_MEASUREMENT_SHAPES = {
    ("yc", False): ("fes_yc_define", (
        "raw_value", "raw_status", "raw_time", "value", "status", "refresh_time",
    ), (0, 2, 3, 1, 2, 3)),
    ("yx", False): ("fes_yx_define", (
        "value", "status", "refresh_time",
    ), (0, 1, 2)),
    ("yc", True): ("fes_yc_define", ("refresh_time",), (0,)),
    ("yx", True): ("fes_yx_define", ("refresh_time",), (0,)),
}
_SIGNATURE_PROJECTION_CACHE: dict[
    tuple[str, str], tuple[tuple[str, ...], str, str]
] = {}

# fes_rtu_define.process_status 的约定。
PROCESS_STOPPED = 0
PROCESS_STARTING = 1
PROCESS_RUNNING = 2
PROCESS_STOPPING = 3
PROCESS_RECOVERING = 4
PROCESS_ERROR = 5
PROCESS_CONTROL_FIELDS = {
    "process_enable", "process_status", "process_pid", "process_start_time",
    "process_stop_time", "process_heartbeat_time", "process_exit_code", "process_error",
}


def _current_thread_id() -> int:
    """Return the native ID of the current communication thread."""
    native_id = getattr(threading.current_thread(), "native_id", None)
    return int(native_id or threading.get_ident())


def _thread_id(thread: threading.Thread) -> int:
    """Return a worker's native ID, including lightweight test threads."""
    native_id = getattr(thread, "native_id", None)
    return int(native_id or getattr(thread, "ident", 0) or 0)

# 这些字段是设备实时状态或命令运行字段。它们由 scdfe_c 自己更新，不能触发
# 通道重启；其余四遥字段仍然属于点表定义，修改后会重新加载主站。
RUNTIME_FIELDS = {
    "_version",
    "raw_value", "raw_status", "raw_time", "value", "status",
    "refresh_time",
    "refresh_time1", "refresh_time2", "refresh_time3",
    "cmd_time", "cmd_handle_time", "cmd_check_id",
    "man_disable_last_tm", "lock_begintime", "lock_holdtime",
    # Packet capture is a live diagnostic policy.  Its cached switch is read
    # by the worker while it is running, so changing it must not force a
    # protocol reconnect or reload the static point definition.
    "packet_recording",
    *PROCESS_CONTROL_FIELDS,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _field(row: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
    if isinstance(row, sqlite3.Row):
        try:
            value = row[name]
        except (IndexError, KeyError):
            value = None
        return default if value is None else value
    value = row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)
    return default if value is None else value


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


@dataclass(frozen=True, order=True)
class ChannelKey:
    """唯一通道标识。RTU 表已合并了通道信息，但仍保留 channel_id。"""

    rtu_id: int
    channel_id: int


@dataclass(frozen=True)
class ChannelSpec:
    """从 RTU 行提取出的、可安全传给通信线程的通道配置。"""

    rtu_id: int
    channel_id: int
    channel_type: str
    name: str | None = None
    process_enable: bool = True

    @property
    def key(self) -> ChannelKey:
        return ChannelKey(self.rtu_id, self.channel_id)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ChannelSpec":
        rtu_id = int(_field(row, "rtu_id"))
        channel_id = int(_field(row, "channel_id"))
        channel_type = _field(row, "channel_type", "")
        if not isinstance(channel_type, str):
            raise ValueError(f"RTU {rtu_id} 的 channel_type 必须是字符串")
        if not channel_type:
            raise ValueError(f"RTU {rtu_id} 缺少 channel_type")
        process_enable = int(_field(row, "process_enable", 1))
        if process_enable not in (0, 1):
            raise ValueError(f"RTU {rtu_id} 的 process_enable 必须是 0 或 1")
        return cls(
            rtu_id=rtu_id,
            channel_id=channel_id,
            channel_type=channel_type,
            name=_field(row, "name"),
            process_enable=bool(process_enable),
        )


@dataclass
class WorkerStats:
    cycles: int = 0
    updated_yc: int = 0
    updated_yx: int = 0
    sent_yk: int = 0
    sent_yt: int = 0
    errors: int = 0
    alarms: int = 0


class ScdfeLogger:
    """多线程/多进程安全的轻量日志写入器。

    每个逻辑进程独立使用一个按小时轮转的运行日志文件，例如
    ``2026/10/29/18/scdfe_c-main-20261029-18.log``。通信线程日志文件只使用
    RTU 编号，例如 ``2026/10/29/18/scdfe_c-rtu-7-20261029-18.log``。RTU 和通道编号
    由文件名与数据库定义关联；启停、连接、异常记录另外保留当时的通道上下文。
    日志类型使用中文标签，且不再
    按 normal/alarm/error/process 拆分文件。正文时间只写本地 ``HH:MM:SS``，
    日期和小时由文件名提供。普通调用追加后关闭文件；通信线程在 session 中复用
    本线程日志文件，每条记录仍立即 flush，退出时关闭。文件不在进程间共享。
    报文通过 ``packet_directory`` 写入同样的时间树和文件名，运行日志报文目录
    完全分离；未指定该参数的直接调用保留原目录行为。主调度线程按小时数定期清理旧文件。
    """

    def __init__(self, directory: Path | str, *, process_name: str, console: bool = False,
                 packet_switch=None, packet_directory: Path | str | None = None,
                 runtime_packet_compat: bool = False):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        # ``packet_directory`` is explicit for managed C/S/T runtimes.  Keep
        # the historical same-directory fallback for small direct callers and
        # older integrations that instantiate ScdfeLogger themselves.
        self.packet_directory = (self.directory if packet_directory is None
                                 else Path(packet_directory).resolve())
        self.packet_directory.mkdir(parents=True, exist_ok=True)
        self.console = console
        self.process_name = process_name
        self.packet_switch = packet_switch
        self.runtime_packet_compat = bool(runtime_packet_compat)
        self._write_lock = threading.Lock()
        self._hour_keys = {"runtime": None, "packet": None}
        self._paths = {"runtime": None, "packet": None}
        self._streams = {"runtime": None, "packet": None}
        self._stream_identities = {"runtime": None, "packet": None}
        self._sessions = 0

    @property
    def _stream(self):
        """Compatibility alias for the pre packet/runtime split logger."""
        return self._streams["runtime"]

    def _close_stream(self, kind=None):
        kinds = ("runtime", "packet") if kind is None else (kind,)
        for stream_kind in kinds:
            stream, self._streams[stream_kind] = self._streams[stream_kind], None
            self._stream_identities[stream_kind] = None
            if stream is None:
                continue
            stream.close()

    @contextmanager
    def session(self):
        """Reuse the handle while a worker runs; every write remains visible."""
        with self._write_lock:
            self._sessions += 1
        try:
            yield self
        finally:
            with self._write_lock:
                self._sessions -= 1
                if not self._sessions:
                    self._close_stream()

    def emit(self, category: str, message: str, *, level: str = "INFO") -> None:
        self._emit_lines(category, (message,), level=level)

    def _emit_lines(self, category, messages, *, level="INFO", raw_hex=False):
        if category not in LOG_CATEGORIES:
            raise ValueError(f"未知日志类别: {category}")
        label = LOG_LEVEL_LABELS.get(level)
        if label is None:
            raise ValueError(f"未知日志级别: {level}")
        kind = "packet" if level == "PACKET" else "runtime"
        root = self.packet_directory if kind == "packet" else self.directory
        with self._write_lock:
            # Only the local wall clock is persisted. Sample under the lock so
            # a queued callback cannot reopen the previous hour after rotation.
            now = datetime.now()
            stamp = f"{now.hour:02d}:{now.minute:02d}:{now.second:02d}"
            hour_key = (now.year, now.month, now.day, now.hour)
            if hour_key != self._hour_keys[kind]:
                self._close_stream(kind)
                directory = root / now.strftime("%Y/%m/%d/%H")
                if not directory.resolve().is_relative_to(root):
                    raise OSError("日志文件超出日志目录")
                directory.mkdir(parents=True, exist_ok=True)
                self._paths[kind] = directory / f"{self.process_name}-{now:%Y%m%d-%H}.log"
                self._hour_keys[kind] = hour_key
            path = self._paths[kind]
            stream = self._streams[kind]
            if stream is not None and os.name != 'nt':
                # Preserve external rotation/removal semantics, including on
                # POSIX where an open file can be unlinked or replaced. Windows
                # append handles deny deletion/rename until closed: checking
                # their path identity for every packet adds expensive I/O.
                try:
                    stat = path.stat()
                    identity = (stat.st_dev, stat.st_ino)
                except FileNotFoundError:
                    identity = None
                if identity != self._stream_identities[kind]:
                    self._close_stream(kind)
            try:
                if self._streams[kind] is None:
                    if not path.resolve().is_relative_to(root):
                        raise OSError("日志文件超出日志目录")
                    try:
                        self._streams[kind] = path.open("a", encoding="utf-8", newline="")
                    except FileNotFoundError:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        self._streams[kind] = path.open("a", encoding="utf-8", newline="")
                    stat = os.fstat(self._streams[kind].fileno())
                    self._stream_identities[kind] = (stat.st_dev, stat.st_ino)
                for message in messages:
                    if raw_hex:
                        # packet_lines generates the suffix solely from bytes.hex;
                        # identity/whitespace cleanup only needs the metadata.
                        prefix, _, hex_data = message.rpartition(' HEX=')
                        message = sanitize_log_message(prefix) + ' HEX=' + hex_data
                    else:
                        message = sanitize_log_message(message)
                    line = f"{stamp} [{label}] {message}\n"
                    self._streams[kind].write(line)
                    if self.console:
                        print(line, end="", flush=True)
                self._streams[kind].flush()
            except Exception:
                self._close_stream()
                raise
            finally:
                if not self._sessions:
                    self._close_stream()

    def normal(self, message: str) -> None:
        self.emit("normal", message)

    def business_lines(self, lines):
        """Flush a frame/change batch once while retaining every point record."""
        self._emit_lines('normal', lines)

    def packet(self, direction, data, detail="") -> None:
        if self.packet_switch is not None and not self.packet_switch.enabled():
            return
        from core.packet_trace import packet_lines
        lines = tuple(packet_lines(direction, data, detail))
        self._emit_lines("normal", lines, level="PACKET", raw_hex=True)
        if self.runtime_packet_compat:
            # Managed C/T processes historically exposed packet lines under
            # their runtime log filename. Keep that read path available while
            # the authoritative packet copy remains in ``packets/``.
            self._emit_lines("normal", lines, level="INFO")

    def packet_text(self, message: str) -> None:
        """Write an already formatted packet record to the packet stream."""
        self._emit_lines("normal", (message,), level="PACKET")

    def alarm(self, message: str) -> None:
        self.emit("alarm", message, level="WARNING")

    def error(self, message: str) -> None:
        self.emit("error", message, level="ERROR")

    def process(self, message: str) -> None:
        self.emit("process", message, level="PROCESS")

    @classmethod
    def cleanup(cls, directory: Path | str, *, retention_hours: int = 720,
                now: float | None = None, process_name: str | None = None,
                packet_directory: Path | str | None = None) -> list[Path]:
        """清理超过保留小时数的按进程小时日志文件。"""
        if isinstance(retention_hours, bool) or retention_hours < 1:
            raise ValueError("retention_hours 必须是正整数")
        cutoff = (time.time() if now is None else now) - retention_hours * 3600
        removed: list[Path] = []
        roots = [Path(directory).resolve()]
        if packet_directory is not None:
            packet_root = Path(packet_directory).resolve()
            if packet_root != roots[0]:
                roots.append(packet_root)
        for root in roots:
            if not root.is_dir():
                continue
            for entry, _ in log_entries(root, "scdfe_c-"):
                path = Path(entry.path)
                match = LOG_FILE_RE.fullmatch(entry.name)
                if not match or (process_name is not None and match.group("process") != process_name):
                    continue
                try:
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff and path.resolve().is_relative_to(root):
                        path.unlink()
                        removed.append(path)
                except OSError:
                    # 日志清理失败不能终止采集进程；下次清理继续尝试。
                    continue
        return removed

    @classmethod
    def list_files(cls, directory: Path | str, *, record: str = "runtime",
                   packet_directory: Path | str | None = None) -> list[dict[str, Any]]:
        if record not in {"runtime", "packet"}:
            raise ValueError("记录类型无效")
        if record == "packet" and packet_directory is not None:
            directory = packet_directory
        root = Path(directory).resolve()
        items = []
        if not root.is_dir():
            return items
        # ``Path.rglob`` plus ``Path.is_file``/``Path.stat`` performs several
        # pathlib and Win32 path calls for every file.  The log tree is read
        # frequently by the Web monitor, so walk it with ``os.scandir`` and
        # reuse each directory entry's cached metadata.  ``follow_symlinks``
        # stays disabled to preserve the endpoint's symlink boundary.
        for entry, relative_name in log_entries(root, "scdfe_c-"):
            match = LOG_FILE_RE.fullmatch(entry.name)
            if match is None:
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            item = {"name": relative_name, "process": match.group("process"),
                    "hour": match.group("hour"), "size": stat.st_size,
                    "modified_at": int(stat.st_mtime * 1000)}
            process_match = re.fullmatch(r"scdfe_c-(?:debug-)?rtu-(?P<rtu_id>\d+)", match.group("process"))
            if process_match is not None:
                item["rtu_id"] = int(process_match.group("rtu_id"))
            items.append(item)
        return sorted(items, key=lambda item: (item["modified_at"], item["name"]), reverse=True)

    @classmethod
    def read_file(cls, directory: Path | str, filename: str, *, tail: int = 200,
                  record: str = "runtime", packet_directory: Path | str | None = None) -> dict[str, Any]:
        if record not in {"runtime", "packet"}:
            raise ValueError("记录类型无效")
        if record == "packet" and packet_directory is not None:
            directory = packet_directory
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("日志文件名无效")
        if not 1 <= tail <= 5000:
            raise ValueError("tail 必须在 1 到 5000 之间")
        root = Path(directory).resolve()
        candidate = Path(filename)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ValueError("日志文件名无效")
        path = (root / candidate).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("日志文件名无效") from exc
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(filename)
        match = LOG_FILE_RE.fullmatch(path.name)
        if not match:
            raise ValueError("日志文件名无效")
        # Read only the bounded tail; refreshing a large hourly file must not
        # allocate/read its entire contents on each Web request.
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            start = stat.st_size
            floor = max(0, start - 2_000_000)
            chunks = []
            newline_count = 0
            while start > floor and newline_count <= tail:
                length = min(16_384, start - floor)
                start -= length
                stream.seek(start)
                chunk = stream.read(length)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
            data = b"".join(reversed(chunks))
        if start:
            # The first chunk can start inside a UTF-8 code point/log line.
            data = data.partition(b"\n")[2]
        all_lines = data.decode("utf-8", "replace").splitlines()
        lines = all_lines[-tail:]
        item = {"name": path.relative_to(root).as_posix(), "process": match.group("process"),
                "hour": match.group("hour"), "size": stat.st_size,
                "modified_at": int(stat.st_mtime * 1000), "content": "\n".join(lines),
                "line_count": len(lines), "truncated": bool(start or len(all_lines) > tail)}
        process_match = re.fullmatch(
            r"scdfe_c-(?:debug-)?rtu-(?P<rtu_id>\d+)",
            match.group("process"),
        )
        if process_match is not None:
            item["rtu_id"] = int(process_match.group("rtu_id"))
        return item


def _worker_process_name(rtu_id: int, *, debug: bool = False) -> str:
    """Return the stable RTU-only log process name."""
    return f"scdfe_c-{'debug-' if debug else ''}rtu-{rtu_id}"


@lru_cache(maxsize=256)
def _batch_update_sql(table: str, value_names: tuple[str, ...],
                      key_names: tuple[str, ...], value_indexes: tuple[int, ...]) -> str:
    """Build and cache the SQL for one table/key/value shape.

    Communication cycles repeatedly update the same YC/YX columns. Caching
    the quoted statement removes string construction from the hot path while
    keeping identifiers validated by :func:`quote_identifier`.
    """
    assignments = ", ".join(
        f"{quote_identifier(name)}=?{index + 1}" for name, index in zip(value_names, value_indexes)
    )
    key_start = len(set(value_indexes)) + 1
    predicate = " AND ".join(f"{quote_identifier(name)}=?{index}"
                             for index, name in enumerate(key_names, key_start))
    return (
        f"UPDATE {quote_identifier(table)} SET {assignments}, _version=_version+1 "
        f"WHERE {predicate}"
    )


@lru_cache(maxsize=256)
def _direct_update_sql(table: str, value_names: tuple[str, ...],
                       key_names: tuple[str, ...] = ("rtu_id", "pnt_no")) -> str:
    """Cache direct-write shapes without rebuilding parameter indexes per row."""
    return _batch_update_sql(table, value_names, key_names, tuple(range(len(value_names))))


def _message_groups(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Decode current point batches or a single RTU/status update."""
    if not isinstance(message, Mapping):
        raise ValueError("批量写数据库消息不是对象")
    if message.get("op") == "batch":
        groups = message.get("groups")
        if not isinstance(groups, list) or not groups:
            raise ValueError("batch 缺少 groups")
        return groups
    if message.get("op") != "update":
        raise ValueError(f"未知批量写入消息: {message.get('op')!r}")
    key, values = message.get("key"), message.get("values")
    if not isinstance(key, Mapping) or not key or not isinstance(values, Mapping) or not values:
        raise ValueError("批量写入消息缺少 key/values")
    key_names = tuple(sorted(key))
    return [{"table": message.get("table"), "key_names": key_names,
             "value_names": tuple(values),
             "rows": [(*values.values(), *(key[name] for name in key_names))]}]


def _validated_batch_group(group: Any) -> tuple[str, tuple, tuple, tuple, list]:
    """Validate common group metadata once, outside the per-point loops."""
    if not isinstance(group, Mapping):
        raise ValueError("批量写入分组不是对象")
    table = group.get("table")
    key_names, value_names = group.get("key_names"), group.get("value_names")
    rows = group.get("rows")
    if not isinstance(table, str) or not table:
        raise ValueError("批量写入分组缺少 table")
    if (not isinstance(key_names, (tuple, list)) or not key_names
            or not isinstance(value_names, (tuple, list)) or not value_names):
        raise ValueError("批量写入分组缺少 key_names/value_names")
    key_names, value_names = tuple(key_names), tuple(value_names)
    if len(set(key_names)) != len(key_names) or len(set(value_names)) != len(value_names):
        raise ValueError("批量写入分组包含重复字段")
    if not isinstance(rows, list) or not rows:
        raise ValueError("批量写入分组缺少 rows")
    value_indexes = group.get("value_indexes", tuple(range(len(value_names))))
    if not isinstance(value_indexes, (tuple, list)):
        raise ValueError("批量写入字段参数索引不是序列")
    value_indexes = tuple(value_indexes)
    # Validate before the SQL cache: (0.0,) and (0,) compare equal in a cache
    # key, but only integer positions are valid input metadata.
    if (len(value_indexes) != len(value_names) or not value_indexes
            or any(type(index) is not int or index < 0 for index in value_indexes)
            or set(value_indexes) != set(range(len(set(value_indexes))))):
        raise ValueError("批量写入字段参数索引必须从 0 开始连续编号")
    return table, key_names, value_names, value_indexes, rows


def _prepare_batch_updates(messages: list[Mapping[str, Any]]) -> dict[str, list[tuple[Any, ...]]]:
    """Validate and merge once, before taking the SQLite write lock.

    Row parameters are already packed by the producer. Keep those tuples for
    unique rows and equal shapes; only overlapping partial updates need field
    dictionaries. Queue order determines the last value of each field.
    """
    # Communication cycles normally arrive as one grouped message.  In that
    # case all rows have complete, canonical keys and unique points, so they
    # can go straight to ``executemany`` without building the per-row merge
    # dictionary used for mixed/partial updates.  The duplicate-key check
    # keeps the existing last-value semantics; unusual messages fall through
    # to the general coalescing path below.
    if len(messages) == 1:
        fast_grouped: dict[str, list[tuple[Any, ...]]] = {}
        fast_seen: dict[tuple[str, tuple[str, ...]], set[tuple[Any, ...]]] = {}
        fast_unique: dict[tuple[str, tuple[str, ...]], tuple[list, int]] = {}
        fast_path = True
        for group in _message_groups(messages[0]):
            table, key_names, value_names, value_indexes, rows = _validated_batch_group(group)
            canonical_keys = tuple(sorted(key_names))
            if key_names != canonical_keys:
                fast_path = False
                break
            sql = _batch_update_sql(table, value_names, canonical_keys, value_indexes)
            value_count = len(set(value_indexes))
            param_count = value_count + len(key_names)
            prepared_rows = fast_grouped.setdefault(sql, [])
            unique_rows = group.get("_unique_rows") is True
            key_group = (table, canonical_keys)
            # A producer's changed and heartbeat groups share a table but
            # normally have disjoint keys. Defer building the first group's
            # key set until a second group needs it; full-change scans still
            # avoid that allocation. Only actual overlaps need the merger.
            previous_unique = fast_unique.pop(key_group, None)
            if previous_unique is not None:
                previous_rows, previous_count = previous_unique
                seen_keys = {row[previous_count:] for row in previous_rows}
                if len(seen_keys) != len(previous_rows):
                    fast_path = False
                    break
                fast_seen[key_group] = seen_keys
            else:
                seen_keys = fast_seen.get(key_group)
            if seen_keys is None:
                if unique_rows:
                    # Retain normalized rows, including tuple subclasses.
                    fast_unique[key_group] = (prepared_rows, value_count)
                else:
                    seen_keys = fast_seen.setdefault(key_group, set())
            for row in rows:
                if type(row) is not tuple:
                    if not isinstance(row, (tuple, list)):
                        raise ValueError("批量写入行参数数量不匹配")
                    row = tuple(row)
                if len(row) != param_count:
                    raise ValueError("批量写入行参数数量不匹配")
                if seen_keys is not None:
                    keys = row[value_count:]
                    if keys in seen_keys:  # also rejects unhashable keys
                        fast_path = False
                        break
                    seen_keys.add(keys)
                prepared_rows.append(row)
            if not fast_path:
                break
        if fast_path:
            return fast_grouped

    coalesced: dict[tuple, tuple[tuple, tuple[Any, ...]]] = {}
    for message in messages:
        for group in _message_groups(message):
            table, key_names, value_names, value_indexes, rows = _validated_batch_group(group)
            # Validate identifiers once per group, not once per point. Key
            # order must be canonical so differently ordered keys still merge.
            canonical_keys = tuple(sorted(key_names))
            _batch_update_sql(table, value_names, canonical_keys, value_indexes)
            key_order = tuple(key_names.index(name) for name in canonical_keys)
            shape = (table, value_names, canonical_keys, value_indexes)
            value_count = len(set(value_indexes))
            param_count = value_count + len(key_names)
            for row in rows:
                if not isinstance(row, (tuple, list)) or len(row) != param_count:
                    raise ValueError("批量写入行参数数量不匹配")
                params = tuple(row)
                keys = params[value_count:]
                if key_names != canonical_keys:
                    keys = tuple(keys[index] for index in key_order)
                    params = params[:value_count] + keys
                token = (table, canonical_keys, keys)
                previous = coalesced.get(token)  # also rejects unhashable keys
                if previous is None or previous[0] == shape:
                    coalesced[token] = (shape, params)
                else:
                    previous_shape, previous_params = previous
                    fields = {name: previous_params[index]
                              for name, index in zip(previous_shape[1], previous_shape[3])}
                    fields.update((name, params[index]) for name, index in zip(value_names, value_indexes))
                    merged_shape = (table, tuple(fields), canonical_keys, tuple(range(len(fields))))
                    coalesced[token] = (merged_shape, (*fields.values(), *keys))
    grouped: dict[tuple, list[tuple[Any, ...]]] = {}
    for shape, params in coalesced.values():
        group_rows = grouped.get(shape)
        if group_rows is None:
            grouped[shape] = [params]
        else:
            group_rows.append(params)
    return {_batch_update_sql(*shape): rows for shape, rows in grouped.items()}


def _apply_batch_updates(connection: sqlite3.Connection,
                         messages: list[Mapping[str, Any]]) -> None:
    """Apply a prepared batch in the caller's transaction."""
    for sql, params in _prepare_batch_updates(messages).items():
        connection.executemany(sql, params)


def _log_batch_receipts(logger, messages, action, **fields):
    """No observer failure may turn a committed transaction into a retry."""
    try:
        from core.communication_log import CommunicationLog
        receipts = [message['business'] for message in messages
                    if isinstance(message, Mapping) and isinstance(message.get('business'), dict)]
        if receipts:
            def write_many(lines):
                for line in lines:
                    logger.normal(line)
            CommunicationLog(getattr(logger, 'business_lines', write_many), 组件='C写库').rows(
                action, receipts, **fields)
    except Exception:
        logging.getLogger(__name__).exception('数据库业务回执日志失败，事务结果保持不变')


def _flush_batch(
    store: ScadaStore,
    logger: ScdfeLogger,
    messages: list[Mapping[str, Any]],
    queue: Any,
    connection: sqlite3.Connection | None = None,
) -> bool | None:
    if not messages:
        return True
    owned_connection = connection is None
    active_connection = connection
    acknowledge = True
    try:
        # Production messages represent separate acquisition/status updates.
        # They may share one SQLite transaction, but each message must retain
        # its own version increment and field ordering. Generic/test messages
        # keep the historical cross-message coalescing behavior.
        preserve_versions = bool(messages) and all(
            isinstance(message, Mapping) and message.get("_preserve_versions") is True
            for message in messages
        )
        prepared_batches = (
            [_prepare_batch_updates([message]) for message in messages]
            if preserve_versions else [_prepare_batch_updates(messages)]
        )
        # Only concatenate adjacent identical statements. Every original row
        # update/version bump and cross-table/field ordering stays intact.
        executions = []
        for prepared in prepared_batches:
            for sql, params in prepared.items():
                if executions and executions[-1][0] == sql:
                    executions[-1][1].extend(params)
                else:
                    executions.append((sql,list(params)))
        if active_connection is None:
            active_connection = store.open_connection()
        for attempt in range(BATCH_WRITER_LOCK_RETRIES):
            try:
                active_connection.execute("BEGIN IMMEDIATE")
                matched = 0
                for sql, params in executions:
                    matched += active_connection.executemany(sql, params).rowcount
                active_connection.commit()
                expected = sum(len(params) for _, params in executions)
                _log_batch_receipts(logger, messages, '写库事务已提交', 事务请求行数=expected,
                                    事务更新行数=matched, 未匹配行数=expected-matched)
                return True
            except sqlite3.OperationalError as exc:
                if active_connection.in_transaction:
                    active_connection.rollback()
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt == BATCH_WRITER_LOCK_RETRIES - 1:
                    _log_batch_receipts(logger, messages, '写库锁冲突，批次保留重试', 错误=str(exc))
                    logger.alarm(
                        f"批量写数据库遇到锁，保留批次稍后重试（消息 {len(messages)}）: {exc}"
                    )
                    acknowledge = False
                    return None
                time.sleep(BATCH_WRITER_LOCK_BACKOFF * (attempt + 1))
            except Exception:
                if active_connection.in_transaction:
                    active_connection.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 - malformed batch or database failure
        _log_batch_receipts(logger, messages, '写库事务失败，未提交', 错误=str(exc))
        logger.error(f"批量写数据库失败（原始消息 {len(messages)}）: {exc}")
        return False
    finally:
        # A transient lock returns ``None`` and leaves JoinableQueue items
        # outstanding. The writer loop restores the batch and acknowledges it
        # only after a later commit; malformed/other failures still release
        # the queue items before the supervisor rebuilds the writer.
        if acknowledge:
            for _ in messages:
                queue.task_done()
        if owned_connection and active_connection is not None:
            active_connection.close()


def _batch_message_size(message: Any) -> int:
    """Return the number of point updates represented by one queue message."""
    if isinstance(message, Mapping) and message.get("op") == "batch":
        groups = message.get("groups")
        if isinstance(groups, list):
            return max(1, sum(len(group["rows"]) for group in groups
                              if isinstance(group, Mapping) and isinstance(group.get("rows"), list)))
    return 1


def _writer_submit_command(store: ScadaStore, connection: sqlite3.Connection,
                           payload: Mapping[str, Any]) -> dict[str, Any]:
    """Legacy IPC compatibility; share the database command contract with T."""
    from core.command_store import submit_commands
    return submit_commands(connection, [(payload.get("rtu_id"), payload.get("kind"),
                                         payload.get("point_no"), payload.get("value"))])[0]


def _batch_writer_entry(
    db_path: str,
    schema_dir: str,
    log_dir: str,
    queue: Any,
    batch_interval: float = BATCH_WRITER_INTERVAL,
    max_batch: int = BATCH_WRITER_MAX_BATCH,
    ready: Any | None = None,
    drain_ack: Any | None = None,
) -> None:
    """Dedicated SQLite writer process for all communication-worker updates."""
    logger = ScdfeLogger(log_dir, process_name="scdfe_c-db-writer",
                         packet_directory=packet_log_directory(log_dir))
    pending: list[Mapping[str, Any]] = []
    pending_updates = 0
    connection: sqlite3.Connection | None = None
    lease = None
    ipc_server: WriterIpcServer | None = None
    ipc_requests: ThreadQueue[WriterRequest] | None = None
    failure = ""
    completed_batches = completed_messages = completed_updates = 0
    next_summary = 0.0

    def flush_pending() -> bool | None:
        nonlocal pending, pending_updates
        nonlocal completed_batches, completed_messages, completed_updates
        batch, pending = pending, []
        updates, pending_updates = pending_updates, 0
        if not batch:
            return True
        flush_result = _flush_batch(store, logger, batch, queue, connection)
        if flush_result is None:
            # The batch was rolled back because another SQLite connection
            # temporarily held the write lock. Keep it ahead of newer input so
            # queue order and JoinableQueue accounting remain exact.
            pending = batch + pending
            pending_updates = updates + pending_updates
            return None
        if not flush_result:
            return False
        completed_batches += 1
        completed_messages += len(batch)
        completed_updates += updates
        return True

    def summary() -> str:
        # Count successfully handled input updates, including coalesced rows;
        # this is not a count of distinct points or SQLite affected rows.
        return (f"累计成功批次={completed_batches} 消息数={completed_messages} "
                f"输入更新数={completed_updates} 待写消息={len(pending)} "
                f"待写更新={pending_updates}")

    try:
        store = ScadaStore(db_path, schema_dir)
        connection = store.open_connection()
        # The writer is the only process that commits measurement batches.
        # Keep WAL checkpoints out of the hot commit cadence; the normal
        # SQLite checkpoint policy remains unchanged for other connections.
        connection.execute(f"PRAGMA wal_autocheckpoint={BATCH_WRITER_WAL_AUTOCHECKPOINT}")
        # Keep each lock attempt bounded. The retry loop above owns the
        # backoff policy; a 30-second SQLite busy timeout would make a stop or
        # recovery operation wait several minutes before the same retry logic
        # can run.
        connection.execute(f"PRAGMA busy_timeout={BATCH_WRITER_BUSY_TIMEOUT_MS}")
        lease = ProcessLease(store, "writer")
        # Expose the writer through a local authenticated endpoint only after
        # it has claimed the writer identity.  A duplicate process therefore
        # cannot replace a live process' endpoint metadata while starting.
        ipc_requests = ThreadQueue(maxsize=1000)
        ipc_server = WriterIpcServer(db_path, ipc_requests).start()
        lease.heartbeat(force=True)
        if ready is not None:
            ready.set()
        logger.process("批量数据库写入进程启动")
        while True:
            lease.heartbeat()
            now = time.monotonic()
            if now >= next_summary:
                logger.normal(f"写库进程运行摘要: {summary()}")
                next_summary = now + PROCESS_LOG_INTERVAL
            # Handle all currently waiting IPC commands before blocking on the
            # acquisition queue.  Each request is acknowledged only after its
            # transaction commits, giving transmission channels reliable ACKs.
            if ipc_requests is not None:
                for _ in range(100):
                    try:
                        ipc_request = ipc_requests.get_nowait()
                    except Empty:
                        break
                    try:
                        ipc_request.response = {"ok": True, "item": _writer_submit_command(
                            store, connection, ipc_request.payload,
                        )}
                    except Exception as exc:  # noqa: BLE001 - return protocol error
                        ipc_request.response = {
                            "ok": False, "error": str(exc), "code": "command_rejected", "status": 409,
                        }
                    finally:
                        # Keep the bounded request-id cache in the loopback
                        # server.  A caller may explicitly repeat a timed-out
                        # request and receive the original committed result;
                        # no automatic retry is performed by the client.
                        if ipc_server is not None:
                            ipc_server.complete(ipc_request)
                        ipc_request.done.set()
            try:
                message = queue.get(timeout=batch_interval)
            except Empty:
                flush_result = flush_pending()
                if flush_result is None:
                    continue
                if not flush_result:
                    raise RuntimeError("批量写数据库失败，写库进程退出等待监督器重建")
                continue
            if (isinstance(message, tuple) and len(message) == 2
                    and message[0] == BATCH_WRITER_DRAIN):
                # A barrier travels through the same FIFO as producer writes.
                # Acknowledge only after every preceding batch has committed.
                # The shared generation lets timed-out callers distinguish a
                # late acknowledgement from the next drain request.
                try:
                    flush_ok = flush_pending()
                    if flush_ok is None:
                        # A drain is a FIFO barrier. Leave it in the queue by
                        # retrying the flush before acknowledging the barrier.
                        while flush_ok is None:
                            time.sleep(BATCH_WRITER_LOCK_BACKOFF)
                            flush_ok = flush_pending()
                    if flush_ok and drain_ack is not None:
                        drain_ack.value = message[1]
                finally:
                    queue.task_done()
                if not flush_ok:
                    raise RuntimeError("批量写数据库失败，写库进程退出等待监督器重建")
                continue
            if message == BATCH_WRITER_STOP:
                try:
                    flush_ok = flush_pending()
                    while flush_ok is None:
                        time.sleep(BATCH_WRITER_LOCK_BACKOFF)
                        flush_ok = flush_pending()
                finally:
                    queue.task_done()
                if not flush_ok:
                    raise RuntimeError("批量写数据库失败，写库进程退出等待监督器重建")
                break
            pending.append(message)
            pending_updates += _batch_message_size(message)
            if pending_updates >= max_batch:
                flush_result = flush_pending()
                if flush_result is None:
                    continue
                if not flush_result:
                    raise RuntimeError("批量写数据库失败，写库进程退出等待监督器重建")
    except Exception as exc:  # noqa: BLE001 - parent reports/restarts the writer
        failure = str(exc)
        logger.error(f"批量数据库写入进程异常退出: {exc}")
        raise
    finally:
        if ipc_server is not None:
            ipc_server.close()
        if ipc_requests is not None:
            # Do not leave socket handlers waiting for an ACK when the writer
            # is stopping (for example after a normal supervisor shutdown).
            while True:
                try:
                    request = ipc_requests.get_nowait()
                except Empty:
                    break
                request.response = {
                    "ok": False, "error": "写库进程正在停止", "code": "writer_stopping", "status": 503,
                }
                request.done.set()
        if pending:
            # A fatal initialization error can happen before a store exists;
            # account for queue items so a stopping supervisor cannot deadlock.
            for _ in pending:
                queue.task_done()
        if connection is not None:
            try:
                connection.close()
            except (OSError, sqlite3.Error):
                pass
        logger.process(f"批量数据库写入进程停止: {summary()}")
        if lease is not None:
            lease.finish(failure)


def _signature_tables(store: ScadaStore) -> tuple[str, ...]:
    """Return the fixed table order used by the definition fingerprint."""
    return tuple(dict.fromkeys(
        ["fes_rtu_define", *CHANNEL_PARAMETER_TABLES.values(),
         "fes_channel_iec104_point_define", "fes_channel_bacnet_point_define",
         "fes_channel_opc_point_define", "fes_channel_can_point_define",
         "fes_yc_define", "fes_yx_define", "fes_yk_define", "fes_yt_define",
         "fes_channel_modbus_define", "fes_channel_modbus_define_call",
         "fes_channel_modbus_define_data", "fes_channel_modbus_define_ytyk"]
    ))


def _signature_projection(
    store: ScadaStore,
    table: str,
) -> tuple[tuple[str, ...], str, str]:
    """Cache static columns and deterministic primary-key ordering per table."""
    cache_key = (str(store.catalog.directory), table)
    projection_info = _SIGNATURE_PROJECTION_CACHE.get(cache_key)
    if projection_info is None:
        schema = store.schema(table)
        columns = tuple(column["eng_name"] for column in schema["columns"]
                        if column["eng_name"] not in RUNTIME_FIELDS)
        primary_keys = tuple(column["eng_name"] for column in schema["columns"]
                             if column.get("primary_key"))
        projection_info = (
            columns,
            ", ".join(quote_identifier(column) for column in columns),
            ", ".join(quote_identifier(column) for column in primary_keys),
        )
        _SIGNATURE_PROJECTION_CACHE[cache_key] = projection_info
    return projection_info


def _definition_digest(
    store: ScadaStore,
    rtu_id: int,
    connection: sqlite3.Connection | None = None,
) -> str:
    """Hash static definitions as an ordered stream without materializing rows."""
    owned = connection is None
    active = connection or store.open_connection()
    digest = hashlib.sha256()
    # SQLite returns only scalar SQL values here. The JSON encoder can encode
    # row tuples directly; converting every cell through Python added most of
    # the CPU cost of repeated definition scans. Rare BLOBs use the same hex
    # representation through the encoder's default callback.
    encode = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), default=_jsonable).encode
    cursor = active.cursor()
    cursor.row_factory = None
    try:
        active.execute("BEGIN")
        for table in _signature_tables(store):
            _, projection, order_by = _signature_projection(store, table)
            digest.update(table.encode("utf-8"))
            digest.update(b"\0")
            order_clause = f" ORDER BY {order_by}" if order_by else ""
            schema_columns = {column["eng_name"] for column in store.schema(table)["columns"]}
            if "rtu_id" in schema_columns:
                scope_clause = "WHERE rtu_id=?"
            elif table.startswith("fes_channel_") and "channel_id" in schema_columns:
                scope_clause = (
                    "WHERE channel_id=(SELECT channel_id FROM fes_rtu_define WHERE rtu_id=?)"
                )
            else:
                scope_clause = ""
            for row in cursor.execute(
                f"SELECT {projection} FROM {quote_identifier(table)} "
                f"{scope_clause}{order_clause}", (rtu_id,) if scope_clause else (),
            ):
                encoded = encode(row).encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            digest.update(b"\xff")
        active.commit()
        return digest.hexdigest()
    except Exception:
        if active.in_transaction:
            active.rollback()
        raise
    finally:
        cursor.close()
        if owned:
            active.close()


def definition_signature(
    store: ScadaStore,
    rtu_id: int,
    connection: sqlite3.Connection | None = None,
) -> str:
    """返回 RTU 通道及四遥静态定义的稳定指纹。

    实时值、质量、时间和命令处理字段被排除，因此采集进程写回数据库不会
    触发自身重启；编辑点号、地址、比例、名称、通道参数或点表增删会改变
    指纹并被父进程自动响应。
    """
    return _definition_digest(store, rtu_id, connection)


def definition_version_token(
    store: ScadaStore,
    rtu_id: int,
    connection: sqlite3.Connection | None = None,
) -> int:
    """Read the trigger-maintained static definition version for one RTU."""
    owned = connection is None
    active = connection or store.open_connection()
    try:
        row = active.execute(
            "SELECT definition_version FROM _scada_rtu_version WHERE rtu_id=?",
            (rtu_id,),
        ).fetchone()
        # ScadaStore creates the internal table and backfills rows during
        # initialization.  Keep a stable sentinel for a partially initialized
        # external database instead of forcing a full definition scan here.
        return 0 if row is None else int(row[0])
    finally:
        if owned:
            active.close()


def command_version_token(
    store: ScadaStore,
    rtu_id: int,
    connection: sqlite3.Connection | None = None,
) -> int:
    """Read the RTU command-input generation maintained by SQLite triggers."""
    owned = connection is None
    active = connection or store.open_connection()
    try:
        row = active.execute(
            "SELECT command_version FROM _scada_rtu_version WHERE rtu_id=?",
            (rtu_id,),
        ).fetchone()
        return 0 if row is None else int(row[0])
    finally:
        if owned:
            active.close()


def discover_channels(store: ScadaStore) -> list[ChannelSpec]:
    """遍历 RTU 表，返回已配置且支持的 RTU-通道。"""
    with store.connection() as connection:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT rtu_id, channel_id, channel_type, name, "
            "process_enable "
            "FROM fes_rtu_define WHERE channel_id IS NOT NULL AND channel_type IS NOT NULL "
            "ORDER BY rtu_id, channel_id"
        ).fetchall()
    result: list[ChannelSpec] = []
    seen: set[ChannelKey] = set()
    for row in rows:
        spec = ChannelSpec.from_row(row)
        if spec.channel_type not in SUPPORTED_CHANNEL_TYPES:
            continue
        if spec.key in seen:
            raise ValueError(f"重复 RTU-通道: {spec.key}")
        seen.add(spec.key)
        result.append(spec)
    return result


def build_master(store: ScadaStore, spec: ChannelSpec):
    """按通道类型构造一个协议主站，但不连接设备。"""
    from client import (BacnetMaster, CanMaster, IEC104Master, ModbusRtuMaster,
                     ModbusTcpMaster, OpcUaMaster)

    classes = {
        "iec104": IEC104Master,
        "modbus-tcp": ModbusTcpMaster,
        "modbus-rtu": ModbusRtuMaster,
        "can": CanMaster,
        "bacnet": BacnetMaster,
        "opc": OpcUaMaster,
    }
    cls = classes[spec.channel_type]
    if spec.channel_type in {"bacnet", "opc"}:
        return cls.from_store(store, spec.rtu_id, default_unmapped=True)
    return cls.from_store(store, spec.rtu_id)


def _measurement(item: Any) -> tuple[str | None, int | None, Any, Any]:
    """统一协议返回值为 kind、点号、值、质量。"""
    # This function runs once for every YC/YX on every poll.  Resolve the
    # container shape once instead of calling the generic four-field helper
    # repeatedly (which otherwise performs four isinstance checks per point).
    try:
        # Built-in protocol adapters return immutable records, so take
        # this branch without any ABC checks in the normal hot path.
        kind = item.kind
        point_no = item.point_no
        value = item.value
        quality = getattr(item, "quality", 0)
    except AttributeError:
        if isinstance(item, Mapping):
            kind = item.get("kind")
            point_no = item.get("point_no")
            value = item.get("value")
            quality = item.get("quality", 0)
        elif isinstance(item, sqlite3.Row):
            try:
                kind = item["kind"]
            except (IndexError, KeyError):
                kind = None
            try:
                point_no = item["point_no"]
            except (IndexError, KeyError):
                point_no = None
            try:
                value = item["value"]
            except (IndexError, KeyError):
                value = None
            try:
                quality = item["quality"]
            except (IndexError, KeyError):
                quality = 0
        else:
            kind = getattr(item, "kind", None)
            point_no = getattr(item, "point_no", None)
            value = getattr(item, "value", None)
            quality = getattr(item, "quality", 0)
    if quality is None:
        quality = 0
    if kind is None or point_no is None:
        return None, None, None, None
    return str(kind), int(point_no), value, quality


class ChannelWorker:
    """单个 RTU-通道通信线程的采集、下发和数据库写回循环。"""

    def __init__(self, store: ScadaStore, spec: ChannelSpec, *, log_dir: Path | str,
                 poll_interval: float = 1.0, retry_interval: float = 5.0,
                 master_factory: Callable[[ScadaStore, ChannelSpec], Any] | None = None,
                 stop_event: Any | None = None, writer_queue: Any | None = None,
                 measurement_heartbeat_interval: float = MEASUREMENT_HEARTBEAT_INTERVAL):
        if poll_interval < 0 or retry_interval <= 0:
            raise ValueError("poll_interval 必须非负，retry_interval 必须为正")
        if measurement_heartbeat_interval < 0:
            raise ValueError("measurement_heartbeat_interval 不能为负")
        self.store = store
        self.spec = spec
        self.poll_interval = float(poll_interval)
        self.retry_interval = float(retry_interval)
        self.measurement_heartbeat_interval = float(measurement_heartbeat_interval)
        self.master_factory = master_factory or build_master
        self.stop_event = stop_event or threading.Event()
        self.writer_queue = writer_queue
        from core.packet_logging import packet_log_switch
        self.logger = ScdfeLogger(log_dir, process_name=_worker_process_name(spec.rtu_id),
                                 packet_directory=packet_log_directory(log_dir),
                                 runtime_packet_compat=True,
                                 packet_switch=packet_log_switch(store.db_path, 'c').for_channel(spec.channel_id)
                                 if store is not None else None)
        self.stats = WorkerStats()
        self._log_context = channel_identity(spec)
        from core.communication_log import CommunicationLog
        self.business = CommunicationLog(self.logger.business_lines, 组件='C',
            RTU编号=spec.rtu_id, 通道编号=spec.channel_id, 规约=spec.channel_type)
        self._command_seen: dict[tuple[str, int], tuple[Any, Any]] | None = None
        self._command_version: int | None = None
        self._last_signature: int | None = None
        self._next_definition_check = 0.0
        self._next_cycle_log = 0.0
        self._master: Any | None = None
        self._command_connection: sqlite3.Connection | None = None
        self._measurement_cache: dict[tuple[str, int], tuple[Any, Any, int, float]] = {}
        self._yc_scaling: dict[int, tuple[Any, Any]] | None = None
        self._master_diagnostic_cursor = (0, 0, 0)
        self._runtime_status = RuntimeStatusStore(store, "scdfe_c") if store is not None else None
        self._runtime_state = {
            "process_status": PROCESS_STOPPED, "process_pid": 0,
            "process_start_time": 0, "process_stop_time": 0,
            "process_heartbeat_time": 0, "process_exit_code": 0,
            "process_error": "",
        }

    def _load_yc_scaling(self) -> None:
        """Load once per connection/definition reload, never once per point."""
        if self.store is None:  # In-memory benchmarks without a database.
            scaling = {}
        else:
            with self.store.connection() as connection:
                scaling = {
                    int(row[0]): (float(row[1]), float(row[2]))
                    for row in connection.execute(
                        "SELECT pnt_no, COALESCE(raw_base, 0.0), COALESCE(raw_scale, 1.0) "
                        "FROM fes_yc_define WHERE rtu_id=?", (self.spec.rtu_id,),
                    )
                }
        self._yc_scaling = scaling
        self._measurement_cache.clear()

    def _close_command_connection(self) -> None:
        connection = self._command_connection
        self._command_connection = None
        if connection is None:
            return
        try:
            connection.close()
        except (OSError, sqlite3.Error):
            pass

    def _read_connection(self) -> sqlite3.Connection:
        if self._command_connection is None:
            self._command_connection = self.store.open_connection()
        return self._command_connection

    @contextmanager
    def _write_transaction(self):
        """Reuse this worker's connection for a short direct-write transaction.

        The running worker already owns a connection for definitions/commands.
        Reuse its statement cache without sharing it across worker threads.
        Standalone callers that have not opened it keep a short-lived
        connection. Always roll back and discard a failed reusable connection.
        """
        connection = self._command_connection
        owned = connection is None
        if owned:
            connection = self.store.open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                if not owned:
                    self._close_command_connection()
            raise
        finally:
            if owned:
                connection.close()

    def _queue_groups(self, groups: list[dict[str, Any]], *, business=None) -> None:
        self.writer_queue.put(
            {"op": "batch", "groups": groups, "_preserve_versions": True,
             **({'business': business} if business else {})},
            timeout=max(1.0, self.retry_interval),
        )

    def _write_rows(self, rows: list[tuple[str, int, int, Mapping[str, Any]]]) -> list[bool]:
        rows = [(table, rtu_id, point_no, updates) for table, rtu_id, point_no, updates in rows
                if updates]
        if not rows:
            return []
        if self.writer_queue is not None:
            coalesced: dict[tuple[str, int, int], dict[str, Any]] = {}
            for table, rtu_id, point_no, updates in rows:
                coalesced.setdefault((table, rtu_id, point_no), {}).update(updates)
            groups: dict[tuple, dict[str, Any]] = {}
            for (table, rtu_id, point_no), point_updates in coalesced.items():
                value_names = tuple(point_updates)
                shape = (table, value_names)
                group = groups.get(shape)
                if group is None:
                    group = {"table": table, "key_names": ("pnt_no", "rtu_id"),
                             "value_names": value_names, "rows": []}
                    groups[shape] = group
                group["rows"].append((*point_updates.values(), point_no, rtu_id))
            command_rows = [{'四遥表': table, 'RTU编号': rtu, '点号': no,
                             '状态': updates.get('status'), '处理时刻': updates['cmd_handle_time']}
                            for table, rtu, no, updates in rows if 'cmd_handle_time' in updates]
            receipt = {'通道编号': self.spec.channel_id, '指令处理结果': command_rows} if command_rows else None
            self._queue_groups(list(groups.values()), business=receipt)
            # The writer owns the connection, so existence is confirmed when
            # it commits. The worker's counters represent accepted updates.
            return [True] * len(rows)
        changed: list[bool] = []
        with self._write_transaction() as connection:
            for table, rtu_id, point_no, updates in rows:
                sql = _direct_update_sql(table, tuple(updates))
                changed.append(bool(connection.execute(
                    sql, (*updates.values(), rtu_id, point_no),
                ).rowcount))
        return changed

    def _write_row(self, table: str, rtu_id: int, point_no: int,
                   updates: Mapping[str, Any]) -> bool:
        return self._write_rows([(table, rtu_id, point_no, updates)])[0:1] == [True]

    def _write_rtu_status(self, status: int) -> None:
        if self.writer_queue is not None:
            self.writer_queue.put({
                "op": "update", "table": "fes_rtu_define",
                "key": {"rtu_id": self.spec.rtu_id},
                "values": {"status": int(status), "refresh_time": _now_ms()},
                "_preserve_versions": True,
            }, timeout=max(1.0, self.retry_interval))
            return
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE fes_rtu_define SET status=?, refresh_time=?, _version=_version+1 WHERE rtu_id=?",
                (int(status), _now_ms(), self.spec.rtu_id),
            )

    def _write_process_state(self, **updates: Any) -> None:
        """原子更新当前 RTU 的通信线程状态。"""
        allowed = PROCESS_CONTROL_FIELDS - {"process_enable"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"不允许写入通信线程字段: {sorted(unknown)}")
        if not updates:
            return
        self._persist_runtime_status(updates)
        if self.writer_queue is not None:
            self.writer_queue.put({
                "op": "update", "table": "fes_rtu_define",
                "key": {"rtu_id": self.spec.rtu_id},
                "values": dict(updates),
                "_preserve_versions": True,
            }, timeout=max(1.0, self.retry_interval))
            return
        value_names = tuple(updates)
        sql = _direct_update_sql("fes_rtu_define", value_names, ("rtu_id",))
        with self._write_transaction() as connection:
            connection.execute(
                sql, (*updates.values(), self.spec.rtu_id),
            )

    def _persist_runtime_status(self, updates: Mapping[str, Any]) -> None:
        runtime = self._runtime_status
        if runtime is None:
            return
        self._runtime_state.update(updates)
        status = int(self._runtime_state.get("process_status") or 0)
        error = str(self._runtime_state.get("process_error") or "")
        values = {
            **self._runtime_state,
            "protocol": self.spec.channel_type,
            "channel_name": self.spec.name or self.spec.channel_type,
            "state": state_for(status, status == PROCESS_RUNNING, error),
            "running": int(status == PROCESS_RUNNING),
            "connection_state": "connected" if status == PROCESS_RUNNING else (
                "retrying" if status == PROCESS_ERROR else "disconnected"
            ),
            "rx_count": int(self.stats.updated_yc + self.stats.updated_yx),
            "tx_count": int(self.stats.sent_yk + self.stats.sent_yt),
            "error_count": int(self.stats.errors),
            "diagnostic_json": {
                "cycles": self.stats.cycles,
                "updated_yc": self.stats.updated_yc,
                "updated_yx": self.stats.updated_yx,
                "sent_yk": self.stats.sent_yk,
                "sent_yt": self.stats.sent_yt,
                "errors": self.stats.errors,
                "channel_type": self.spec.channel_type,
            },
            "last_error": error,
            "last_error_time": now_ms() if error else 0,
            "process_heartbeat_time": int(self._runtime_state.get("process_heartbeat_time") or now_ms()),
        }
        try:
            runtime.upsert(
                self.spec.channel_id,
                values,
                force="process_status" in updates and status != PROCESS_RUNNING,
                connection=self._command_connection,
            )
        except Exception as exc:  # diagnostics must not interrupt communication
            self.logger.error(f"写入通道运行状态失败: {exc}")

    def _queue_measurements(self, pending: dict[tuple[str, int], tuple[Any, Any, int, float]],
                            changed: set[tuple[str, int]], now: int) -> None:
        # There are only four production shapes (YC/YX x changed/heartbeat).
        # Keep first-seen order for existing message semantics, while using
        # fixed slots instead of hashing a two-item shape tuple for every point.
        slots: list[dict[str, Any] | None] = [None, None, None, None]
        ordered: list[dict[str, Any]] = []
        rtu_id = self.spec.rtu_id
        for cache_key, (raw_value, value, quality, _) in pending.items():
            kind, point_no = cache_key
            heartbeat = cache_key not in changed
            slot = (2 if kind == "yx" else 0) + heartbeat
            group = slots[slot]
            if group is None:
                shape = (kind, heartbeat)
                table, columns, value_indexes = _MEASUREMENT_SHAPES[shape]
                group = {"table": table, "key_names": ("pnt_no", "rtu_id"),
                         "value_names": columns, "value_indexes": value_indexes,
                         # candidate_cache is keyed by (kind, point_no), so
                         # each producer group is unique by construction. The
                         # writer still validates row shape; this internal
                         # marker only avoids rebuilding a duplicate-key set.
                         "_unique_rows": True, "rows": []}
                slots[slot] = group
                ordered.append(group)
            if heartbeat:
                row = (now, point_no, rtu_id)
            elif kind == "yc":
                row = (raw_value, value, quality, now, point_no, rtu_id)
            else:
                row = (value, quality, now, point_no, rtu_id)
            group["rows"].append(row)
        receipt = None
        if changed:
            receipt = {'写库批次': f'{os.getpid()}-{time.time_ns()}',
                       'RTU编号': rtu_id, '通道编号': self.spec.channel_id,
                       '变化点数': len(changed)}
        self._queue_groups(ordered, business=receipt)
        return receipt

    def _sync_measurements(self, values: Any) -> tuple[int, int]:
        if self._yc_scaling is None:
            self._load_yc_scaling()
        yc = yx = 0
        queued = self.writer_queue is not None
        pending: list[tuple[str, int, int, Mapping[str, Any]]] = []
        metadata: list[tuple[str, tuple[str, int], tuple[Any, Any, int, float]]] = []
        changed_points: set[tuple[str, int]] = set()
        # Only retain states seen in this result.  Copying the whole cache on
        # every poll made stable 200-point scans O(points) before any work was
        # selected, and is unnecessary because duplicate point numbers are the
        # only case that needs a tentative state within the current result.
        candidate_cache: dict[tuple[str, int], tuple[Any, Any, int, float]] = {}
        monotonic_now = time.monotonic()
        now = _now_ms()
        scaling = self._yc_scaling
        cached_state = self._measurement_cache.get
        heartbeat_interval = self.measurement_heartbeat_interval
        for item in values or ():
            try:
                # Built-in protocol adapters return immutable records. Read
                # those attributes inline; mappings/SQLite rows and custom
                # adapter objects retain the validated fallback parser.
                try:
                    kind = item.kind
                    point_no = item.point_no
                    value = item.value
                    quality = getattr(item, "quality", 0)
                except AttributeError:
                    kind, point_no, value, quality = _measurement(item)
                if type(kind) is not str or type(point_no) is not int:
                    if kind is None or point_no is None:
                        continue
                    kind, point_no = str(kind), int(point_no)
                if kind not in {"yc", "yx"} or point_no is None:
                    continue
                # Convert YC/YX inline; unsupported kinds were rejected above.
                if type(quality) is int:
                    quality_value = quality
                elif isinstance(quality, int):
                    quality_value = int(quality)
                else:
                    try:
                        quality_value = int(getattr(quality, "value", quality or 0))
                    except (TypeError, ValueError):
                        quality_value = 0
                cache_key = (kind, point_no)
                previous = candidate_cache.get(cache_key)
                if previous is None:
                    previous = cached_state(cache_key)
                if kind == "yc":
                    raw_value = float(value)
                    base, scale = scaling.get(point_no, (0.0, 1.0))
                    engineering_value = base + scale * raw_value
                    if not math.isfinite(raw_value) or not math.isfinite(engineering_value):
                        raise ValueError(f"YC {point_no} 原始值或工程值不是有限数")
                elif value is None:
                    # IEC104 double-point intermediate/indeterminate states
                    # have no Boolean value. Preserve the last business value
                    # and publish invalid quality instead of inventing OFF.
                    quality_value |= 128
                    if previous is not None:
                        raw_value, engineering_value = previous[:2]
                    else:
                        row = self._read_connection().execute(
                            "SELECT value FROM fes_yx_define WHERE rtu_id=? AND pnt_no=?",
                            (self.spec.rtu_id, point_no),
                        ).fetchone()
                        if row is None:
                            continue
                        raw_value = engineering_value = row["value"]
                else:
                    raw_value = engineering_value = int(bool(value))
                # Avoid allocating a three-item slice for every stable or
                # changed point in the common polling path.
                value_changed = (
                    previous is None
                    or previous[0] != raw_value
                    or previous[1] != engineering_value
                    or previous[2] != quality_value
                )
                if not value_changed and not (
                        heartbeat_interval == 0
                        or monotonic_now - previous[3] >= heartbeat_interval):
                    continue
                # Allocate one accepted state, shared by the tentative and
                # committed caches. Stable points need no state tuple at all.
                state = (raw_value, engineering_value, quality_value, monotonic_now)
                if queued:
                    # A later duplicate heartbeat must not discard a full
                    # value/quality update already seen in this sample.
                    if value_changed:
                        changed_points.add(cache_key)
                    if kind == "yc":
                        yc += 1
                    else:
                        yx += 1
                else:
                    table, columns, value_indexes = _MEASUREMENT_SHAPES[(kind, not value_changed)]
                    params = ((raw_value, engineering_value, quality_value, now) if kind == "yc"
                              else (engineering_value, quality_value, now)) if value_changed else (now,)
                    updates = {name: params[index] for name, index in zip(columns, value_indexes)}
                    pending.append((table, self.spec.rtu_id, point_no, updates))
                    metadata.append((kind, cache_key, state))
                candidate_cache[cache_key] = state
            except Exception as exc:  # noqa: BLE001 - 单点异常不能中断整站采集
                self.stats.errors += 1
                self.logger.error(f"写回实时点失败: {exc}")
        if queued and candidate_cache:
            receipt = self._queue_measurements(candidate_cache, changed_points, now)
            if receipt:
                self.business.rows('实时更新已入写库队列', (
                    self._change_detail(key, self._measurement_cache.get(key), candidate_cache[key], now)
                    for key in candidate_cache if key in changed_points), **receipt)
            # Publish cache state only after the entire message was accepted.
            # Full queues and failed puts leave every point eligible for retry.
            self._measurement_cache.update(candidate_cache)
        elif pending:
            changed = self._write_rows(pending)
            details = []
            # _write_rows 保持输入顺序；数据库更新不存在的点时对应项为 False。
            for (kind, cache_key, state), was_changed in zip(metadata, changed):
                if was_changed:
                    previous = self._measurement_cache.get(cache_key)
                    if previous is None or previous[:3] != state[:3]:
                        details.append(self._change_detail(cache_key, previous, state, now))
                    self._measurement_cache[cache_key] = state
                    if kind == "yc":
                        yc += 1
                    else:
                        yx += 1
            self.business.rows('实时更新已入库', details)
        return yc, yx

    @staticmethod
    def _change_detail(key, previous, state, stamp):
        return {'四遥': key[0].upper(), '点号': key[1],
                '变化': '初值' if previous is None else '值/质量变化',
                '旧值': previous[1] if previous else '未知', '新值': state[1],
                '原始值': state[0], '旧质量': previous[2] if previous else '未知',
                '新质量': state[2], '刷新时刻': stamp}

    def _command_rows(self) -> list[tuple[str, dict[str, Any]]] | None:
        result: list[tuple[str, dict[str, Any]]] = []
        connection = self._read_connection()
        try:
            # Aggregate each command table in place.  The previous UNION ALL
            # materialised every matching _version row before summing it;
            # command polling only needs the two table totals.  The common
            # unchanged path is a read-only statement; avoid opening and
            # committing a transaction when no command rows need to be read.
            # SQLite triggers increment this one-row token only when command
            # input changes.  Runtime status/ack writes leave it untouched,
            # avoiding two full table aggregates on every idle poll.
            version = command_version_token(self.store, self.spec.rtu_id, connection)
            if self._command_seen is not None and self._command_version == version:
                return None

            # A changed version loads command rows in one read transaction.
            # The aggregate above is a cheap change hint. If a commit races
            # the hint query, the row snapshot either includes that commit or
            # the next poll observes its new aggregate; neither case loses a
            # command, and the unchanged path avoids BEGIN/COMMIT entirely.
            connection.execute("BEGIN")
            for table, kind in (("fes_yk_define", "yk"), ("fes_yt_define", "yt")):
                rows = connection.execute(
                    f"SELECT pnt_no, value, cmd_time, cmd_handle_time "
                    f"FROM {quote_identifier(table)} WHERE rtu_id=? ORDER BY pnt_no",
                    (self.spec.rtu_id,),
                ).fetchall()
                result.extend((kind, dict(row)) for row in rows)
            connection.commit()
            self._command_version = version
            return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            self._close_command_connection()
            raise

    def _send_command(self, kind: str, row: Mapping[str, Any]) -> Any:
        if self._master is None:
            raise RuntimeError("主站尚未建立")
        point_no = int(row["pnt_no"])
        if kind == "yk":
            if not callable(getattr(self._master, "send_yk_point", None)):
                raise NotImplementedError(
                    f"{self.spec.channel_type} 主站没有遥控下发 API；请配置该规约的应用层命令映射"
                )
            return self._master.send_yk_point(point_no, bool(row.get("value", 0)))
        if not callable(getattr(self._master, "send_yt_point", None)):
            raise NotImplementedError(
                f"{self.spec.channel_type} 主站没有遥调下发 API；请配置该规约的应用层命令映射"
            )
        return self._master.send_yt_point(point_no, float(row.get("value", 0.0)))

    def _sync_commands(self) -> tuple[int, int]:
        rows = self._command_rows()
        if rows is None:
            return 0, 0
        state = {(kind, int(row["pnt_no"])): (row.get("cmd_time"), row.get("value"))
                 for kind, row in rows}
        first_sync = self._command_seen is None
        if first_sync:
            self._command_seen = state
        sent_yk = sent_yt = 0
        for kind, row in rows:
            key = (kind, int(row["pnt_no"]))
            current = (row.get("cmd_time"), row.get("value"))
            pending = False
            if first_sync:
                command_time = int(row.get("cmd_time") or 0)
                handled_time = int(row.get("cmd_handle_time") or 0)
                # 重启后只恢复明确标记为未处理的命令；数据库中的初始值不会
                # 被误当成首次下发，避免守护进程启动就操作现场设备。
                pending = command_time > handled_time
            if (first_sync and not pending) or (
                    not first_sync and self._command_seen.get(key) == current):
                continue
            self._command_seen[key] = current
            command_key = (f'RTU编号={self.spec.rtu_id} 类型={kind} 点号={row["pnt_no"]} '
                           f'命令时标={int(row.get("cmd_time") or 0)}')
            self.business.event('指令开始下发', 四遥=kind.upper(), 点号=row['pnt_no'],
                                值=row.get('value'), 命令时标=row.get('cmd_time'))
            try:
                result = self._send_command(kind, row)
                self.business.event('指令驱动调用完成', 四遥=kind.upper(), 点号=row['pnt_no'],
                                    值=row.get('value'), 命令时标=row.get('cmd_time'), 驱动返回=result)
                now = max(_now_ms(), int(row.get("cmd_time") or 0))
                stored = self._write_row(
                    "fes_yk_define" if kind == "yk" else "fes_yt_define",
                    self.spec.rtu_id, int(row["pnt_no"]),
                    {"status": 1, "cmd_handle_time": now, "refresh_time": now},
                )
                if kind == "yk":
                    sent_yk += 1
                else:
                    sent_yt += 1
                self.business.event(('指令处理状态已入队' if self.writer_queue is not None else '指令处理状态已入库')
                                    if stored else '指令处理状态写入未匹配测点',
                                    命令=command_key, 状态=1, 处理时刻=now)
            except Exception as exc:  # noqa: BLE001 - 通道必须继续服务其他点
                self.stats.errors += 1
                self._write_row(
                    "fes_yk_define" if kind == "yk" else "fes_yt_define",
                    self.spec.rtu_id, int(row["pnt_no"]),
                    {"status": 3, "cmd_handle_time": max(_now_ms(), int(row.get("cmd_time") or 0))},
                )
                self.business.event('指令下发或结果写入失败', 命令=command_key, 错误=str(exc), 状态=3)
        return sent_yk, sent_yt

    def _close_master(self) -> None:
        if self._master is not None:
            try:
                self._master.close()
            except Exception as exc:  # noqa: BLE001
                self._log_communication(f"关闭通道失败: {exc}", level='error')
            finally:
                self._master = None

    def _record_master_diagnostics(self, thread_id: int) -> None:
        """Persist new IEC104 transport errors without breaking acquisition."""
        master = self._master
        if master is None:
            return
        diagnostics = getattr(master, "diagnostics", None)
        if not isinstance(diagnostics, Mapping):
            return
        errors = list(diagnostics.get("protocol_errors", ()))
        states = list(diagnostics.get("connection_states", ()))
        unexpected = list(diagnostics.get("unexpected_messages", ()))
        old_errors, old_states, old_unexpected = self._master_diagnostic_cursor
        new_errors = errors[old_errors:]
        new_states = states[old_states:]
        new_unexpected = unexpected[old_unexpected:]
        self._master_diagnostic_cursor = (len(errors), len(states), len(unexpected))
        abnormal_states = [item for item in new_states
                           if str(item.get("state", "")).upper() in {
                               "CLOSED", "CLOSED_AWAIT_OPEN", "CLOSED_AWAIT_RECONNECT",
                               "OPEN_AWAIT_CLOSED",
                           }]
        # c104 reports the complete connection-state history generated during
        # connect().  A normal STARTDT handshake can therefore leave a
        # CLOSED_AWAIT_OPEN entry in the cursor even though the current link
        # is already OPEN and collect_once() is receiving data.  Do not turn
        # that historical/transition state into a persistent process error;
        # protocol errors and unexpected ASDUs remain actionable diagnostics.
        if bool(getattr(master, "connected", False)):
            abnormal_states = []
        if not (new_errors or abnormal_states or new_unexpected):
            return
        details = []
        if new_errors:
            details.append(f"无效报文 {len(new_errors)} 条: {new_errors[-1]}")
        if abnormal_states:
            details.append(f"链路状态 {abnormal_states[-1].get('state')}")
        if new_unexpected:
            details.append(f"异常 ASDU {len(new_unexpected)} 条")
        message = "IEC104 通信异常：" + "；".join(details)
        self.stats.errors += len(new_errors) + len(abnormal_states) + len(new_unexpected)
        self._log_communication(message, level='error')
        try:
            self._write_process_state(
                process_status=PROCESS_ERROR, process_pid=thread_id,
                process_heartbeat_time=_now_ms(), process_error=message,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics cannot stop polling
            self._log_communication(f"写入 IEC104 异常状态失败: {exc}", level='error')

    def _stop_requested(self) -> bool:
        return self.stop_event.is_set()

    def run(self, *, max_cycles: int | None = None) -> WorkerStats:
        with self.logger.session():
            return self._run(max_cycles=max_cycles)

    def _log_communication(self, message: str, *, level: str = 'process') -> None:
        getattr(self.logger, level)(f'{message}；{self._log_context}')

    def _run(self, *, max_cycles: int | None = None) -> WorkerStats:
        """运行采集循环；连接失败会告警并按 retry_interval 重试。

        This method never lets a channel exception escape to the supervisor's
        scheduler.  A failure is converted into this thread's error state,
        the protocol object is closed, and the channel retries independently.
        """
        cycles = 0
        thread_id = _current_thread_id()
        self._log_context = (channel_context(self.store, self.spec)
                             + f' 主进程PID={os.getpid()} 线程ID={thread_id}')
        self._log_communication(
            f'通信线程启动 采集间隔={self.poll_interval:g}s 重试间隔={self.retry_interval:g}s')
        attempts = failures = 0
        stop_reason = '收到停止请求'
        process_start = _now_ms()
        next_heartbeat = 0.0
        try:
            self._write_process_state(
                process_status=PROCESS_STARTING, process_pid=thread_id,
                process_start_time=process_start, process_heartbeat_time=process_start,
                process_exit_code=0, process_error="",
            )
        except Exception as exc:  # noqa: BLE001 - 状态记录失败不应阻断现场通讯
            self._log_communication(f"写入通信线程启动状态失败: {exc}", level='error')
        try:
            while not self._stop_requested():
                phase = '加载配置'
                try:
                    current_signature = definition_version_token(
                        self.store, self.spec.rtu_id, self._read_connection(),
                    )
                    if self._last_signature is not None and current_signature != self._last_signature:
                        stop_reason = '重连前检测到定义变化，退出并请求重载'
                        self._log_communication(stop_reason)
                        return self.stats
                    self._last_signature = current_signature
                    self._load_yc_scaling()
                    self._master = self.master_factory(self.store, self.spec)
                    self._master_diagnostic_cursor = (0, 0, 0)
                    if hasattr(self._master, "set_packet_handler"):
                        if self.spec.channel_type == 'iec104':
                            self.business.bind_points(self._master)
                            receiver = getattr(self._master, '_soe_receiver', None)
                            if receiver is not None:
                                receiver.on_commit = lambda rows: self.business.soe('SOE已入库', rows)
                        self._master.set_packet_handler(self.business.wrap_packet(self.logger.packet))
                    attempts += 1
                    phase = '建立连接'
                    self._log_communication(f'开始连接 连接尝试={attempts}')
                    connect_started = time.monotonic()
                    self._master.connect()
                    connect_ms = (time.monotonic() - connect_started) * 1000
                    phase = '写入连接状态'
                    self._measurement_cache.clear()
                    self._write_rtu_status(1)
                    self._write_process_state(
                        process_status=PROCESS_RUNNING,
                        process_pid=thread_id, process_heartbeat_time=_now_ms(),
                        process_error="",
                    )
                    next_heartbeat = time.monotonic() + WORKER_HEARTBEAT_INTERVAL
                    self._log_communication(
                        f'通道连接成功，开始实时采集 连接尝试={attempts} '
                        f'连接耗时={connect_ms:.1f}ms 恢复前连续失败={failures}')
                    while not self._stop_requested():
                        # 通信线程也做一次定义检查；即使主调度扫描周期较长，
                        # 单线程调用方式同样能自动退出并由上层重载。
                        monotonic_now = time.monotonic()
                        phase = '检查配置变化'
                        if monotonic_now >= self._next_definition_check:
                            current_signature = definition_version_token(
                                self.store, self.spec.rtu_id, self._read_connection(),
                            )
                            self._next_definition_check = monotonic_now + max(
                                WORKER_DEFINITION_CHECK_INTERVAL, self.poll_interval,
                            )
                            if current_signature != self._last_signature:
                                stop_reason = '检测到通道或四遥定义变化，退出并请求重载'
                                self._log_communication(stop_reason)
                                return self.stats
                        phase = '采集遥测/遥信'
                        values = self._master.collect_once()
                        phase = '写入采集值及诊断'
                        self._record_master_diagnostics(thread_id)
                        yc, yx = self._sync_measurements(values)
                        phase = '下发遥控/遥调'
                        sent_yk, sent_yt = self._sync_commands()
                        failures = 0
                        self.stats.cycles += 1
                        self.stats.updated_yc += yc
                        self.stats.updated_yx += yx
                        self.stats.sent_yk += sent_yk
                        self.stats.sent_yt += sent_yt
                        if monotonic_now >= next_heartbeat:
                            phase = '更新心跳'
                            self._write_process_state(
                                process_status=PROCESS_RUNNING,
                                process_pid=thread_id, process_heartbeat_time=_now_ms(),
                            )
                            next_heartbeat = monotonic_now + WORKER_HEARTBEAT_INTERVAL
                        if monotonic_now >= self._next_cycle_log:
                            self.logger.normal(
                                f"采集周期摘要 cycles={self.stats.cycles} yc={self.stats.updated_yc} "
                                f"yx={self.stats.updated_yx} yk={self.stats.sent_yk} yt={self.stats.sent_yt}"
                            )
                            self._next_cycle_log = monotonic_now + 30.0
                        cycles += 1
                        if max_cycles is not None and cycles >= max_cycles:
                            stop_reason = '达到指定采集周期数'
                            return self.stats
                        self.stop_event.wait(self.poll_interval)
                except Exception as exc:  # noqa: BLE001 - 线程应隔离并重试
                    self.stats.errors += 1
                    failures += 1
                    try:
                        self._write_rtu_status(3)
                    except Exception as db_exc:  # noqa: BLE001
                        self._log_communication(f"写入 RTU 错误状态失败: {db_exc}", level='error')
                    try:
                        self._write_process_state(
                            process_status=PROCESS_ERROR, process_pid=thread_id,
                            process_heartbeat_time=_now_ms(), process_error=str(exc),
                        )
                    except Exception as state_exc:  # noqa: BLE001
                        self._log_communication(f"写入通信线程错误状态失败: {state_exc}", level='error')
                    self._log_communication(
                        f'通道运行异常 阶段={phase} 连接尝试={attempts} 连续失败={failures} '
                        f'异常={type(exc).__name__}: {exc} 重试间隔={self.retry_interval:g}s', level='error')
                    self.stop_event.wait(self.retry_interval)
                finally:
                    self._close_master()
        finally:
            try:
                self._write_rtu_status(0)
            except Exception as exc:  # noqa: BLE001
                self._log_communication(f"写入 RTU 停止状态失败: {exc}", level='error')
            try:
                self._write_process_state(
                process_status=PROCESS_STOPPED, process_pid=0,
                    process_stop_time=_now_ms(), process_heartbeat_time=_now_ms(),
                    process_exit_code=0,
                )
            except Exception as exc:  # noqa: BLE001
                self._log_communication(f"写入通信线程停止状态失败: {exc}", level='error')
            self._close_master()
            self._close_command_connection()
            self._log_communication(
                f"通信线程停止 cycles={self.stats.cycles} yc={self.stats.updated_yc} "
                f"yx={self.stats.updated_yx} yk={self.stats.sent_yk} yt={self.stats.sent_yt} "
                f"累计异常={self.stats.errors} 停止原因={stop_reason}"
            )
        return self.stats


def _rtu_lock(rtu_id: int) -> ServerInstanceLock:
    # One RTU owns one channel in the current schema. Lock the RTU, so editing
    # channel_id cannot let a second owner overwrite the same runtime state.
    return ServerInstanceLock(rtu_id)


def _debug_spec(store: ScadaStore, rtu_id: int, channel_id: int) -> ChannelSpec:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM fes_rtu_define WHERE rtu_id=?", (rtu_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"RTU {rtu_id} 不存在")
    if row["channel_id"] != channel_id:
        raise ValueError(f"RTU {rtu_id} 的通道已从 {channel_id} 变为 {row['channel_id']}，请重新启动调试")
    spec = ChannelSpec.from_row(row)
    if spec.channel_type not in SUPPORTED_CHANNEL_TYPES:
        raise ValueError(f"不支持调试规约: {spec.channel_type}")
    if spec.process_enable:
        raise ValueError(f"请先将 RTU {rtu_id} 的 process_enable 设为 0，停止自动通信后再调试")
    return spec


class _DebugChannelWorker(ChannelWorker):
    """A foreground channel with its own logs and no supervisor/writer process."""

    def __init__(self, *args, send_commands: bool, scan_interval: float,
                 retention_hours: int, process_lease=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.send_commands = send_commands
        self.scan_interval = scan_interval
        self.retention_hours = retention_hours
        self.process_lease = process_lease
        self._next_control_check = 0.0
        self._next_log_cleanup = 0.0

    def _sync_commands(self) -> tuple[int, int]:
        return super()._sync_commands() if self.send_commands else (0, 0)

    def _stop_requested(self) -> bool:
        if self.stop_event.is_set():
            return True
        self._next_cycle_log = 0.0  # Debug console gets a summary every cycle.
        now = time.monotonic()
        if now >= self._next_control_check:
            if self.process_lease is not None:
                self.process_lease.heartbeat()
                if not self.process_lease.enabled():
                    self.logger.process("收到进程监控停止请求，结束独立调试")
                    self.stop_event.set()
                    return True
            row = self._read_connection().execute(
                "SELECT channel_id, process_enable FROM fes_rtu_define WHERE rtu_id=?",
                (self.spec.rtu_id,),
            ).fetchone()
            self._next_control_check = now + self.scan_interval
            if row is None or row["channel_id"] != self.spec.channel_id or row["process_enable"]:
                self.logger.process("RTU 已删除、通道已更换或自动运行已启用，结束独立调试")
                self.stop_event.set()
                return True
        if now >= self._next_log_cleanup:
            ScdfeLogger.cleanup(self.logger.directory, retention_hours=self.retention_hours,
                                process_name=self.logger.process_name,
                                packet_directory=self.logger.packet_directory)
            self._next_log_cleanup = now + 60.0
        return False


def run_channel_debug(
    store: ScadaStore, rtu_id: int, *,
    log_dir: Path | str | None = None, poll_interval: float = 1.0,
    retry_interval: float = 5.0, scan_interval: float = 2.0,
    retention_hours: int = 720, max_cycles: int | None = None,
    send_commands: bool = False, stop_event: threading.Event | None = None,
    master_factory: Callable[[ScadaStore, ChannelSpec], Any] | None = None,
    launch_token: str | None = None,
) -> WorkerStats:
    """Debug exactly one disabled RTU/channel in the calling process.

    The per-RTU lock also covers supervisor status writes and queue drain.
    Keep it across definition reloads and release only after protocol/SQLite
    cleanup. The main control row and other RTUs are never changed here.
    """
    if type(rtu_id) is not int or rtu_id <= 0:
        raise ValueError("rtu_id 必须为正整数")
    if (not math.isfinite(poll_interval) or poll_interval < 0
            or not math.isfinite(retry_interval) or retry_interval <= 0
            or not math.isfinite(scan_interval) or scan_interval <= 0):
        raise ValueError("采集周期必须为有限非负数，重试/扫描周期必须为有限正数")
    if type(retention_hours) is not int or not 1 <= retention_hours <= 87600:
        raise ValueError("retention_hours 必须为 1..87600 的整数")
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles <= 0):
        raise ValueError("max_cycles 必须为正整数")
    lock = _rtu_lock(rtu_id)
    if not lock.acquire():
        raise ValueError(f"本服务器 RTU {rtu_id} 正由自动通信或另一个调试进程使用，请等待该通道停止后重试")
    lease = None
    failure = ""
    try:
        with store.connection() as connection:
            row = connection.execute(
                "SELECT channel_id FROM fes_rtu_define WHERE rtu_id=?", (rtu_id,),
            ).fetchone()
        if row is None or row["channel_id"] is None:
            raise ValueError(f"RTU {rtu_id} 不存在或尚未配置通道")
        channel_id = row["channel_id"]
        spec = _debug_spec(store, rtu_id, channel_id)
        lease = ProcessLease(store, f"debug:{rtu_id}", launch_token)
        store.update_row(PROCESS_TABLE, [lease.key], {"process_enable": 1})
        lease.heartbeat(force=True)
        if log_dir is None:
            with store.connection() as connection:
                control = connection.execute(f"SELECT * FROM {CONTROL_TABLE} WHERE id=1").fetchone()
            log_dir = log_directory(store, control) if control is not None else store.db_path.parent / "logs"
        from core.packet_logging import packet_log_switch
        logger = ScdfeLogger(log_dir, process_name=_worker_process_name(rtu_id, debug=True), console=True,
                             packet_directory=packet_log_directory(log_dir),
                             packet_switch=packet_log_switch(store.db_path, 'c').for_channel(spec.channel_id))
        stop_event = stop_event or threading.Event()
        stats = WorkerStats()
        logger.process(f"独立调试启动 规约={spec.channel_type} "
                       f"命令下发={'启用' if send_commands else '关闭'}")
        try:
            while not stop_event.is_set():
                worker = _DebugChannelWorker(
                    store, spec, log_dir=log_dir, poll_interval=poll_interval,
                    retry_interval=retry_interval, stop_event=stop_event,
                    master_factory=master_factory, send_commands=send_commands,
                    scan_interval=scan_interval, retention_hours=retention_hours,
                    process_lease=lease,
                )
                worker.logger = logger
                worker.stats = stats
                remaining = None if max_cycles is None else max_cycles - stats.cycles
                worker.run(max_cycles=remaining)
                if stop_event.is_set() or (max_cycles is not None and stats.cycles >= max_cycles):
                    break
                # run() returned early for a definition change. Resolve only
                # the selected RTU again, using fresh protocol/scaling data.
                spec = _debug_spec(store, rtu_id, channel_id)
                logger.process("重新加载指定 RTU 通道及四遥定义")
            return stats
        except Exception as exc:
            logger.error(f"独立调试异常结束: {exc}")
            raise
        finally:
            logger.process(f"独立调试结束 cycles={stats.cycles} errors={stats.errors}")
    except Exception as exc:
        failure = str(exc)
        raise
    finally:
        if lease is not None:
            lease.finish(failure)
        lock.release()


def _worker_thread_entry(store: ScadaStore, spec_data: dict[str, Any], log_dir: str,
                         poll_interval: float, retry_interval: float, stop_event: threading.Event,
                         writer_queue: Any | None = None) -> None:
    """通信线程入口；任何未预期异常都在本线程内收敛。"""
    spec = ChannelSpec(**spec_data)
    try:
        ChannelWorker(store, spec, log_dir=log_dir, poll_interval=poll_interval,
                      retry_interval=retry_interval, stop_event=stop_event,
                      writer_queue=writer_queue).run()
    except Exception as exc:  # noqa: BLE001 - 主调度线程负责拉起并记录异常
        ScdfeLogger(log_dir, process_name=_worker_process_name(spec.rtu_id),
                    packet_directory=packet_log_directory(log_dir)).error(
            f"通信线程异常退出: {type(exc).__name__}: {exc}；{channel_identity(spec)} "
            f"主进程PID={os.getpid()} 线程ID={_current_thread_id()}"
        )
        # Thread exceptions must not escape into the main scheduler.  The
        # supervisor observes thread.is_alive() and will mark/restart it on
        # the next scan; logging here preserves the traceback boundary without
        # terminating unrelated communication threads.


@dataclass
class _ManagedWorker:
    spec: ChannelSpec
    thread: threading.Thread
    stop_event: threading.Event
    channel_lock: ServerInstanceLock
    started_at: float = field(default_factory=time.time)
    log_context: str = ''


class ScdfeSupervisor:
    """主调度线程：每个 RTU-通道一个通信线程，自动隔离和重载。"""

    def __init__(self, db_path: Path | str = DEFAULT_DB, *, schema_dir: Path | str = SCHEMA_DIR,
                 log_dir: Path | str | None = None, poll_interval: float = 1.0,
                 scan_interval: float = 2.0, retry_interval: float = 5.0,
                 retention_hours: int = 720, context: mp.context.BaseContext | None = None,
                 writer_batch_interval: float = BATCH_WRITER_INTERVAL,
                 writer_max_batch: int = BATCH_WRITER_MAX_BATCH,
                 worker_start_delay: float = 0.25,
                 launch_token: str | None = None,
                 thread_factory: Callable[..., threading.Thread] | None = None):
        if poll_interval < 0 or scan_interval <= 0 or retry_interval <= 0:
            raise ValueError("poll_interval 必须非负，scan_interval/retry_interval 必须为正")
        self.db_path = Path(db_path).resolve()
        self.schema_dir = Path(schema_dir).resolve()
        self._log_dir_from_control = log_dir is None
        self.log_dir = Path(log_dir or self.db_path.parent / "logs").resolve()
        self.poll_interval = float(poll_interval)
        self.scan_interval = float(scan_interval)
        self.retry_interval = float(retry_interval)
        if isinstance(retention_hours, bool) or retention_hours < 1:
            raise ValueError("retention_hours 必须是正整数")
        self.retention_hours = int(retention_hours)
        if writer_batch_interval <= 0 or writer_max_batch < 1:
            raise ValueError("writer_batch_interval 必须为正，writer_max_batch 必须为正整数")
        if worker_start_delay < 0:
            raise ValueError("worker_start_delay 不能为负")
        self.writer_batch_interval = float(writer_batch_interval)
        self.writer_max_batch = int(writer_max_batch)
        self.worker_start_delay = float(worker_start_delay)
        self.context = context or mp.get_context("spawn")
        self.store = ScadaStore(self.db_path, self.schema_dir)
        ensure_control_row(self.store)
        ensure_process(self.store, "writer")
        self._process_lease = None
        self.launch_token = launch_token
        self.stop_event = threading.Event()
        self._workers: dict[ChannelKey, _ManagedWorker] = {}
        self._writer_queue: Any | None = None
        self._writer_process: mp.Process | None = None
        self._writer_stop_sent = False
        self._writer_drain_ack: Any | None = None
        self._writer_drain_generation = 0
        self._reload_pending = False
        self._last_worker_start = 0.0
        self._scan_count = 0
        self._next_log_summary = 0.0
        self._next_log_cleanup = 0.0
        self.thread_factory = thread_factory or threading.Thread
        self.logger = ScdfeLogger(self.log_dir, process_name="scdfe_c-main",
                                  packet_directory=packet_log_directory(self.log_dir))

    def _maintain_logs(self) -> None:
        """Keep hourly logs active even while communication/writing is idle."""
        now = time.monotonic()
        if now >= self._next_log_summary:
            alive = sum(managed.thread.is_alive() for managed in self._workers.values())
            writer = self._writer_process
            writer_alive = writer is not None and writer.is_alive()
            self.logger.normal(
                f"主进程运行摘要: 扫描次数={self._scan_count} "
                f"通信线程={alive}/{len(self._workers)} "
                f"写库进程={'运行' if writer_alive else '停止'}",
            )
            self._next_log_summary = now + PROCESS_LOG_INTERVAL
        if now >= self._next_log_cleanup:
            removed = ScdfeLogger.cleanup(self.log_dir, retention_hours=self.retention_hours,
                                          packet_directory=self.logger.packet_directory)
            self._next_log_cleanup = now + LOG_CLEANUP_INTERVAL
            if removed:
                self.logger.process(f"日志清理完成，删除 {len(removed)} 个过期文件")

    def _start_writer(self) -> None:
        if self._writer_process is not None and self._writer_process.is_alive():
            return
        queue = self.context.JoinableQueue(maxsize=10000)
        ready = self.context.Event()
        drain_ack = self.context.Value("Q", 0)
        self._writer_queue = queue
        self._writer_drain_ack = drain_ack
        self._writer_drain_generation = 0
        self._writer_stop_sent = False
        process = self.context.Process(
            target=_batch_writer_entry,
            args=(str(self.db_path), str(self.schema_dir), str(self.log_dir),
                  queue, self.writer_batch_interval, self.writer_max_batch, ready, drain_ack),
            name="scdfe-c-db-writer",
        )
        process.daemon = True
        try:
            process.start()
        except Exception:
            self._writer_queue = None
            self._writer_drain_ack = None
            try:
                queue.close()
                queue.join_thread()
            except (AttributeError, OSError):
                pass
            try:
                process.close()
            except (ValueError, OSError):
                pass
            raise
        self._writer_process = process
        self.logger.process(
            f'启动批量数据库写入进程 进程PID={process.pid} '
            f'批量间隔={self.writer_batch_interval:g}s 批量上限={self.writer_max_batch} '
            f'数据库={self.db_path}')
        deadline = time.monotonic() + 15.0
        while not ready.wait(0.05):
            if not process.is_alive():
                try:
                    row = self.store.get_row(PROCESS_TABLE, ["writer"])
                    detail = row.get("process_error") or f"退出码 {process.exitcode}"
                except Exception:
                    detail = f"退出码 {process.exitcode}"
                raise RuntimeError(f"写库进程未能就绪：{detail}；暂停启动通信线程")
            if time.monotonic() >= deadline:
                raise RuntimeError("写库进程未能就绪，暂停启动通信线程")

    def _recover_writer_failure(self) -> bool:
        process = self._writer_process
        if process is None or process.is_alive():
            return True
        self.logger.error(f"批量数据库写入进程已退出 exitcode={process.exitcode}，重建通信线程")
        # A dead writer cannot acknowledge or drain the old queue. Stop
        # workers that still reference it, then recreate both sides together;
        # parent-owned state writes below restore authoritative thread status.
        for key, managed in list(self._workers.items()):
            stopped = self._stop_worker(
                key, reason="批量数据库写入进程异常退出，停止通信线程", unexpected=True,
            )
            if not stopped:
                self.logger.error(
                    "通信线程未能在超时内退出；"
                    "暂不启动重复线程",
                )
            # _stop_worker records the final error while it still owns the
            # RTU lock. Do not overwrite another owner's state afterwards.
        if self._workers:
            return False
        return self._stop_writer()

    def _drain_writer(self, timeout: float | None = None) -> bool:
        queue = self._writer_queue
        if queue is None:
            return True
        process, ack = self._writer_process, self._writer_drain_ack
        if process is None or not process.is_alive() or ack is None:
            return False
        # Do not create an unbounded queue.join waiter: a failed writer can
        # never wake it. A FIFO barrier and shared acknowledgement provide a
        # bounded wait using public multiprocessing APIs on Windows and Unix.
        wait_seconds = timeout if timeout is not None else max(1.0, self.retry_interval * 2)
        if wait_seconds <= 0:
            return False
        deadline = time.monotonic() + wait_seconds
        self._writer_drain_generation += 1
        generation = self._writer_drain_generation
        try:
            queue.put((BATCH_WRITER_DRAIN, generation), timeout=wait_seconds)
        except (Full, OSError, ValueError):
            return False
        ack_lock = ack.get_lock()
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            # A terminated writer can leave a multiprocessing lock held.
            # Never use the synchronized .value getter's unbounded acquire.
            if ack_lock.acquire(timeout=min(0.01, remaining)):
                try:
                    if ack.get_obj().value >= generation:
                        return True
                finally:
                    ack_lock.release()
            if not process.is_alive():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def _stop_writer(self) -> bool:
        process, queue = self._writer_process, self._writer_queue
        if process is None or queue is None:
            return True
        try:
            if process.is_alive():
                if not self._writer_stop_sent:
                    queue.put(BATCH_WRITER_STOP, timeout=max(1.0, self.retry_interval))
                    self._writer_stop_sent = True
                process.join(timeout=max(2.0, self.retry_interval * 2))
        except (Full, OSError) as exc:
            self.logger.error(f"停止批量数据库写入进程失败: {exc}")
        if process.is_alive():
            self.logger.error("写库进程尚未退出，保留进程和队列，等待下次检查")
            return False
        process.join(timeout=0)
        self._writer_process = None
        self._writer_queue = None
        self._writer_drain_ack = None
        try:
            if process.exitcode:
                queue.cancel_join_thread()
            queue.close()
            if not process.exitcode:
                queue.join_thread()
        except (AttributeError, OSError):
            pass
        try:
            process.close()
        except (ValueError, OSError):
            pass
        return True

    def _pause_communication(self, reason: str) -> bool:
        # Signal all producers before waiting for any one of them/its queue.
        for managed in self._workers.values():
            managed.stop_event.set()
        for key in list(self._workers):
            self._stop_worker(key, reason=reason)
        if self._workers:
            return False
        process = self._writer_process
        if process is not None and process.is_alive() and not self._drain_writer():
            return False
        return self._stop_writer()

    def _control(self) -> dict[str, Any]:
        return self.store.get_row(CONTROL_TABLE, [1])

    def _update_control_state(self, **updates: Any) -> None:
        allowed = {"process_status", "process_pid", "process_start_time", "process_stop_time",
                   "process_heartbeat_time", "process_exit_code", "process_error"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"不允许写入 scdfe_c 状态字段: {sorted(unknown)}")
        if not updates:
            return
        assignments = ", ".join(f"{quote_identifier(name)}=?" for name in updates)
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"UPDATE {quote_identifier(CONTROL_TABLE)} SET {assignments}, _version=_version+1 WHERE id=1", [*updates.values()])
            connection.commit()

    def _apply_control_config(self, row: Mapping[str, Any]) -> bool:
        scan_interval = max(0.05, int(row.get("scan_interval_ms") or 2000) / 1000)
        poll_interval = max(0.0, int(row.get("poll_interval_ms") or 1000) / 1000)
        retry_interval = max(0.05, int(row.get("retry_interval_ms") or 5000) / 1000)
        # These values are passed into each communication worker at thread
        # creation time.  A live worker cannot observe changed constructor
        # arguments, so changing either interval must trigger a graceful
        # worker reload during the next reconcile cycle.
        worker_config_changed = (
            poll_interval != self.poll_interval or retry_interval != self.retry_interval
        )
        self.scan_interval = scan_interval
        self.poll_interval = poll_interval
        self.retry_interval = retry_interval
        # 0 is a valid explicit value; only a missing/NULL field should use
        # the throttled default so an incomplete control row cannot reintroduce a
        # burst of simultaneous protocol-thread starts.
        worker_start_delay_ms = row.get("worker_start_delay_ms")
        if worker_start_delay_ms is None:
            worker_start_delay_ms = 250
        self.worker_start_delay = max(0.0, int(worker_start_delay_ms) / 1000)
        retention_hours = int(row.get("log_retention_hours") or self.retention_hours)
        if retention_hours != self.retention_hours:
            self.retention_hours = retention_hours
            self._next_log_cleanup = 0.0
        configured = log_directory(self.store, row)
        if self._log_dir_from_control and configured != self.log_dir:
            self.log_dir = configured
            self.logger = ScdfeLogger(self.log_dir, process_name="scdfe_c-main",
                                      packet_directory=packet_log_directory(self.log_dir))
            self._next_log_summary = self._next_log_cleanup = 0.0
            worker_config_changed = True
        return worker_config_changed

    @property
    def workers(self) -> dict[ChannelKey, _ManagedWorker]:
        return dict(self._workers)

    def _update_process_state(self, rtu_id: int, **updates: Any) -> None:
        allowed = PROCESS_CONTROL_FIELDS - {"process_enable"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"不允许写入通信线程字段: {sorted(unknown)}")
        if not updates:
            return
        assignments = ", ".join(f"{quote_identifier(name)}=?" for name in updates)
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE fes_rtu_define SET {assignments}, _version=_version+1 WHERE rtu_id=?",
                [*updates.values(), rtu_id],
            )
            connection.commit()

    def _stop_worker(self, key: ChannelKey, *, reason: str,
                     unexpected: bool = False) -> bool:
        managed = self._workers.get(key)
        if managed is None:
            return True
        context = ((managed.log_context or channel_identity(managed.spec))
                   + f' 主进程PID={os.getpid()} 线程ID={_thread_id(managed.thread)}')
        self.logger.process(f"停止通信线程 停止原因={reason}；{context}")
        try:
            self._update_process_state(
                key.rtu_id, process_status=PROCESS_STOPPING,
                process_pid=_thread_id(managed.thread), process_stop_time=0,
                process_error=reason,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"写入通信线程停止状态失败: {exc}；{context}")
        managed.stop_event.set()
        managed.thread.join(timeout=max(WORKER_STOP_TIMEOUT, self.retry_interval))
        if managed.thread.is_alive():
            self.logger.error(
                "通信线程停止超时；"
                f"等待上限={max(WORKER_STOP_TIMEOUT, self.retry_interval):g}s，"
                f"保留线程记录，避免重复启动同一通道；停止原因={reason}；{context}",
            )
            try:
                self._update_process_state(
                    key.rtu_id, process_status=PROCESS_ERROR,
                    process_pid=_thread_id(managed.thread), process_heartbeat_time=_now_ms(),
                    process_exit_code=-1, process_error=f"通信线程停止超时: {reason}",
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"写入通信线程超时状态失败: {exc}；{context}")
            return False

        drained = self._drain_writer(timeout=max(1.0, self.retry_interval * 2))
        if not drained and self._writer_process is not None and self._writer_process.is_alive():
            self.logger.error(f"写库队列尚未排空，继续保留通道锁；{context}")
            return False
        self._workers.pop(key, None)
        final_status = PROCESS_ERROR if unexpected else PROCESS_STOPPED
        try:
            self._update_process_state(
                key.rtu_id, process_status=final_status, process_pid=0,
                process_stop_time=_now_ms(), process_heartbeat_time=_now_ms(),
                process_exit_code=-1 if unexpected else 0,
                process_error=(reason if unexpected else ""),
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"写入通信线程最终状态失败: {exc}；{context}")
        finally:
            managed.channel_lock.release()
        self.logger.process(f'通信线程已停止 停止原因={reason} '
                            f'退出性质={"异常" if unexpected else "正常"}；{context}')
        return True

    def _start_worker(self, spec: ChannelSpec, *, recovering: bool = False,
                      channel_lock: ServerInstanceLock | None = None) -> bool:
        if any(key.rtu_id == spec.rtu_id for key in self._workers):
            return False
        if channel_lock is None:
            channel_lock = _rtu_lock(spec.rtu_id)
            if not channel_lock.acquire():
                return False
        start_time = _now_ms()
        if self._last_worker_start:
            remaining = self.worker_start_delay - (time.monotonic() - self._last_worker_start)
            if remaining > 0:
                self.stop_event.wait(remaining)
                if self.stop_event.is_set():
                    channel_lock.release()
                    return False
        context = channel_context(self.store, spec)
        owner_context = context + f' 主进程PID={os.getpid()}'
        try:
            # 启动阶段先清空线程 ID，待新线程启动后写入其 native ID。
            updates: dict[str, Any] = {
                "process_status": PROCESS_RECOVERING if recovering else PROCESS_STARTING,
                "process_pid": 0, "process_start_time": start_time,
                "process_heartbeat_time": start_time, "process_exit_code": 0,
                "process_error": "",
            }
            self._update_process_state(spec.rtu_id, **updates)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"写入通信线程启动状态失败: {exc}；{owner_context}")
        event = threading.Event()
        try:
            thread = self.thread_factory(
                target=_worker_thread_entry,
                args=(self.store, {
                    "rtu_id": spec.rtu_id, "channel_id": spec.channel_id,
                    "channel_type": spec.channel_type, "name": spec.name,
                }, str(self.log_dir), self.poll_interval, self.retry_interval, event,
                self._writer_queue),
                name=f"scdfe-c-rtu-{spec.rtu_id}-ch-{spec.channel_id}",
                daemon=True,
            )
            thread.start()
            self._last_worker_start = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            try:
                self._update_process_state(
                    spec.rtu_id, process_status=PROCESS_ERROR, process_pid=0,
                    process_stop_time=_now_ms(), process_heartbeat_time=_now_ms(),
                    process_exit_code=-1, process_error=str(exc),
                )
            except Exception as state_exc:  # noqa: BLE001
                self.logger.error(f"写入通信线程启动错误状态失败: {state_exc}；{owner_context}")
            self.logger.error(f"启动通信线程失败: {type(exc).__name__}: {exc}；{owner_context}")
            channel_lock.release()
            return False
        self._workers[spec.key] = _ManagedWorker(spec, thread, event, channel_lock, log_context=context)
        thread_id = _thread_id(thread)
        if thread.is_alive():
            try:
                self._update_process_state(spec.rtu_id, process_pid=thread_id)
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"写入通信线程 ID 失败: {exc}；{owner_context}")
        self.logger.process(
            f'启动通信线程 启动原因={"异常恢复" if recovering else "配置启用或重载"} '
            f'采集间隔={self.poll_interval:g}s 重试间隔={self.retry_interval:g}s；'
            f'{owner_context} 线程ID={thread_id}')
        return True

    def reconcile(self) -> dict[str, int]:
        """扫描 RTU 表并同步通信线程集合，返回本轮启动/重载/停止数量。"""
        started = reloaded = stopped = 0
        if not self._recover_writer_failure():
            self._maintain_logs()
            return {"started": 0, "reloaded": 0, "stopped": 0, "running": len(self._workers)}
        control = self._control()
        config_changed = self._apply_control_config(control)
        self._maintain_logs()
        self._reload_pending = self._reload_pending or config_changed
        if self._reload_pending:
            if not self._pause_communication("scdfe_c 运行参数已更新"):
                return {"started": 0, "reloaded": 0, "stopped": 0, "running": len(self._workers)}
            self._reload_pending = False
            # The normal definition scan below starts them with the new path.
        now = _now_ms()
        if not int(control.get("process_enable", 0)):
            stopped = len(self._workers)
            self._update_control_state(process_status=PROCESS_STOPPING, process_pid=os.getpid(),
                                       process_heartbeat_time=now, process_error="")
            if self._process_lease:
                self._process_lease.heartbeat(PROCESS_STOPPING, force=True)
            # Exit cooperatively after children have been joined.  This keeps
            # SQLite/log cleanup intact on Windows and lets Web start a fresh
            # instance later.
            if self._pause_communication("主进程停止，先退出通信线程并排空写库队列"):
                self.stop_event.set()
            self._scan_count += 1
            return {"started": 0, "reloaded": 0, "stopped": stopped, "running": 0}
        self._update_control_state(process_status=PROCESS_RUNNING, process_pid=os.getpid(),
                                   process_heartbeat_time=now, process_error="")
        if self._process_lease:
            self._process_lease.heartbeat()
        if not self.store.get_row(PROCESS_TABLE, ["writer"])["process_enable"]:
            stopped = len(self._workers)
            if self._pause_communication("独立停止写库进程，暂停自动通信"):
                writer = self.store.get_row(PROCESS_TABLE, ["writer"])
                if writer["process_status"] != PROCESS_STOPPED or not writer["process_stop_time"]:
                    self.store.update_row(PROCESS_TABLE, ["writer"], {
                        "process_status": PROCESS_STOPPED, "process_pid": 0,
                        "process_stop_time": _now_ms(),
                    })
            self._scan_count += 1
            return {"started": 0, "reloaded": 0, "stopped": stopped, "running": len(self._workers)}
        if self._writer_stop_sent and not self._stop_writer():
            return {"started": 0, "reloaded": 0, "stopped": 0, "running": len(self._workers)}
        self._start_writer()
        specs = {spec.key: spec for spec in discover_channels(self.store)}
        for key in list(self._workers):
            if key not in specs:
                self._stop_worker(key, reason="RTU-通道已删除或禁用")
                stopped += 1
        next_control_check = 0.0
        for key, spec in specs.items():
            # Large RTU sets may take minutes to start. Observe stop requests
            # between starts, not only after finishing the complete scan.
            if self.stop_event.is_set():
                break
            if time.monotonic() >= next_control_check:
                self._maintain_logs()
                if (not self._control()["process_enable"] or
                        not self.store.get_row(PROCESS_TABLE, ["writer"])["process_enable"]):
                    break
                next_control_check = time.monotonic() + 0.2
            if self._process_lease:
                self._process_lease.heartbeat()
            managed = self._workers.get(key)
            # ``process_enable`` is an ownership boundary, not merely a stop
            # request. Once disabled, the supervisor must not rewrite
            # status/PID/error fields: a manual ``--rtu-id`` debugger owns
            # those while it holds the RTU lock.
            if not spec.process_enable:
                if managed is not None:
                    self._stop_worker(key, reason="RTU process_enable=0，停止通信线程")
                    stopped += 1
                continue
            lease = None
            if managed is None:
                lease = _rtu_lock(spec.rtu_id)
                if not lease.acquire():
                    # An independent debugger owns this RTU. Leave its
                    # runtime fields untouched.
                    continue
            try:
                if managed is None:
                    if self._start_worker(spec, channel_lock=lease):
                        lease = None  # Ownership transfers to the managed worker.
                        started += 1
                    continue
                if managed.spec != spec:
                    stopped_ok = self._stop_worker(key, reason="RTU 通道配置已修改，重新加载")
                    if stopped_ok and self._start_worker(spec):
                        reloaded += 1
                elif not managed.thread.is_alive():
                    stopped_ok = self._stop_worker(key, reason="通信线程已退出", unexpected=True)
                    if stopped_ok and self._start_worker(spec, recovering=True):
                        started += 1
            finally:
                if lease is not None:
                    lease.release()
        self._scan_count += 1
        return {"started": started, "reloaded": reloaded, "stopped": stopped,
                "running": len(self._workers)}

    def start(self) -> dict[str, int]:
        return self.reconcile()

    def stop(self) -> bool:
        self.stop_event.set()
        if self._process_lease:
            self._process_lease.heartbeat(PROCESS_STOPPING, force=True)
        if not self._pause_communication("收到停止请求"):
            return False
        # A worker may have stopped while its queued status writes were still
        # draining. With the writer stopped, finish cleanup and release leases.
        for key, managed in list(self._workers.items()):
            if not managed.thread.is_alive():
                self._stop_worker(key, reason="写库进程已停止，完成通道清理")
        try:
            self._update_control_state(process_status=PROCESS_STOPPED, process_pid=0,
                                       process_stop_time=_now_ms(), process_heartbeat_time=_now_ms(),
                                       process_exit_code=0)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"写入 scdfe_c 停止状态失败: {exc}")
        self.logger.process("scdfe_c 守护进程停止")
        return True

    def run(self, *, max_scans: int | None = None) -> None:
        # Keep the WAL and prepared statements warm between control reads,
        # discovery and heartbeat writes. Never share an idle handle via fork.
        if self.context.get_start_method() != "spawn":
            return self._run(max_scans=max_scans)
        self.store.connection_pool_size = 1
        try:
            self._run(max_scans=max_scans)
        finally:
            self.store.close()

    def _run(self, *, max_scans: int | None) -> None:
        self._process_lease = ProcessLease(self.store, "main", self.launch_token)
        failure = ""
        try:
            self._update_control_state(process_status=PROCESS_STARTING, process_pid=os.getpid(),
                                       process_start_time=_now_ms(), process_heartbeat_time=_now_ms(),
                                       process_exit_code=0, process_error="")
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"写入 scdfe_c 启动状态失败: {exc}")
        self.logger.process("scdfe_c 守护进程启动")
        try:
            while not self.stop_event.is_set():
                self.reconcile()
                if max_scans is not None and self._scan_count >= max_scans:
                    break
                # A long configured scan interval must not leave whole hours
                # without a main-process log. No extra thread is required.
                deadline = time.monotonic() + self.scan_interval
                while not self.stop_event.is_set():
                    self._maintain_logs()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.stop_event.wait(min(remaining, PROCESS_LOG_INTERVAL))
        except Exception as exc:
            failure = str(exc)
            raise
        finally:
            while not self.stop():
                time.sleep(0.2)
            self._process_lease.finish(failure)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SCADA scdfe_c RTU-通道守护采集程序")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR, help="JSON 表定义目录")
    parser.add_argument("--log-dir", type=Path, default=None, help="日志目录，默认 db/logs")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="采集周期（秒）")
    parser.add_argument("--scan-interval", type=float, default=2.0, help="RTU/定义扫描周期（秒）")
    parser.add_argument("--retry-interval", type=float, default=5.0, help="连接失败重试周期（秒）")
    parser.add_argument("--retention-hours", type=int, default=720, help="日志保留小时数")
    parser.add_argument("--worker-start-delay", type=float, default=0.25,
                        help="通信线程连续启动间隔（秒），数据库控制表优先")
    parser.add_argument("--max-scans", type=int, default=None, help="测试用：扫描指定次数后退出")
    parser.add_argument("--rtu-id", type=int, help="独立进程调试该 RTU，自动查找其通道")
    parser.add_argument("--max-cycles", type=int, help="独立调试：成功采集指定周期数后退出")
    parser.add_argument("--send-commands", action="store_true",
                        help="独立调试：允许下发待处理及变化的遥控/遥调（默认关闭）")
    parser.add_argument("--launch-token", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    debug = args.rtu_id is not None
    if debug and args.max_scans is not None:
        parser.error("独立调试请用 --max-cycles，不能使用 --max-scans")
    if not debug and (args.max_cycles is not None or args.send_commands):
        parser.error("--max-cycles/--send-commands 仅用于 --rtu-id 独立调试")
    if debug and not args.db.is_file():
        parser.error("独立调试需要已存在的数据库，请检查 --db 路径")
    if args.rtu_id is not None and args.rtu_id <= 0:
        parser.error("--rtu-id 必须为正整数")
    for name in ("max_cycles", "max_scans"):
        if getattr(args, name) is not None and getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} 必须为正整数")
    for name, zero_allowed in (("poll_interval", True), ("scan_interval", False),
                               ("retry_interval", False), ("worker_start_delay", True)):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (value == 0 and not zero_allowed):
            parser.error(f"--{name.replace('_', '-')} 必须为有限{'非负' if zero_allowed else '正'}数")
    if not 1 <= args.retention_hours <= 87600:
        parser.error("--retention-hours 必须在 1..87600 之间")
    lock = None
    previous_handlers = {}
    stop_event = threading.Event()
    try:
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is not None:
                # Only set a flag; cleanup happens outside the signal handler.
                previous_handlers[sig] = signal.signal(sig, lambda *_: stop_event.set())
        if debug:
            run_channel_debug(
                ScadaStore(args.db, args.schema_dir), args.rtu_id,
                log_dir=args.log_dir, poll_interval=args.poll_interval,
                retry_interval=args.retry_interval, scan_interval=args.scan_interval,
                retention_hours=args.retention_hours, max_cycles=args.max_cycles,
                send_commands=args.send_commands, stop_event=stop_event,
                launch_token=args.launch_token,
            )
            return 0
        lock = ServerInstanceLock()
        if not lock.acquire():
            print("本服务器已有 scdfe_c 自动守护实例运行，不能重复启动（与数据库路径无关）", flush=True)
            return 2
        supervisor = ScdfeSupervisor(
            args.db, schema_dir=args.schema_dir, log_dir=args.log_dir,
            poll_interval=args.poll_interval, scan_interval=args.scan_interval,
            retry_interval=args.retry_interval, retention_hours=args.retention_hours,
            worker_start_delay=args.worker_start_delay,
            launch_token=args.launch_token,
        )
        supervisor.stop_event = stop_event
        supervisor.run(max_scans=args.max_scans)
        return 0
    except ValueError as exc:
        print(f"scdfe_c: {exc}", flush=True)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(f"scdfe_c 运行失败: {exc}", flush=True)
        return 1
    finally:
        if lock is not None:
            lock.release()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def __getattr__(name: str):
    """Lazily expose the TCP command client without a package import cycle."""
    if name == "CommandClient":
        from .command_server import CommandClient
        return CommandClient
    raise AttributeError(name)


if __name__ == "__main__":  # pragma: no cover - exercised by CLI smoke tests
    raise SystemExit(main())

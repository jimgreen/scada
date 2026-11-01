"""SCADA ``scdfe_c`` 通道管理进程。

该模块是旧版 ``scdfe_c`` 的 Python 运行时替代层。父进程扫描
``fes_rtu_define``，每个 ``(rtu_id, channel_id)`` 启动一个独立通信线程；线程
加载当前协议主站，周期采集遥测/遥信并写回 SQLite，同时检测遥控/遥调命令
的变化并下发。主调度线程监视各通信线程和通道定义指纹，定义变化时平滑停止
旧线程并重新加载点表。数据库批量写入仍由独立进程负责，避免多个通信线程争抢
SQLite 写锁。

默认启动方式：

    python -m scdfe_c --db D:\\codex\\scada\\db\\scada.sqlite3

每个通信线程只拥有自己的协议主站和数据库短连接。SQLite 使用项目已有的
WAL、busy_timeout 和每次操作独立连接策略；不会在线程之间共享 SQLite 连接。
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
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
from queue import Empty, Full

from core.scdfe_control import CONTROL_TABLE, InstanceLock, LOG_PATTERN as LOG_FILE_RE, ensure_control_row, log_directory
from web.service import CHANNEL_PARAMETER_TABLES, DEFAULT_DB, SCHEMA_DIR, ScadaStore, quote_identifier

__all__ = [
    "ChannelKey",
    "ChannelSpec",
    "ChannelWorker",
    "ScdfeLogger",
    "ScdfeSupervisor",
    "build_master",
    "definition_signature",
    "discover_channels",
    "main",
    "PROCESS_STOPPED", "PROCESS_STARTING", "PROCESS_RUNNING",
    "PROCESS_STOPPING", "PROCESS_RESTARTING", "PROCESS_ERROR",
]


SUPPORTED_CHANNEL_TYPES = frozenset(CHANNEL_PARAMETER_TABLES)
LOG_CATEGORIES = ("normal", "alarm", "error", "process")
BATCH_WRITER_STOP = "__scdfe_c_batch_writer_stop__"
BATCH_WRITER_INTERVAL = 0.1
BATCH_WRITER_MAX_BATCH = 500
BATCH_WRITER_WAL_AUTOCHECKPOINT = 10000
WORKER_HEARTBEAT_INTERVAL = 5.0
WORKER_DEFINITION_CHECK_INTERVAL = 2.0
MEASUREMENT_HEARTBEAT_INTERVAL = 5.0
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
PROCESS_RESTARTING = 4
PROCESS_ERROR = 5
PROCESS_CONTROL_FIELDS = {
    "process_enable", "process_restart", "process_status", "process_pid",
    "process_start_time", "process_stop_time", "process_restart_time",
    "process_heartbeat_time", "process_exit_code", "process_restart_count",
    "process_error",
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
    address: str | None
    port: int | None
    name: str | None = None
    process_enable: bool = True
    process_restart: bool = False

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
        process_restart = int(_field(row, "process_restart", 0))
        if process_enable not in (0, 1) or process_restart not in (0, 1):
            raise ValueError(f"RTU {rtu_id} 的 process_enable/process_restart 必须是 0 或 1")
        return cls(
            rtu_id=rtu_id,
            channel_id=channel_id,
            channel_type=channel_type,
            address=_field(row, "chan_addr"),
            port=_field(row, "chan_port"),
            name=_field(row, "name"),
            process_enable=bool(process_enable),
            process_restart=bool(process_restart),
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

    每个逻辑进程独立使用一个按小时轮转的文件，例如
    ``scdfe_c-main-20261029-18.log``。日志级别保留在每行内容中，但不再
    按 normal/alarm/error/process 拆分文件。每条记录打开、追加并关闭文件，
    避免在线程和写库进程间共享 logging handler；主调度线程按小时数定期清理旧文件。
    """

    def __init__(self, directory: Path | str, *, process_name: str | None = None,
                 rtu_id: int | None = None, channel_id: int | None = None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.rtu_id = rtu_id
        self.channel_id = channel_id
        # Runtime callers provide an explicit process name.  The stable
        # fallback keeps library callers within the same hourly-file contract.
        self.process_name = process_name or "scdfe_c-unknown"

    def emit(self, category: str, message: str, *, level: str = "INFO") -> None:
        if category not in LOG_CATEGORIES:
            raise ValueError(f"未知日志类别: {category}")
        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        identity = ""
        if self.rtu_id is not None:
            identity = f" rtu={self.rtu_id} channel={self.channel_id}"
        line = f"{stamp} [{level}] pid={os.getpid()}{identity} {message}\n"
        hour = datetime.now().strftime("%Y%m%d-%H")
        path = self.directory / f"{self.process_name}-{hour}.log"
        with path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(line)
            stream.flush()

    def normal(self, message: str) -> None:
        self.emit("normal", message)

    def alarm(self, message: str) -> None:
        self.emit("alarm", message, level="WARNING")

    def error(self, message: str) -> None:
        self.emit("error", message, level="ERROR")

    def process(self, message: str) -> None:
        self.emit("process", message, level="PROCESS")

    @classmethod
    def cleanup(cls, directory: Path | str, *, retention_hours: int = 720,
                now: float | None = None) -> list[Path]:
        """清理超过保留小时数的按进程小时日志文件。"""
        if isinstance(retention_hours, bool) or retention_hours < 1:
            raise ValueError("retention_hours 必须是正整数")
        root = Path(directory).resolve()
        if not root.is_dir():
            return []
        cutoff = (time.time() if now is None else now) - retention_hours * 3600
        removed: list[Path] = []
        for path in root.glob("*.log"):
            if not LOG_FILE_RE.fullmatch(path.name):
                continue
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed.append(path)
            except OSError:
                # 日志清理失败不能终止采集进程；下次清理继续尝试。
                continue
        return removed

    @classmethod
    def list_files(cls, directory: Path | str) -> list[dict[str, Any]]:
        root = Path(directory).resolve()
        items = []
        if not root.is_dir():
            return items
        for path in root.glob("scdfe_c-*.log"):
            match = LOG_FILE_RE.match(path.name)
            if not match or not path.is_file():
                continue
            stat = path.stat()
            item = {"name": path.name, "process": match.group("process"),
                    "hour": match.group("hour"), "size": stat.st_size,
                    "modified_at": int(stat.st_mtime * 1000)}
            process_match = re.fullmatch(
                r"scdfe_c-rtu-(?P<rtu_id>\d+)-ch-(?P<channel_id>\d+)",
                match.group("process"),
            )
            if process_match is not None:
                item.update(rtu_id=int(process_match.group("rtu_id")),
                            channel_id=int(process_match.group("channel_id")))
            items.append(item)
        return sorted(items, key=lambda item: (item["modified_at"], item["name"]), reverse=True)

    @classmethod
    def read_file(cls, directory: Path | str, filename: str, *, tail: int = 200) -> dict[str, Any]:
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("日志文件名无效")
        if not 1 <= tail <= 5000:
            raise ValueError("tail 必须在 1 到 5000 之间")
        root = Path(directory).resolve()
        path = (root / filename).resolve()
        if path.parent != root or not path.is_file():
            raise FileNotFoundError(filename)
        match = LOG_FILE_RE.match(path.name)
        if not match:
            raise ValueError("日志文件名无效")
        data = path.read_bytes()
        text = data[-2_000_000:].decode("utf-8", "replace")
        lines = text.splitlines()[-tail:]
        item = {"name": path.name, "process": match.group("process"),
                "hour": match.group("hour"), "size": path.stat().st_size,
                "modified_at": int(path.stat().st_mtime * 1000), "content": "\n".join(lines)}
        process_match = re.fullmatch(
            r"scdfe_c-rtu-(?P<rtu_id>\d+)-ch-(?P<channel_id>\d+)",
            match.group("process"),
        )
        if process_match is not None:
            item.update(rtu_id=int(process_match.group("rtu_id")),
                        channel_id=int(process_match.group("channel_id")))
        return item


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
        fast_unique: set[tuple[str, tuple[str, ...]]] = set()
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
            # The producer guarantees uniqueness within its group only. If
            # another group touches the same keys, use the general merger
            # so partial updates still increment a point's version just once.
            if key_group in fast_unique or (unique_rows and key_group in fast_seen):
                fast_path = False
                break
            if unique_rows:
                fast_unique.add(key_group)
            # Table and key names are fixed for the entire group. Hash them
            # once per group instead of wrapping every point key in a token.
            seen_keys = (None if unique_rows else
                         fast_seen.setdefault(key_group, set()))
            for row in rows:
                if type(row) is not tuple:
                    if not isinstance(row, (tuple, list)):
                        raise ValueError("批量写入行参数数量不匹配")
                    row = tuple(row)
                if len(row) != param_count:
                    raise ValueError("批量写入行参数数量不匹配")
                if not unique_rows:
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


def _flush_batch(
    store: ScadaStore,
    logger: ScdfeLogger,
    messages: list[Mapping[str, Any]],
    queue: Any,
    connection: sqlite3.Connection | None = None,
) -> bool:
    if not messages:
        return True
    owned_connection = connection is None
    active_connection = connection
    try:
        prepared = _prepare_batch_updates(messages)
        if active_connection is None:
            active_connection = store.open_connection()
        for attempt in range(4):
            try:
                active_connection.execute("BEGIN IMMEDIATE")
                for sql, params in prepared.items():
                    active_connection.executemany(sql, params)
                active_connection.commit()
                return True
            except sqlite3.OperationalError as exc:
                if active_connection.in_transaction:
                    active_connection.rollback()
                if attempt == 3 or ("locked" not in str(exc).lower() and "busy" not in str(exc).lower()):
                    raise
                time.sleep(0.05 * (attempt + 1))
            except Exception:
                if active_connection.in_transaction:
                    active_connection.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 - malformed batch or database failure
        logger.error(f"批量写数据库失败（原始消息 {len(messages)}）: {exc}")
        return False
    finally:
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


def _batch_writer_entry(
    db_path: str,
    schema_dir: str,
    log_dir: str,
    queue: Any,
    batch_interval: float = BATCH_WRITER_INTERVAL,
    max_batch: int = BATCH_WRITER_MAX_BATCH,
) -> None:
    """Dedicated SQLite writer process for all communication-worker updates."""
    logger = ScdfeLogger(log_dir, process_name="scdfe_c-db-writer")
    pending: list[Mapping[str, Any]] = []
    pending_updates = 0
    connection: sqlite3.Connection | None = None
    try:
        store = ScadaStore(db_path, schema_dir)
        connection = store.open_connection()
        # The writer is the only process that commits measurement batches.
        # Keep WAL checkpoints out of the hot commit cadence; the normal
        # SQLite checkpoint policy remains unchanged for other connections.
        connection.execute(f"PRAGMA wal_autocheckpoint={BATCH_WRITER_WAL_AUTOCHECKPOINT}")
        logger.process("批量数据库写入进程启动")
        while True:
            try:
                message = queue.get(timeout=batch_interval)
            except Empty:
                batch, pending = pending, []
                pending_updates = 0
                if not _flush_batch(store, logger, batch, queue, connection):
                    raise RuntimeError("批量写数据库失败，写库进程退出等待监督器重建")
                continue
            if message == BATCH_WRITER_STOP:
                batch, pending = pending, []
                pending_updates = 0
                try:
                    flush_ok = _flush_batch(store, logger, batch, queue, connection)
                finally:
                    queue.task_done()
                if not flush_ok:
                    raise RuntimeError("批量写数据库失败，写库进程退出等待监督器重建")
                break
            pending.append(message)
            pending_updates += _batch_message_size(message)
            if pending_updates >= max_batch:
                batch, pending = pending, []
                pending_updates = 0
                if not _flush_batch(store, logger, batch, queue, connection):
                    raise RuntimeError("批量写数据库失败，写库进程退出等待监督器重建")
    except Exception as exc:  # noqa: BLE001 - parent reports/restarts the writer
        logger.error(f"批量数据库写入进程异常退出: {exc}")
        raise
    finally:
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
        logger.process("批量数据库写入进程停止")


def _signature_tables(store: ScadaStore) -> tuple[str, ...]:
    """Return the fixed table order used by the definition fingerprint."""
    return tuple(dict.fromkeys(
        ["fes_rtu_define", *CHANNEL_PARAMETER_TABLES.values(),
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
    try:
        active.execute("BEGIN")
        for table in _signature_tables(store):
            columns, projection, order_by = _signature_projection(store, table)
            digest.update(table.encode("utf-8"))
            digest.update(b"\0")
            order_clause = f" ORDER BY {order_by}" if order_by else ""
            column_indexes = range(len(columns))
            for row in active.execute(
                f"SELECT {projection} FROM {quote_identifier(table)} "
                f"WHERE rtu_id=?{order_clause}", (rtu_id,),
            ):
                encoded = json.dumps(
                    [_jsonable(row[index]) for index in column_indexes],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
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


def discover_channels(store: ScadaStore) -> list[ChannelSpec]:
    """遍历 RTU 表，返回已配置且支持的 RTU-通道。"""
    with store.connection() as connection:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT rtu_id, channel_id, channel_type, chan_addr, chan_port, name, "
            "process_enable, process_restart "
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


def _point_rows(store: ScadaStore, rtu_id: int) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    with store.connection() as connection:
        connection.execute("BEGIN")
        for table, kind in (("fes_yc_define", "yc"), ("fes_yx_define", "yx"),
                            ("fes_yk_define", "yk"), ("fes_yt_define", "yt")):
            result[kind] = [dict(row) for row in connection.execute(
                f"SELECT * FROM {quote_identifier(table)} WHERE rtu_id=? ORDER BY pnt_no",
                (rtu_id,),
            )]
    return result


def _default_point_map(store: ScadaStore, spec: ChannelSpec) -> dict[tuple[str, int], Any]:
    """为没有专用映射列的 BACnet/OPC 点表建立可覆盖的默认映射。"""
    points = _point_rows(store, spec.rtu_id)
    mapped: dict[tuple[str, int], Any] = {}
    if spec.channel_type == "bacnet":
        object_type = {
            "yc": "analog-value", "yx": "binary-input",
            "yk": "binary-output", "yt": "analog-output",
        }
        for kind, rows in points.items():
            for row in rows:
                number = int(row["pnt_no"])
                mapped[(kind, number)] = (object_type[kind], number)
    elif spec.channel_type == "opc":
        for kind, rows in points.items():
            for row in rows:
                number = int(row["pnt_no"])
                mapped[(kind, number)] = f"ns=2;s={kind.upper()}_{number:03d}"
    return mapped


def build_master(store: ScadaStore, spec: ChannelSpec):
    """按通道类型构造一个协议主站，但不连接设备。"""
    from api import (BacnetMaster, CanMaster, IEC104Master, ModbusRtuMaster,
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
    kwargs: dict[str, Any] = {}
    if spec.channel_type in {"bacnet", "opc"}:
        kwargs["point_map"] = _default_point_map(store, spec)
    return cls.from_store(store, spec.rtu_id, **kwargs)


def _measurement(item: Any) -> tuple[str | None, int | None, Any, Any]:
    """统一协议返回值为 kind、点号、值、质量。"""
    # This function runs once for every YC/YX on every poll.  Resolve the
    # container shape once instead of calling the generic four-field helper
    # repeatedly (which otherwise performs four isinstance checks per point).
    try:
        # All built-in protocol adapters return frozen dataclasses, so take
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
        self.logger = ScdfeLogger(log_dir, process_name=f"scdfe_c-rtu-{spec.rtu_id}-ch-{spec.channel_id}", rtu_id=spec.rtu_id, channel_id=spec.channel_id)
        self.stats = WorkerStats()
        self._command_seen: dict[tuple[str, int], tuple[Any, Any]] | None = None
        self._command_version: int | None = None
        self._last_signature: str | None = None
        self._next_definition_check = 0.0
        self._next_cycle_log = 0.0
        self._master: Any | None = None
        self._command_connection: sqlite3.Connection | None = None
        self._measurement_cache: dict[tuple[str, int], tuple[Any, Any, int, float]] = {}
        self._yc_scaling: dict[int, tuple[Any, Any]] | None = None

    def _load_yc_scaling(self) -> None:
        """Load once per connection/definition reload, never once per point."""
        if self.store is None:  # In-memory benchmarks without a database.
            scaling = {}
        else:
            with self.store.connection() as connection:
                scaling = {int(row[0]): (row[1], row[2]) for row in connection.execute(
                    "SELECT pnt_no, COALESCE(raw_base, 0.0), COALESCE(raw_scale, 1.0) "
                    "FROM fes_yc_define WHERE rtu_id=?", (self.spec.rtu_id,),
                )}
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

    def _queue_groups(self, groups: list[dict[str, Any]]) -> None:
        self.writer_queue.put(
            {"op": "batch", "groups": groups},
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
            self._queue_groups(list(groups.values()))
            # The writer owns the connection, so existence is confirmed when
            # it commits. The worker's counters represent accepted updates.
            return [True] * len(rows)
        changed: list[bool] = []
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for table, rtu_id, point_no, updates in rows:
                assignments = ", ".join(f"{quote_identifier(name)}=?" for name in updates)
                params = list(updates.values()) + [rtu_id, point_no]
                changed.append(bool(connection.execute(
                    f"UPDATE {quote_identifier(table)} SET {assignments}, _version=_version+1 "
                    "WHERE rtu_id=? AND pnt_no=?", params,
                ).rowcount))
            connection.commit()
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
            }, timeout=max(1.0, self.retry_interval))
            return
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE fes_rtu_define SET status=?, refresh_time=?, _version=_version+1 WHERE rtu_id=?",
                (int(status), _now_ms(), self.spec.rtu_id),
            )
            connection.commit()

    def _write_process_state(self, **updates: Any) -> None:
        """原子更新当前 RTU 的通信线程状态。"""
        allowed = PROCESS_CONTROL_FIELDS - {"process_enable", "process_restart"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"不允许写入通信线程字段: {sorted(unknown)}")
        if not updates:
            return
        if self.writer_queue is not None:
            self.writer_queue.put({
                "op": "update", "table": "fes_rtu_define",
                "key": {"rtu_id": self.spec.rtu_id},
                "values": dict(updates),
            }, timeout=max(1.0, self.retry_interval))
            return
        assignments = ", ".join(f"{quote_identifier(name)}=?" for name in updates)
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE fes_rtu_define SET {assignments}, _version=_version+1 WHERE rtu_id=?",
                [*updates.values(), self.spec.rtu_id],
            )
            connection.commit()

    def _measurement_state(self, kind: str, point_no: int, value: Any,
                           quality: Any) -> tuple[Any, Any, int]:
        if isinstance(quality, int):
            quality_value = int(quality)
        else:
            try:
                quality_value = int(getattr(quality, "value", quality or 0))
            except (TypeError, ValueError):
                quality_value = 0
        if kind == "yc":
            raw_value = float(value)
            base, scale = self._yc_scaling.get(point_no, (0.0, 1.0))
            engineering_value = float(base) + float(scale) * raw_value
            if not math.isfinite(raw_value) or not math.isfinite(engineering_value):
                raise ValueError(f"YC {point_no} 原始值或工程值不是有限数")
            return raw_value, engineering_value, quality_value
        if kind == "yx":
            value = int(bool(value))
            return value, value, quality_value
        raise ValueError(f"不支持的实时点类型: {kind}")

    def _queue_measurements(self, pending: dict[tuple[str, int], tuple[Any, Any, int, float]],
                            changed: set[tuple[str, int]], now: int) -> None:
        groups: dict[tuple[str, bool], dict[str, Any]] = {}
        rtu_id = self.spec.rtu_id
        for cache_key, (raw_value, value, quality, _) in pending.items():
            kind, point_no = cache_key
            heartbeat = cache_key not in changed
            shape = (kind, heartbeat)
            group = groups.get(shape)
            if group is None:
                table, columns, value_indexes = _MEASUREMENT_SHAPES[shape]
                group = {"table": table, "key_names": ("pnt_no", "rtu_id"),
                         "value_names": columns, "value_indexes": value_indexes,
                         # candidate_cache is keyed by (kind, point_no), so
                         # each producer group is unique by construction. The
                         # writer still validates row shape; this internal
                         # marker only avoids rebuilding a duplicate-key set.
                         "_unique_rows": True, "rows": []}
                groups[shape] = group
            if heartbeat:
                row = (now, point_no, rtu_id)
            elif kind == "yc":
                row = (raw_value, value, quality, now, point_no, rtu_id)
            else:
                row = (value, quality, now, point_no, rtu_id)
            group["rows"].append(row)
        self._queue_groups(list(groups.values()))

    def _sync_measurements(self, values: Any) -> tuple[int, int]:
        if self._yc_scaling is None:
            self._load_yc_scaling()
        yc = yx = 0
        queued = self.writer_queue is not None
        pending: list[tuple[str, int, int, Mapping[str, Any]]] = []
        metadata: list[tuple[str, tuple[str, int], tuple[Any, Any, int], float]] = []
        changed_points: set[tuple[str, int]] = set()
        # Only retain states seen in this result.  Copying the whole cache on
        # every poll made stable 200-point scans O(points) before any work was
        # selected, and is unnecessary because duplicate point numbers are the
        # only case that needs a tentative state within the current result.
        candidate_cache: dict[tuple[str, int], tuple[Any, Any, int, float]] = {}
        monotonic_now = time.monotonic()
        now = _now_ms()
        for item in values or ():
            try:
                kind, point_no, value, quality = _measurement(item)
                if kind not in {"yc", "yx"} or point_no is None:
                    continue
                state = self._measurement_state(kind, point_no, value, quality)
                cache_key = (kind, point_no)
                previous = candidate_cache.get(cache_key)
                if previous is None:
                    previous = self._measurement_cache.get(cache_key)
                value_changed = previous is None or previous[:3] != state
                heartbeat_due = (
                    previous is None
                    or self.measurement_heartbeat_interval == 0
                    or monotonic_now - previous[3] >= self.measurement_heartbeat_interval
                )
                if not value_changed and not heartbeat_due:
                    continue
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
                    params = ((*state, now) if kind == "yc" else (*state[1:], now)) if value_changed else (now,)
                    updates = {name: params[index] for name, index in zip(columns, value_indexes)}
                    pending.append((table, self.spec.rtu_id, point_no, updates))
                    metadata.append((kind, cache_key, state, monotonic_now))
                candidate_cache[cache_key] = (*state, monotonic_now)
            except Exception as exc:  # noqa: BLE001 - 单点异常不能中断整站采集
                self.stats.errors += 1
                self.logger.error(f"写回实时点失败: {exc}")
        if queued and candidate_cache:
            self._queue_measurements(candidate_cache, changed_points, now)
            # Publish cache state only after the entire message was accepted.
            # Full queues and failed puts leave every point eligible for retry.
            self._measurement_cache.update(candidate_cache)
        elif pending:
            changed = self._write_rows(pending)
            # _write_rows 保持输入顺序；数据库更新不存在的点时对应项为 False。
            for (kind, cache_key, state, write_time), was_changed in zip(metadata, changed):
                if was_changed:
                    self._measurement_cache[cache_key] = (*state, write_time)
                    if kind == "yc":
                        yc += 1
                    else:
                        yx += 1
        return yc, yx

    def _command_rows(self) -> list[tuple[str, dict[str, Any]]] | None:
        result: list[tuple[str, dict[str, Any]]] = []
        connection = self._read_connection()
        try:
            connection.execute("BEGIN")
            version = int(connection.execute(
                "SELECT COALESCE(SUM(_version), 0) FROM ("
                "SELECT _version FROM fes_yk_define WHERE rtu_id=? "
                "UNION ALL "
                "SELECT _version FROM fes_yt_define WHERE rtu_id=?"
                ")", (self.spec.rtu_id, self.spec.rtu_id),
            ).fetchone()[0] or 0)
            if self._command_seen is not None and self._command_version == version:
                connection.commit()
                return None
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
            try:
                self._send_command(kind, row)
                now = _now_ms()
                self._write_row(
                    "fes_yk_define" if kind == "yk" else "fes_yt_define",
                    self.spec.rtu_id, int(row["pnt_no"]),
                    {"status": 1, "cmd_handle_time": now, "refresh_time": now},
                )
                if kind == "yk":
                    sent_yk += 1
                else:
                    sent_yt += 1
                self.logger.normal(f"下发{kind.upper()}点 {row['pnt_no']} 成功，值={row.get('value')!r}")
            except Exception as exc:  # noqa: BLE001 - 通道必须继续服务其他点
                self.stats.errors += 1
                self._write_row(
                    "fes_yk_define" if kind == "yk" else "fes_yt_define",
                    self.spec.rtu_id, int(row["pnt_no"]),
                    {"status": 3, "cmd_handle_time": _now_ms()},
                )
                self.logger.error(f"下发{kind.upper()}点 {row['pnt_no']} 失败: {exc}")
        return sent_yk, sent_yt

    def _close_master(self) -> None:
        if self._master is not None:
            try:
                self._master.close()
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"关闭通道失败: {exc}")
            finally:
                self._master = None

    def run(self, *, max_cycles: int | None = None) -> WorkerStats:
        """运行采集循环；连接失败会告警并按 retry_interval 重试。

        This method never lets a channel exception escape to the supervisor's
        scheduler.  A failure is converted into this thread's error state,
        the protocol object is closed, and the channel retries independently.
        """
        self.logger.process(f"通信线程启动，规约={self.spec.channel_type}")
        cycles = 0
        thread_id = _current_thread_id()
        process_start = _now_ms()
        next_heartbeat = 0.0
        try:
            self._write_process_state(
                process_status=PROCESS_STARTING, process_pid=thread_id,
                process_start_time=process_start, process_heartbeat_time=process_start,
                process_exit_code=0, process_error="",
            )
        except Exception as exc:  # noqa: BLE001 - 状态记录失败不应阻断现场通讯
            self.logger.error(f"写入通信线程启动状态失败: {exc}")
        try:
            while not self.stop_event.is_set():
                try:
                    self._last_signature = definition_signature(
                        self.store, self.spec.rtu_id, self._read_connection(),
                    )
                    self._load_yc_scaling()
                    self._master = self.master_factory(self.store, self.spec)
                    self._master.connect()
                    self._measurement_cache.clear()
                    self._write_rtu_status(1)
                    self._write_process_state(
                        process_status=PROCESS_RUNNING,
                        process_pid=thread_id, process_heartbeat_time=_now_ms(),
                        process_error="",
                    )
                    next_heartbeat = time.monotonic() + WORKER_HEARTBEAT_INTERVAL
                    self.logger.process("通道连接成功，开始实时采集")
                    self.logger.normal("通道进入正常运行状态")
                    while not self.stop_event.is_set():
                        # 通信线程也做一次定义检查；即使主调度扫描周期较长，
                        # 单线程调用方式同样能自动退出并由上层重载。
                        monotonic_now = time.monotonic()
                        if monotonic_now >= self._next_definition_check:
                            current_signature = definition_signature(
                                self.store, self.spec.rtu_id, self._read_connection(),
                            )
                            self._next_definition_check = monotonic_now + max(
                                WORKER_DEFINITION_CHECK_INTERVAL, self.poll_interval,
                            )
                            if current_signature != self._last_signature:
                                self.logger.process("检测到通道或四遥定义变化，退出并请求重载")
                                return self.stats
                        values = self._master.collect_once()
                        yc, yx = self._sync_measurements(values)
                        sent_yk, sent_yt = self._sync_commands()
                        self.stats.cycles += 1
                        self.stats.updated_yc += yc
                        self.stats.updated_yx += yx
                        self.stats.sent_yk += sent_yk
                        self.stats.sent_yt += sent_yt
                        if monotonic_now >= next_heartbeat:
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
                            return self.stats
                        self.stop_event.wait(self.poll_interval)
                except Exception as exc:  # noqa: BLE001 - 线程应隔离并重试
                    self.stats.errors += 1
                    try:
                        self._write_rtu_status(3)
                    except Exception as db_exc:  # noqa: BLE001
                        self.logger.error(f"写入 RTU 错误状态失败: {db_exc}")
                    try:
                        self._write_process_state(
                            process_status=PROCESS_ERROR, process_pid=thread_id,
                            process_heartbeat_time=_now_ms(), process_error=str(exc),
                        )
                    except Exception as state_exc:  # noqa: BLE001
                        self.logger.error(f"写入通信线程错误状态失败: {state_exc}")
                    self.logger.error(f"通道运行异常: {exc}")
                    self.logger.alarm(f"通道断开或采集失败，将在 {self.retry_interval:g}s 后重试")
                    self.stop_event.wait(self.retry_interval)
                finally:
                    self._close_master()
        finally:
            try:
                self._write_rtu_status(0)
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"写入 RTU 停止状态失败: {exc}")
            try:
                self._write_process_state(
                process_status=PROCESS_STOPPED, process_pid=0,
                    process_stop_time=_now_ms(), process_heartbeat_time=_now_ms(),
                    process_exit_code=0,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"写入通信线程停止状态失败: {exc}")
            self._close_master()
            self._close_command_connection()
            self.logger.process(
                f"通信线程停止 cycles={self.stats.cycles} yc={self.stats.updated_yc} "
                f"yx={self.stats.updated_yx} yk={self.stats.sent_yk} yt={self.stats.sent_yt}"
            )
        return self.stats


def _worker_thread_entry(db_path: str, schema_dir: str, spec_data: dict[str, Any], log_dir: str,
                         poll_interval: float, retry_interval: float, stop_event: threading.Event,
                         writer_queue: Any | None = None) -> None:
    """通信线程入口；任何未预期异常都在本线程内收敛。"""
    spec = ChannelSpec(**spec_data)
    try:
        store = ScadaStore(db_path, schema_dir)
        ChannelWorker(store, spec, log_dir=log_dir, poll_interval=poll_interval,
                      retry_interval=retry_interval, stop_event=stop_event,
                      writer_queue=writer_queue).run()
    except Exception as exc:  # noqa: BLE001 - 主调度线程负责拉起并记录异常
        ScdfeLogger(log_dir, process_name=f"scdfe_c-rtu-{spec.rtu_id}-ch-{spec.channel_id}", rtu_id=spec.rtu_id, channel_id=spec.channel_id).error(
            f"通信线程异常退出: {exc}"
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
    started_at: float = field(default_factory=time.time)


class ScdfeSupervisor:
    """主调度线程：每个 RTU-通道一个通信线程，自动隔离和重载。"""

    def __init__(self, db_path: Path | str = DEFAULT_DB, *, schema_dir: Path | str = SCHEMA_DIR,
                 log_dir: Path | str | None = None, poll_interval: float = 1.0,
                 scan_interval: float = 2.0, retry_interval: float = 5.0,
                 retention_hours: int = 720, context: mp.context.BaseContext | None = None,
                 writer_batch_interval: float = BATCH_WRITER_INTERVAL,
                 writer_max_batch: int = BATCH_WRITER_MAX_BATCH,
                 worker_start_delay: float = 0.25,
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
        self.stop_event = threading.Event()
        self._workers: dict[ChannelKey, _ManagedWorker] = {}
        self._writer_queue: Any | None = None
        self._writer_process: mp.Process | None = None
        self._last_worker_start = 0.0
        self._scan_count = 0
        self.thread_factory = thread_factory or threading.Thread
        self.logger = ScdfeLogger(self.log_dir, process_name="scdfe_c-main")

    def _start_writer(self) -> None:
        if self._writer_process is not None and self._writer_process.is_alive():
            return
        queue = self.context.JoinableQueue(maxsize=10000)
        self._writer_queue = queue
        process = self.context.Process(
            target=_batch_writer_entry,
            args=(str(self.db_path), str(self.schema_dir), str(self.log_dir),
                  queue, self.writer_batch_interval, self.writer_max_batch),
            name="scdfe-c-db-writer",
        )
        process.daemon = True
        try:
            process.start()
        except Exception:
            self._writer_queue = None
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
        self.logger.process(f"启动批量数据库写入进程 pid={process.pid}")

    def _recover_writer_failure(self) -> None:
        process = self._writer_process
        if process is None or process.is_alive():
            return
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
                    f"通信线程未能在超时内退出 rtu={key.rtu_id} channel={key.channel_id}；"
                    "暂不启动重复线程",
                )
            try:
                if stopped:
                    self._update_process_state(
                        key.rtu_id, process_status=PROCESS_ERROR, process_pid=0,
                        process_stop_time=_now_ms(), process_heartbeat_time=_now_ms(),
                        process_exit_code=-1, process_error="批量数据库写入进程异常退出，通道正在重启",
                    )
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"写入写库进程故障后的通道状态失败: {exc}")
        self._stop_writer()

    def _drain_writer(self, timeout: float | None = None) -> bool:
        queue = self._writer_queue
        if queue is None:
            return True
        # JoinableQueue.join() is the only public way to wait for all
        # task_done() acknowledgements, but it has no timeout.  Run that
        # public operation in a short-lived daemon waiter and observe it from
        # this process.  Do not inspect CPython's private unfinished-task
        # semaphore: its implementation differs across Python/platform builds.
        wait_seconds = timeout if timeout is not None else max(1.0, self.retry_interval * 2)
        if wait_seconds <= 0:
            return False
        completed = threading.Event()

        def wait_for_queue() -> None:
            try:
                queue.join()
            finally:
                completed.set()

        waiter = threading.Thread(
            target=wait_for_queue,
            name="scdfe-c-db-writer-drain",
            daemon=True,
        )
        waiter.start()
        deadline = time.monotonic() + wait_seconds
        while not completed.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
            if self._writer_process is not None and not self._writer_process.is_alive():
                # A dead writer cannot acknowledge the remaining messages. The
                # supervisor will discard this queue and recreate it; return
                # promptly instead of waiting for a task that can never finish.
                return False
            if time.monotonic() >= deadline:
                return False
        return True

    def _stop_writer(self) -> None:
        process, queue = self._writer_process, self._writer_queue
        self._writer_process = None
        self._writer_queue = None
        if process is None or queue is None:
            return
        try:
            if process.is_alive():
                queue.put(BATCH_WRITER_STOP, timeout=max(1.0, self.retry_interval))
                process.join(timeout=max(2.0, self.retry_interval * 2))
        except (Full, OSError) as exc:
            self.logger.error(f"停止批量数据库写入进程失败: {exc}")
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
        try:
            queue.close()
            queue.join_thread()
        except (AttributeError, OSError):
            pass
        try:
            process.close()
        except (ValueError, OSError):
            pass

    def _control(self) -> dict[str, Any]:
        return self.store.get_row(CONTROL_TABLE, [1])

    def _update_control_state(self, **updates: Any) -> None:
        allowed = {"process_status", "process_pid", "process_start_time", "process_stop_time",
                   "process_restart_time", "process_heartbeat_time", "process_exit_code",
                   "process_restart_count", "process_error", "process_restart"}
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

    def _consume_control_restart(self) -> bool:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                f"UPDATE {quote_identifier(CONTROL_TABLE)} SET process_restart=0, _version=_version+1 WHERE id=1 AND process_restart<>0"
            ).rowcount
            connection.commit()
        return bool(changed)

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
        self.retention_hours = int(row.get("log_retention_hours") or self.retention_hours)
        configured = log_directory(self.store, row)
        if self._log_dir_from_control and configured != self.log_dir:
            self.log_dir = configured
            self.logger = ScdfeLogger(self.log_dir, process_name="scdfe_c-main")
            worker_config_changed = True
        return worker_config_changed

    @property
    def workers(self) -> dict[ChannelKey, _ManagedWorker]:
        return dict(self._workers)

    def _update_process_state(self, rtu_id: int, **updates: Any) -> None:
        allowed = PROCESS_CONTROL_FIELDS - {"process_enable", "process_restart"}
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

    def _consume_restart(self, rtu_id: int) -> bool:
        """消费一次性重启标志，避免同一请求在扫描周期内重复执行。"""
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE fes_rtu_define SET process_restart=0, _version=_version+1 "
                "WHERE rtu_id=? AND process_restart<>0", (rtu_id,),
            ).rowcount
            connection.commit()
        return bool(changed)

    def _stop_worker(self, key: ChannelKey, *, reason: str,
                     unexpected: bool = False) -> bool:
        managed = self._workers.get(key)
        if managed is None:
            return True
        self.logger.process(f"停止通信线程 rtu={key.rtu_id} channel={key.channel_id}: {reason}")
        try:
            self._update_process_state(
                key.rtu_id, process_status=PROCESS_STOPPING,
                process_pid=_thread_id(managed.thread), process_stop_time=0,
                process_error=reason,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"写入通信线程停止状态失败: {exc}")
        managed.stop_event.set()
        managed.thread.join(timeout=max(1.0, self.retry_interval))
        if managed.thread.is_alive():
            self.logger.error(
                f"通信线程停止超时 rtu={key.rtu_id} channel={key.channel_id}；"
                "保留线程记录，避免重复启动同一通道",
            )
            try:
                self._update_process_state(
                    key.rtu_id, process_status=PROCESS_ERROR,
                    process_pid=_thread_id(managed.thread), process_heartbeat_time=_now_ms(),
                    process_exit_code=-1, process_error=f"通信线程停止超时: {reason}",
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"写入通信线程超时状态失败: {exc}")
            return False

        self._workers.pop(key, None)
        self._drain_writer(timeout=max(1.0, self.retry_interval * 2))
        final_status = PROCESS_ERROR if unexpected else PROCESS_STOPPED
        try:
            self._update_process_state(
                key.rtu_id, process_status=final_status, process_pid=0,
                process_stop_time=_now_ms(), process_heartbeat_time=_now_ms(),
                process_exit_code=-1 if unexpected else 0,
                process_error=(reason if unexpected else ""),
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"写入通信线程最终状态失败: {exc}")
        return True

    def _start_worker(self, spec: ChannelSpec, *, restarted: bool = False) -> bool:
        start_time = _now_ms()
        if self._last_worker_start:
            remaining = self.worker_start_delay - (time.monotonic() - self._last_worker_start)
            if remaining > 0:
                self.stop_event.wait(remaining)
                if self.stop_event.is_set():
                    return False
        try:
            # 启动阶段先清空线程 ID，待新线程启动后写入其 native ID。
            updates: dict[str, Any] = {
                "process_status": PROCESS_RESTARTING if restarted else PROCESS_STARTING,
                "process_pid": 0, "process_start_time": start_time,
                "process_heartbeat_time": start_time, "process_exit_code": 0,
                "process_error": "",
            }
            if restarted:
                updates["process_restart_time"] = start_time
                with self.store.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "UPDATE fes_rtu_define SET process_restart_count="
                        "COALESCE(process_restart_count, 0)+1, "
                        "process_status=?, process_pid=?, process_start_time=?, "
                        "process_restart_time=?, process_heartbeat_time=?, "
                        "process_exit_code=0, process_error='', _version=_version+1 "
                        "WHERE rtu_id=?",
                        (PROCESS_RESTARTING, 0, start_time, start_time, start_time, spec.rtu_id),
                    )
                    connection.commit()
            else:
                self._update_process_state(spec.rtu_id, **updates)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"写入通信线程启动状态失败: {exc}")
        event = threading.Event()
        try:
            thread = self.thread_factory(
                target=_worker_thread_entry,
                args=(str(self.db_path), str(self.schema_dir), {
                    "rtu_id": spec.rtu_id, "channel_id": spec.channel_id,
                    "channel_type": spec.channel_type, "address": spec.address,
                    "port": spec.port, "name": spec.name,
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
                self.logger.error(f"写入通信线程启动错误状态失败: {state_exc}")
            self.logger.error(f"启动通信线程失败 rtu={spec.rtu_id} channel={spec.channel_id}: {exc}")
            return False
        self._workers[spec.key] = _ManagedWorker(spec, thread, event)
        thread_id = _thread_id(thread)
        if thread.is_alive():
            try:
                self._update_process_state(spec.rtu_id, process_pid=thread_id)
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"写入通信线程 ID 失败: {exc}")
        self.logger.process(
            f"启动通信线程 tid={thread_id} "
            f"rtu={spec.rtu_id} channel={spec.channel_id}",
        )
        return True

    def reconcile(self) -> dict[str, int]:
        """扫描 RTU 表并同步通信线程集合，返回本轮启动/重载/停止数量。"""
        started = reloaded = stopped = 0
        self._recover_writer_failure()
        control = self._control()
        config_changed = self._apply_control_config(control)
        if config_changed:
            for key in list(self._workers):
                self._stop_worker(key, reason="scdfe_c 运行参数已更新")
            self._stop_writer()
            # The normal definition scan below starts them with the new path.
        restart_all = bool(control.get("process_restart")) and self._consume_control_restart()
        if restart_all:
            for key in list(self._workers):
                self._stop_worker(key, reason="scdfe_c process_restart=1，执行全量重启")
                stopped += 1
            now = _now_ms()
            self._update_control_state(process_status=PROCESS_RESTARTING, process_pid=os.getpid(),
                                       process_restart_time=now, process_heartbeat_time=now,
                                       process_restart_count=int(control.get("process_restart_count") or 0) + 1,
                                       process_error="")
        now = _now_ms()
        if not int(control.get("process_enable", 0)):
            for key in list(self._workers):
                self._stop_worker(key, reason="scdfe_c process_enable=0，停止通信线程")
                stopped += 1
            self._update_control_state(process_status=PROCESS_STOPPED, process_pid=os.getpid(),
                                       process_heartbeat_time=now, process_error="")
            # Exit cooperatively after children have been joined.  This keeps
            # SQLite/log cleanup intact on Windows and lets Web start a fresh
            # instance later.
            self.stop_event.set()
            self._stop_writer()
            self._scan_count += 1
            return {"started": 0, "reloaded": 0, "stopped": stopped, "running": 0}
        self._update_control_state(process_status=PROCESS_RUNNING, process_pid=os.getpid(),
                                   process_heartbeat_time=now, process_error="")
        self._start_writer()
        specs = {spec.key: spec for spec in discover_channels(self.store)}
        for key in list(self._workers):
            if key not in specs:
                self._stop_worker(key, reason="RTU-通道已删除或禁用")
                stopped += 1
        for key, spec in specs.items():
            managed = self._workers.get(key)
            restart_requested = spec.process_restart and self._consume_restart(spec.rtu_id)
            if not spec.process_enable:
                if managed is not None:
                    self._stop_worker(key, reason="RTU process_enable=0，停止通信线程")
                    stopped += 1
                else:
                    try:
                        with self.store.connection() as connection:
                            row = connection.execute(
                                "SELECT process_status, process_pid, process_error "
                                "FROM fes_rtu_define WHERE rtu_id=?", (spec.rtu_id,),
                            ).fetchone()
                        ignored_error = "process_enable=0，忽略重启请求" if restart_requested else ""
                        if row is not None and (
                                row["process_status"] != PROCESS_STOPPED
                                or row["process_pid"] != 0
                                or row["process_error"] != ignored_error):
                            self._update_process_state(
                                spec.rtu_id, process_status=PROCESS_STOPPED,
                                process_pid=0, process_stop_time=_now_ms(),
                                process_heartbeat_time=_now_ms(),
                                process_error=ignored_error,
                            )
                    except Exception as exc:  # noqa: BLE001
                        self.logger.error(f"写入禁用进程状态失败: {exc}")
                continue
            if restart_requested:
                stopped_ok = True
                if managed is not None:
                    stopped_ok = self._stop_worker(key, reason="RTU process_restart=1，执行重启")
                if stopped_ok and self._start_worker(spec, restarted=True):
                    reloaded += 1
                continue
            if managed is None:
                if self._start_worker(spec):
                    started += 1
                continue
            if not managed.thread.is_alive():
                stopped_ok = self._stop_worker(key, reason="通信线程已退出", unexpected=True)
                if stopped_ok and self._start_worker(spec, restarted=True):
                    started += 1
                continue
        self._scan_count += 1
        removed = ScdfeLogger.cleanup(self.log_dir, retention_hours=self.retention_hours)
        if removed:
            self.logger.process(f"日志清理完成，删除 {len(removed)} 个过期文件")
        return {"started": started, "reloaded": reloaded, "stopped": stopped,
                "running": len(self._workers)}

    def start(self) -> dict[str, int]:
        return self.reconcile()

    def stop(self) -> None:
        self.stop_event.set()
        for key in list(self._workers):
            self._stop_worker(key, reason="收到停止请求")
        self._drain_writer(timeout=max(1.0, self.retry_interval * 2))
        self._stop_writer()
        try:
            self._update_control_state(process_status=PROCESS_STOPPED, process_pid=0,
                                       process_stop_time=_now_ms(), process_heartbeat_time=_now_ms(),
                                       process_exit_code=0)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"写入 scdfe_c 停止状态失败: {exc}")
        self.logger.process("scdfe_c 守护进程停止")

    def run(self, *, max_scans: int | None = None) -> None:
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
                self.stop_event.wait(self.scan_interval)
        finally:
            self.stop()


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    lock = InstanceLock(args.db, "main")
    if not lock.acquire():
        print("scdfe_c 已有实例运行", flush=True)
        return 2
    supervisor = ScdfeSupervisor(
        args.db, schema_dir=args.schema_dir, log_dir=args.log_dir,
        poll_interval=args.poll_interval, scan_interval=args.scan_interval,
        retry_interval=args.retry_interval, retention_hours=args.retention_hours,
        worker_start_delay=args.worker_start_delay,
    )
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            # Signal handlers must only set a flag.  Doing SQLite writes and
            # joining communication threads inside a handler can interrupt an
            # in-progress reconcile and leave the control row half-updated.
            signal.signal(sig, lambda *_: supervisor.stop_event.set())
    try:
        supervisor.run(max_scans=args.max_scans)
        return 0
    finally:
        lock.release()


if __name__ == "__main__":  # pragma: no cover - exercised by CLI smoke tests
    raise SystemExit(main())

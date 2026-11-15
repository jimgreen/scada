"""Low-frequency, cross-process runtime status snapshots.

Protocol workers keep their hot state in memory and continue to use the
existing definition tables for process lifecycle fields.  This module stores a
small, uniform snapshot for Web/HMI readers.  Writes are coalesced per process
and throttled so a fast polling loop or a high-rate protocol does not turn the
status table into a database bottleneck.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from datetime import datetime
import json
import math
import sqlite3
import threading
import time
from typing import Any


TABLE = "fes_channel_runtime_status"
DEFAULT_WRITE_INTERVAL_MS = 1000

_KEYS = ("service_name", "channel_id")
_VALUE_FIELDS = (
    "protocol", "channel_name", "state", "running", "connection_state",
    "process_status", "process_pid", "process_start_time", "process_stop_time",
    "process_heartbeat_time", "process_exit_code", "process_error", "rx_count",
    "tx_count", "error_count", "dropped_count", "reconnect_count", "last_rx_time",
    "last_tx_time", "last_error_time", "last_error", "diagnostic_json", "sampled_at",
)
_UPSERT_COLUMNS = (*_KEYS, *_VALUE_FIELDS)
_UPSERT_SQL = (
    f'INSERT INTO "{TABLE}" (' + ', '.join(f'"{name}"' for name in _UPSERT_COLUMNS)
    + ') VALUES (' + ', '.join('?' for _ in _UPSERT_COLUMNS)
    + ') ON CONFLICT("service_name", "channel_id") DO UPDATE SET '
    + f'"_version"="{TABLE}"."_version"+1, '
    + ', '.join(f'"{field}"=excluded."{field}"' for field in _VALUE_FIELDS)
)


def now_ms() -> int:
    return int(time.time() * 1000)


# Traffic and heartbeat samples are periodic. Lifecycle, error and loss
# transitions still publish immediately, even inside the sampling interval.
_IMMEDIATE_FIELDS = tuple(field for field in _VALUE_FIELDS if field not in {
    "rx_count", "tx_count", "last_rx_time", "last_tx_time", "last_error_time",
    "process_heartbeat_time", "diagnostic_json", "sampled_at",
})


def state_for(process_status: Any, running: Any = False, error: Any = "") -> str:
    """Map existing numeric lifecycle fields to a stable API/database state."""
    try:
        status = int(process_status or 0)
    except (TypeError, ValueError):
        status = 0
    if status == 5 or error:
        return "error"
    if bool(running) or status == 2:
        return "running"
    return {
        1: "starting",
        3: "stopping",
        4: "degraded",
    }.get(status, "stopped")


def _json_safe(value: Any) -> Any:
    # Diagnostics mostly contain strings and counters. Handle exact built-in
    # scalars before the more expensive Mapping/sequence ABC checks. Keep the
    # recursive path for mixed keys, bytes, datetimes and non-finite floats.
    kind = type(value)
    if value is None or kind is str or kind is int or kind is bool:
        return value
    if kind is float:
        return value if math.isfinite(value) else None
    if kind is dict or isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex(" ")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "_asdict"):
        return _json_safe(value._asdict())
    return value


def diagnostic_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except (TypeError, ValueError):
            value = {"message": value}
    try:
        return json.dumps(_json_safe(value or {}), ensure_ascii=False,
                          allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return json.dumps({"message": str(value)}, ensure_ascii=False,
                          separators=(",", ":"))


def diagnostic_value(value: Any) -> Any:
    """Return a JSON object/list for API consumers from a stored diagnostic."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {"message": value}
        return parsed
    return _json_safe(value or {})


def public_runtime_row(row: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    """Expose the snapshot with parsed diagnostics while retaining raw JSON."""
    item = dict(row)
    item["diagnostic"] = diagnostic_value(item.get("diagnostic_json", "{}"))
    return item


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class RuntimeStatusStore:
    """Throttled upsert/read facade for ``fes_channel_runtime_status``."""

    def __init__(self, store, service_name: str, *, write_interval_ms: int = DEFAULT_WRITE_INTERVAL_MS):
        if not isinstance(service_name, str) or not service_name.strip():
            raise ValueError("service_name 必须是非空字符串")
        self.store = store
        self.service_name = service_name.strip()
        self.write_interval_ms = max(0, int(write_interval_ms))
        self._lock = threading.Lock()
        self._last_write: dict[int, tuple[float, tuple[Any, ...]]] = {}
        self._sequence = 0
        self._last_applied: dict[int, int] = {}

    def upsert(self, channel_id: Any, values: Mapping[str, Any] | None = None,
               *, force: bool = False, connection=None) -> bool:
        snapshot = self._prepare(channel_id, values, force=force)
        if snapshot is None:
            return False
        return bool(_write_snapshots(self.store, [snapshot], connection=connection))

    def _due(self, snapshot, clock):
        # Called under _lock. An older capture delayed acquiring its database
        # connection must not overwrite a later capture already applied.
        _, channel_id, _, signature, sequence, force = snapshot
        if sequence <= self._last_applied.get(channel_id, 0):
            return False
        previous = self._last_write.get(channel_id)
        return (force or previous is None or previous[1] != signature
                or (clock - previous[0]) * 1000 >= self.write_interval_ms)

    def _prepare(self, channel_id, values=None, *, force=False):
        channel_id = _integer(channel_id)
        if channel_id <= 0:
            return None
        payload = {field: (values or {}).get(field) for field in _VALUE_FIELDS}
        payload["protocol"] = str(payload.get("protocol") or "")
        payload["channel_name"] = str(payload.get("channel_name") or "")
        payload["state"] = str(payload.get("state") or "unknown")
        payload["running"] = int(bool(payload.get("running")))
        payload["connection_state"] = str(payload.get("connection_state") or "unknown")
        for field in (
            "process_status", "process_pid", "process_start_time", "process_stop_time",
            "process_heartbeat_time", "process_exit_code", "rx_count", "tx_count",
            "error_count", "dropped_count", "reconnect_count", "last_rx_time",
            "last_tx_time", "last_error_time",
        ):
            payload[field] = _integer(payload[field])
        payload["process_error"] = str(payload.get("process_error") or "")
        payload["last_error"] = str(payload.get("last_error") or "")
        sampled_at = _integer(payload.get("sampled_at")) or now_ms()
        payload["sampled_at"] = sampled_at
        signature = tuple(payload[field] for field in _IMMEDIATE_FIELDS)
        with self._lock:
            self._sequence += 1
            snapshot = (self, channel_id, payload, signature, self._sequence, force)
            if not self._due(snapshot, time.monotonic()):
                return None
        # Encode only due diagnostics, before entering the write transaction.
        payload["diagnostic_json"] = diagnostic_json(payload.get("diagnostic_json"))
        return snapshot

    def invalidate(self, channel_id: Any) -> None:
        """Forget a write recorded in a transaction that was later rolled back."""
        with self._lock:
            self._last_write.pop(_integer(channel_id), None)

    def list(self, *, channel_id: Any | None = None) -> list[dict[str, Any]]:
        sql = f'SELECT * FROM "{TABLE}" WHERE "service_name"=?'
        params: list[Any] = [self.service_name]
        if channel_id is not None:
            sql += ' AND "channel_id"=?'
            params.append(_integer(channel_id))
        sql += ' ORDER BY "channel_id"'
        with self.store.connection() as connection:
            return [public_runtime_row(row) for row in connection.execute(sql, params)]


def _write_snapshots(store, snapshots, *, connection=None):
    if not snapshots:
        return 0
    if connection is None:
        with store.connection() as current:
            return _write_snapshots(store, snapshots, connection=current)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        # Database transaction BEFORE publisher locks: an outer-transaction
        # caller may already own SQLite's writer lock. Recheck after waiting,
        # then commit/cache under the same locks to preserve publication order.
        with ExitStack() as locks:
            for publisher in sorted({row[0] for row in snapshots}, key=id):
                locks.enter_context(publisher._lock)
            selected = [row for row in snapshots if row[0]._due(row, time.monotonic())]
            if selected:
                # All snapshots in a monitor batch use the same UPSERT shape.
                # executemany keeps one transaction and avoids one Python/SQLite
                # call per channel while preserving each channel's row update.
                connection.executemany(_UPSERT_SQL, [
                    [publisher.service_name, channel_id,
                     *(payload[field] for field in _VALUE_FIELDS)]
                    for publisher, channel_id, payload, _, _, _ in selected
                ])
            if owns_transaction:
                connection.commit()
            clock = time.monotonic()
            for publisher, channel_id, _, signature, sequence, _ in selected:
                publisher._last_applied[channel_id] = sequence
                if owns_transaction:
                    publisher._last_write[channel_id] = (clock, signature)
                else:
                    # An outer owner may roll back later. Never throttle from
                    # an unconfirmed transaction, even without invalidate().
                    publisher._last_write.pop(channel_id, None)
            return len(selected)
    except BaseException:
        if owns_transaction and connection.in_transaction:
            connection.rollback()
        raise


def write_runtime_status_batch(store, pending):
    """Atomically publish due snapshots; skipped polls take no write lock.

    Callers retain their lifecycle locks from capture until this returns.
    Traffic is sampled from the next call's latest state, not queued as events.
    """
    snapshots = []
    for publisher, channel_id, values in pending:
        if publisher.store is not store:
            raise ValueError("运行状态批次必须使用同一数据库 store")
        snapshot = publisher._prepare(channel_id, values)
        if snapshot is not None:
            snapshots.append(snapshot)
    return _write_snapshots(store, snapshots)


def runtime_status_rows(store, *, service_name: str | None = None,
                        channel_id: Any | None = None,
                        channel_ids: Any | None = None) -> list[dict[str, Any]]:
    """Read HMI snapshots with indexed queries bounded by SQLite's bind limit.

    ``channel_ids`` keeps paginated multi-RTU monitors from scanning and
    decoding snapshots for channels that are not present on the current page.
    The scalar ``channel_id`` form remains the cheaper single-channel path.
    """
    clauses, params = [], []
    if service_name:
        clauses.append('"service_name"=?')
        params.append(service_name)
    if channel_id is not None:
        clauses.append('"channel_id"=?')
        params.append(_integer(channel_id))
    elif channel_ids is not None:
        ids, seen = [], set()
        for value in channel_ids:
            current = _integer(value)
            if current > 0 and current not in seen:
                seen.add(current)
                ids.append(current)
        if not ids:
            return []
        # Large HMI selections can exceed SQLite's per-statement parameter
        # budget. Keep one read snapshot across chunks and preserve SQL order.
        with store.connection() as connection:
            size = connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - len(params)
            if size < 1:
                raise sqlite3.OperationalError('SQLite variable limit is too small for channel selection')
            owns_transaction = len(ids) > size and not connection.in_transaction
            if owns_transaction:
                connection.execute('BEGIN')
            try:
                rows = []
                for start in range(0, len(ids), size):
                    chunk = ids[start:start + size]
                    where = ' AND '.join([*clauses, '"channel_id" IN (' + ','.join('?' for _ in chunk) + ')'])
                    rows.extend(public_runtime_row(row) for row in connection.execute(
                        f'SELECT * FROM "{TABLE}" WHERE {where} ORDER BY "service_name", "channel_id"',
                        [*params, *chunk]))
                if owns_transaction:
                    connection.commit()
                return sorted(rows, key=lambda row: (row['service_name'], row['channel_id'])) if len(ids) > size else rows
            except BaseException:
                if owns_transaction:
                    connection.rollback()
                raise
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with store.connection() as connection:
        return [public_runtime_row(row) for row in connection.execute(
            f'SELECT * FROM "{TABLE}"{where} ORDER BY "service_name", "channel_id"', params
        )]

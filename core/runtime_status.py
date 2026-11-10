"""Low-frequency, cross-process runtime status snapshots.

Protocol workers keep their hot state in memory and continue to use the
existing definition tables for process lifecycle fields.  This module stores a
small, uniform snapshot for Web/HMI readers.  Writes are coalesced per process
and throttled so a fast polling loop or a high-rate protocol does not turn the
status table into a database bottleneck.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from datetime import datetime
import json
import math
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


def now_ms() -> int:
    return int(time.time() * 1000)


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
    if isinstance(value, Mapping):
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
        self._last_write: dict[int, tuple[int, tuple[Any, ...]]] = {}

    def upsert(self, channel_id: Any, values: Mapping[str, Any] | None = None,
               *, force: bool = False, connection=None) -> bool:
        channel_id = _integer(channel_id)
        if channel_id <= 0:
            return False
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
        payload["diagnostic_json"] = diagnostic_json(payload.get("diagnostic_json"))
        sampled_at = _integer(payload.get("sampled_at")) or now_ms()
        payload["sampled_at"] = sampled_at
        signature = tuple(payload[field] for field in _VALUE_FIELDS if field != "sampled_at")
        with self._lock:
            previous = self._last_write.get(channel_id)
            if (not force and previous is not None
                    and sampled_at - previous[0] < self.write_interval_ms
                    and previous[1] == signature):
                return False
            self._last_write[channel_id] = (sampled_at, signature)

        names = ["service_name", "channel_id", *_VALUE_FIELDS]
        values_to_bind = [self.service_name, channel_id, *(payload[field] for field in _VALUE_FIELDS)]
        quoted_names = ", ".join(f'"{name}"' for name in names)
        assignments = ", ".join(
            f'"{field}"=excluded."{field}"' for field in _VALUE_FIELDS
        )
        sql = (
            f'INSERT INTO "{TABLE}" ({quoted_names}) '
            f'VALUES ({", ".join("?" for _ in names)}) '
            f'ON CONFLICT("service_name", "channel_id") DO UPDATE SET '
            f'"_version"="{TABLE}"."_version"+1, {assignments}'
        )
        try:
            context = nullcontext(connection) if connection is not None else self.store.connection()
            with context as current:
                current.execute("BEGIN IMMEDIATE")
                current.execute(sql, values_to_bind)
                current.commit()
            return True
        except Exception:
            with self._lock:
                self._last_write.pop(channel_id, None)
            raise

    def list(self, *, channel_id: Any | None = None) -> list[dict[str, Any]]:
        sql = f'SELECT * FROM "{TABLE}" WHERE "service_name"=?'
        params: list[Any] = [self.service_name]
        if channel_id is not None:
            sql += ' AND "channel_id"=?'
            params.append(_integer(channel_id))
        sql += ' ORDER BY "channel_id"'
        with self.store.connection() as connection:
            return [public_runtime_row(row) for row in connection.execute(sql, params)]


def runtime_status_rows(store, *, service_name: str | None = None,
                        channel_id: Any | None = None) -> list[dict[str, Any]]:
    """Read snapshots with one indexed query for HMI/API projections."""
    clauses, params = [], []
    if service_name:
        clauses.append('"service_name"=?')
        params.append(service_name)
    if channel_id is not None:
        clauses.append('"channel_id"=?')
        params.append(_integer(channel_id))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with store.connection() as connection:
        return [public_runtime_row(row) for row in connection.execute(
            f'SELECT * FROM "{TABLE}"{where} ORDER BY "service_name", "channel_id"', params
        )]

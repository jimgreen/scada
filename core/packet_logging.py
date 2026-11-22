"""Per-channel packet recording policy backed by the service database.

The channel definition is the only source of truth. There is deliberately no
service-wide default or sidecar JSON file: an unbound or unknown channel never
records packets, and operators change the policy through the channel editor.
"""
from functools import lru_cache
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

_COMPAT_LOCK_GUARD = threading.Lock()
_COMPAT_LOCKS = {}


def _compat_lock(path):
    key = str(Path(path).resolve())
    with _COMPAT_LOCK_GUARD:
        lock = _COMPAT_LOCKS.get(key)
        if lock is None:
            lock = _COMPAT_LOCKS[key] = threading.RLock()
        return lock


class ChannelPacketLogSwitch:
    """A channel binding shares the cached policy and keeps its channel id."""

    def __init__(self, switch, channel_id):
        self.switch = switch
        self.channel_id = channel_id

    def enabled(self, *, refresh=False):
        return self.switch.enabled(self.channel_id, refresh=refresh)


class DatabasePacketLogSwitch:
    """Read and cache ``packet_recording`` from channel definition rows."""

    _TABLES = {"c": "fes_rtu_define", "s": "fes_rtu_define", "t": "fes_scdfe_t_define"}

    def __init__(self, db_path, family):
        if family not in self._TABLES:
            raise ValueError("未知服务")
        self.db_path = Path(db_path).resolve()
        self.family = family
        self.table = self._TABLES[family]
        self._lock = threading.Lock()
        self._cache = {}
        self._next_check = {}
        self._bulk_next_check = 0.0
        self._db_rows = None
        self._legacy_cache = None
        self._refreshing = False
        self._db_stat = None
        self._legacy_stat = None

    @property
    def path(self):
        """Compatibility path for pre-channel packet policy clients.

        Current Web writes go to ``packet_recording`` in the channel table.
        The small sidecar is retained only for old in-process callers that
        used a service-wide default before channel policies were introduced.
        It is never consulted for a channel row with an explicit 0/1 value.
        """
        return Path(f"{self.db_path}.{self.family}.packet-recording.json")

    def _legacy_policy(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
                raise ValueError("报文记录兼容配置格式无效")
            channels = payload.get("channels", {})
            if not isinstance(channels, dict):
                raise ValueError("报文记录兼容通道配置格式无效")
            for key, value in channels.items():
                self._channel_key(key)
                if type(value) is not bool:
                    raise ValueError("报文记录兼容通道配置必须是布尔值")
            return payload
        except FileNotFoundError:
            return {"enabled": False, "channels": {}}

    def _write_legacy_policy(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            temporary.write_text(content, encoding="utf-8")
            # Windows 上其他进程同时读文件时 replace 可能瞬态触发 PermissionError，做短暂退避重试。
            for attempt in range(5):
                try:
                    temporary.replace(self.path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _channel_key(channel_id):
        if isinstance(channel_id, bool):
            raise ValueError("channel_id 必须为非负整数")
        if isinstance(channel_id, int):
            value = channel_id
        elif isinstance(channel_id, str) and channel_id.isascii() and channel_id.isdecimal():
            value = int(channel_id)
        else:
            raise ValueError("channel_id 必须为非负整数")
        if value < 0:
            raise ValueError("channel_id 必须为非负整数")
        return value

    def _read_all(self):
        try:
            with closing(sqlite3.connect(self.db_path, timeout=2)) as con:
                rows = con.execute(
                    f"SELECT channel_id, packet_recording FROM {self.table} "
                    "WHERE channel_id IS NOT NULL"
                ).fetchall()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return None
        return {
            int(channel_id): (None if value is None else bool(value))
            for channel_id, value in rows
        }

    def _safe_legacy_policy(self):
        try:
            return self._legacy_policy()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {"enabled": False, "channels": {}}

    def _file_stamp(self, path: Path):
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _db_stamp(self):
        wal = Path(f"{self.db_path}-wal")
        return (self._file_stamp(self.db_path), self._file_stamp(wal))

    def _refresh_outside_lock(self, now, *, force=False):
        db_stamp = self._db_stamp()
        legacy_stamp = self._file_stamp(self.path)
        if not force and self._db_rows is not None and db_stamp == self._db_stat:
            db_rows = self._db_rows
        else:
            db_rows = self._read_all()
        if not force and self._legacy_cache is not None and legacy_stamp == self._legacy_stat:
            legacy = self._legacy_cache
        else:
            legacy = self._safe_legacy_policy()
        with self._lock:
            if db_rows is not None:
                self._db_rows = db_rows
                self._db_stat = db_stamp
                self._bulk_next_check = now + 1.0
            else:
                # SQLite 瞬态锁冲突时保留上次成功缓存，严禁清空为 {} 导致静默丢报文。
                if self._db_rows is None:
                    self._db_rows = {}
                self._bulk_next_check = now + 0.5
            self._legacy_cache = legacy
            self._legacy_stat = legacy_stamp
            self._cache.clear()
            self._next_check.clear()

    def _resolve_locked(self, key):
        if key is not None and self._db_rows is not None:
            value = self._db_rows.get(key)
            if value is not None:
                return value
        legacy = self._legacy_cache if self._legacy_cache is not None else self._safe_legacy_policy()
        if key is not None:
            return bool(legacy["channels"].get(str(key), legacy["enabled"]))
        return bool(legacy["enabled"])

    def enabled(self, channel_id=None, *, refresh=False):
        key = None if channel_id is None else self._channel_key(channel_id)
        now = time.monotonic()
        need_refresh = False
        with self._lock:
            if refresh:
                need_refresh = True
                self._refreshing = True
            elif now >= self._bulk_next_check and not self._refreshing:
                need_refresh = True
                self._refreshing = True
        if need_refresh:
            try:
                self._refresh_outside_lock(now, force=refresh)
            finally:
                with self._lock:
                    self._refreshing = False
        with self._lock:
            cached = self._cache.get(key)
            if cached is None or now >= self._next_check.get(key, 0.0):
                cached = bool(self._resolve_locked(key))
                self._cache[key] = cached
                self._next_check[key] = self._bulk_next_check
            return cached

    def for_channel(self, channel_id):
        return ChannelPacketLogSwitch(self, self._channel_key(channel_id))

    def snapshot(self):
        """Return persisted channel policies for diagnostics only."""
        try:
            with closing(sqlite3.connect(self.db_path, timeout=2)) as con:
                rows = con.execute(
                    f"SELECT channel_id, packet_recording FROM {self.table} "
                    "WHERE channel_id IS NOT NULL ORDER BY channel_id"
                ).fetchall()
        except (OSError, sqlite3.Error):
            rows = []
        legacy = self._safe_legacy_policy()
        # 与 _resolve_locked 保持一致：数据库非空配置优先覆盖兼容 sidecar 中的旧值。
        values = dict(legacy["channels"])
        values.update({str(int(channel_id)): bool(value) for channel_id, value in rows if value is not None})
        return {"enabled": bool(legacy["enabled"]), "channels": values,
                "source": "channel_parameter"}

    def channel_snapshot(self, channel_id):
        key = self._channel_key(channel_id)
        try:
            with closing(sqlite3.connect(self.db_path, timeout=2)) as con:
                row = con.execute(
                    f"SELECT packet_recording FROM {self.table} WHERE channel_id=?", (key,)
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError("读取通道报文策略失败") from exc
        if row is None:
            raise KeyError(f"通道不存在: {key}")
        override = None if row[0] is None else bool(row[0])
        if override is not None:
            effective = override
        else:
            legacy = self._safe_legacy_policy()
            effective = bool(legacy["channels"].get(str(key), legacy["enabled"]))
        now = time.monotonic()
        with self._lock:
            if self._db_rows is not None:
                self._db_rows[key] = override
            self._cache[key] = effective
            self._next_check[key] = now + 1.0
        return {"channel_id": key, "override": override, "enabled": effective}

    def set_enabled(self, enabled):
        """Set the deprecated unbound default used by old integrations."""
        if type(enabled) is not bool:
            raise ValueError("enabled 必须为布尔值")
        with _compat_lock(self.path), self._lock:
            payload = self._legacy_policy()
            payload["enabled"] = enabled
            self._write_legacy_policy(payload)
            self._cache.clear()
            self._next_check.clear()
            self._bulk_next_check = 0.0
            self._legacy_cache = payload

    def set_channel(self, channel_id, enabled):
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("enabled 必须为布尔值或 null")
        key = self._channel_key(channel_id)
        # Validate the compatibility file before touching SQLite. This keeps
        # a corrupt legacy policy fail-closed and preserves the operator's
        # original file for diagnosis.
        with _compat_lock(self.path), self._lock:
            legacy = self._legacy_policy()
            persisted = False
            try:
                with closing(sqlite3.connect(self.db_path, timeout=10)) as con:
                    cursor = con.execute(
                        f"UPDATE {self.table} SET packet_recording=?, _version=_version+1 WHERE channel_id=?",
                        (None if enabled is None else (1 if enabled else 0), key),
                    )
                    persisted = cursor.rowcount == 1
                    if persisted:
                        con.commit()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
            if not persisted:
                # Old callers used an independent policy store and may run
                # before a schema has been created.  Keep that compatibility
                # path isolated from the production Web routes, which first
                # verify the channel row with channel_snapshot().
                if enabled is None:
                    legacy["channels"].pop(str(key), None)
                else:
                    legacy["channels"][str(key)] = enabled
                self._write_legacy_policy(legacy)
            else:
                if legacy["channels"].pop(str(key), None) is not None and self.path.is_file():
                    try:
                        self._write_legacy_policy(legacy)
                    except OSError:
                        pass
            if self._db_rows is not None:
                if persisted:
                    self._db_rows[key] = enabled
                else:
                    self._db_rows.pop(key, None)
            self._legacy_cache = legacy
            if enabled is None:
                self._cache[key] = bool(legacy["channels"].get(str(key), legacy["enabled"]))
            else:
                self._cache[key] = enabled
            self._next_check[key] = time.monotonic() + 1.0


class PacketLogSwitch(DatabasePacketLogSwitch):
    """Backward-compatible name for the channel-aware database switch.

    Old callers may address a channel before its database row exists. Keep
    those calls in the compatibility sidecar; production Web/API paths still
    use :class:`DatabasePacketLogSwitch` and reject unknown channels.
    """

    def set_channel(self, channel_id, enabled):
        try:
            return super().set_channel(channel_id, enabled)
        except KeyError:
            if enabled is not None and type(enabled) is not bool:
                raise ValueError("enabled 必须为布尔值或 null")
            key = self._channel_key(channel_id)
            with _compat_lock(self.path), self._lock:
                payload = self._legacy_policy()
                if enabled is None:
                    payload["channels"].pop(str(key), None)
                else:
                    payload["channels"][str(key)] = enabled
                self._write_legacy_policy(payload)
                self._legacy_cache = payload
                self._cache[key] = (payload["channels"].get(str(key), payload["enabled"])
                                     if enabled is None else enabled)
                self._next_check[key] = time.monotonic() + 1.0


@lru_cache(maxsize=128)
def packet_log_switch(db_path, family):
    return DatabasePacketLogSwitch(db_path, family)


def rtu_channel_id(store, rtu_id):
    """Resolve an RTU's channel once when a simulator starts."""
    if rtu_id is None:
        return None
    with store.connection() as con:
        row = con.execute(
            'SELECT channel_id FROM fes_rtu_define WHERE rtu_id=?', (rtu_id,)
        ).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


def packet_recording_state(db_path, family):
    """Return the compact API projection used by all three Web services."""
    switch = packet_log_switch(db_path, family)
    try:
        with closing(sqlite3.connect(Path(db_path).resolve(), timeout=2)) as con:
            rows = con.execute(
                f"SELECT channel_id, packet_recording FROM {switch.table} "
                "WHERE channel_id IS NOT NULL ORDER BY channel_id"
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError("读取报文记录通道失败") from exc
    legacy = switch._safe_legacy_policy()
    default_enabled = bool(legacy["enabled"])
    legacy_channels = legacy["channels"]
    channels = []
    db_rows = {}
    for channel_id, raw in rows:
        key = int(channel_id)
        override = None if raw is None else bool(raw)
        db_rows[key] = override
        effective = override if override is not None else bool(legacy_channels.get(str(key), default_enabled))
        channels.append({"channel_id": key, "override": override, "enabled": effective})
    now = time.monotonic()
    with switch._lock:
        switch._db_rows = db_rows
        switch._legacy_cache = legacy
        switch._cache.clear()
        switch._next_check.clear()
        switch._bulk_next_check = now + 1.0
    return {"enabled": default_enabled, "channels": channels}

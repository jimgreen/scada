"""Per-channel packet recording policy backed by the service database.

The channel definition is the only source of truth. There is deliberately no
service-wide default or sidecar JSON file: an unbound or unknown channel never
records packets, and operators change the policy through the channel editor.
"""
from functools import lru_cache
from pathlib import Path
import sqlite3
import threading
import time


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

    def _read(self, channel_id):
        try:
            with sqlite3.connect(self.db_path, timeout=2) as con:
                row = con.execute(
                    f"SELECT packet_recording FROM {self.table} WHERE channel_id=?",
                    (channel_id,),
                ).fetchone()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return None
        return bool(row[0]) if row is not None else None

    def enabled(self, channel_id=None, *, refresh=False):
        # There is no service-level policy. Callers must bind a channel.
        if channel_id is None:
            return False
        key = self._channel_key(channel_id)
        now = time.monotonic()
        with self._lock:
            if refresh or now >= self._next_check.get(key, 0.0):
                value = self._read(key)
                self._cache[key] = False if value is None else value
                self._next_check[key] = now + 1.0
            return self._cache.get(key, False)

    def for_channel(self, channel_id):
        return ChannelPacketLogSwitch(self, self._channel_key(channel_id))

    def snapshot(self):
        """Return persisted channel policies for diagnostics only."""
        try:
            with sqlite3.connect(self.db_path, timeout=2) as con:
                rows = con.execute(
                    f"SELECT channel_id, packet_recording FROM {self.table} "
                    "WHERE channel_id IS NOT NULL ORDER BY channel_id"
                ).fetchall()
        except (OSError, sqlite3.Error):
            rows = []
        values = {str(int(channel_id)): bool(value) for channel_id, value in rows}
        return {"enabled": False, "channels": values, "source": "channel_parameter"}

    def set_channel(self, channel_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled 必须为布尔值")
        key = self._channel_key(channel_id)
        with sqlite3.connect(self.db_path, timeout=10) as con:
            cursor = con.execute(
                f"UPDATE {self.table} SET packet_recording=?, _version=_version+1 WHERE channel_id=?",
                (1 if enabled else 0, key),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"通道不存在: {key}")
            con.commit()
        with self._lock:
            self._cache[key] = enabled
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

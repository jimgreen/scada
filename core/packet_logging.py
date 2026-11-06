"""Default-off packet recording with persistent, live per-channel overrides."""
from functools import lru_cache
import json
import os
from pathlib import Path
import tempfile
import threading
import time


class PacketLogSwitch:
    def __init__(self, db_path, family):
        if family not in {'c', 's', 't'}:
            raise ValueError('未知服务')
        db = Path(db_path).resolve()
        self.db_path = db
        self.family = family
        self.path = db.with_name(db.name + f'.scdfe_{family}.packet-log.json')
        self._lock = threading.Lock()
        self._state = (False, {})
        self._next_check = 0.0

    def _read(self, *, strict=False):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or type(data.get('enabled')) is not bool:
                raise ValueError('invalid default')
            channels = data.get('channels', {})
            if (not isinstance(channels, dict) or
                    any(not key.isascii() or not key.isdecimal() or str(int(key)) != key or type(value) is not bool
                        for key, value in channels.items())):
                raise ValueError('invalid channel policy')
            return data['enabled'], channels
        except FileNotFoundError:
            return False, {}
        except (OSError, ValueError, RecursionError) as exc:
            if strict:
                raise OSError('无法读取已有报文保存设置，请检查设置文件；原文件已保留') from exc
            return False, {}

    def _refresh(self, refresh=False, *, strict=False):
        now = time.monotonic()
        if not refresh and now < self._next_check:
            return
        with self._lock:
            if refresh or now >= self._next_check:
                try:
                    self._state = self._read(strict=strict)
                except OSError:
                    self._state = (False, {})
                    raise
                self._next_check = now + 1.0

    def enabled(self, channel_id=None, *, refresh=False):
        self._refresh(refresh)
        default, channels = self._state
        return default if channel_id is None else channels.get(str(channel_id), default)

    def snapshot(self):
        self._refresh(True, strict=True)
        default, channels = self._state
        return {'enabled': default, 'channels': dict(channels)}

    @staticmethod
    def _channel_key(channel_id):
        if type(channel_id) is not int or channel_id < 0:
            raise ValueError('channel_id 必须为非负整数')
        return str(channel_id)

    def for_channel(self, channel_id):
        self._channel_key(channel_id)
        return ChannelPacketLogSwitch(self, channel_id)

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled 必须为布尔值')
        self._save(None, enabled)
        return enabled

    def set_channel(self, channel_id, enabled):
        key = self._channel_key(channel_id)
        if enabled is not None and type(enabled) is not bool:
            raise ValueError('enabled 必须为布尔值或 null（使用默认设置）')
        self._save(key, enabled)

    def _save(self, key, enabled):
        from core.scdfe_control import InstanceLock
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Serialize read/modify/replace across Web processes, preserving
            # independent operators' edits to other channels and the default.
            lock = InstanceLock(self.db_path, f'packet-log-{self.family}')
            deadline = time.monotonic() + 2
            while not lock.acquire():
                if time.monotonic() >= deadline:
                    raise OSError('报文保存设置正在更新，请重试')
                time.sleep(.01)
            temp_path = None
            try:
                default, channels = self._read(strict=True)
                if key is None:
                    default = enabled
                elif enabled is None:
                    channels.pop(key, None)
                else:
                    channels[key] = enabled
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                                 prefix=self.path.name + '.', suffix='.tmp', delete=False) as stream:
                    temp_path = Path(stream.name)
                    json.dump({'enabled': default, 'channels': channels}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                for attempt in range(5):
                    try:
                        os.replace(temp_path, self.path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(.01)
                self._state = (default, channels)
                self._next_check = time.monotonic() + 1.0
            finally:
                try:
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)
                finally:
                    lock.release()


class ChannelPacketLogSwitch:
    """A channel binding shares the cached policy, never a mutable channel ID."""
    def __init__(self, switch, channel_id):
        self.switch = switch
        self.channel_id = channel_id

    def enabled(self, *, refresh=False):
        return self.switch.enabled(self.channel_id, refresh=refresh)


@lru_cache(maxsize=128)
def packet_log_switch(db_path, family):
    return PacketLogSwitch(db_path, family)


def rtu_channel_id(store, rtu_id):
    """Resolve once when a simulator starts; no database reads per frame."""
    if rtu_id is None:
        return None
    with store.connection() as con:
        row = con.execute('SELECT channel_id FROM fes_rtu_define WHERE rtu_id=?', (rtu_id,)).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


def _channel_catalog(store, family, channel_id=None):
    args = () if channel_id is None else (channel_id,)
    with store.connection() as con:
        if family == 't':
            where = '' if channel_id is None else ' WHERE channel_id=?'
            rows = con.execute('SELECT channel_id, name, protocol FROM fes_scdfe_t_define' + where + ' ORDER BY channel_id', args)
            return [{'channel_id': int(row[0]), 'label': f'通道 {row[0]} · {row[1] or row[2]}'} for row in rows]
        where = 'channel_id IS NOT NULL' if channel_id is None else 'channel_id=?'
        rows = con.execute('SELECT channel_id, rtu_id, name, channel_type FROM fes_rtu_define WHERE ' +
                           where + ' ORDER BY channel_id, rtu_id', args)
        grouped = {}
        for cid, rid, name, protocol in rows:
            grouped.setdefault(int(cid), []).append(f'RTU {rid} · {name or protocol}')
        return [{'channel_id': cid, 'label': f'通道 {cid} · ' + ' / '.join(labels)}
                for cid, labels in grouped.items()]


def handle_packet_recording(handler, parts, query, method, store, family):
    """Common API; True means a response was written (never fall through)."""
    if parts[:2] != ['api', 'packet-recording']:
        return False
    from scdfe_c.web.service import ApiError
    channel_id = None
    if len(parts) != 2:
        if len(parts) != 4 or parts[2] != 'channels' or not parts[3].isascii() or not parts[3].isdecimal():
            raise ApiError(400, '请指定有效的通道编号')
        try:
            channel_id = int(parts[3])
        except ValueError as exc:
            raise ApiError(400, '通道编号无效') from exc
    if query:
        raise ApiError(400, '报文保存设置不接受查询参数')
    if method not in {'GET', 'PUT'}:
        raise ApiError(405, '报文保存设置只支持 GET/PUT')
    catalog = _channel_catalog(store, family, channel_id) if channel_id is None or channel_id < 2**63 else []
    if channel_id is not None and not catalog:
        raise ApiError(404, '通道不存在')
    switch = packet_log_switch(store.db_path, family)
    if method == 'PUT':
        body = handler._body()
        if (not isinstance(body, dict) or set(body) != {'enabled'} or
                not (type(body['enabled']) is bool or (channel_id is not None and body['enabled'] is None))):
            raise ApiError(400, '请提供布尔值 enabled；单个通道可用 null 恢复默认设置')
    try:
        if method == 'PUT':
            if channel_id is None:
                switch.set_enabled(body['enabled'])
            else:
                switch.set_channel(channel_id, body['enabled'])
        state = switch.snapshot()
    except OSError as exc:
        raise ApiError(503, str(exc), code='packet_policy_unavailable') from exc
    for row in catalog:
        row['override'] = state['channels'].get(str(row['channel_id']))
        row['enabled'] = state['enabled'] if row['override'] is None else row['override']
    result = {'enabled': state['enabled'], 'service': 'scdfe_' + family, 'apply_within_seconds': 1}
    if channel_id is None:
        result['channels'] = catalog
    else:
        result.update(catalog[0])
        result['default_enabled'] = state['enabled']
    handler._json(200, result)
    return True

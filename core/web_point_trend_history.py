"""Keep Web trend settings and observations apart from the live business DB."""

from contextlib import closing, contextmanager
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

from core.scdfe_control import InstanceLock
from core.web_point_trend import MAX_POINTS, POINT_TABLES, _identifier, read_point_trend
from scdfe_c.web.service import ApiError


LOG = logging.getLogger(__name__)
_APPLICATION_ID = int.from_bytes(b'SDTH', 'big')
_MAX_SETTINGS = 65536
_MAX_RESULTS = 10000
DEFAULT_INTERVAL_SECONDS = 1
MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 3600
DEFAULT_RETENTION_POINTS = 10000
MIN_RETENTION_POINTS = 1
MAX_RETENTION_POINTS = 100000


def _now_ms():
    return time.time_ns() // 1_000_000


def _revision(payload):
    return '"' + sha256((payload or '').encode('utf-8')).hexdigest() + '"'


def _key(point):
    return ':'.join(point.values())


def _normalize_interval(value, *, fallback=DEFAULT_INTERVAL_SECONDS):
    if value is None:
        return fallback
    if type(value) is not int or not MIN_INTERVAL_SECONDS <= value <= MAX_INTERVAL_SECONDS:
        raise ApiError(400, f'采样周期需要为 {MIN_INTERVAL_SECONDS}–{MAX_INTERVAL_SECONDS} 秒的整数')
    return value


def _normalize_retention_points(value, *, fallback=DEFAULT_RETENTION_POINTS):
    if value is None:
        return fallback
    if type(value) is not int or not MIN_RETENTION_POINTS <= value <= MAX_RETENTION_POINTS:
        raise ApiError(400, f'后台留存点数需要为 {MIN_RETENTION_POINTS}–{MAX_RETENTION_POINTS} 的整数')
    return value


def _settings(body, role, *, allow_legacy=False):
    allowed_top = {'series', 'view'}
    if allow_legacy:
        allowed_top |= {'interval_seconds', 'retention_seconds'}
    if not isinstance(body, dict) or body.keys() - allowed_top:
        raise ApiError(400, '曲线设置格式无效')

    legacy_interval = DEFAULT_INTERVAL_SECONDS
    if allow_legacy and 'interval_seconds' in body:
        raw_legacy = body.get('interval_seconds')
        if type(raw_legacy) is int and MIN_INTERVAL_SECONDS <= raw_legacy <= MAX_INTERVAL_SECONDS:
            legacy_interval = raw_legacy

    view = body.get('view', {})
    series = body.get('series')
    if not isinstance(view, dict) or not isinstance(series, list) or len(series) > MAX_POINTS:
        raise ApiError(400, f'曲线列表最多 {MAX_POINTS} 条，视图设置需要为对象')

    normalized = []
    seen = set()
    owner = 'channel_id' if role == 't' else 'rtu_id'
    for item in series:
        point = item.get('point') if isinstance(item, dict) else None
        if not isinstance(point, dict):
            raise ApiError(400, '曲线需要关联一个测点')
        if role == 'd':
            if point.keys() != {'measurement_id'}:
                raise ApiError(400, '设备量测需要 measurement_id')
            point = {'measurement_id': str(_identifier(point['measurement_id'], 'measurement_id'))}
        else:
            if point.keys() != {'kind', owner, 'pnt_no'}:
                raise ApiError(400, f'测点需要 kind、{owner} 和 pnt_no')
            kind = point['kind']
            if not isinstance(kind, str) or kind not in POINT_TABLES:
                raise ApiError(400, 'kind 必须为 yc、yx、yk 或 yt')
            point = {
                'kind': kind,
                owner: str(_identifier(point[owner], owner)),
                'pnt_no': str(_identifier(point['pnt_no'], 'pnt_no', minimum=0)),
            }
        key = _key(point)
        if key in seen:
            raise ApiError(400, '测点在曲线列表中重复出现')
        seen.add(key)

        interval_seconds = _normalize_interval(
            item.get('interval_seconds'),
            fallback=legacy_interval if allow_legacy else DEFAULT_INTERVAL_SECONDS,
        )
        retention_points = _normalize_retention_points(
            item.get('retention_points'),
            fallback=DEFAULT_RETENTION_POINTS,
        )
        cleaned = {k: v for k, v in item.items() if k != 'retention_seconds'}
        cleaned['point'] = point
        cleaned['interval_seconds'] = interval_seconds
        cleaned['retention_points'] = retention_points
        normalized.append(cleaned)

    settings = dict(series=normalized, view=view)
    try:
        serialized = json.dumps(settings, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ApiError(400, '曲线设置包含无效内容') from exc
    if len(serialized) > _MAX_SETTINGS:
        raise ApiError(400, '曲线设置内容过长')
    return settings, serialized


class PointTrendHistory:
    """One sampler per database path and role, with per-point interval and point-count retention."""

    def __init__(self, store, role, workspace_root):
        self.store = store
        self.role = role
        self.root = Path(workspace_root).resolve(strict=True)
        source = Path(store.db_path).resolve(strict=True)
        if not source.is_relative_to(self.root):
            raise ValueError('测点数据库超出工作区根目录')
        key = sha256(os.path.normcase(str(source)).encode('utf8')).hexdigest()[:32]
        self.path = source.parent / '.scada-trends' / f'{role}-{key}.sqlite3'
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread_lock = threading.Lock()
        self._thread = None
        try:
            with self._database() as con:
                if con is not None and self._load(con) is not None:
                    self._start()
        except (ApiError, OSError, sqlite3.Error, ValueError):
            LOG.warning('实时曲线设置无法加载：%s', self.path, exc_info=True)

    def _check_path(self, path, *, directory=False):
        try:
            if not path.is_relative_to(self.root) or (directory and path.resolve() != path):
                raise ApiError(400, '曲线历史路径不能使用链接或超出工作区')
            info = path.lstat()
        except FileNotFoundError:
            return False
        except (OSError, RuntimeError) as exc:
            raise ApiError(400, '曲线历史路径不可用') from exc
        reparse = getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
        allowed = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        if reparse or not allowed:
            raise ApiError(400, '曲线历史路径不能使用链接或特殊文件')
        return True

    @contextmanager
    def _database(self, *, create=False):
        exists = self._check_path(self.path.parent, directory=True)
        if not exists and create:
            self.path.parent.mkdir(mode=0o700, exist_ok=True)
            self._check_path(self.path.parent, directory=True)
        exists = self._check_path(self.path)
        for suffix in ('-wal', '-shm', '-journal'):
            self._check_path(self.path.with_name(self.path.name + suffix))
        if not exists and not create:
            yield None
            return
        if not exists:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except FileExistsError:
                pass
            self._check_path(self.path)
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=3)) as con, con:
            self._configure_connection(con, create=create)
            yield con

    @staticmethod
    def _ensure_point_sample_table(con):
        """确保按点存储的采样表存在，并一次性平滑迁移历史 trend_sample 旧数据。"""
        has_new = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trend_point_sample'"
        ).fetchone() is not None
        if has_new:
            return
        con.execute('''CREATE TABLE IF NOT EXISTS trend_point_sample (
            point_key TEXT NOT NULL,
            time_ms INTEGER NOT NULL,
            value REAL,
            quality INTEGER,
            source_time_ms INTEGER,
            PRIMARY KEY (point_key, time_ms)
        ) WITHOUT ROWID''')
        has_legacy = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trend_sample'"
        ).fetchone() is not None
        if not has_legacy:
            return
        migrated = []
        for stamp, raw in con.execute('SELECT time_ms, samples FROM trend_sample ORDER BY time_ms'):
            try:
                values = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(values, dict):
                continue
            for point_key, entry in values.items():
                if not isinstance(entry, dict):
                    continue
                migrated.append((
                    str(point_key),
                    int(stamp),
                    entry.get('value'),
                    entry.get('quality'),
                    entry.get('time_ms'),
                ))
        if migrated:
            con.executemany(
                'INSERT OR IGNORE INTO trend_point_sample (point_key, time_ms, value, quality, source_time_ms) '
                'VALUES (?, ?, ?, ?, ?)',
                migrated,
            )
        con.execute('DELETE FROM trend_sample')

    def _configure_connection(self, con, *, create=False):
        con.execute('PRAGMA trusted_schema=OFF')
        con.execute('PRAGMA busy_timeout=5000')
        con.execute('PRAGMA temp_store=MEMORY')
        con.execute('PRAGMA synchronous=NORMAL')
        if create:
            if con.execute('PRAGMA page_count').fetchone()[0] == 0:
                con.execute('PRAGMA auto_vacuum=INCREMENTAL')
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('BEGIN IMMEDIATE')
        application_id = con.execute('PRAGMA application_id').fetchone()[0]
        if application_id == 0 and create and con.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone() is None:
            con.execute(f'PRAGMA application_id={_APPLICATION_ID}')
            con.execute('CREATE TABLE trend_settings (id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT NOT NULL)')
            con.execute('''CREATE TABLE trend_point_sample (
                point_key TEXT NOT NULL,
                time_ms INTEGER NOT NULL,
                value REAL,
                quality INTEGER,
                source_time_ms INTEGER,
                PRIMARY KEY (point_key, time_ms)
            ) WITHOUT ROWID''')
        elif application_id != _APPLICATION_ID:
            raise ApiError(400, '曲线历史文件格式无效')
        else:
            self._ensure_point_sample_table(con)

    def _open_sampler_connection(self):
        exists = self._check_path(self.path.parent, directory=True)
        if not exists:
            return None, None
        exists = self._check_path(self.path)
        for suffix in ('-wal', '-shm', '-journal'):
            self._check_path(self.path.with_name(self.path.name + suffix))
        if not exists:
            return None, None
        info = self.path.lstat()
        identity = (info.st_dev, info.st_ino)
        con = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=3)
        try:
            self._configure_connection(con, create=False)
            con.commit()
        except BaseException:
            con.close()
            raise
        return con, identity

    @staticmethod
    def _load(con):
        row = con.execute('SELECT settings FROM trend_settings WHERE id=1').fetchone()
        return row[0] if row else None

    @staticmethod
    def _prune_point(con, point_key, retention_points):
        """按点数精确裁剪单测点历史，仅保留最新的 retention_points 条。"""
        boundary = con.execute(
            'SELECT time_ms FROM trend_point_sample '
            'WHERE point_key=? ORDER BY time_ms DESC LIMIT 1 OFFSET ?',
            (point_key, retention_points - 1),
        ).fetchone()
        if boundary is None:
            return 0
        removed = con.execute(
            'DELETE FROM trend_point_sample WHERE point_key=? AND time_ms < ?',
            (point_key, boundary[0]),
        ).rowcount
        return max(0, removed)

    @classmethod
    def _prune(cls, con, settings):
        removed = 0
        for item in settings['series']:
            removed += cls._prune_point(con, _key(item['point']), item['retention_points'])
        return removed

    @staticmethod
    def _retained_point_rows(con, point_key, after_ms, retention_points):
        """读取单测点留存点数上限之内且晚于 after_ms 的样本，超上限时保首尾抽样。"""
        boundary = con.execute(
            'SELECT time_ms FROM trend_point_sample '
            'WHERE point_key=? ORDER BY time_ms DESC LIMIT 1 OFFSET ?',
            (point_key, retention_points - 1),
        ).fetchone()
        min_keep_ms = boundary[0] if boundary is not None else 0
        total = con.execute(
            'SELECT count(*) FROM trend_point_sample '
            'WHERE point_key=? AND time_ms > ? AND time_ms >= ?',
            (point_key, after_ms, min_keep_ms),
        ).fetchone()[0]
        if total == 0:
            return []
        if total <= _MAX_RESULTS:
            return con.execute(
                'SELECT time_ms, value, quality, source_time_ms FROM trend_point_sample '
                'WHERE point_key=? AND time_ms > ? AND time_ms >= ? ORDER BY time_ms',
                (point_key, after_ms, min_keep_ms),
            ).fetchall()
        return con.execute(
            '''WITH timeline AS (
                SELECT time_ms, row_number() OVER (ORDER BY time_ms) - 1 AS position
                FROM trend_point_sample
                WHERE point_key=? AND time_ms > ? AND time_ms >= ?
            )
            SELECT sample.time_ms, sample.value, sample.quality, sample.source_time_ms
            FROM timeline
            JOIN trend_point_sample sample
              ON sample.point_key=? AND sample.time_ms = timeline.time_ms
            WHERE position=0 OR position * ? / ? > (position - 1) * ? / ?
            ORDER BY sample.time_ms''',
            (point_key, after_ms, min_keep_ms, point_key,
             _MAX_RESULTS - 1, total - 1, _MAX_RESULTS - 1, total - 1),
        ).fetchall()

    def get(self, after_ms=0):
        """Return settings and per-series retained observations after a UTC epoch-ms cursor."""
        after_ms = _identifier(after_ms, 'after_ms', minimum=0)
        with self._database() as con:
            if con is None:
                return dict(settings=None, series_samples=[], revision=_revision(None))
            con.execute('BEGIN')
            if (payload := self._load(con)) is None:
                return dict(settings=None, series_samples=[], revision=_revision(None))
            settings, _ = _settings(json.loads(payload), self.role, allow_legacy=True)
            if not settings['series']:
                return dict(settings=settings, series_samples=[], revision=_revision(payload))
            series_samples = []
            for item in settings['series']:
                point_key = _key(item['point'])
                rows = self._retained_point_rows(con, point_key, after_ms, item['retention_points'])
                series_samples.append([
                    dict(time_ms=stamp, value=value, quality=quality, source_time_ms=source_time_ms)
                    for stamp, value, quality, source_time_ms in rows
                ])
        return dict(
            settings=settings,
            series_samples=series_samples,
            revision=_revision(payload),
            server_time_ms=_now_ms(),
        )

    def put(self, body, *, expected_revision=None):
        settings, serialized = _settings(body, self.role, allow_legacy=False)
        with self._database(create=True) as con:
            if expected_revision is not None and expected_revision != _revision(self._load(con)):
                raise ApiError(409, '采样设置已在其他页面修改，请查看最新设置后重试', code='trend_settings_changed')
            con.execute(
                'INSERT INTO trend_settings VALUES(1,?) '
                'ON CONFLICT(id) DO UPDATE SET settings=excluded.settings',
                (serialized,),
            )
            self._prune(con, settings)
        self._start()
        self._wake.set()
        return dict(settings=settings, series_samples=[], revision=_revision(serialized))

    def _start(self):
        with self._thread_lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._thread = threading.Thread(target=self._run, name=f'point-trend-{self.role}', daemon=True)
            self._thread.start()

    def close(self):
        with self._thread_lock:
            self._stop.set()
            self._wake.set()
            worker = self._thread
        if worker is not None:
            worker.join()

    def _run(self):
        lease = InstanceLock(self.path, 'point-trend')
        next_prune = 0.0
        next_scan = {}
        known_intervals = {}
        last_payload = None
        failed = False
        con = None
        con_identity = None
        try:
            while not self._stop.is_set():
                self._wake.clear()
                delay = 2.0
                try:
                    self._check_path(self.path.parent, directory=True)
                    if lease.stream is None:
                        self._check_path(lease.path)
                        if not lease.acquire():
                            self._wake.wait(delay)
                            continue
                    if con is not None:
                        try:
                            info = self.path.lstat()
                            if (info.st_dev, info.st_ino) != con_identity:
                                con.close()
                                con = None
                                con_identity = None
                        except FileNotFoundError:
                            con.close()
                            con = None
                            con_identity = None
                    if con is None:
                        con, con_identity = self._open_sampler_connection()
                    payload = None
                    if con is not None:
                        con.execute('BEGIN')
                        try:
                            payload = self._load(con)
                        finally:
                            con.rollback()
                    if payload is not None:
                        settings, _ = _settings(json.loads(payload), self.role, allow_legacy=True)
                        series = settings['series']
                        active_keys = {_key(item['point']) for item in series}
                        for obsolete in list(next_scan):
                            if obsolete not in active_keys:
                                next_scan.pop(obsolete, None)
                                known_intervals.pop(obsolete, None)

                        start = time.monotonic()
                        due_items = []
                        for item in series:
                            point_key = _key(item['point'])
                            interval = item['interval_seconds']
                            prev_interval = known_intervals.get(point_key)
                            if prev_interval is not None and prev_interval != interval:
                                # 缩短或变更某测点周期时，按新周期即时收紧下一次调度时间。
                                next_scan[point_key] = min(next_scan.get(point_key, start), start + interval)
                            known_intervals[point_key] = interval
                            scheduled = next_scan.get(point_key, 0.0)
                            if scheduled <= 0.0 or start >= scheduled:
                                due_items.append((point_key, item))
                                # 固定网格推进该测点采样节拍，若已落后多拍则以当前时刻对齐避免连发。
                                next_due = (scheduled + interval) if scheduled > 0.0 else (start + interval)
                                next_scan[point_key] = next_due if next_due > start else (start + interval)

                        batch = None
                        if due_items:
                            batch = read_point_trend(
                                self.store,
                                self.role,
                                {'points': [item['point'] for _, item in due_items]},
                            )

                        prune_due = bool(series) and (start >= next_prune or payload != last_payload)
                        if (batch is not None or prune_due) and not self._stop.is_set() and con is not None:
                            pruned_count = 0
                            with con:
                                con.execute('BEGIN IMMEDIATE')
                                if self._load(con) == payload:
                                    if batch is not None:
                                        stamp = batch['time_ms']
                                        records = [
                                            (
                                                point_key,
                                                stamp,
                                                entry.get('value'),
                                                entry.get('quality'),
                                                entry.get('time_ms'),
                                            )
                                            for (point_key, _), entry in zip(due_items, batch['values'])
                                        ]
                                        con.executemany(
                                            'INSERT INTO trend_point_sample '
                                            '(point_key, time_ms, value, quality, source_time_ms) '
                                            'VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING',
                                            records,
                                        )
                                        for point_key, item in due_items:
                                            pruned_count += self._prune_point(
                                                con, point_key, item['retention_points'],
                                            )
                                    if prune_due:
                                        pruned_count += self._prune(con, settings)
                                        next_prune = start + 15.0
                                else:
                                    self._wake.set()
                            if pruned_count > 0:
                                try:
                                    con.execute('PRAGMA incremental_vacuum(200)')
                                except sqlite3.Error:
                                    pass
                            last_payload = payload

                        if next_scan:
                            delay = max(0.0, min(2.0, min(next_scan.values()) - time.monotonic()))
                        else:
                            delay = 2.0
                    failed = False
                except (ApiError, OSError, sqlite3.Error, ValueError):
                    if con is not None:
                        try:
                            con.close()
                        except sqlite3.Error:
                            pass
                        con = None
                        con_identity = None
                    if not failed:
                        LOG.warning('实时曲线采样失败：%s', self.path, exc_info=True)
                        failed = True
                self._wake.wait(delay)
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            lease.release()

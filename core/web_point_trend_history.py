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
_MAX_RESULTS = 7200
_EMPTY = {'value': None, 'quality': None, 'time_ms': None}


def _now_ms():
    return time.time_ns() // 1_000_000


def _revision(payload):
    return '"' + sha256((payload or '').encode('utf-8')).hexdigest() + '"'


def _key(point):
    return ':'.join(point.values())


def _settings(body, role):
    if not isinstance(body, dict) or body.keys() - {'interval_seconds', 'retention_seconds', 'series', 'view'}:
        raise ApiError(400, '曲线设置格式无效')
    interval = body.get('interval_seconds', 5)
    retention = body.get('retention_seconds', 3600)
    if type(interval) is not int or not 1 <= interval <= 60:
        raise ApiError(400, '刷新周期需要为 1–60 秒的整数')
    if type(retention) is not int or not 1 <= retention <= 86400:
        raise ApiError(400, '留存时间需要为 1–86400 秒的整数')
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
            point = {'kind': kind, owner: str(_identifier(point[owner], owner)),
                     'pnt_no': str(_identifier(point['pnt_no'], 'pnt_no', minimum=0))}
        key = _key(point)
        if key in seen:
            raise ApiError(400, '测点在曲线列表中重复出现')
        seen.add(key)
        normalized.append({**item, 'point': point})
    settings = dict(interval_seconds=interval, retention_seconds=retention, series=normalized, view=view)
    try:
        serialized = json.dumps(settings, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ApiError(400, '曲线设置包含无效内容') from exc
    if len(serialized) > _MAX_SETTINGS:
        raise ApiError(400, '曲线设置内容过长')
    return settings, serialized


class PointTrendHistory:
    """One sampler per database path and role, with a disk-backed time index."""

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
            con.execute('CREATE TABLE trend_sample (time_ms INTEGER PRIMARY KEY, samples TEXT NOT NULL)')
        elif application_id != _APPLICATION_ID:
            raise ApiError(400, '曲线历史文件格式无效')

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
    def _prune(con, settings):
        cutoff = _now_ms() - settings['retention_seconds'] * 1000
        removed = con.execute('DELETE FROM trend_sample WHERE time_ms < ?', (cutoff,)).rowcount
        return max(0, removed)

    @staticmethod
    def _retained_rows(con, cutoff):
        total = con.execute('SELECT count(*) FROM trend_sample WHERE time_ms>?', (cutoff,)).fetchone()[0]
        if total <= _MAX_RESULTS:
            return con.execute('SELECT time_ms,samples FROM trend_sample WHERE time_ms>? ORDER BY time_ms',
                               (cutoff,)).fetchall()
        # Spread the available points across the interval, including its first
        # and last observations. Read sample payloads only for selected times.
        return con.execute('''WITH timeline AS (
            SELECT time_ms, row_number() OVER (ORDER BY time_ms) - 1 AS position
            FROM trend_sample WHERE time_ms>?
        )
        SELECT sample.time_ms,sample.samples FROM timeline
        JOIN trend_sample sample USING (time_ms)
        WHERE position=0 OR position * ? / ? > (position - 1) * ? / ?
        ORDER BY sample.time_ms''', (cutoff, _MAX_RESULTS - 1, total - 1,
                                      _MAX_RESULTS - 1, total - 1)).fetchall()

    def get(self, after_ms=0):
        """Return settings and retained observations after a UTC epoch-ms cursor."""
        after_ms = _identifier(after_ms, 'after_ms', minimum=0)
        with self._database() as con:
            if con is None:
                return dict(settings=None, samples=[], revision=_revision(None))
            con.execute('BEGIN')
            if (payload := self._load(con)) is None:
                return dict(settings=None, samples=[], revision=_revision(None))
            settings, _ = _settings(json.loads(payload), self.role)
            keys = [_key(item['point']) for item in settings['series']]
            if not keys:
                return dict(settings=settings, samples=[], revision=_revision(payload))
            cutoff = max(after_ms, _now_ms() - settings['retention_seconds'] * 1000)
            rows = self._retained_rows(con, cutoff)
        samples = []
        for stamp, raw in rows:
            values = json.loads(raw)
            samples.append(dict(time_ms=stamp, values=[values.get(key, _EMPTY) for key in keys]))
        return dict(settings=settings, samples=samples, revision=_revision(payload), server_time_ms=_now_ms())

    def put(self, body, *, expected_revision=None):
        settings, serialized = _settings(body, self.role)
        with self._database(create=True) as con:
            if expected_revision is not None and expected_revision != _revision(self._load(con)):
                raise ApiError(409, '采样设置已在其他页面修改，请查看最新设置后重试', code='trend_settings_changed')
            con.execute('INSERT INTO trend_settings VALUES(1,?) '
                        'ON CONFLICT(id) DO UPDATE SET settings=excluded.settings', (serialized,))
            self._prune(con, settings)
        self._start()
        self._wake.set()
        return dict(settings=settings, samples=[], revision=_revision(serialized))

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
        next_prune = 0
        next_scan = 0.0
        last_payload = None
        failed = False
        con = None
        con_identity = None
        try:
            while not self._stop.is_set():
                self._wake.clear()
                delay = 2
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
                        settings, _ = _settings(json.loads(payload), self.role)
                        points = [item['point'] for item in settings['series']]
                        start = time.monotonic()
                        batch = None
                        interval = settings['interval_seconds']
                        if points:
                            if next_scan <= 0.0 or start >= next_scan:
                                # 固定网格推进采样节拍，避免单次 I/O 抖动导致长期漂移。
                                next_scan = max(start, (next_scan + interval) if next_scan > 0.0 else (start + interval))
                                batch = read_point_trend(self.store, self.role, {'points': points})
                        else:
                            next_scan = 0.0
                        prune_due = start >= next_prune or payload != last_payload
                        # 空曲线配置且表内无过期样本时跳过空写事务，减少无意义磁盘写盘。
                        if prune_due and batch is None and con is not None:
                            cutoff_check = _now_ms() - settings['retention_seconds'] * 1000
                            has_expired = con.execute(
                                'SELECT 1 FROM trend_sample WHERE time_ms < ? LIMIT 1', (cutoff_check,)
                            ).fetchone() is not None
                            if not has_expired:
                                prune_due = False
                                next_prune = start + 15
                                last_payload = payload
                        if (batch is not None or prune_due) and not self._stop.is_set() and con is not None:
                            pruned_count = 0
                            with con:
                                con.execute('BEGIN IMMEDIATE')
                                if self._load(con) == payload:
                                    if batch is not None:
                                        values = dict(zip((_key(point) for point in points), batch['values']))
                                        con.execute('INSERT INTO trend_sample VALUES(?,?) ON CONFLICT DO NOTHING',
                                                    (batch['time_ms'], json.dumps(values, separators=(',', ':'))))
                                    if prune_due:
                                        pruned_count = self._prune(con, settings)
                                        next_prune = start + 15
                                else:
                                    self._wake.set()
                            if pruned_count > 0:
                                try:
                                    con.execute('PRAGMA incremental_vacuum(200)')
                                except sqlite3.Error:
                                    pass
                            last_payload = payload
                        delay = max(0.0, min(2.0, next_scan - time.monotonic())) if points else 2.0
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

"""Transactional latest-value index for S monitoring points (not an SOE log).

Each historical point key occupies at most one row. Consumers have independent
cursors; only the SOE journal preserves every transition and its source time.
"""
from __future__ import annotations

import sqlite3

from core.rtdb_schema import sync_embedded_schema

TABLES = {'_scada_monitor_change'}


def ensure_monitor_changes(connection: sqlite3.Connection, *, track_refresh=False) -> None:
    """幂等安装监控变化触发器：非事务态用 BEGIN IMMEDIATE 避免 DEFERRED 锁升级直报 BUSY。"""
    if not connection.in_transaction:
        connection.execute('BEGIN IMMEDIATE')
        try:
            created = not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_scada_monitor_change'"
            ).fetchone()
            _ensure_monitor_changes(connection, track_refresh=track_refresh)
            if created:
                sync_embedded_schema(connection)
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        return
    connection.execute('SAVEPOINT monitor_schema')
    try:
        created = not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_scada_monitor_change'"
        ).fetchone()
        _ensure_monitor_changes(connection, track_refresh=track_refresh)
        if created:
            sync_embedded_schema(connection)
    except BaseException:
        connection.execute('ROLLBACK TO monitor_schema')
        raise
    finally:
        connection.execute('RELEASE monitor_schema')


def _ensure_monitor_changes(connection, *, track_refresh):
    connection.execute('''CREATE TABLE IF NOT EXISTS _scada_monitor_change (
        change_id INTEGER PRIMARY KEY AUTOINCREMENT,
        rtu_id INTEGER NOT NULL, kind TEXT NOT NULL, pnt_no INTEGER NOT NULL,
        UNIQUE(rtu_id,kind,pnt_no))''')
    connection.execute('''CREATE INDEX IF NOT EXISTS _scada_monitor_change_rtu
        ON _scada_monitor_change(rtu_id,change_id)''')
    for kind in ('yc', 'yx'):
        table = f'fes_{kind}_define'

        def mark(alias):
            return f'''INSERT INTO _scada_monitor_change(rtu_id,kind,pnt_no)
                VALUES({alias}.rtu_id,'{kind}',{alias}.pnt_no)
                ON CONFLICT(rtu_id,kind,pnt_no) DO UPDATE SET change_id=excluded.change_id;'''

        for event, alias in (('INSERT', 'NEW'), ('DELETE', 'OLD')):
            connection.execute(f'''CREATE TRIGGER IF NOT EXISTS _monitor_{kind}_{event.lower()}
                AFTER {event} ON {table} BEGIN {mark(alias)} END''')
        refresh_exists = connection.execute('SELECT 1 FROM sqlite_master WHERE type=\'trigger\' AND name=?',
                                           (f'_monitor_{kind}_refresh',)).fetchone()
        auto_refresh = connection.execute('SELECT sql FROM sqlite_master WHERE type=\'trigger\' AND name=?',
                                         (f'_scada_point_refresh_{table}',)).fetchone()
        # With T tracking enabled, an omitted timestamp is supplied by the
        # point-refresh trigger. Mark that inner update once, not both updates.
        condition = 'OLD.value IS NOT NEW.value OR OLD.status IS NOT NEW.status'
        if (track_refresh or refresh_exists) and auto_refresh and 'COALESCE' in auto_refresh[0]:
            condition = f'({condition}) AND OLD.refresh_time IS NOT NEW.refresh_time'
        sql = f'''CREATE TRIGGER _monitor_{kind}_update
            AFTER UPDATE OF value,status,rtu_id,pnt_no ON {table}
            WHEN ({condition})
                OR OLD.rtu_id IS NOT NEW.rtu_id OR OLD.pnt_no IS NOT NEW.pnt_no
            BEGIN {mark('NEW')} END'''
        previous = connection.execute('SELECT sql FROM sqlite_master WHERE type=\'trigger\' AND name=?',
                                      (f'_monitor_{kind}_update',)).fetchone()
        if previous is None or previous[0] != sql:
            connection.execute(f'DROP TRIGGER IF EXISTS _monitor_{kind}_update')
            connection.execute(sql)
        connection.execute(f'''CREATE TRIGGER IF NOT EXISTS _monitor_{kind}_move
            AFTER UPDATE OF rtu_id,pnt_no ON {table}
            WHEN OLD.rtu_id IS NOT NEW.rtu_id OR OLD.pnt_no IS NOT NEW.pnt_no
            BEGIN {mark('OLD')} END''')
        if track_refresh:
            # T also displays source refresh timestamps; S only publishes value
            # and quality changes. The databases have independent roles.
            connection.execute(f'''CREATE TRIGGER IF NOT EXISTS _monitor_{kind}_refresh
                AFTER UPDATE OF refresh_time ON {table}
                WHEN OLD.refresh_time IS NOT NEW.refresh_time
                    AND OLD.value IS NEW.value AND OLD.status IS NEW.status
                BEGIN {mark('NEW')} END''')


def read_monitor_changes(connection: sqlite3.Connection, rtu_id: int, cursor: int | None):
    """Return (cursor, live rows, deleted keys) from one consistent read snapshot.

    Live rows are (kind, point number, value, quality, source refresh time).
    A consumer acknowledges the returned cursor only after successful delivery.
    """
    connection.execute('SAVEPOINT monitor_snapshot')
    try:
        upper = connection.execute('''SELECT COALESCE(MAX(change_id),0)
            FROM _scada_monitor_change WHERE rtu_id=?''', (rtu_id,)).fetchone()[0]
        rows, deleted = [], set()
        if cursor is None:
            for kind in ('yc', 'yx'):
                rows.extend((kind, *row) for row in connection.execute(
                    f'''SELECT pnt_no,value,COALESCE(status,0),refresh_time
                        FROM fes_{kind}_define WHERE rtu_id=? ORDER BY pnt_no''', (rtu_id,)))
        elif upper > cursor:
            for kind in ('yc', 'yx'):
                for no, value, quality, stamp, present in connection.execute(f'''
                    SELECT c.pnt_no,p.value,COALESCE(p.status,0),p.refresh_time,p.pnt_no
                    FROM _scada_monitor_change c LEFT JOIN fes_{kind}_define p
                        ON p.rtu_id=c.rtu_id AND p.pnt_no=c.pnt_no
                    WHERE c.rtu_id=? AND c.change_id>? AND c.change_id<=? AND c.kind=?
                    ORDER BY c.change_id''', (rtu_id, cursor, upper, kind)):
                    if present is None:
                        deleted.add((kind, no))
                    else:
                        rows.append((kind, no, value, quality, stamp))
        return upper, rows, deleted
    finally:
        connection.execute('RELEASE monitor_snapshot')


class MappedMonitorReader:
    """Independent T snapshot/cache. Read only changed mapped source keys.

    Cursor advancement only acknowledges this local cache. The caller retains
    failed target keys for publication retries; SOE uses its separate journal.
    """
    def __init__(self, store, points):
        self.store = store
        self.groups = {}
        for point in points:
            if point.kind in ('yc', 'yx'):
                self.groups.setdefault((point.source_rtu_id, point.kind), {}).setdefault(
                    point.source_pnt_no, []).append((point.kind, point.pnt_no))
        self.cursors = {}
        self.current = {}
        self.rows_read = self.polls = self.cache_hits = 0
        self.full_reads = self.delta_reads = 0
        self.numbers = {group:sorted(targets) for group,targets in self.groups.items()}
        with store.connection() as connection:
            ensure_monitor_changes(connection, track_refresh=True)

    def poll(self):
        touched, changes, watermarks, dirty = set(), {}, {}, {}
        self.polls += 1
        with self.store.connection() as con:
            con.execute('SAVEPOINT mapped_monitor')
            try:
                for rid in dict.fromkeys(rid for rid, _ in self.groups):
                    watermarks[rid] = con.execute('''SELECT COALESCE(MAX(change_id),0)
                        FROM _scada_monitor_change WHERE rtu_id=?''', (rid,)).fetchone()[0]
                    cursor = self.cursors.get(rid)
                    if cursor is not None and cursor != watermarks[rid]:
                        for kind,no in con.execute('''SELECT kind,pnt_no FROM _scada_monitor_change
                                INDEXED BY _scada_monitor_change_rtu
                                WHERE rtu_id=? AND change_id>? AND change_id<=?''',
                                (rid,cursor,watermarks[rid])):
                            if no in self.groups.get((rid,kind),{}):
                                dirty.setdefault((rid,kind),set()).add(no)
                chunk = max(1, min(900, con.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)-4))
                for (rid, kind), targets in self.groups.items():
                    cursor, upper = self.cursors.get(rid), watermarks[rid]
                    if cursor == upper:
                        continue
                    changed = targets.keys() if cursor is None else dirty.get((rid,kind),set())
                    if not changed:
                        continue
                    for no in changed:
                        changes.update({key: None for key in targets[no]})
                    full = len(changed)*2 >= len(targets)
                    numbers = self.numbers[(rid,kind)] if full else sorted(changed)
                    self.full_reads += int(full)
                    self.delta_reads += int(not full)
                    for start in range(0, len(numbers), chunk):
                        batch = numbers[start:start+chunk]
                        if batch[-1]-batch[0]+1 == len(batch):
                            clause,params = 'pnt_no BETWEEN ? AND ?', (batch[0],batch[-1])
                        else:
                            clause,params = 'pnt_no IN ('+','.join('?' for _ in batch)+')', batch
                        rows = con.execute(f'''SELECT pnt_no,value,COALESCE(status,0),refresh_time
                            FROM fes_{kind}_define WHERE rtu_id=? AND {clause}''',(rid,*params))
                        for no, value, quality, stamp in rows:
                            self.rows_read += 1
                            if no not in changed:
                                continue
                            sample = (value,quality,int(stamp or 0)) if value is not None else None
                            changes.update({key: sample for key in targets[no]})
            finally:
                con.execute('RELEASE mapped_monitor')
        # No cache/cursor mutation until every query in the snapshot succeeded.
        for key, sample in changes.items():
            if sample is None:
                self.current.pop(key, None)
            else:
                self.current[key] = sample
            touched.add(key)
        self.cursors.update(watermarks)
        if not touched:
            self.cache_hits += 1
        return self.current, touched

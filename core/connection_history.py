"""Persistent, bounded per-client observations, separate from the business DB.

Transport callbacks only enqueue. One writer per process batches SQLite writes;
the Web process reads the same WAL database without contacting a protocol process.
"""
from __future__ import annotations

import atexit
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid

from core.rtdb_schema import sync_embedded_schema

LOG = logging.getLogger(__name__)
_journals = {}
_lock = threading.Lock()
MAX_EVENTS = 10000
SCHEMA = """
CREATE TABLE IF NOT EXISTS connection_session (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, service TEXT NOT NULL,
 protocol TEXT NOT NULL, channel_id INTEGER, rtu_id INTEGER,
 peer TEXT NOT NULL, local TEXT NOT NULL, state TEXT NOT NULL,
 opened REAL NOT NULL, updated REAL NOT NULL, closed REAL,
 rx_bytes INTEGER NOT NULL DEFAULT 0, tx_bytes INTEGER NOT NULL DEFAULT 0,
 rx_count INTEGER NOT NULL DEFAULT 0, tx_count INTEGER NOT NULL DEFAULT 0,
 removed INTEGER NOT NULL DEFAULT 0, heartbeat REAL NOT NULL,
 packet_recording INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS connection_channel ON connection_session(channel_id,opened DESC);
CREATE INDEX IF NOT EXISTS connection_opened ON connection_session(opened DESC);
CREATE TABLE IF NOT EXISTS connection_event (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, time REAL NOT NULL,
 kind TEXT NOT NULL, direction TEXT NOT NULL, summary TEXT NOT NULL, data BLOB);
CREATE INDEX IF NOT EXISTS connection_events ON connection_event(session_id,id DESC);
CREATE TABLE IF NOT EXISTS connection_writer (
 owner TEXT PRIMARY KEY, heartbeat REAL NOT NULL, dropped INTEGER NOT NULL,
 error TEXT NOT NULL);
"""


def history_path(db_path, component):
    path = Path(db_path)
    return path.with_name(f'{path.stem}.scdfe_{component}.connections.sqlite3')


def endpoint(address):
    if not address:
        return '未知'
    if isinstance(address, (tuple, list)):
        host, port = address[:2]
        return f'[{host}]:{port}' if ':' in str(host) else f'{host}:{port}'
    return str(address)


def journal(db_path, component):
    path = history_path(db_path, component).resolve()
    with _lock:
        if path not in _journals:
            _journals[path] = ConnectionJournal(path)
        _journals[path].users += 1
        return _journals[path]


def release_journal(writer):
    with _lock:
        writer.users -= 1
        if writer.users > 0:
            return
        _journals.pop(writer.path, None)
    writer.close()


class ConnectionJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.owner = uuid.uuid4().hex
        self.users = 0
        self.queue = queue.Queue(32768)
        self.dropped = 0
        self.error = ''
        self._policies = {}
        self._policy_values = {}
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name='connection-history', daemon=True)
        self._thread.start()
        self._ready.wait(5)
        atexit.register(self.close)

    def put(self, operation, *args):
        try:
            self.queue.put_nowait((operation, args))
        except queue.Full:
            self.dropped += 1

    def open(self, metadata, peer, local, at=None):
        ident = uuid.uuid4().hex
        self.put('open', ident, dict(metadata), endpoint(peer), endpoint(local), at or time.time())
        return ident

    def event(self, ident, kind, summary, direction='', data=None, at=None):
        self.put('event', ident, at or time.time(), kind, direction, str(summary),
                 None if data is None else bytes(data))

    def state(self, ident, state, reason, at=None):
        self.put('state', ident, state, str(reason), at or time.time())

    def flush(self, timeout=5):
        done = threading.Event()
        try:
            self.queue.put(('flush', (done,)), timeout=timeout)
        except queue.Full:
            return False
        return done.wait(timeout) and not self.error

    def close(self):
        if not self._stop.is_set():
            self.flush()
            self._stop.set()
            self._thread.join(5)
            atexit.unregister(self.close)

    def _run(self):
        con = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.path, timeout=5)
            con.execute('PRAGMA busy_timeout=5000')
            con.execute('PRAGMA temp_store=MEMORY')
            if con.execute('PRAGMA page_count').fetchone()[0] == 0:
                con.execute('PRAGMA auto_vacuum=INCREMENTAL')
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA synchronous=NORMAL')
            con.executescript('BEGIN IMMEDIATE;\n' + SCHEMA)
            if 'packet_recording' not in {r[1] for r in con.execute('PRAGMA table_info(connection_session)')}:
                con.execute('ALTER TABLE connection_session ADD COLUMN packet_recording INTEGER NOT NULL DEFAULT 1')
            sync_embedded_schema(con)
            con.commit()
            self._ready.set()
            last_beat = 0
            pending_prune = set()
            while not self._stop.is_set() or not self.queue.empty():
                batch = []
                try:
                    batch.append(self.queue.get(timeout=.1))
                except queue.Empty:
                    pass
                while len(batch) < 512:
                    try:
                        batch.append(self.queue.get_nowait())
                    except queue.Empty:
                        break
                flushed = []
                try:
                    for attempt in range(3):
                        try:
                            touched = set()
                            for operation, args in batch:
                                if operation == 'flush':
                                    if attempt == 0:
                                        flushed.append(args[0])
                                else:
                                    self._apply(con, operation, args)
                                    touched.add(args[0])
                            pending_prune.update(touched)
                            prune = time.time()-getattr(self, '_last_prune', 0) >= 1 or bool(flushed)
                            pruned_rows = 0
                            for ident in pending_prune if prune else ():
                                cutoff = con.execute('SELECT id FROM connection_event WHERE session_id=? '
                                                     'ORDER BY id DESC LIMIT 1 OFFSET ?', (ident, MAX_EVENTS)).fetchone()
                                if cutoff:
                                    removed = con.execute('DELETE FROM connection_event WHERE session_id=? AND id<=?',
                                                          (ident, cutoff[0])).rowcount
                                    pruned_rows += max(0, removed)
                                    con.execute('UPDATE connection_session SET removed=removed+? WHERE id=?', (removed, ident))
                            if prune:
                                pending_prune.clear()
                                self._last_prune = time.time()
                            now = time.time()
                            if now - last_beat >= 5 or flushed:
                                con.execute('UPDATE connection_session SET heartbeat=? WHERE owner=? AND closed IS NULL',
                                            (now, self.owner))
                                con.execute('INSERT OR REPLACE INTO connection_writer VALUES(?,?,?,?)',
                                            (self.owner, now, self.dropped, self.error))
                                expired = now - 7*86400
                                pruned_rows += max(0, con.execute(
                                    'DELETE FROM connection_event WHERE session_id IN '
                                    '(SELECT id FROM connection_session WHERE closed<? OR heartbeat<?)',
                                    (expired, expired)).rowcount)
                                con.execute('DELETE FROM connection_session WHERE closed<? OR heartbeat<?',
                                            (expired, expired))
                                last_beat = now
                            con.commit()
                            if pruned_rows > 0:
                                try:
                                    con.execute('PRAGMA incremental_vacuum(200)')
                                except sqlite3.Error:
                                    pass
                            break
                        except sqlite3.OperationalError as op_exc:
                            con.rollback()
                            msg = str(op_exc).lower()
                            if attempt < 2 and ('locked' in msg or 'busy' in msg):
                                time.sleep(0.05 * (attempt + 1))
                                continue
                            raise
                except Exception as exc:
                    con.rollback()
                    self.dropped += sum(op != 'flush' for op, _ in batch)
                    self.error = str(exc)
                    now_m = time.monotonic()
                    if now_m >= getattr(self, '_next_write_error_log', 0.0):
                        self._next_write_error_log = now_m + 60.0
                        LOG.exception('连接历史写入失败，通信继续；丢失记录数=%s', self.dropped)
                finally:
                    for done in flushed:
                        done.set()
        except Exception as exc:
            self.error = str(exc)
            LOG.exception('连接历史数据库不可用')
            self._ready.set()
        finally:
            if con:
                con.close()

    def _apply(self, con, operation, args):
        if operation == 'open':
            ident, meta, peer, local, at = args
            policy = meta.get('packet_enabled', lambda: True)
            self._policies[ident] = policy
            self._policy_values[ident] = bool(policy())
            con.execute('INSERT INTO connection_session '
                        '(id,owner,service,protocol,channel_id,rtu_id,peer,local,state,opened,updated,heartbeat) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (ident,self.owner,meta['service'],meta['protocol'],meta.get('channel_id'),
                         meta.get('rtu_id'),peer,local,'connected',at,at,at))
            con.execute('UPDATE connection_session SET packet_recording=? WHERE id=?', (int(self._policy_values[ident]),ident))
            con.execute('INSERT INTO connection_event(session_id,time,kind,direction,summary) VALUES(?,?,?,?,?)',
                        (ident,at,'log','',f'客户端接入 {peer} → {local}'))
        elif operation == 'state':
            ident, state, reason, at = args
            con.execute('UPDATE connection_session SET state=?,updated=?,closed=? WHERE id=?',
                        (state,at,at if state == 'closed' else None,ident))
            con.execute('INSERT INTO connection_event(session_id,time,kind,direction,summary) VALUES(?,?,?,?,?)',
                        (ident,at,'log','',reason))
            if state == 'closed':
                self._policies.pop(ident, None)
                self._policy_values.pop(ident, None)
        elif operation == 'event':
            ident, at, kind, direction, summary, data = args
            if not con.execute('SELECT 1 FROM connection_session WHERE id=?',(ident,)).fetchone():
                self.dropped += 1
                return
            enabled = True
            if kind == 'packet':
                enabled = bool(self._policies.get(ident, lambda: True)())
                if self._policy_values.get(ident) != enabled:
                    self._policy_values[ident] = enabled
                    con.execute('UPDATE connection_session SET packet_recording=? WHERE id=?',(int(enabled),ident))
                    con.execute('INSERT INTO connection_event(session_id,time,kind,direction,summary) VALUES(?,?,?,?,?)',
                                (ident,at,'log','','原始报文保存'+('开启' if enabled else '关闭')))
            if kind != 'packet' or enabled:
                con.execute('INSERT INTO connection_event(session_id,time,kind,direction,summary,data) VALUES(?,?,?,?,?,?)', args)
            if direction in ('RX','TX') and data is not None:
                side = direction.lower()
                con.execute(f'UPDATE connection_session SET {side}_bytes={side}_bytes+?, '
                            f'{side}_count={side}_count+1,updated=? WHERE id=?', (len(data),at,ident))
                if enabled or _is_important_interaction(summary, data):
                    decoded = describe_packet(summary, direction, data)
                    if decoded:
                        con.execute('INSERT INTO connection_event(session_id,time,kind,direction,summary) VALUES(?,?,?,?,?)',
                                    (ident,at,'interaction',direction,decoded))


def _is_important_interaction(protocol, data):
    """报文保存关闭时仅对关键控制交互解码，跳过高频心跳/周期帧与常规读响应以降低 CPU 与写放大。"""
    if not data:
        return False
    if protocol == 'IEC104' and len(data) >= 6 and data[0] == 0x68:
        ctrl = data[2]
        if (ctrl & 3) == 3:
            # U 帧：保留 STARTDT/STOPDT，跳过周期保活 TESTFR(0x43/0x83)。
            return ctrl not in (0x43, 0x83)
        if (ctrl & 3) == 1:
            # S 帧仅为序号确认。
            return False
        if len(data) >= 9:
            # I 帧：跳过高频周期扫描帧(COT=1)，保留总召、变位与遥控/遥调指令。
            return (data[8] & 63) != 1
        return True
    if protocol == 'Modbus-TCP' and len(data) >= 8:
        fc = data[7]
        # 保留异常响应与写入指令，跳过常规高频读请求/响应。
        return bool(fc & 128) or fc in (5, 6, 15, 16)
    return False


def describe_packet(protocol, direction, data):
    try:
        if protocol == 'IEC104':
            from core.iec104_protocol import describe_apdu, parse_apdu
            from core.communication_log import decode_asdu
            frame = parse_apdu(data)
            text = describe_apdu(data)
            if frame.asdu_length:
                text += '\n' + json.dumps(decode_asdu(data[6:]), ensure_ascii=False, default=str)
            return text
        if protocol == 'Modbus-TCP' and len(data) >= 8:
            tid, pid, length = (int.from_bytes(data[i:i+2],'big') for i in (0,2,4))
            unit, fc = data[6:8]
            text = f'事务={tid} 站号={unit} 功能码={fc:02d} MBAP长度={length}'
            if pid or length != len(data)-6:
                return text + ' 无效MBAP长度或协议标识'
            if fc & 128 and len(data) >= 9:
                return text + f' 异常码={data[8]}'
            if len(data) >= 12 and (direction == 'RX' or fc in (5,6,15,16)):
                text += f' 地址(0基)={int.from_bytes(data[8:10],"big")} 数量/值={int.from_bytes(data[10:12],"big")}'
            if len(data)>8:
                text += ' 数据=' + data[8:].hex(' ')
            return text
    except Exception as exc:
        return f'解码失败：{exc}'
    return ''


def read_history(db_path, component, ident=None, *, events=False, query=None):
    query = query or {}
    path = history_path(db_path, component)
    def integer(key, default, low, high):
        value = int(query.get(key, default))
        if not low <= value <= high:
            raise ValueError(f'{key} 必须在 {low}..{high}')
        return value
    limit = integer('limit', 50, 1, 200)
    before = integer('before', 9223372036854775807, 1, 9223372036854775807)
    after = integer('after', 0, 0, 9223372036854775807)
    offset = integer('offset', 0, 0, 1000000)
    kind = query.get('kind','')
    if kind not in ('','packet','interaction','log'):
        raise ValueError('kind 必须是 packet、interaction 或 log')
    channel = integer('channel_id', 0, 0, 2147483647)
    rtu = integer('rtu_id', 0, 0, 2147483647)
    state = query.get('state','')
    if state not in ('','open','closed'):
        raise ValueError('state 必须是 open 或 closed')
    empty = {'items':[], 'total':0, 'writers':[], 'retention':'每连接最近10000条记录；离线历史保留7天'}
    if not path.exists():
        return None if ident else empty
    con = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    try:
        writers = [dict(row) for row in con.execute('SELECT * FROM connection_writer')]
        if ident:
            row = con.execute('SELECT * FROM connection_session WHERE id=?',(ident,)).fetchone()
            if row is None:
                return None
            item = dict(row)
            item['stale'] = item['closed'] is None and time.time()-item['heartbeat'] > 20
            if not events:
                return {'item':item,'writers':[w for w in writers if w['owner']==item['owner']], 'retention':empty['retention']}
            where, values = 'session_id=? AND id<? AND id>?', [ident,before,after]
            if kind:
                where += ' AND kind=?'; values.append(kind)
            # Forward polling must start at the oldest unseen record; fetching
            # the newest page skips a burst larger than the page size.
            forward = 'after' in query and 'before' not in query
            order = 'ASC' if forward else 'DESC'
            rows = con.execute('SELECT * FROM connection_event WHERE '+where+
                               f' ORDER BY id {order} LIMIT ?',values+[limit+1]).fetchall()
            result = []
            for row in rows[:limit]:
                obj = dict(row)
                obj['hex'] = bytes(obj.pop('data') or b'').hex(' ')
                result.append(obj)
            if forward:
                result.reverse()  # All pages remain newest-first for display.
            return {'items':result,'has_more':len(rows)>limit,
                    'next_before':result[-1]['id'] if result else None,
                    'next_after':result[0]['id'] if result else after}
        where, values = '1=1', []
        if channel:
            where += ' AND channel_id=?'; values.append(channel)
        if rtu:
            where += ' AND rtu_id=?'; values.append(rtu)
        if state:
            where += ' AND closed IS '+('NOT NULL' if state=='closed' else 'NULL')
        rows = [dict(row) for row in con.execute('SELECT * FROM connection_session WHERE '+where+
                                               ' ORDER BY opened DESC,id DESC LIMIT ? OFFSET ?',values+[limit,offset])]
        for row in rows:
            row['stale'] = row['closed'] is None and time.time()-row['heartbeat'] > 20
        empty.update(items=rows,total=con.execute('SELECT count(*) FROM connection_session WHERE '+where,values).fetchone()[0],writers=writers)
        return empty
    finally:
        con.close()

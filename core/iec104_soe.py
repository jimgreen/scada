"""IEC104 digital event journal and bounded, acknowledgement-driven forwarding.

The journal is independent of latest-value tables. Delivery is at least once:
an APCI acknowledgement is not a downstream database commit acknowledgement.
CP56 calendar bytes are retained exactly; UTC is the display convention here.
"""
from datetime import datetime, timezone
import sqlite3
import threading
import time

TABLES = {'_scada_iec104_soe', '_scada_iec104_soe_cursor', 'sqlite_sequence'}
FIELDS = ('origin', 'channel_id', 'rtu_id', 'pnt_no', 'common_address', 'io_address',
          'type_id', 'raw_state', 'quality', 'cot', 'originator', 'is_test', 'negative',
          'event_time_ms', 'received_time_ms', 'cp56_hex', 'time_invalid',
          'time_substituted', 'summer')


def ensure_schema(connection, *, simulator=False):
    connection.execute('''CREATE TABLE IF NOT EXISTS _scada_iec104_soe (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        origin TEXT NOT NULL CHECK(origin IN ('S','C')),
        channel_id INTEGER NOT NULL, rtu_id INTEGER NOT NULL, pnt_no INTEGER NOT NULL,
        common_address INTEGER NOT NULL, io_address INTEGER NOT NULL,
        type_id INTEGER NOT NULL, raw_state INTEGER NOT NULL, quality INTEGER NOT NULL,
        cot INTEGER NOT NULL, originator INTEGER NOT NULL DEFAULT 0,
        is_test INTEGER NOT NULL DEFAULT 0, negative INTEGER NOT NULL DEFAULT 0,
        event_time_ms INTEGER, received_time_ms INTEGER NOT NULL,
        cp56_hex TEXT, time_invalid INTEGER NOT NULL DEFAULT 0,
        time_substituted INTEGER NOT NULL DEFAULT 0, summer INTEGER NOT NULL DEFAULT 0)''')
    connection.execute('CREATE INDEX IF NOT EXISTS _scada_soe_origin_event '
                       'ON _scada_iec104_soe(origin,event_id)')
    connection.execute('CREATE INDEX IF NOT EXISTS _scada_soe_rtu_event '
                       'ON _scada_iec104_soe(origin,rtu_id,event_id)')
    connection.execute('''CREATE TABLE IF NOT EXISTS _scada_iec104_soe_cursor (
        consumer TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL DEFAULT 0,
        ack_time_ms INTEGER NOT NULL DEFAULT 0)''')
    if simulator:
        # Source values and events commit/roll back together. Refresh-time-only
        # trigger updates cannot create duplicate events. No HMI callback needed.
        stamp = "CAST((julianday('now')-2440587.5)*86400000+0.5 AS INTEGER)"
        connection.execute(f'''CREATE TRIGGER IF NOT EXISTS _scada_s_iec104_soe
            AFTER UPDATE OF value,status ON fes_yx_define
            WHEN OLD.value IS NOT NEW.value OR OLD.status IS NOT NEW.status
            BEGIN
              INSERT INTO _scada_iec104_soe
                (origin,channel_id,rtu_id,pnt_no,common_address,io_address,
                 type_id,raw_state,quality,cot,event_time_ms,received_time_ms)
              SELECT 'S',r.channel_id,NEW.rtu_id,NEW.pnt_no,COALESCE(d.rtu_addr,1),
                COALESCE(m.io_address,COALESCE(NULLIF(d.yxbegin_addr,0),1)+NEW.pnt_no-1),
                CASE WHEN m.type_id IN (3,31) THEN 31 ELSE 30 END,
                CASE WHEN m.type_id IN (3,31) THEN CASE WHEN NEW.value THEN 2 ELSE 1 END
                     ELSE CASE WHEN NEW.value THEN 1 ELSE 0 END END,
                COALESCE(NEW.status,0)&240,3,{stamp},{stamp}
              FROM fes_rtu_define r
              JOIN fes_channel_iec104_define d ON d.channel_id=r.channel_id
              JOIN fes_channel_iec104_point_define m
                ON m.channel_id=r.channel_id AND m.kind='yx' AND m.pnt_no=NEW.pnt_no
              WHERE r.rtu_id=NEW.rtu_id AND r.channel_type='iec104'
                AND (m.type_id IN (30,31) OR
                     (m.type_id IN (1,3) AND m.time_tag='CP56Time2a'));
            END''')


def decode_events(asdu):
    """Decode SP/DP ASDUs without datetime bindings or Boolean DP conversion."""
    asdu = bytes(asdu)
    if len(asdu) < 6:
        raise ValueError('SOE ASDU 头不完整')
    typ, vsq, cot, oa = asdu[:4]
    if typ not in (1,3,30,31):
        return []
    count, sequential = vsq & 127, bool(vsq & 128)
    size = 8 if typ in (30,31) else 1
    if not count or len(asdu) != 6 + (3 if sequential else 3*count) + size*count:
        raise ValueError('SOE 信息体长度不合法')
    result, offset, base = [], 6, 0
    for index in range(count):
        if not sequential or index == 0:
            base = int.from_bytes(asdu[offset:offset+3], 'little'); offset += 3
        ioa = base+index if sequential else base
        if ioa > 0xffffff:
            raise ValueError('SOE IOA 溢出')
        siq = asdu[offset]; offset += 1
        row = dict(common_address=int.from_bytes(asdu[4:6], 'little'), io_address=ioa,
                   type_id=typ, raw_state=siq & (3 if typ in (3,31) else 1),
                   quality=siq & 240, cot=cot & 63, originator=oa,
                   is_test=int(bool(cot & 128)), negative=int(bool(cot & 64)),
                   event_time_ms=None, cp56_hex=None, time_invalid=0,
                   time_substituted=0, summer=0)
        if size == 8:
            raw = asdu[offset:offset+7]; offset += 7
            row.update(cp56_hex=raw.hex(), time_invalid=int(bool(raw[2]&128)),
                       time_substituted=int(bool(raw[2]&64)), summer=int(bool(raw[3]&128)))
            ms = int.from_bytes(raw[:2], 'little')
            try:
                stamp = datetime(2000+(raw[6]&127),raw[5]&15,raw[4]&31,raw[3]&31,
                                 raw[2]&63,ms//1000,ms%1000*1000,tzinfo=timezone.utc)
                row['event_time_ms'] = int(stamp.timestamp()*1000)
            except ValueError:
                # Invalid clock bytes are evidence, not a reason to lose an event.
                row['time_invalid'] = 1
        result.append(row)
    return result


def _cp56(row):
    if row.get('cp56_hex'):
        raw = bytes.fromhex(row['cp56_hex'])
        if len(raw) != 7: raise ValueError('CP56 长度必须为 7')
        return raw
    stamp = datetime.fromtimestamp(row['event_time_ms']/1000, timezone.utc)
    if not 2000 <= stamp.year <= 2099: raise ValueError('CP56 年份超出 2000..2099')
    ms = stamp.second*1000+stamp.microsecond//1000
    return ms.to_bytes(2,'little') + bytes((stamp.minute | (128 if row.get('time_invalid') else 0)
        | (64 if row.get('time_substituted') else 0),
        stamp.hour | (128 if row.get('summer') else 0),
        stamp.day | (stamp.isoweekday()<<5),stamp.month,stamp.year-2000))


def encode_events(events, max_length=249):
    """Pack adjacent compatible events, preserving order and raw CP56 flags."""
    if not 17 <= max_length <= 249: raise ValueError('SOE ASDU 长度必须在 17..249')
    batches, payload, previous, count = [], bytearray(), None, 0
    def flush():
        if count:
            typ, cot, oa, ca = previous
            batches.append(bytes((typ,count,cot,oa))+ca.to_bytes(2,'little')+payload)
    for row in events:
        typ = int(row['type_id'])
        if typ not in (30,31): raise ValueError('可靠 SOE 转发要求 CP56 单点/双点类型 30/31')
        state, quality = int(row['raw_state']), int(row['quality'])
        if not 0 <= state <= (3 if typ == 31 else 1) or quality & ~240:
            raise ValueError('SOE 状态或品质无效')
        ca, ioa, oa = int(row['common_address']), int(row['io_address']), int(row.get('originator',0))
        if not 1 <= ca <= 65534 or not 0 <= ioa <= 0xffffff or not 0 <= oa <= 255:
            raise ValueError('SOE 地址超出范围')
        cot = int(row['cot']) | (128 if row.get('is_test') else 0) | (64 if row.get('negative') else 0)
        if cot & 63 != 3 or cot & 64: raise ValueError('SOE 转发仅接受正向突发 COT=3')
        header = (typ,cot,oa,ca)
        if count and (header != previous or 6+len(payload)+11 > max_length or count == 127):
            flush(); payload, count = bytearray(), 0
        previous = header
        payload.extend(ioa.to_bytes(3,'little')+bytes((state|quality,))+_cp56(row)); count += 1
    flush()
    return batches


def _connection(store):
    # A native connection can move callbacks to another OS thread on reconnect.
    # Owners serialize access; no cross-thread transaction sharing with the HMI.
    con = sqlite3.connect(str(store.db_path), timeout=5, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=5000')
    con.execute('PRAGMA synchronous=FULL')
    return con


class SoeReceiver:
    def __init__(self, store, rtu_id, channel_id, addresses):
        self.store, self.rtu_id, self.channel_id = store, rtu_id, channel_id
        self.addresses = addresses
        self.connection, self.last_message = None, None
        self.lock = threading.RLock()
        self.count, self.error = 0, None
        self.on_commit = None

    def receive(self, message):
        with self.lock:
            if message is self.last_message:
                return
            # Keep the wrapper alive until the next ASDU. pybind returns the
            # registered shared_ptr instance on later callbacks in this ASDU.
            rows = decode_events(bytes(message.raw)[6:])
            prepared = []
            now = time.time_ns()//1_000_000
            for row in rows:
                # GI/periodic snapshots still update the current-value cache.
                # They are not state-change events and must not grow the SOE
                # journal on every interrogation/report cycle.
                if row['cot'] != 3 or row['negative']:
                    continue
                key = (row['io_address'],row['type_id'])
                if key not in self.addresses: continue
                row.update(origin='C',channel_id=self.channel_id,rtu_id=self.rtu_id,
                           pnt_no=self.addresses[key],received_time_ms=now)
                prepared.append(tuple(row.get(name) for name in FIELDS))
            if prepared:
                if self.connection is None: self.connection = _connection(self.store)
                con = self.connection
                con.execute('BEGIN IMMEDIATE')
                try:
                    con.executemany(f'INSERT INTO _scada_iec104_soe ({",".join(FIELDS)}) '
                                    f'VALUES ({",".join("?" for _ in FIELDS)})', prepared)
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
                self.count += len(prepared)
                if self.on_commit is not None:
                    try:
                        self.on_commit([dict(zip(FIELDS, row)) for row in prepared])
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception('SOE已入库，但业务日志失败')
            self.last_message = message

    def close(self):
        with self.lock:
            if self.connection is not None:
                self.connection.close(); self.connection = None
            self.last_message = None


class SoeForwarder:
    """One bounded in-flight batch per channel; cursor commits after APCI ACK.

    A failed/disconnected batch is retried. Crash after ACK before cursor commit
    can duplicate it. There is deliberately no timer that discards old events.
    """
    def __init__(self, store, consumer, origin, route, *, rtu_id=None, batch_size=128, on_ack=None, on_event=None):
        if not 1 <= batch_size <= 128: raise ValueError('SOE batch_size 必须在 1..128')
        self.store, self.consumer, self.origin, self.route = store, consumer, origin, route
        self.rtu_id, self.batch_size = rtu_id, batch_size
        self.connection = None
        self.last_event_id = 0
        self.pending = None
        self.acked = False
        self.error = None
        self.on_ack = on_ack
        self.on_event = on_event
        self.pending_events = []

    def tick(self, sender):
        if self.connection is None:
            self.connection = _connection(self.store)
            row = self.connection.execute('SELECT last_event_id FROM _scada_iec104_soe_cursor WHERE consumer=?',
                                          (self.consumer,)).fetchone()
            self.last_event_id = int(row[0]) if row else 0
        if self.pending is not None:
            state = 1 if self.acked else sender.poll_soe()
            if state == 0: return 0
            if state < 0:
                self._event('SOE未确认，保留游标重试', self.pending_events)
                self.pending = None
                self.pending_events = []
                self.error = 'SOE 批次连接中断或发送未完成，保留游标等待重试'
                return 0
            self.acked = True
            self._advance(self.pending)
            self.pending, self.acked, self.error = None, False, None
            delivered, self.pending_events = self.pending_events, []
            self._event('SOE收到APCI确认并推进游标', delivered)
            if self.on_ack is not None:
                self.on_ack(delivered, time.time_ns()//1_000_000)
        where, args = 'origin=? AND event_id>?', [self.origin,self.last_event_id]
        if self.rtu_id is not None:
            where += ' AND rtu_id=?'; args.append(self.rtu_id)
        rows = self.connection.execute(f'SELECT * FROM _scada_iec104_soe WHERE {where} '
                                       'ORDER BY event_id LIMIT ?', (*args,self.batch_size)).fetchall()
        if not rows: return 0
        events = []
        for row in rows:
            if row['cot'] == 3 and not row['negative']:
                events.extend(self.route(dict(row)))
        target = rows[-1]['event_id']
        if not events:
            self._advance(target)
        elif sender.submit_soe(events):
            self.pending = target
            self.pending_events = events
            self._event('SOE已提交发送，等待APCI确认', events)
        return len(events)

    def _event(self, action, events):
        if self.on_event is not None:
            try:
                self.on_event(action, events)
            except Exception:
                import logging
                logging.getLogger(__name__).exception('SOE业务日志失败；投递状态保持不变')

    def _advance(self, target):
        self.connection.execute('''INSERT INTO _scada_iec104_soe_cursor(consumer,last_event_id,ack_time_ms)
            VALUES (?,?,?) ON CONFLICT(consumer) DO UPDATE SET
            last_event_id=MAX(last_event_id,excluded.last_event_id),ack_time_ms=excluded.ack_time_ms''',
            (self.consumer,target,time.time_ns()//1_000_000))
        self.last_event_id = target

    def close(self):
        if self.connection is not None:
            self.connection.close(); self.connection = None
        self.pending, self.acked = None, False
        self.pending_events = []

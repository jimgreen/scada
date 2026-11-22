"""Explicit command feedback rules and observations; never sends device commands."""
import math
import time
from datetime import datetime
from core.command_durability import durable_connection

TABLES={'_scada_command_feedback_rule','_scada_command_feedback_sample'}
ACTIVE=('waiting','mismatch','invalid')
LABELS={'unconfigured':'未关联回采验证','armed':'待执行后回采','waiting':'等待新回采',
        'mismatch':'新回采暂不符合','invalid':'新回采质量无效','matched':'新回采符合预期',
        'timeout':'回采超时','superseded':'已被后续命令替代','unconfirmed':'规约结果未确认'}


def ensure_schema(con):
    fields={'feedback_rtu_id':'INTEGER','feedback_kind':'TEXT','feedback_pnt_no':'INTEGER',
            'feedback_expected':'REAL','feedback_tolerance':'REAL','feedback_timeout_ms':'INTEGER',
            'feedback_deadline':'INTEGER','feedback_state':"TEXT NOT NULL DEFAULT 'unconfigured'",
            'feedback_value':'REAL','feedback_quality':'INTEGER','feedback_received_time':'INTEGER',
            'feedback_checked_time':'INTEGER','review_time':'INTEGER','review_conclusion':'TEXT',
            'review_note':'TEXT','review_actor':'TEXT','resolution_time':'INTEGER',
            'resolution_actor':'TEXT','resolution_note':'TEXT'}
    existing={r[1] for r in con.execute('PRAGMA table_info(_scada_command_trace)')}
    for name,definition in fields.items():
        if name not in existing:con.execute(f'ALTER TABLE _scada_command_trace ADD COLUMN {name} {definition}')
    con.execute('''CREATE TABLE IF NOT EXISTS _scada_command_feedback_rule(
        rtu_id INTEGER NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('yk','yt')),pnt_no INTEGER NOT NULL,
        feedback_rtu_id INTEGER NOT NULL,feedback_kind TEXT NOT NULL CHECK(feedback_kind IN ('yx','yc')),
        feedback_pnt_no INTEGER NOT NULL,gain REAL NOT NULL,offset REAL NOT NULL,tolerance REAL NOT NULL CHECK(tolerance>=0),
        timeout_ms INTEGER NOT NULL CHECK(timeout_ms>=100),enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        version INTEGER NOT NULL,PRIMARY KEY(rtu_id,kind,pnt_no)) WITHOUT ROWID''')
    con.execute('''CREATE TABLE IF NOT EXISTS _scada_command_feedback_sample(
        rtu_id INTEGER NOT NULL,kind TEXT NOT NULL,pnt_no INTEGER NOT NULL,value REAL,
        quality INTEGER NOT NULL,received_time INTEGER NOT NULL,
        PRIMARY KEY(rtu_id,kind,pnt_no)) WITHOUT ROWID''')
    con.execute('''CREATE INDEX IF NOT EXISTS _command_feedback_active ON _scada_command_trace(rtu_id,command_id)
        WHERE feedback_state IN ('waiting','mismatch','invalid')''')
    con.execute('''CREATE INDEX IF NOT EXISTS _command_feedback_target ON _scada_command_trace(feedback_rtu_id,feedback_kind,feedback_pnt_no)
        WHERE feedback_state IN ('armed','waiting','mismatch','invalid')''')
    con.execute('CREATE INDEX IF NOT EXISTS _command_rule_target ON _scada_command_feedback_rule(feedback_rtu_id,enabled)')


def save_rule(store,body):
    required={'rtu_id','kind','pnt_no','feedback_rtu_id','feedback_pnt_no','gain','offset','tolerance','timeout_ms','enabled','version'}
    if not isinstance(body,dict) or set(body)!=required:raise ValueError('回采规则字段不完整或包含未知字段')
    data=dict(body)
    for name in ('rtu_id','pnt_no','feedback_rtu_id','feedback_pnt_no','timeout_ms','version'):
        if type(data[name]) is not int or data[name]<(0 if name=='version' else 1):raise ValueError(f'{name} 必须为有效整数')
    if not 100<=data['timeout_ms']<=3600000:raise ValueError('回采超时必须为 100..3600000 毫秒')
    if data['kind'] not in ('yk','yt'):raise ValueError('只支持遥控/遥调回采')
    if type(data['enabled']) not in (bool,int) or data['enabled'] not in (0,1):raise ValueError('enabled 必须为布尔值')
    data['enabled']=int(data['enabled'])
    for name in ('gain','offset','tolerance'):
        if isinstance(data[name],bool):raise ValueError('回采参数必须为有限数')
        try:data[name]=float(data[name])
        except (ValueError,TypeError,OverflowError):raise ValueError('回采参数必须为有限数')
        if not math.isfinite(data[name]):raise ValueError('回采参数必须为有限数')
    if data['tolerance']<0:raise ValueError('偏差不能小于 0')
    if data['kind']=='yk' and (data['tolerance']!=0 or (data['gain'],data['offset']) not in ((1,0),(-1,1))):
        raise ValueError('遥控反馈仅支持同向(1,0)或反向(-1,1)，偏差为0')
    data['feedback_kind']='yx' if data['kind']=='yk' else 'yc'
    with durable_connection(store) as con:
        con.execute('BEGIN IMMEDIATE')
        for kind,rid,no in ((data['kind'],data['rtu_id'],data['pnt_no']),
                            (data['feedback_kind'],data['feedback_rtu_id'],data['feedback_pnt_no'])):
            if not con.execute(f'SELECT 1 FROM fes_{kind}_define WHERE rtu_id=? AND pnt_no=?',(rid,no)).fetchone():
                raise ValueError('命令点或回采点不存在')
        keys=(data['rtu_id'],data['kind'],data['pnt_no'])
        current=con.execute('SELECT version FROM _scada_command_feedback_rule WHERE rtu_id=? AND kind=? AND pnt_no=?',keys).fetchone()
        if data['version']!=(current[0] if current else 0):raise ValueError('规则已被修改，请重新读取')
        data['version']+=1
        columns=tuple(data)
        assignments=','.join(f'{name}=excluded.{name}' for name in columns if name not in ('rtu_id','kind','pnt_no'))
        con.execute(f'''INSERT INTO _scada_command_feedback_rule({','.join(columns)}) VALUES({','.join('?' for _ in columns)})
                     ON CONFLICT(rtu_id,kind,pnt_no) DO UPDATE SET {assignments}''',tuple(data.values()))
        con.commit()
    from core import operation_audit as audit
    audit.emit('回采规则已保存',rule=data)
    return data


def snapshot_rule(con,ident,rtu,kind,no,value):
    con.execute('''UPDATE _scada_command_trace SET feedback_state='superseded' WHERE rtu_id=? AND kind=? AND pnt_no=?
        AND command_id<? AND feedback_state IN ('waiting','mismatch','invalid')''',(rtu,kind,no,ident))
    rule=con.execute('SELECT * FROM _scada_command_feedback_rule WHERE rtu_id=? AND kind=? AND pnt_no=? AND enabled=1',(rtu,kind,no)).fetchone()
    if not rule:return
    expected=float(value)*rule['gain']+rule['offset']
    if not math.isfinite(expected):raise ValueError('回采预期值溢出，禁止派发')
    con.execute('''UPDATE _scada_command_trace SET feedback_rtu_id=?,feedback_kind=?,feedback_pnt_no=?,
        feedback_expected=?,feedback_tolerance=?,feedback_timeout_ms=?,feedback_state='armed' WHERE command_id=?''',
        (rule['feedback_rtu_id'],rule['feedback_kind'],rule['feedback_pnt_no'],expected,rule['tolerance'],rule['timeout_ms'],ident))


def record_samples(store,rtu,samples):
    if not samples:return
    with store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        con.executemany('''INSERT INTO _scada_command_feedback_sample(rtu_id,kind,pnt_no,value,quality,received_time)
            VALUES(?,?,?,?,?,?) ON CONFLICT(rtu_id,kind,pnt_no) DO UPDATE SET
            value=excluded.value,quality=excluded.quality,received_time=excluded.received_time
            WHERE excluded.received_time>received_time OR (excluded.received_time=received_time
                AND (excluded.value IS NOT value OR excluded.quality IS NOT quality))''',[(rtu,*row) for row in samples])
        con.commit()


def evaluate_feedback(store, rtu, *, now=None, con=None):
    now=time.time_ns()//1000000 if now is None else now
    sql = '''SELECT h.*,s.value AS observed_value,s.quality AS observed_quality,s.received_time AS observed_time
            FROM _scada_command_trace h LEFT JOIN _scada_command_feedback_sample s
            ON s.rtu_id=h.feedback_rtu_id AND s.kind=h.feedback_kind AND s.pnt_no=h.feedback_pnt_no
            WHERE h.rtu_id=? AND h.feedback_state IN ('waiting','mismatch','invalid')
            AND h.stage IN ('protocol_confirmed','driver_completed')'''
    if con is not None:
        rows = con.execute(sql, (rtu,)).fetchall()
    else:
        with store.connection() as own_con:
            rows = own_con.execute(sql, (rtu,)).fetchall()
    updates=[]
    for row in rows:
        stamp=row['observed_time'];state=row['feedback_state']
        valid_time=stamp is not None and row['execute_time']<stamp<=row['feedback_deadline'] and stamp<=now
        value,quality=row['observed_value'],row['observed_quality']
        if valid_time:
            if quality!=0 or value is None or not math.isfinite(value):state='invalid'
            elif abs(value-row['feedback_expected'])<=row['feedback_tolerance']:state='matched'
            else:state='mismatch'
        if state!='matched' and now>=row['feedback_deadline']:state='timeout'
        observation=(value,quality,stamp) if valid_time else (row['feedback_value'],row['feedback_quality'],row['feedback_received_time'])
        if (state,*observation)!=(row['feedback_state'],row['feedback_value'],row['feedback_quality'],row['feedback_received_time']):
            updates.append((state,*observation,now,row['command_id'],row['stage']))
    if updates:
        with durable_connection(store) as con:
            con.execute('BEGIN IMMEDIATE')
            con.executemany('''UPDATE _scada_command_trace SET feedback_state=?,feedback_value=?,feedback_quality=?,
                feedback_received_time=?,feedback_checked_time=? WHERE command_id=? AND stage=?
                AND feedback_state IN ('waiting','mismatch','invalid')''',updates)
            con.commit()
    return len(updates)


class FeedbackTracker:
    """Watch pending commands only; a revision hint avoids repeated detail reads."""
    def __init__(self,store,rtu):
        self.store,self.rtu=store,rtu
        self.keys=set();self.revision=None;self.seen={};self.deadline=None

    def refresh(self, con=None):
        now=time.time_ns()//1000000
        def _query(conn):
            revision=conn.execute('SELECT revision FROM _scada_command_revision WHERE id=1').fetchone()[0]
            if self.revision==revision and (self.deadline is None or now<self.deadline):
                return
            # Read the hint before the rows. A concurrent commit either appears
            # here or changes the next hint; it cannot hide a new subscription.
            rows=conn.execute('''SELECT feedback_kind,feedback_pnt_no,feedback_deadline FROM _scada_command_trace
                WHERE feedback_rtu_id=? AND feedback_state IN ('armed','waiting','mismatch','invalid')
                AND resolution_time IS NULL AND (feedback_deadline IS NULL OR feedback_deadline>?)''',(self.rtu,now)).fetchall()
            self.keys={(row[0],row[1]) for row in rows}
            self.deadline=min((row[2] for row in rows if row[2] is not None),default=None)
            self.revision=revision
        if con is not None:
            _query(con)
        else:
            with self.store.connection() as own_con:
                _query(own_con)
        self.seen={key:value for key,value in self.seen.items() if key in self.keys}

    def sample(self,kind,no,value,quality,item):
        if (kind,no) not in self.keys:return None
        stamp=item.get('received_at') if isinstance(item,dict) else getattr(item,'received_at',None)
        if isinstance(stamp,datetime):stamp=stamp.timestamp()*1000 if stamp.tzinfo is not None else None
        elif isinstance(stamp,(int,float)) and not isinstance(stamp,bool):stamp*=1000
        else:return None
        if stamp is None or not math.isfinite(stamp) or stamp<=0:return None
        row=(kind,no,value,quality,int(stamp))
        return None if self.seen.get((kind,no))==row else row

    def commit(self,rows):
        record_samples(self.store,self.rtu,rows)
        for row in rows:self.seen[row[:2]]=row


def save_review(store,ident,body):
    if not isinstance(body,dict) or set(body)!={'stage','review_time','conclusion','note','actor'}:raise ValueError('核对记录字段错误')
    if body['conclusion'] not in ('observed_expected','observed_not_expected','unknown'):raise ValueError('未知核对结论')
    if type(body['review_time']) is not int or body['review_time']<0:raise ValueError('核对版本错误')
    for key,limit in (('note',1000),('actor',80)):
        if not isinstance(body[key],str) or not 1<=len(body[key].strip())<=limit:raise ValueError('请填写操作人声明和核对说明')
    with durable_connection(store) as con:
        con.execute('BEGIN IMMEDIATE')
        now=max(time.time_ns()//1000000,body['review_time']+1)
        updated=con.execute('''UPDATE _scada_command_trace SET review_time=?,review_conclusion=?,review_note=?,review_actor=?
            WHERE command_id=? AND stage=? AND COALESCE(review_time,0)=?''',
            (now,body['conclusion'],body['note'].strip(),body['actor'].strip(),ident,body['stage'],body['review_time'])).rowcount
        if not updated:raise ValueError('指令状态或核对记录已变化，请刷新后重试')
        con.commit()
    from core import operation_audit as audit
    audit.emit('指令人工核对记录已保存',command_id=ident,conclusion=body['conclusion'],note=body['note'],actor_declared=body['actor'])
    return dict(command_id=ident,review_time=now)

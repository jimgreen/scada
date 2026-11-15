"""Durable command lifecycle; acknowledgements never imply physical feedback."""
import os
import time
from core.command_durability import durable_connection
from core.command_feedback import TABLES as FEEDBACK_TABLES

TABLES={'_scada_command_trace','_scada_command_revision'} | FEEDBACK_TABLES


def ensure_schema(con, *, simulator=False):
    con.execute('''CREATE TABLE IF NOT EXISTS _scada_command_trace(
        command_id INTEGER PRIMARY KEY AUTOINCREMENT,
        rtu_id INTEGER NOT NULL, kind TEXT NOT NULL, pnt_no INTEGER NOT NULL,
        cmd_time INTEGER NOT NULL, value REAL, stage TEXT NOT NULL DEFAULT 'queued',
        dispatch_time INTEGER, select_time INTEGER, select_ack_time INTEGER,
        execute_time INTEGER, finish_time INTEGER, owner_pid INTEGER,
        confirmation_scope TEXT NOT NULL DEFAULT 'database_commit', error TEXT NOT NULL DEFAULT '')''')
    from core.command_feedback import ensure_schema as ensure_feedback
    ensure_feedback(con)
    con.execute('CREATE TABLE IF NOT EXISTS _scada_command_revision(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL)')
    con.execute('INSERT OR IGNORE INTO _scada_command_revision VALUES(1,0)')
    for table in ('_scada_command_trace','_scada_command_feedback_rule'):
        for action in ('INSERT','UPDATE','DELETE'):
            con.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_rev_{action.lower()}
                AFTER {action} ON {table} BEGIN UPDATE _scada_command_revision SET revision=revision+1 WHERE id=1; END''')
    for table in ('fes_rtu_define','fes_yk_define','fes_yt_define','fes_yc_define','fes_yx_define'):
        for action,suffix in [('INSERT','ins'),('DELETE','del'),('UPDATE OF name','name')]:
            con.execute(f'''CREATE TRIGGER IF NOT EXISTS _command_names_{table}_{suffix}
                AFTER {action} ON {table} BEGIN UPDATE _scada_command_revision SET revision=revision+1 WHERE id=1; END''')
    # History/log links also depend on channel binding, not just RTU names.
    columns={row[1] for row in con.execute('PRAGMA table_info(fes_rtu_define)')}
    if {'channel_id','channel_type'}<=columns:
        con.execute('''CREATE TRIGGER IF NOT EXISTS _command_rtu_binding
            AFTER UPDATE OF channel_id,channel_type ON fes_rtu_define
            BEGIN UPDATE _scada_command_revision SET revision=revision+1 WHERE id=1; END''')
    # C/T submission tokens are unique; two S commands may arrive in one ms.
    con.execute('''CREATE UNIQUE INDEX IF NOT EXISTS _scada_command_trace_token
        ON _scada_command_trace(rtu_id,kind,pnt_no,cmd_time) WHERE stage!='received' ''')
    con.execute('CREATE INDEX IF NOT EXISTS _scada_command_trace_stage ON _scada_command_trace(stage,command_id)')
    con.execute('CREATE INDEX IF NOT EXISTS _scada_command_trace_rtu ON _scada_command_trace(rtu_id,command_id)')
    for kind in ('yk','yt'):
        table=f'fes_{kind}_define'
        for action in ('INSERT','UPDATE OF cmd_time'):
            suffix='ins' if action=='INSERT' else 'upd'
            con.execute(f'''CREATE TRIGGER IF NOT EXISTS _command_trace_{kind}_{suffix}
                AFTER {action} ON {table}
                WHEN NEW.cmd_time>COALESCE(NEW.cmd_handle_time,0)
                BEGIN INSERT OR IGNORE INTO _scada_command_trace(rtu_id,kind,pnt_no,cmd_time,value)
                    VALUES(NEW.rtu_id,'{kind}',NEW.pnt_no,NEW.cmd_time,NEW.value); END''')
        if simulator:
            con.execute(f'''CREATE TRIGGER IF NOT EXISTS _command_trace_{kind}_received
                AFTER UPDATE OF cmd_handle_time ON {table}
                WHEN NEW.cmd_handle_time>0
                BEGIN INSERT OR IGNORE INTO _scada_command_trace
                    (rtu_id,kind,pnt_no,cmd_time,value,stage,finish_time,confirmation_scope)
                    VALUES(NEW.rtu_id,'{kind}',NEW.pnt_no,NEW.cmd_handle_time,NEW.value,
                           'received',NEW.cmd_handle_time,'simulator_database'); END''')


def claim(store,rtu,kind,no,stamp,value):
    """Commit before I/O. A crash in dispatch must not blindly replay a command."""
    with durable_connection(store) as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute(f'SELECT cmd_time,cmd_handle_time FROM fes_{kind}_define WHERE rtu_id=? AND pnt_no=?',
                        (rtu,no)).fetchone()
        if not row or int(row[0] or 0)!=stamp or stamp<=int(row[1] or 0):
            return None
        con.execute('''INSERT OR IGNORE INTO _scada_command_trace(rtu_id,kind,pnt_no,cmd_time,value)
                       VALUES(?,?,?,?,?)''',(rtu,kind,no,stamp,value))
        updated=con.execute('''UPDATE _scada_command_trace SET stage='dispatching',dispatch_time=?,owner_pid=?
                   WHERE rtu_id=? AND kind=? AND pnt_no=? AND cmd_time=? AND stage='queued' ''',
                   (time.time_ns()//1_000_000,os.getpid(),rtu,kind,no,stamp)).rowcount
        trace=con.execute('SELECT command_id,stage,finish_time FROM _scada_command_trace WHERE rtu_id=? AND kind=? AND pnt_no=? AND cmd_time=?',
                          (rtu,kind,no,stamp)).fetchone()
        if updated:
            from core.command_feedback import snapshot_rule
            snapshot_rule(con,trace[0],rtu,kind,no,value)
        if not updated and trace[1] in {'protocol_confirmed','driver_completed','failed'} and trace[2]:
            # Recover a lost queued status write from the durable result, without
            # repeating device I/O or acknowledging a subsequently queued command.
            con.execute(f'''UPDATE fes_{kind}_define SET status=?,cmd_handle_time=?,refresh_time=?,_version=_version+1
                        WHERE rtu_id=? AND pnt_no=? AND cmd_time=? AND COALESCE(cmd_handle_time,0)<?''',
                        (3 if trace[1]=='failed' else 1,max(trace[2],stamp),trace[2],rtu,no,stamp,stamp))
        con.commit()
        return trace[0] if updated else None


def advance(store,command_id,stage,*,scope=None,error=''):
    columns={'select_sent':'select_time','select_confirmed':'select_ack_time','execute_sent':'execute_time',
             'protocol_confirmed':'finish_time','driver_completed':'finish_time','failed':'finish_time'}
    column=columns[stage]
    with durable_connection(store) as con:
        con.execute('BEGIN IMMEDIATE')
        now=time.time_ns()//1_000_000
        con.execute(f'''UPDATE _scada_command_trace SET stage=?,{column}=?,error=?,
            confirmation_scope=COALESCE(?,confirmation_scope) WHERE command_id=?''',
            (stage,now,str(error)[:2000],scope,command_id))
        if stage=='execute_sent':
            con.execute('''UPDATE _scada_command_trace SET feedback_state='waiting',feedback_deadline=?+feedback_timeout_ms
                WHERE command_id=? AND feedback_kind IS NOT NULL''',(now,command_id))
        elif stage=='failed':
            con.execute("UPDATE _scada_command_trace SET feedback_state='unconfirmed' WHERE command_id=? AND feedback_kind IS NOT NULL",(command_id,))
        con.commit()


UNCERTAIN={'dispatching','select_sent','select_confirmed','execute_sent'}


def resolution_blocker(row):
    """Conservative UI hint; the mutation repeats these checks under the DB lock."""
    from core.process_registry import process_is_alive
    if row['resolution_time']:return '该命令已人工结案'
    if row['stage'] not in UNCERTAIN:return '仅支持派发后结果未知的命令'
    if not row['owner_pid']:return '缺少原派发进程标识，不能安全结案'
    if process_is_alive(int(row['owner_pid'])):return '原派发进程仍在运行，请先停止所属采集进程并核实现场'
    return None


def resolve_uncertain(store,ident,body):
    """Close a dead owner's command slot without inventing an execution result."""
    required={'stage','cmd_time','actor','note','confirm_no_replay'}
    if not isinstance(body,dict) or set(body)!=required:raise ValueError('结案字段不完整')
    if body['confirm_no_replay'] is not True:raise ValueError('必须确认只结案，不重发旧命令')
    if not isinstance(body['stage'],str) or body['stage'] not in UNCERTAIN or type(body['cmd_time']) is not int:raise ValueError('结案版本错误')
    for name,limit in (('actor',80),('note',1000)):
        if not isinstance(body[name],str) or not 1<=len(body[name].strip())<=limit:raise ValueError('请填写操作人声明和结案依据')
    with durable_connection(store) as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM _scada_command_trace WHERE command_id=?',(ident,)).fetchone()
        if row is None:raise ValueError('命令不存在')
        if row['stage']!=body['stage'] or row['cmd_time']!=body['cmd_time']:raise ValueError('命令阶段已变化，请刷新')
        blocker=resolution_blocker(row)
        if blocker:raise ValueError(blocker)
        now=time.time_ns()//1000000
        # The old token must still occupy the point. Never acknowledge a newer
        # command, never mark execution successful, and keep the protocol phase.
        changed=con.execute(f'''UPDATE fes_{row['kind']}_define SET cmd_handle_time=cmd_time,
            status=3,_version=_version+1 WHERE rtu_id=? AND pnt_no=? AND cmd_time=?
            AND COALESCE(cmd_handle_time,0)<cmd_time''',(row['rtu_id'],row['pnt_no'],row['cmd_time'])).rowcount
        if changed!=1:raise ValueError('点命令槽位已变化或已处理，不能结案')
        con.execute('''UPDATE _scada_command_trace SET resolution_time=?,resolution_actor=?,resolution_note=?,
            feedback_state=CASE WHEN feedback_state IN ('armed','waiting','mismatch','invalid') THEN 'unconfirmed' ELSE feedback_state END
            WHERE command_id=?''',(now,body['actor'].strip(),body['note'].strip(),ident))
        con.commit()
    from core import operation_audit as audit
    audit.emit('不确定指令已人工结案，旧指令未重发',command_id=ident,rtu_id=row['rtu_id'],
               kind=row['kind'],pnt_no=row['pnt_no'],cmd_time=row['cmd_time'],original_stage=row['stage'],
               actor_declared=body['actor'],reason=body['note'])
    return dict(command_id=ident,resolution='closed_without_replay',resolution_time=now)

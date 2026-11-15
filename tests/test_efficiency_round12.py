"""Command durability, fresh feedback and conditional-query acceptance."""
import sqlite3
import time
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from scdfe_c.web.service import ScadaStore


@pytest.fixture
def store(tmp_path):
    data=ScadaStore(tmp_path/'business.sqlite3')
    for rid in (1,2):
        data.create_row('fes_rtu_define',dict(rtu_id=rid,channel_id=rid,channel_type='iec104',name=f'站{rid}'))
        for kind in ('yc','yx','yk','yt'):
            data.create_row(f'fes_{kind}_define',dict(rtu_id=rid,pnt_no=1,name=f'{kind}点',value=0,status=0))
    yield data
    data.close()


def rule(data,kind='yk',**extra):
    from core.command_feedback import save_rule
    values=dict(rtu_id=1,kind=kind,pnt_no=1,feedback_rtu_id=2,feedback_pnt_no=1,
                gain=1,offset=0,tolerance=0,timeout_ms=1000,enabled=True,version=0)
    values.update(extra)
    return save_rule(data,values)


def command(data,kind='yk',value=1):
    from core.command_store import DatabaseCommandWriter
    from core.command_trace import claim,advance
    row=DatabaseCommandWriter(data).submit_command(1,kind,1,value)
    ident=claim(data,1,kind,1,row['cmd_time'],value)
    advance(data,ident,'execute_sent')
    advance(data,ident,'protocol_confirmed',scope='iec104_activation_confirmation')
    return ident


def trace(data,ident):
    with data.connection() as con:
        return dict(con.execute('SELECT * FROM _scada_command_trace WHERE command_id=?',(ident,)).fetchone())


def test_full_sync_restores_pooled_connection_and_rolls_back(store):
    from core.command_durability import durable_connection
    with durable_connection(store) as con:
        assert con.execute('PRAGMA synchronous').fetchone()[0]==2
        con.execute('BEGIN IMMEDIATE')
        con.execute('UPDATE fes_yk_define SET cmd_time=999 WHERE rtu_id=1')
    with store.connection() as con:
        assert con.execute('PRAGMA synchronous').fetchone()[0]==1
        assert con.execute('SELECT cmd_time FROM fes_yk_define WHERE rtu_id=1').fetchone()[0]!=999


def test_feedback_rejects_old_cached_invalid_and_out_of_window_samples(store):
    from core.command_feedback import record_samples,evaluate_feedback
    rule(store);ident=command(store);row=trace(store,ident);stamp=row['execute_time']
    record_samples(store,2,[('yx',1,1,0,stamp-1)])
    evaluate_feedback(store,1,now=stamp+100)
    assert trace(store,ident)['feedback_state']=='waiting'
    # Editing the business point/heartbeat is never receive evidence.
    store.update_row('fes_yx_define',[2,1],dict(value=1,refresh_time=stamp+200))
    evaluate_feedback(store,1,now=stamp+201)
    assert trace(store,ident)['feedback_state']=='waiting'
    record_samples(store,2,[('yx',1,1,128,stamp+202)])
    evaluate_feedback(store,1,now=stamp+203)
    assert trace(store,ident)['feedback_state']=='invalid'
    record_samples(store,2,[('yx',1,0,0,stamp+204)])
    evaluate_feedback(store,1,now=stamp+205)
    assert trace(store,ident)['feedback_state']=='mismatch'
    record_samples(store,2,[('yx',1,1,0,stamp+2000)])
    evaluate_feedback(store,1,now=stamp+2001)
    assert trace(store,ident)['feedback_state']=='timeout'


def test_feedback_matching_uses_frozen_rule_and_survives_restart(store):
    from core.command_feedback import record_samples,evaluate_feedback
    rule(store,'yt',gain=2,offset=1,tolerance=.1)
    ident=command(store,'yt',5);row=trace(store,ident)
    rule(store,'yt',gain=1,offset=0,version=1)
    reopened=ScadaStore(store.db_path)
    try:
        record_samples(reopened,2,[('yc',1,11.05,0,row['execute_time']+1)])
        evaluate_feedback(reopened,1,now=row['execute_time']+2)
        result=trace(reopened,ident)
        assert result['feedback_state']=='matched' and result['feedback_expected']==11
        assert result['stage']=='protocol_confirmed'
    finally:reopened.close()


def test_feedback_tracker_uses_receive_time_even_for_unchanged_value(store,tmp_path):
    from core.command_feedback import FeedbackTracker,evaluate_feedback
    from scdfe_c import ChannelWorker,ChannelSpec
    rule(store);ident=command(store);stamp=trace(store,ident)['execute_time']
    worker=ChannelWorker(store,ChannelSpec(2,2,'iec104'),log_dir=tmp_path/'logs')
    try:
        def sample(received):return SimpleNamespace(kind='yx',point_no=1,value=1,quality=0,received_at=datetime.fromtimestamp(received/1000,timezone.utc))
        worker._sync_measurements([sample(stamp-1)])
        evaluate_feedback(store,1,now=stamp+1)
        assert trace(store,ident)['feedback_state']=='waiting'
        worker._sync_measurements([sample(stamp+2)])
        evaluate_feedback(store,1,now=stamp+3)
        assert trace(store,ident)['feedback_state']=='matched'
    finally:worker._close_command_connection()


def test_feedback_rule_validation_and_review_do_not_dispatch(store):
    from core.command_feedback import save_review
    with pytest.raises(ValueError):rule(store,tolerance=-1)
    with pytest.raises(ValueError):rule(store,feedback_pnt_no=999)
    rule(store)
    with pytest.raises(ValueError):rule(store,version=0)
    ident=command(store)
    row=trace(store,ident)
    save_review(store,ident,dict(stage=row['stage'],review_time=0,conclusion='unknown',note='现场待核对',actor='值班员'))
    reviewed=trace(store,ident)
    assert reviewed['review_conclusion']=='unknown' and reviewed['stage']==row['stage']
    with pytest.raises(ValueError):
        save_review(store,ident,dict(stage=row['stage'],review_time=0,conclusion='unknown',note='重复提交',actor='值班员'))


def test_command_sql_transitions_are_full_sync_and_restore_normal(tmp_path,monkeypatch):
    from core.command_store import DatabaseCommandWriter
    from core.command_trace import claim,advance
    calls=[];original=sqlite3.connect
    class Observed(sqlite3.Connection):
        def executemany(self,sql,*args,**kwargs):
            if sql.startswith('UPDATE fes_yk_define SET value=') and 'cmd_time' in sql:
                calls.append(sqlite3.Connection.execute(self,'PRAGMA synchronous').fetchone()[0])
            return super().executemany(sql,*args,**kwargs)
        def execute(self,sql,*args,**kwargs):
            if sql.startswith('UPDATE _scada_command_trace SET stage=') or (sql.startswith('UPDATE fes_yk_define SET value=') and 'cmd_time' in sql):
                calls.append(sqlite3.Connection.execute(self,'PRAGMA synchronous').fetchone()[0])
            return super().execute(sql,*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**kw:original(*a,**dict(kw,factory=Observed)))
    data=ScadaStore(tmp_path/'durable.sqlite3')
    try:
        data.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='iec104'))
        data.create_row('fes_yk_define',dict(rtu_id=1,pnt_no=1,value=0))
        row=DatabaseCommandWriter(data).submit_command(1,'yk',1,1)
        ident=claim(data,1,'yk',1,row['cmd_time'],1)
        for stage in ('select_sent','select_confirmed','execute_sent','protocol_confirmed'):advance(data,ident,stage)
        assert len(calls)>=6 and set(calls)=={2}
        with data.connection() as con:assert con.execute('PRAGMA synchronous').fetchone()[0]==1
    finally:data.close()


def test_abrupt_process_exit_after_claim_does_not_replay(store,tmp_path):
    import subprocess,sys
    from core.command_store import DatabaseCommandWriter
    from core.command_trace import claim
    row=DatabaseCommandWriter(store).submit_command(1,'yk',1,1)
    code='''import os,sys
from scdfe_c.web.service import ScadaStore
from core.command_trace import claim
data=ScadaStore(sys.argv[1]);assert claim(data,1,'yk',1,int(sys.argv[2]),1)
os._exit(0)
'''
    result=subprocess.run([sys.executable,'-c',code,str(store.db_path),str(row['cmd_time'])],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert claim(store,1,'yk',1,row['cmd_time'],1) is None
    with store.connection() as con:assert con.execute('SELECT stage FROM _scada_command_trace').fetchone()[0]=='dispatching'


def test_real_iec104_feedback_is_correlated_after_execute(store,tmp_path):
    from contextlib import ExitStack
    from server.iec104_server import IEC104Server
    from client.iec104 import IEC104Master
    from core.command_store import DatabaseCommandWriter
    from scdfe_c import ChannelWorker,ChannelSpec
    from tools.verify_iec104_soe import port
    rule(store,feedback_rtu_id=1)
    with ExitStack() as stack:
        server=IEC104Server(ip='127.0.0.1',port=port());stack.callback(server.stop)
        server.add_point('yx',1,data_type=1,value=0)
        def execute(event):
            time.sleep(.005)
            server.set_value('yx',1,event.value,transmit=True)
            return True
        server.add_point('yk',1,data_type=45,command_handler=execute);server.start()
        master=IEC104Master('127.0.0.1',server.port,init='NONE');stack.callback(master.close)
        master.configure([dict(kind='yk',pnt_no=1,type_id=45),dict(kind='yx',pnt_no=1,type_id=1)]);master.connect(timeout=3)
        worker=ChannelWorker(store,ChannelSpec(1,1,'iec104'),log_dir=tmp_path/'logs');worker._master=master
        stack.callback(worker._close_command_connection)
        DatabaseCommandWriter(store).submit_command(1,'yk',1,1)
        assert worker._sync_commands()==(1,0)
        with store.connection() as con:ident=con.execute('SELECT command_id FROM _scada_command_trace').fetchone()[0]
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            worker._sync_measurements(master.collect_once());worker._sync_commands()
            if trace(store,ident)['feedback_state']=='matched':break
            time.sleep(.01)
        row=trace(store,ident)
        assert row['feedback_state']=='matched' and row['feedback_received_time']>row['execute_time']


@pytest.mark.parametrize('component',['c','s','t'])
def test_history_conditional_query_names_and_review_api(tmp_path,component):
    import http.client,json,threading
    from importlib import import_module
    from core.command_store import DatabaseCommandWriter
    server=import_module(f'scdfe_{component}.web.server').make_server('127.0.0.1',0,db_path=tmp_path/'api.sqlite3')
    data=server.trans_store.store if component=='t' else server.store
    data.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='iec104',name='真实站名'))
    for kind in ('yk','yx'):data.create_row(f'fes_{kind}_define',dict(rtu_id=1,pnt_no=1,name='开关点',value=0,status=0))
    DatabaseCommandWriter(data).submit_command(1,'yk',1,1)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def call(path,method='GET',body=None,headers=None):
        con=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        con.request(method,path,body=json.dumps(body) if body is not None else None,headers={'Content-Type':'application/json',**(headers or {})})
        response=con.getresponse();result=(response.status,response.read(),dict(response.getheaders()));con.close();return result
    try:
        status,body,headers=call('/api/command-traces');assert status==200
        item=json.loads(body)['items'][0];tag=headers['ETag']
        assert item['point_name']=='开关点' and item['feedback_label']=='未关联回采验证'
        assert call('/api/command-traces',headers={'If-None-Match':tag})[:2]==(304,b'')
        assert call('/api/command-traces?from=20&to=10')[0]==400
        data.update_row('fes_yk_define',[1,1],dict(name='新的点名'))
        assert call('/api/command-traces',headers={'If-None-Match':tag})[0]==200
        status,body,_=call('/api/command-options');assert status==200 and json.loads(body)['items'][0]['name']=='真实站名'
        payload=dict(rtu_id=1,kind='yk',pnt_no=1,feedback_rtu_id=1,feedback_pnt_no=1,gain=1,offset=0,tolerance=0,timeout_ms=5000,enabled=True,version=0)
        assert call('/api/command-feedback','PUT',payload)[0]==(409 if component=='s' else 200)
        body=dict(stage='queued',review_time=0,conclusion='unknown',note='仅核对，不下发',actor='测试值班员')
        assert call(f"/api/command-traces/{item['command_id']}/review",'POST',body)[0]==200
        assert call(f"/api/command-traces/{item['command_id']}/review",'POST',body)[0]==400
        assert trace(data,item['command_id'])['stage']=='queued'
    finally:server.shutdown();server.server_close();thread.join(3)


def test_writer_wait_metrics_separate_lock_apply_and_commit(store):
    from unittest.mock import Mock
    from core.pipeline_metrics import LatencyStats
    from scdfe_c import _flush_batch
    store._writer_latency=LatencyStats()
    message=dict(op='update',table='fes_yc_define',key=dict(rtu_id=1,pnt_no=1),values=dict(value=3))
    assert _flush_batch(store,Mock(),[message],Mock()) is True
    timing=store._writer_latency.snapshot()
    for key in ('c_db_lock_wait_ms','c_db_apply_ms','c_db_commit_ms'):
        assert timing[key]['count']==1 and timing[key]['last_ms']>=0


def test_writer_lock_conflict_preserves_batch_and_reports_wait(store,monkeypatch):
    from unittest.mock import Mock
    from core.pipeline_metrics import LatencyStats
    import scdfe_c
    store._writer_latency=LatencyStats();store._writer_contention=dict(lock_conflicts=0,retry_backoff_ms=0)
    monkeypatch.setattr(scdfe_c,'BATCH_WRITER_LOCK_RETRIES',2)
    monkeypatch.setattr(scdfe_c,'BATCH_WRITER_LOCK_BACKOFF',.001)
    writer=store.open_connection();writer.execute('PRAGMA busy_timeout=5');queue=Mock()
    with store.connection() as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        try:
            assert scdfe_c._flush_batch(store,Mock(),[dict(op='update',table='fes_yc_define',key=dict(rtu_id=1,pnt_no=1),values=dict(value=3))],queue,writer) is None
            assert store._writer_contention['lock_conflicts']==2
            assert store._writer_latency.snapshot()['c_db_lock_wait_ms']['count']==2
            queue.task_done.assert_not_called()
        finally:blocker.rollback();writer.close()


def test_round11_schema_migration_keeps_existing_command(tmp_path):
    from core.command_trace import ensure_schema
    con=sqlite3.connect(tmp_path/'migration.sqlite3');con.row_factory=sqlite3.Row
    try:
        for table in ('fes_rtu_define','fes_yk_define','fes_yt_define','fes_yc_define','fes_yx_define'):
            con.execute(f'CREATE TABLE {table}(rtu_id INT,pnt_no INT,name TEXT,value REAL,cmd_time INT,cmd_handle_time INT)')
        con.execute('''CREATE TABLE _scada_command_trace(command_id INTEGER PRIMARY KEY AUTOINCREMENT,
            rtu_id INTEGER,kind TEXT,pnt_no INTEGER,cmd_time INTEGER,value REAL,stage TEXT,
            dispatch_time INTEGER,select_time INTEGER,select_ack_time INTEGER,execute_time INTEGER,
            finish_time INTEGER,owner_pid INTEGER,confirmation_scope TEXT,error TEXT)''')
        con.execute("INSERT INTO _scada_command_trace(command_id,rtu_id,kind,pnt_no,cmd_time,value,stage) VALUES(9,1,'yk',1,100,1,'execute_sent')")
        ensure_schema(con);ensure_schema(con);con.commit()
        row=con.execute('SELECT * FROM _scada_command_trace').fetchone()
        assert row['command_id']==9 and row['stage']=='execute_sent' and row['cmd_time']==100
        assert row['feedback_state']=='unconfigured' and row['review_time'] is None
        assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally:con.close()

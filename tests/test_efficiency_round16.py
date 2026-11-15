"""Command acceptance and concurrent display snapshot writes use separate paths."""
from contextlib import contextmanager
import sqlite3
import threading
import time
from types import SimpleNamespace
import pytest

from scdfe_t.trans import TransRuntime


@pytest.fixture
def runtime(tmp_path):
    runtime=TransRuntime(tmp_path/'test.sqlite3',channel_id=1)
    runtime.store.connection_pool_size=2
    with runtime.store.connection() as con:
        con.execute("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(1,1,'iec104')")
        con.execute("INSERT INTO fes_scdfe_t_define(channel_id,name,protocol) VALUES(1,'转发测试','iec104')")
        for no in range(1,5):
            con.execute('INSERT INTO fes_yk_define(rtu_id,pnt_no,value) VALUES(1,?,0)',(no,))
            con.execute("INSERT INTO fes_scdfe_t_point_define(channel_id,pnt_no,kind,source_rtu_id,source_pnt_no) VALUES(1,?,'yk',1,?)",(no,no))
    yield runtime
    runtime.store.close()


def point(no):return SimpleNamespace(kind='yk',pnt_no=no,source_rtu_id=1,source_pnt_no=no)


def test_accepted_commands_do_not_run_display_transactions(runtime,monkeypatch):
    def forbidden():raise AssertionError('display write on protocol response path')
    monkeypatch.setattr(runtime,'_flush_point_runtime',forbidden)
    assert runtime.command(point(1),1)
    row=runtime.store.get_row('fes_yk_define',[1,1])
    assert row['cmd_time']>0 and row['value']==1
    assert runtime._point_runtime_pending[('yk',1)]['command_receive_time']>0


def test_committed_command_survives_log_observer_failure(runtime,monkeypatch):
    monkeypatch.setattr(runtime.logger,'normal',lambda *a,**k:None)
    original=runtime.business.rows
    def observer(action,rows):
        if action=='转发指令已入源库，等待C处理':raise OSError('disk full')
        return original(action,rows)
    monkeypatch.setattr(runtime.business,'rows',observer)
    assert runtime.command(point(1),1)
    assert runtime._flush_point_runtime()
    assert runtime.store.get_row('fes_scdfe_t_point_define',[1,1,'yk'])['value']==1


def test_simultaneous_flush_cannot_overwrite_newer_snapshot(runtime):
    entered,release=threading.Event(),threading.Event()
    original=runtime.store.connection;result=[]
    @contextmanager
    def held():
        if threading.current_thread().name=='older-snapshot':
            entered.set(); assert release.wait(3)
        with original() as con:yield con
    runtime.store.connection=held
    runtime._queue_point_runtime(('yk',1),value=1,status=0)
    thread=threading.Thread(target=lambda:result.append(runtime._flush_point_runtime()),name='older-snapshot')
    thread.start()
    try:
        assert entered.wait(1)
        runtime._queue_point_runtime(('yk',1),value=0,status=1)
        assert runtime._flush_point_runtime() is False
    finally:release.set();thread.join(4)
    assert result==[True] and not thread.is_alive()
    assert runtime._flush_point_runtime()
    row=runtime.store.get_row('fes_scdfe_t_point_define',[1,1,'yk'])
    assert (row['value'],row['status'])==(0,1)


def test_burst_coalesces_display_rows_without_changing_command_tokens(runtime):
    statements=[]
    with runtime.store.connection() as con:con.set_trace_callback(statements.append)
    for no in range(1,5):assert runtime.command(point(no),1)
    assert len([s for s in statements if s=='COMMIT'])==4
    assert runtime._flush_point_runtime()
    assert len([s for s in statements if s=='COMMIT'])==5
    with runtime.store.connection() as con:
        assert con.execute('SELECT count(*) FROM _scada_command_trace').fetchone()[0]==4
        rows=con.execute('SELECT value,command_receive_time,refresh_time FROM fes_scdfe_t_point_define').fetchall()
    assert all(r[0]==1 and r[1]>0 and r[1]==r[2] for r in rows)


def test_display_lock_conflict_returns_promptly_and_retries_latest(runtime):
    held=sqlite3.connect(runtime.store.db_path,isolation_level=None);held.execute('BEGIN IMMEDIATE')
    runtime._queue_point_runtime(('yk',1),value=1,status=0)
    done=threading.Event();results=[]
    def flush():results.append(runtime._flush_point_runtime());done.set()
    thread=threading.Thread(target=flush);thread.start()
    try:
        assert done.wait(.5),'Display flush waited for a busy business database'
        assert results==[False]
        runtime._queue_point_runtime(('yk',1),value=0,status=1)
        stats=runtime._point_runtime_diagnostic()
        assert stats['pending_points']==1 and stats['lock_conflicts']==1 and stats['pending_age_ms']>=0
    finally:held.rollback();held.close();thread.join(4)
    assert runtime._flush_point_runtime()
    row=runtime.store.get_row('fes_scdfe_t_point_define',[1,1,'yk'])
    assert (row['value'],row['status'])==(0,1)
    with runtime.store.connection() as con:assert con.execute('PRAGMA busy_timeout').fetchone()[0]==30000


def test_failed_display_batch_rolls_back_and_preserves_definition_version(runtime):
    with runtime.store.connection() as con:
        versions=list(con.execute('SELECT _version FROM fes_scdfe_t_point_define ORDER BY pnt_no'))
        con.execute("CREATE TRIGGER fail_display BEFORE UPDATE ON fes_scdfe_t_point_define WHEN NEW.pnt_no=2 BEGIN SELECT RAISE(ABORT,'display failure'); END")
    for no in (1,2):runtime._queue_point_runtime(('yk',no),value=1,status=1)
    assert not runtime._flush_point_runtime()
    with runtime.store.connection() as con:
        assert all(row[0] in (None,0) for row in con.execute('SELECT value FROM fes_scdfe_t_point_define'))
        assert con.execute('PRAGMA busy_timeout').fetchone()[0]==30000
        con.execute('DROP TRIGGER fail_display')
    assert runtime._flush_point_runtime()
    with runtime.store.connection() as con:
        assert list(con.execute('SELECT _version FROM fes_scdfe_t_point_define ORDER BY pnt_no'))==versions
    assert runtime._point_runtime_diagnostic()['pending_points']==0
    assert runtime._point_runtime_diagnostic()['failures']==1


def test_managed_loop_flushes_commands_between_long_measurement_scans(runtime,monkeypatch):
    # Real scheduler and SQLite; no network controls or live service definitions.
    spec,points,signature=runtime.load()
    from dataclasses import replace
    spec=replace(spec,scan_interval=5)
    monkeypatch.setattr(runtime,'load',lambda:(spec,points,signature))
    def rebuild(*a,**k):runtime.server=SimpleNamespace(stop=lambda:None);runtime.signature=signature
    monkeypatch.setattr(runtime,'rebuild',rebuild)
    worker=threading.Thread(target=runtime.run);worker.start()
    try:
        deadline=time.monotonic()+3
        while runtime._cycles==0 and time.monotonic()<deadline:time.sleep(.01)
        assert runtime._cycles==1
        assert runtime.command(point(1),1)
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            if runtime.store.get_row('fes_scdfe_t_point_define',[1,1,'yk'])['command_receive_time']:break
            time.sleep(.01)
        else:pytest.fail('Command display waited for the five second source scan')
        assert runtime._cycles==1
    finally:runtime.stop_event.set();worker.join(5)
    assert not worker.is_alive()


def test_pending_snapshot_diagnostics_are_queryable_without_latency(tmp_path):
    import http.client,json
    from scdfe_t.web.server import make_server
    from core.runtime_status import RuntimeStatusStore
    server=make_server('127.0.0.1',0,db_path=tmp_path/'api.sqlite3')
    stats=dict(pending_points=2,pending_age_ms=90,batches=5,rows=8,failures=1,lock_conflicts=1,
               log_unconfirmed=0,last_error='database is locked')
    RuntimeStatusStore(server.trans_store.store,'scdfe_t').upsert(1,dict(running=1,diagnostic_json={'point_runtime':stats}))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=http.client.HTTPConnection('127.0.0.1',server.server_port);client.request('GET','/api/diagnostics')
        response=client.getresponse();data=json.loads(response.read());client.close()
        assert response.status==200 and data['items'][0]['point_runtime']==stats
    finally:server.shutdown();server.server_close();thread.join(3)

"""Bulk command atomicity and evidence for slow command admission."""
import sqlite3
import threading
import time
import pytest
from core.command_store import DatabaseCommandWriter
from scdfe_c.web.service import ScadaStore


@pytest.fixture
def store(tmp_path):
    data=ScadaStore(tmp_path/'commands.sqlite3',connection_pool_size=2)
    with data.connection() as con:
        con.execute('BEGIN')
        for rid in (1,2):
            con.execute("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,'modbus-tcp')",(rid,rid))
            for kind in ('yk','yt'):
                con.executemany(f'INSERT INTO fes_{kind}_define(rtu_id,pnt_no,value) VALUES(?,?,0)',[(rid,n) for n in range(1,451)])
        con.commit()
    yield data
    data.close()


def test_bulk_multi_rtu_preserves_order_rows_and_reduces_queries(store):
    commands=[(r,k,n,n/10 if k=='yt' else n%2) for n in range(1,451) for r,k in ((2,'yt'),(1,'yk'))]
    statements=[]
    with store.connection() as con:con.set_trace_callback(statements.append)
    rows=DatabaseCommandWriter(store).submit_commands(commands)
    assert [(r['rtu_id'],r['pnt_no'],r['value']) for r in rows]==[(r,n,v) for r,k,n,v in commands]
    assert all(r['cmd_time']>int(r['cmd_handle_time'] or 0) and r['status']==0 for r in rows)
    reads=[s for s in statements if s.upper().startswith('SELECT')]
    assert len(reads)<=20, len(reads)
    assert 'name' in rows[0] and '_version' in rows[0]


def test_last_invalid_item_does_not_publish_any_command(store):
    writer=DatabaseCommandWriter(store)
    with store.connection() as con:con.execute('UPDATE fes_yt_define SET eng_max=1 WHERE rtu_id=2 AND pnt_no=300')
    with pytest.raises(ValueError,match='上下限'):
        writer.submit_commands([(1,'yk',n,1) for n in range(1,301)]+[(2,'yt',300,2)])
    with store.connection() as con:
        assert con.execute('SELECT count(*) FROM _scada_command_trace').fetchone()[0]==0
    sample=writer.metrics.snapshot()['recent'][-1]
    assert not sample['committed'] and sample['failed_stage']=='validation_ms'


def test_writer_lock_wait_and_failed_attempt_are_identified(store):
    writer=DatabaseCommandWriter(store)
    lock=sqlite3.connect(store.db_path,isolation_level=None);lock.execute('BEGIN IMMEDIATE')
    result=[]
    def submit():result.extend(writer.submit_commands([(1,'yk',1,1)],timeout=2))
    thread=threading.Thread(target=submit);thread.start();time.sleep(.15);lock.rollback();thread.join(5)
    assert result and not thread.is_alive()
    sample=writer.metrics.snapshot()['recent'][-1]
    assert sample['committed'] and sample['lock_wait_ms']>=100
    assert sample['commit_ms']<sample['total_ms'] and sample['points'][0]['cmd_time']==result[0]['cmd_time']
    lock.execute('BEGIN IMMEDIATE')
    with pytest.raises(sqlite3.OperationalError):writer.submit_commands([(1,'yk',2,1)],timeout=.03)
    lock.rollback();lock.close()
    sample=writer.metrics.snapshot()['recent'][-1]
    assert not sample['committed'] and sample['failed_stage']=='lock_wait_ms'
    assert store.get_row('fes_yk_define',[1,2])['cmd_time'] in (None,0)


def test_metrics_failure_cannot_change_committed_result(store,monkeypatch):
    writer=DatabaseCommandWriter(store)
    monkeypatch.setattr(writer.metrics,'record',lambda *a,**k:(_ for _ in ()).throw(OSError('diagnostic unavailable')))
    row=writer.submit_command(1,'yk',1,1)
    assert row['cmd_time'] and store.get_row('fes_yk_define',[1,1])['value']==1


def test_resolution_rejects_unhashable_stage_as_validation_error(store):
    from core.command_trace import resolve_uncertain
    with pytest.raises(ValueError):
        resolve_uncertain(store,1,dict(stage=[],cmd_time=1,actor='人员',note='核实',confirm_no_replay=True))


def test_command_diagnostics_bounded_and_persisted_through_http(tmp_path):
    import http.client,json
    from scdfe_t.web.server import make_server
    from core.command_metrics import CommandWriteStats
    from core.runtime_status import RuntimeStatusStore
    stats=CommandWriteStats()
    for n in range(30):stats.record(dict(committed=True,total_ms=150,lock_wait_ms=120),[(1,'yk',1,1)],[dict(cmd_time=n)],None)
    snapshot=stats.snapshot()
    assert snapshot['count']==30 and len(snapshot['recent'])==16
    server=make_server('127.0.0.1',0,db_path=tmp_path/'api.sqlite3')
    RuntimeStatusStore(server.trans_store.store,'scdfe_t').upsert(7,dict(channel_name='指令诊断',running=1,diagnostic_json={'command_write':snapshot}))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        c=http.client.HTTPConnection('127.0.0.1',server.server_port);c.request('GET','/api/diagnostics');res=c.getresponse();data=json.loads(res.read());c.close()
        assert res.status==200
        row=data['items'][0];assert row['command_write']['count']==30
        assert row['latency']['command_lock_wait_ms']['last_ms']==120
    finally:server.shutdown();server.server_close();thread.join(3)

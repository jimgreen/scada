"""Scoped polling, compact history and explicitly closed uncertain commands."""
import os
import time
from types import SimpleNamespace
import pytest
from tests.test_efficiency_round12 import store,rule,command,trace


def test_feedback_observes_only_pending_commands_and_reacts_without_one_second_gap(store):
    from core.command_feedback import FeedbackTracker,evaluate_feedback
    tracker=FeedbackTracker(store,2);rule(store);tracker.refresh()
    assert tracker.keys==set()
    ident=command(store);row=trace(store,ident);tracker.refresh()
    assert tracker.keys=={('yx',1)}
    sample=tracker.sample('yx',1,1,0,SimpleNamespace(received_at=(row['execute_time']+1)/1000))
    tracker.commit([sample]);evaluate_feedback(store,1,now=row['execute_time']+2)
    tracker.refresh();assert tracker.keys==set()
    assert trace(store,ident)['feedback_state']=='matched'


def test_feedback_idle_refresh_skips_detail_query(store):
    from core.command_feedback import FeedbackTracker
    tracker=FeedbackTracker(store,2);tracker.refresh();statements=[]
    with store.connection() as con:con.set_trace_callback(statements.append)
    tracker.refresh();tracker.refresh()
    with store.connection() as con:con.set_trace_callback(None)
    assert not any('FROM _scada_command_trace' in sql for sql in statements)


def test_manual_close_rejects_live_owner_and_preserves_protocol_evidence(store,monkeypatch):
    from core.command_store import DatabaseCommandWriter
    from core.command_trace import claim,advance,resolve_uncertain
    from core import process_registry
    writer=DatabaseCommandWriter(store);row=writer.submit_command(1,'yk',1,1)
    ident=claim(store,1,'yk',1,row['cmd_time'],1);advance(store,ident,'execute_sent')
    payload=dict(stage='execute_sent',cmd_time=row['cmd_time'],actor='值班员',note='核对现场并停止原采集进程',confirm_no_replay=True)
    with pytest.raises(ValueError,match='仍在运行'):resolve_uncertain(store,ident,payload)
    monkeypatch.setattr(process_registry,'process_is_alive',lambda pid:False)
    result=resolve_uncertain(store,ident,payload)
    assert result['resolution']=='closed_without_replay'
    evidence=trace(store,ident)
    assert evidence['stage']=='execute_sent' and evidence['finish_time'] is None
    assert evidence['resolution_actor']=='值班员' and evidence['resolution_time']>0
    assert claim(store,1,'yk',1,row['cmd_time'],1) is None
    with pytest.raises(ValueError):resolve_uncertain(store,ident,payload)
    fresh=writer.submit_command(1,'yk',1,0)
    assert fresh['cmd_time']>row['cmd_time']
    with pytest.raises(ValueError):resolve_uncertain(store,ident,payload)
    assert store.get_row('fes_yk_define',[1,1])['cmd_time']==fresh['cmd_time']


def test_manual_close_requires_explicit_confirmation_and_uncertain_phase(store):
    from core.command_trace import resolve_uncertain
    ident=command(store)
    body=dict(stage='protocol_confirmed',cmd_time=trace(store,ident)['cmd_time'],actor='人员',note='理由',confirm_no_replay=True)
    with pytest.raises(ValueError):resolve_uncertain(store,ident,body)
    body['confirm_no_replay']=False
    with pytest.raises(ValueError):resolve_uncertain(store,ident,body)


def test_batch_deadline_includes_time_already_spent_in_queue():
    from scdfe_c import _message_pending_since
    now=time.monotonic();queued=time.perf_counter_ns()-300_000_000
    assert now-_message_pending_since({'_queued_at_ns':queued},now)>=.29
    assert _message_pending_since({},now)==now
    assert _message_pending_since({'_queued_at_ns':'bad'},now)==now
    assert _message_pending_since({'_queued_at_ns':time.perf_counter_ns()+999_000_000},now)==now


def test_batch_prefetch_preserves_barriers_and_queue_accounting():
    import queue
    from scdfe_c import _prefetch_batch,BATCH_WRITER_DRAIN,BATCH_WRITER_STOP
    for barrier in [(BATCH_WRITER_DRAIN,42),BATCH_WRITER_STOP]:
        q=queue.Queue();first={'op':'update','values':{'value':1}};second={'op':'update','values':{'value':2}}
        for item in (first,barrier,second):q.put(item)
        pending=[];updates,deferred=_prefetch_batch(q,pending,0,500)
        assert pending==[first] and deferred==barrier and q.get_nowait()==second
        assert q.unfinished_tasks==3


def test_slow_batch_contains_correlated_times_and_bounded_records(store):
    from unittest.mock import Mock
    from core.pipeline_metrics import SlowBatchStats
    from scdfe_c import _flush_batch
    store._writer_slow=SlowBatchStats(limit=2)
    message=dict(op='update',table='fes_yc_define',key=dict(rtu_id=1,pnt_no=1),values=dict(value=3),
                 source_rtu=1,_queued_at_ns=time.perf_counter_ns()-200_000_000)
    for _ in range(3):assert _flush_batch(store,Mock(),[message],Mock()) is True
    samples=store._writer_slow.snapshot();assert samples['count']==3 and len(samples['items'])==2
    last=samples['items'][-1]
    assert last['queue_wait_ms']>=190 and last['rtus']==[1]
    assert last['queue_commit_ms']>=last['transaction_ms']
    assert abs(last['queue_commit_ms']-last['queue_wait_ms']-last['transaction_ms'])<.005


def test_prefetched_drain_on_fatal_flush_is_accounted_without_ack(store,tmp_path,monkeypatch):
    import queue
    import scdfe_c
    q=queue.Queue();ack=SimpleNamespace(value=0)
    q.put(dict(op='update',table='fes_yc_define',key=dict(rtu_id=1,pnt_no=1),values=dict(value=3),
               _queued_at_ns=time.perf_counter_ns()-1_000_000_000))
    q.put((scdfe_c.BATCH_WRITER_DRAIN,77))
    def fail(store,logger,messages,queue,connection):
        for _ in messages:queue.task_done()
        return False
    monkeypatch.setattr(scdfe_c,'_flush_batch',fail)
    with pytest.raises(RuntimeError,match='批量写数据库失败'):
        scdfe_c._batch_writer_entry(str(store.db_path),str(store.catalog.directory),str(tmp_path/'logs'),q,.1,500,drain_ack=ack)
    assert ack.value==0 and q.unfinished_tasks==0


@pytest.mark.parametrize('component',['c','s','t'])
def test_compact_history_detail_and_channel_rebind_invalidation(tmp_path,component):
    import http.client,json,threading
    from importlib import import_module
    from core.command_store import DatabaseCommandWriter
    server=import_module(f'scdfe_{component}.web.server').make_server('127.0.0.1',0,db_path=tmp_path/'api.sqlite3')
    data=server.trans_store.store if component=='t' else server.store
    data.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='iec104',name='测试站'))
    data.create_row('fes_yk_define',dict(rtu_id=1,pnt_no=1,name='开关',value=0,status=0))
    DatabaseCommandWriter(data).submit_command(1,'yk',1,1)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def get(path,headers=None,method='GET',body=None):
        c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);c.request(method,path,body=json.dumps(body) if body is not None else None,headers={'Content-Type':'application/json',**(headers or {})})
        response=c.getresponse();r=(response.status,response.read(),dict(response.getheaders()));c.close();return r
    try:
        status,body,headers=get('/api/command-traces?view=summary');assert status==200
        item=json.loads(body)['items'][0];assert 'feedback_expected' not in item and 'review_note' not in item
        status,detail,_=get(f"/api/command-traces/{item['command_id']}");assert status==200
        assert 'feedback_expected' in json.loads(detail)['item']
        assert get('/api/command-traces?view=summary',{'If-None-Match':headers['ETag']})[0]==304
        data.update_row('fes_rtu_define',[1],dict(channel_id=2))
        status,body,_=get('/api/command-traces?view=summary',{'If-None-Match':headers['ETag']})
        assert status==200 and json.loads(body)['items'][0]['channel_id']==2
        assert get('/api/command-traces?view=wrong')[0]==400
        assert get('/api/command-traces/999999')[0]==404
        from core.command_trace import claim,advance
        from unittest.mock import patch
        ident=claim(data,1,'yk',1,item['cmd_time'],1);advance(data,ident,'execute_sent')
        path=f'/api/command-traces/{ident}/resolve'
        payload=dict(stage='execute_sent',cmd_time=item['cmd_time'],actor='值班员',note='已停止进程并核实现场',confirm_no_replay=True)
        assert get(path,method='POST',body=payload)[0]==(409 if component=='s' else 400)
        if component!='s':
            with patch('core.process_registry.process_is_alive',return_value=False):
                assert get(path,method='POST',body=payload)[0]==200
                assert get(path,method='POST',body=payload)[0]==400
            record=json.loads(get(f'/api/command-traces/{ident}')[1])['item']
            assert record['resolution_time'] and record['stage']=='execute_sent'
    finally:server.shutdown();server.server_close();thread.join(3)

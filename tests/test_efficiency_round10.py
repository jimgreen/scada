"""T change scans, bounded timings and non-invasive startup diagnostics."""
from types import SimpleNamespace
from unittest.mock import patch
import socket
import http.client
import json
import threading

import pytest

from scdfe_c.web.service import ScadaStore


@pytest.fixture
def store(tmp_path):
    store = ScadaStore(tmp_path/'data.sqlite3')
    for rid in (1, 2):
        store.create_row('fes_rtu_define', dict(rtu_id=rid, channel_id=rid, channel_type='iec104'))
        for no in range(1, 4):
            store.create_row('fes_yc_define', dict(rtu_id=rid,pnt_no=no,value=no,status=0))
    yield store
    store.close()


def point(rid, no, dest):
    return SimpleNamespace(kind='yc',source_rtu_id=rid,source_pnt_no=no,pnt_no=dest)


def test_delta_maps_only_requested_points_and_independent_readers(store):
    from core.monitor_changes import MappedMonitorReader
    points=[point(1,1,11), point(1,1,12), point(2,2,22)]
    a,b=MappedMonitorReader(store,points),MappedMonitorReader(store,points)
    current,touched=a.poll()
    assert touched=={('yc',11),('yc',12),('yc',22)}
    assert current[('yc',11)]==current[('yc',12)]
    b.poll()
    before=a.rows_read
    assert a.poll()[1]==set() and a.rows_read==before
    store.update_row('fes_yc_define',[1,3],{'value':99})
    assert a.poll()[1]==set() and a.rows_read==before
    store.update_row('fes_yc_define',[1,1],{'value':15,'status':128})
    for reader in (a,b):
        values,touched=reader.poll()
        assert touched=={('yc',11),('yc',12)}
        assert values[('yc',12)][:2]==(15,128)
    assert a.rows_read==before+1


def test_delta_timestamp_rollback_null_delete_and_reinsert(store):
    from core.monitor_changes import MappedMonitorReader
    reader=MappedMonitorReader(store,[point(1,1,1)])
    reader.poll()
    with store.connection() as con:
        con.execute('BEGIN')
        con.execute('UPDATE fes_yc_define SET value=77 WHERE rtu_id=1 AND pnt_no=1')
        con.rollback()
    assert not reader.poll()[1]
    store.update_row('fes_yc_define',[1,1],{'refresh_time':12345})
    assert reader.poll()[0][('yc',1)][2]==12345
    with store.connection() as con:
        con.execute('UPDATE fes_yc_define SET value=NULL WHERE rtu_id=1 AND pnt_no=1')
    assert reader.poll()==({}, {('yc',1)})
    store.delete_row('fes_yc_define',[1,1])
    assert reader.poll()==({}, {('yc',1)})
    store.create_row('fes_yc_define',dict(rtu_id=1,pnt_no=1,value=8))
    assert reader.poll()[0][('yc',1)][0]==8
    fresh=MappedMonitorReader(store,[point(1,1,2)])
    assert fresh.poll()[0][('yc',2)][0]==8


def test_t_retries_partial_publish_without_new_database_change(store,tmp_path):
    from scdfe_t.trans import TransRuntime
    from core.monitor_changes import MappedMonitorReader
    runtime=TransRuntime(store.db_path,channel_id=1,log_dir=tmp_path/'logs')
    try:
        runtime.points={('yc',1):point(1,1,1)}
        runtime._source_reader=MappedMonitorReader(runtime.store,runtime.points.values())
        runtime.values={('yc',1):0};runtime.qualities={('yc',1):0}
        class Server:
            active_connection_count=1
            calls=0
            def update_monitoring(self,values,**kwargs):
                self.calls+=1
                return {('yc',1):OSError('busy')} if self.calls==1 else {}
        runtime.server=Server()
        runtime.refresh();assert runtime.values[('yc',1)]==0
        runtime.refresh();assert runtime.values[('yc',1)]==1
        assert runtime.server.calls==2
        runtime.refresh();assert runtime.server.calls==2
    finally:
        runtime.store.close()


def test_timing_window_bounds_and_percentiles():
    from core.pipeline_metrics import LatencyStats
    stats=LatencyStats(window=4)
    for n in range(1,7):stats.observe('read_ms',n)
    stats.observe('read_ms',-1);stats.observe('read_ms',float('nan'))
    result=stats.snapshot()['read_ms']
    assert result['count']==6 and result['window_count']==4
    assert result['p50_ms']==4 and result['p95_ms']==6 and result['p99_ms']==6
    assert result['invalid_count']==2


def test_writer_persists_commit_timing_and_stopped_state(store,tmp_path):
    import time
    from queue import Queue
    import scdfe_c
    from core.runtime_status import RuntimeStatusStore
    queue=Queue()
    queue.put(dict(op='update',table='fes_yc_define',key=dict(rtu_id=1,pnt_no=1),
                   values=dict(value=77),_queued_at_ns=time.perf_counter_ns()))
    queue.put(scdfe_c.BATCH_WRITER_STOP)
    scdfe_c._batch_writer_entry(str(store.db_path),str(store.catalog.directory),
                               str(tmp_path/'logs'),queue)
    row=RuntimeStatusStore(store,'scdfe_c_writer').list()[0]
    assert row['running']==0 and row['state']=='stopped'
    timing=row['diagnostic']['latency']
    assert timing['c_queue_commit_ms']['count']==1
    assert timing['c_queue_commit_ms']['last_ms']>=timing['c_write_ms']['last_ms']>=0
    assert store.get_row('fes_yc_define',[1,1])['value']==77
    assert queue.unfinished_tasks==0


def test_preflight_detects_wildcard_collisions_without_equating_two_addresses():
    from core.startup_checks import listener_conflicts
    rows=[dict(channel_id=1,bind_host='0.0.0.0',bind_port=2404),
          dict(channel_id=2,bind_host='127.0.0.1',bind_port=2404),
          dict(channel_id=3,bind_host='127.0.0.2',bind_port=2405),
          dict(channel_id=4,bind_host='127.0.0.1',bind_port=2405)]
    found=listener_conflicts(rows)
    assert len(found)==1 and found[0]['channels']==[1,2]


def test_preflight_does_not_create_a_tcp_connection():
    from core.startup_checks import check_listener
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));sock.listen()
        host,port=sock.getsockname()
        with patch('socket.create_connection',side_effect=AssertionError('active probe forbidden')):
            issue=check_listener(host,port)
        assert issue['code']=='listener_unavailable' and str(port) in issue['message']
    assert check_listener(host,port) is None


def test_t_preflight_rejects_occupied_listener_before_spawn(store):
    from scdfe_t.web.service import TransStore
    from scdfe_c.web.service import ApiError
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));sock.listen()
        store.create_row('fes_scdfe_t_define',dict(channel_id=1,name='test',protocol='iec104',
                         bind_host='127.0.0.1',bind_port=sock.getsockname()[1]))
        trans=TransStore(store.db_path)
        try:
            with patch('scdfe_t.web.service.subprocess.Popen') as spawn:
                with pytest.raises(ApiError,match='无法监听'):trans.start(1)
                spawn.assert_not_called()
            assert store.get_row('fes_scdfe_t_define',[1])['process_status']==0
        finally:trans.store.close()


@pytest.mark.parametrize('component',['c','s','t'])
def test_diagnostics_http_is_read_only_and_serves_timing_snapshot(tmp_path,component):
    from importlib import import_module
    from core.runtime_status import RuntimeStatusStore
    server=import_module(f'scdfe_{component}.web.server').make_server('127.0.0.1',0,db_path=tmp_path/'api.sqlite3')
    store=server.trans_store.store if component=='t' else server.store
    RuntimeStatusStore(store,'scdfe_'+component).upsert(7,dict(channel_name='测试站',running=1,
        diagnostic_json={'latency':{'read_ms':{'count':1,'window_count':1,'last_ms':2,'p50_ms':2,'p95_ms':2,'p99_ms':2}}}))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def call(path,method='GET'):
        con=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        con.request(method,path);response=con.getresponse();data=response.read();con.close()
        return response.status,data
    try:
        for path in ['/diagnostics','/static/diagnostics.js','/static/diagnostics.css']:
            assert call(path)[0]==200
        status,data=call('/api/diagnostics?checks=1')
        assert status==200
        payload=json.loads(data)
        assert payload['component']==component and 'checks' in payload
        assert payload['items'][0]['latency']['read_ms']['p95_ms']==2
        assert call('/api/diagnostics?checks=2')[0]==400
        assert call('/api/diagnostics','POST')[0]==405
    finally:
        server.shutdown();server.server_close();thread.join(3)

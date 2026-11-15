"""Dense change indexing and command/SOE isolation regressions."""
from pathlib import Path
import pytest
from scdfe_c.web.service import ScadaStore


@pytest.fixture
def store(tmp_path):
    result=ScadaStore(tmp_path/'data.sqlite3')
    result.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='iec104'))
    for kind in ('yc','yx','yk','yt'):
        result.create_row(f'fes_{kind}_define',dict(rtu_id=1,pnt_no=1,value=0,status=0))
    yield result
    result.close()


def test_value_and_auto_timestamp_create_only_one_change_marker(store):
    from core.monitor_changes import ensure_monitor_changes,read_monitor_changes
    with store.connection() as con:
        ensure_monitor_changes(con,track_refresh=True)
        def seq():
            row=con.execute("SELECT seq FROM sqlite_sequence WHERE name='_scada_monitor_change'").fetchone()
            return row[0] if row else 0
        for sql in ["SET value=1", "SET status=128", "SET value=2,refresh_time=123", "SET refresh_time=124"]:
            before=seq()
            con.execute('UPDATE fes_yc_define '+sql+' WHERE rtu_id=1 AND pnt_no=1')
            assert con.execute('SELECT refresh_time FROM fes_yc_define').fetchone()[0] is not None
            assert seq()-before==1,sql
            assert len(read_monitor_changes(con,1,before)[1])==1
        # Another consumer's default initialization must not downgrade T tracking.
        ensure_monitor_changes(con)
        before=seq();con.execute('UPDATE fes_yc_define SET refresh_time=125')
        assert seq()-before==1


def test_delta_dense_and_sparse_preserve_unmapped_and_deletion_boundaries(store):
    from core.monitor_changes import MappedMonitorReader
    from types import SimpleNamespace
    with store.connection() as con:
        con.executemany('INSERT INTO fes_yc_define(rtu_id,pnt_no,value,status) VALUES(1,?,?,0)',[(n,n) for n in range(2,1002)])
    reader=MappedMonitorReader(store,[SimpleNamespace(kind='yc',source_rtu_id=1,source_pnt_no=n,pnt_no=n) for n in range(1,1001)])
    reader.poll()
    with store.connection() as con:
        con.execute('UPDATE fes_yc_define SET value=value+10')
        con.execute('DELETE FROM fes_yc_define WHERE pnt_no=500')
    values,touched=reader.poll()
    assert len(touched)==1000 and len(values)==999 and ('yc',1001) not in touched
    assert values[('yc',999)][0]==1009
    store.update_row('fes_yc_define',[1,1],dict(status=16))
    assert reader.poll()[1]=={('yc',1)}


def test_simulator_preserves_commands_received_in_the_same_millisecond(tmp_path):
    from scdfe_s.database import SimulatorStore
    data=SimulatorStore(tmp_path/'simulator.sqlite3')
    try:
        data.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='iec104'))
        data.create_row('fes_yk_define',dict(rtu_id=1,pnt_no=1,value=0))
        for value in (1,1,0):
            data.update_row('fes_yk_define',[1,1],dict(value=value,cmd_handle_time=123456))
        with data.connection() as con:
            rows=con.execute('SELECT value,cmd_time,stage FROM _scada_command_trace ORDER BY command_id').fetchall()
            assert [tuple(row) for row in rows]==[(1,123456,'received'),(1,123456,'received'),(0,123456,'received')]
    finally:data.close()


def test_command_trace_commits_with_submission_and_never_reclaims_uncertain_io(store):
    from core.command_store import DatabaseCommandWriter
    from core.command_trace import claim,advance
    command=DatabaseCommandWriter(store).submit_command(1,'yk',1,1)
    stamp=command['cmd_time']
    with store.connection() as con:
        assert con.execute('SELECT stage FROM _scada_command_trace').fetchone()[0]=='queued'
    ident=claim(store,1,'yk',1,stamp,1)
    assert ident and claim(store,1,'yk',1,stamp,1) is None
    advance(store,ident,'select_sent');advance(store,ident,'select_confirmed')
    advance(store,ident,'execute_sent')
    reopened=ScadaStore(store.db_path)
    try:
        assert claim(reopened,1,'yk',1,stamp,1) is None
        with reopened.connection() as con:
            row=dict(con.execute('SELECT * FROM _scada_command_trace').fetchone())
        assert row['stage']=='execute_sent' and row['finish_time'] is None
        assert row['select_time']<=row['select_ack_time']<=row['execute_time']
    finally:reopened.close()


def test_command_trace_rolls_back_and_false_driver_is_failure(store,tmp_path):
    from core.command_store import DatabaseCommandWriter
    from scdfe_c import ChannelWorker,ChannelSpec
    with store.connection() as con:
        con.execute('BEGIN');con.execute('UPDATE fes_yk_define SET cmd_time=5');con.rollback()
        assert con.execute('SELECT COUNT(*) FROM _scada_command_trace').fetchone()[0]==0
    DatabaseCommandWriter(store).submit_command(1,'yk',1,1)
    worker=ChannelWorker(store,ChannelSpec(1,1,'iec104'),log_dir=tmp_path/'logs')
    worker._master=type('Master',(),{'send_yk_point':lambda *_:False})()
    assert worker._sync_commands()==(0,0)
    with store.connection() as con:
        row=dict(con.execute('SELECT * FROM _scada_command_trace').fetchone())
    assert row['stage']=='failed' and '拒绝' in row['error']
    assert store.get_row('fes_yk_define',[1,1])['status']==3
    worker._close_command_connection()


def test_iec104_select_execute_trace_and_status_recovery_without_resend(store,tmp_path):
    from contextlib import ExitStack
    from tools.verify_iec104_soe import port
    from server.iec104_server import IEC104Server
    from client.iec104 import IEC104Master
    from scdfe_c import ChannelWorker,ChannelSpec
    from core.command_store import DatabaseCommandWriter
    from core.command_trace import claim,advance
    with ExitStack() as stack:
        server=IEC104Server(ip='127.0.0.1',port=port());stack.callback(server.stop)
        calls=[]
        server.add_point('yk',1,data_type=45,command_mode='SELECT_AND_EXECUTE',command_handler=lambda event:calls.append(event.selected) or True)
        server.start()
        master=IEC104Master('127.0.0.1',server.port,init='NONE');stack.callback(master.close)
        master.configure([dict(kind='yk',pnt_no=1,type_id=45,command_mode='SELECT_AND_EXECUTE')]);master.connect(timeout=3)
        command=DatabaseCommandWriter(store).submit_command(1,'yk',1,1)
        worker=ChannelWorker(store,ChannelSpec(1,1,'iec104'),log_dir=tmp_path/'logs');worker._master=master
        stack.callback(worker._close_command_connection)
        assert worker._sync_commands()==(1,0)
        with store.connection() as con:row=dict(con.execute('SELECT * FROM _scada_command_trace').fetchone())
        assert row['stage']=='protocol_confirmed' and row['confirmation_scope']=='iec104_activation_confirmation'
        assert row['dispatch_time']<=row['select_time']<=row['select_ack_time']<=row['execute_time']<=row['finish_time']
        assert calls==[False]  # The server validates selection without executing its handler.
        # A crash after acknowledged I/O but before the result slot was committed.
        stamp=command['cmd_time']
        with store.connection() as con:con.execute('UPDATE fes_yk_define SET cmd_handle_time=0,status=0')
        assert claim(store,1,'yk',1,stamp,1) is None
        assert store.get_row('fes_yk_define',[1,1])['status']==1
        assert calls==[False]


def test_slow_soe_client_does_not_block_fast_client_and_reconnect_replays_gap(tmp_path):
    import socket,time
    from contextlib import ExitStack
    from scdfe_s.database import SimulatorStore
    from tools.verify_iec104_soe import fill_one,port
    from tools.verify_iec104_wire_contracts import RawPeer
    from core.iec104_soe import SoeForwarder,decode_events
    from server.iec104_server import IEC104Server
    from client.iec104 import IEC104Master
    with ExitStack() as stack:
        data=SimulatorStore(tmp_path/'s.sqlite3');stack.callback(data.close);fill_one(data,2404)
        with data.connection() as con:
            con.execute('BEGIN')
            for n in range(384):con.execute('UPDATE fes_yx_define SET value=? WHERE rtu_id=1 AND pnt_no=1',((n+1)%2,))
            con.commit()
        forward=SoeForwarder(data,'isolation','S',lambda row:[row],rtu_id=1);stack.callback(forward.close)
        server=IEC104Server(ip='127.0.0.1',port=port(),t1=10,tick_rate_ms=50)
        stack.callback(server.stop);server.add_point('yx',1,data_type=30);server.start()
        captured=[]
        def capture(direction,frame,detail=''):
            if direction=='RX' and len(frame)>12 and frame[2]&1==0 and frame[6]==30:
                captured.extend(decode_events(frame[6:]))
        fast=IEC104Master('127.0.0.1',server.port,init='NONE',w=1,t2=1)
        stack.callback(fast.close);fast.add_point('yx',1,data_type=30);fast.set_packet_handler(capture);fast.connect(timeout=3)
        slow_sock=socket.create_connection(('127.0.0.1',server.port),timeout=3);stack.callback(slow_sock.close)
        slow=RawPeer(slow_sock);slow.u(7);slow.until(lambda b:b[2]==11)
        def pump_until(predicate):
            end=time.monotonic()+4
            while time.monotonic()<end:
                forward.tick(server)
                if predicate():return
                time.sleep(.005)
            raise AssertionError(forward.peer_progress())
        pump_until(lambda:len(captured)==384)
        assert [e['raw_state'] for e in captured]==[(n+1)%2 for n in range(384)]
        assert forward.last_event_id==0
        assert max(x['acknowledged'] for x in forward.peer_progress()['connections'])>=256
        slow_sock.close()
        pump_until(lambda:forward.peer_progress()['disconnected_floor']==0)
        replacement_sock=socket.create_connection(('127.0.0.1',server.port),timeout=3);stack.callback(replacement_sock.close)
        replacement=RawPeer(replacement_sock);replacement.u(7);replacement.until(lambda b:b[2]==11)
        recovered=[];end=time.monotonic()+5
        while len(recovered)<384 and time.monotonic()<end:
            forward.tick(server)
            try:frame=replacement.receive(.05)
            except socket.timeout:continue
            if len(frame)>12 and frame[2]&1==0 and frame[6]==30:recovered.extend(decode_events(frame[6:]))
        assert len(recovered)==384
        assert [(x['raw_state'],x['quality'],x['cp56_hex']) for x in recovered]==[(x['raw_state'],x['quality'],x['cp56_hex']) for x in captured]
        pump_until(lambda:forward.last_event_id==384)
        assert len(captured)==384,'fast live client must not replay the slow client gap'


@pytest.mark.parametrize('component',['c','s','t'])
def test_command_trace_http_pagination_and_read_only(tmp_path,component):
    import threading,http.client,json
    from importlib import import_module
    from core.command_store import DatabaseCommandWriter
    server=import_module(f'scdfe_{component}.web.server').make_server('127.0.0.1',0,db_path=tmp_path/'web.sqlite3')
    data=server.trans_store.store if component=='t' else server.store
    data.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='iec104',name='追踪测试站'))
    data.create_row('fes_yk_define',dict(rtu_id=1,pnt_no=1,value=0))
    DatabaseCommandWriter(data).submit_command(1,'yk',1,1)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def get(path,method='GET'):
        con=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=4);con.request(method,path)
        r=con.getresponse();body=r.read();con.close();return r.status,body
    try:
        assert get('/command-traces')[0]==200
        assert get('/static/command-traces.js')[0]==200
        status,body=get('/api/command-traces?rtu_id=1&kind=yk&limit=1')
        row=json.loads(body)['items'][0]
        assert status==200 and row['stage']=='queued' and row['device_execution_confirmed'] is None
        assert row['rtu_name']=='追踪测试站'
        assert not json.loads(get('/api/command-traces?before='+str(row['command_id']))[1])['items']
        assert get('/api/command-traces?limit=201')[0]==400
        assert get('/api/command-traces','POST')[0]==405
    finally:server.shutdown();server.server_close();thread.join(3)

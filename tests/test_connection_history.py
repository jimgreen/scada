import http.client
import json
import socket
import sqlite3
import threading
import time

import pytest

from core.connection_history import ConnectionJournal, history_path, read_history, describe_packet
from core.connection_trace import ClientTrace


def wait_until(check, timeout=5):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        result=check()
        if result:
            return result
        time.sleep(.03)
    raise AssertionError('condition timed out')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


@pytest.fixture
def history(tmp_path):
    db=tmp_path/'scada.sqlite3'
    writer=ConnectionJournal(history_path(db,'s'))
    yield db,writer
    writer.close()


def meta(protocol='IEC104'):
    return dict(service='scdfe_s',protocol=protocol,channel_id=7,rtu_id=8)


def test_retention_pagination_and_restart(history,monkeypatch):
    import core.connection_history as module
    monkeypatch.setattr(module,'MAX_EVENTS',5)
    db,writer=history
    ident=writer.open(meta(),('127.0.0.1',1234),('0.0.0.0',2404))
    for n in range(12):
        writer.event(ident,'log',str(n))
    writer.state(ident,'closed','normal end')
    assert writer.flush()
    writer.close()
    item=read_history(db,'s',ident)['item']
    assert item['closed'] and item['removed']==9
    rows=read_history(db,'s',ident,events=True,query={'limit':2})
    older=read_history(db,'s',ident,events=True,query={'limit':5,'before':rows['next_before']})
    assert len(rows['items'])==2 and len(older['items'])==3
    assert not set(r['id'] for r in rows['items']) & set(r['id'] for r in older['items'])
    assert read_history(db,'s',query={'rtu_id':99})['total']==0


@pytest.mark.parametrize('query',[{'limit':'abc'},{'limit':201},{'before':-1},{'state':'oops'},{'kind':'sql'}])
def test_query_validation(history,query):
    with pytest.raises(ValueError):
        read_history(history[0],'s',query=query)


def test_split_and_coalesced_modbus_packets_are_per_client(history):
    db,writer=history
    a=ClientTrace(writer,meta('Modbus-TCP'),('127.0.0.1',1),('127.0.0.1',502))
    b=ClientTrace(writer,meta('Modbus-TCP'),('127.0.0.1',2),('127.0.0.1',502))
    first=bytes.fromhex('000100000006010300000002')
    second=bytes.fromhex('000100000006010600071234')
    a.packet('RX',first[:4]); b.packet('RX',second); a.packet('RX',first[4:]+first)
    a.close('close');b.close('close');writer.flush()
    events=lambda obj:read_history(db,'s',obj.ident,events=True,query={'kind':'packet'})['items']
    assert [e['hex'] for e in events(a)]==[first.hex(' ')]*2
    assert [e['hex'] for e in events(b)]==[second.hex(' ')]
    assert read_history(db,'s',a.ident)['item']['rx_bytes']==24


def test_decode_gi_command_and_invalid():
    gi=bytes.fromhex('680e0000000064010600010000000014')
    assert '总召' in describe_packet('IEC104','RX',gi) or 'QOI' in describe_packet('IEC104','RX',gi)
    command=bytes.fromhex('680e000000002d010600010001600081')
    text=describe_packet('IEC104','RX',command)
    assert '选择' in text and '24577' in text
    assert '解码失败' in describe_packet('IEC104','RX',b'bad')


def test_packet_policy_keeps_counts_and_interactions_without_raw_bytes(history):
    db,writer=history
    ident=writer.open({**meta(),'packet_enabled':lambda:False},'peer','local')
    writer.event(ident,'packet','IEC104','RX',bytes.fromhex('680407000000'))
    writer.flush()
    assert read_history(db,'s',ident)['item']['rx_bytes']==6
    assert read_history(db,'s',ident)['item']['packet_recording']==0
    assert read_history(db,'s',ident,events=True,query={'kind':'packet'})['items']==[]
    assert 'STARTDT' in read_history(db,'s',ident,events=True,query={'kind':'interaction'})['items'][0]['summary']


def test_stale_process_is_not_reported_as_normal_disconnection(history):
    db,writer=history
    ident=writer.open(meta(),'peer','local')
    writer.flush();writer.close()
    with sqlite3.connect(history_path(db,'s')) as con:
        con.execute('UPDATE connection_session SET heartbeat=? WHERE id=?',(time.time()-30,ident))
    item=read_history(db,'s',ident)['item']
    assert item['stale'] and item['closed'] is None


def test_shared_writer_released_after_last_server(tmp_path):
    from core.connection_history import journal,release_journal
    db=tmp_path/'db.sqlite3'
    first=journal(db,'s'); second=journal(db,'s')
    assert first is second
    release_journal(first)
    assert second._thread.is_alive()
    release_journal(second)
    assert not second._thread.is_alive()


def test_opc_stream_disconnect_and_raw_observation(history):
    from core.connection_trace import observe_protocol
    class Protocol:
        def connection_made(self,transport):self.transport=transport
        def data_received(self,data):self.received=data
        def connection_lost(self,exc):self.error=exc
    class Transport:
        def get_extra_info(self,key):return ('127.0.0.1',4840 if key=='sockname' else 1234)
        def write(self,data):self.written=data
    db,writer=history
    protocol=observe_protocol(Protocol(),writer,meta('OPC-UA'))
    protocol.connection_made(Transport())
    protocol.data_received(b'HELdata');protocol.transport.write(b'ACKdata')
    protocol.connection_lost(None);writer.flush()
    row=read_history(db,'s')['items'][0]
    assert row['closed'] and row['rx_bytes']==row['tx_bytes']==7
    assert len(read_history(db,'s',row['id'],events=True,query={'kind':'packet'})['items'])==2


@pytest.mark.parametrize('component',['s','t'])
def test_http_pages_and_history(tmp_path,component):
    if component=='s':
        from scdfe_s.web.server import make_server
    else:
        from scdfe_t.web.server import make_server
    db=tmp_path/'data.sqlite3'
    server=make_server('127.0.0.1',0,db_path=db)
    store = server.store if component=='s' else server.trans_store.store
    table = 'fes_rtu_define' if component=='s' else 'fes_scdfe_t_define'
    definition = dict(rtu_id=8,channel_id=7,name='测试站 <甲>',channel_type='iec104') if component=='s' else dict(channel_id=7,name='测试转发 <甲>',protocol='iec104')
    store.create_row(table,definition)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    writer=ConnectionJournal(history_path(db,component))
    ident=writer.open(meta(),('127.0.0.1',1234),('127.0.0.1',2404))
    writer.event(ident,'log','<script>alert(1)</script>');writer.flush()
    def get(path):
        con=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        con.request('GET',path);res=con.getresponse();data=res.read();con.close()
        return res.status,data
    try:
        for path in ('/connections','/connections/'+ident,'/static/connections.js','/static/connections.css'):
            status,_=get(path);assert status==200,path
        status,body=get('/api/connections');assert status==200
        assert json.loads(body)['items'][0]['id']==ident
        assert json.loads(body)['items'][0]['display_name']==definition['name']
        status,body=get('/api/connections/options');assert status==200
        option=json.loads(body)['items'][0]
        assert option['name']==definition['name']
        assert option['query']==({'rtu_id':8} if component=='s' else {'channel_id':7})
        assert definition['name'] in option['label']
        status,body=get('/api/connections/'+ident);assert status==200
        assert json.loads(body)['item']['display_name']==definition['name']
        status,body=get('/api/connections/'+ident+'/events?kind=log');assert status==200
        assert len(json.loads(body)['items'])==2
        assert get('/api/connections/not-valid')[0]==400
        assert get('/api/connections/'+('f'*32))[0]==404
        assert get('/api/connections?unknown=1')[0]==400
        assert get('/api/connections/options?unknown=1')[0]==400
        key=[8] if component=='s' else [7]
        store.update_row(table,key,{'name':'新名称'})
        assert json.loads(get('/api/connections')[1])['items'][0]['display_name']=='新名称'
    finally:
        writer.close();server.shutdown();thread.join(3);server.server_close()


def test_name_options_keep_duplicate_names_and_historical_rtu_separate(tmp_path):
    from scdfe_c.web.service import ScadaStore
    from core.web_connections import connection_options,attach_connection_names
    db=tmp_path/'data.sqlite3'
    store=ScadaStore(db)
    writer=ConnectionJournal(history_path(db,'s'))
    try:
        for rid,channel,protocol in ((8,7,'iec104'),(9,7,'modbus-tcp'),(10,8,'iec104')):
            store.create_row('fes_rtu_define',dict(rtu_id=rid,channel_id=channel,channel_type=protocol,name='同名站'))
        historical=writer.open({**meta(),'rtu_id':99},'peer','local');writer.flush()
        items=connection_options(store,'s')['items']
        assert len(items)==4 and len({x['label'] for x in items})==4
        assert {x['query']['rtu_id'] for x in items}=={8,9,10,99}
        assert '已删除' in next(x['name'] for x in items if x['rtu_id']==99)
        result=read_history(db,'s',historical)
        attach_connection_names(result,store,'s',detail=True)
        assert result['item']['display_name']=='已删除的RTU（99）'
    finally:
        writer.close();store.close()


def test_modbus_real_two_clients(history):
    from server.modbus_server import ModbusTcpServer
    db,writer=history
    server=ModbusTcpServer(port=free_port())
    server.set_connection_history(writer,meta('Modbus-TCP'))
    clients=[]
    try:
        server.start()
        for address in (1,9):
            sock=socket.create_connection(('127.0.0.1',server.port),timeout=2);clients.append(sock)
            sock.sendall(bytes.fromhex('0001000000060103')+address.to_bytes(2,'big')+b'\0\1')
            assert sock.recv(1024)[7]==3
        wait_until(lambda: writer.flush() and read_history(db,'s')['total']==2)
        sessions=read_history(db,'s')['items']
        assert len({s['peer'] for s in sessions})==2
        for row in sessions:
            packets=read_history(db,'s',row['id'],events=True,query={'kind':'packet'})['items']
            assert len(packets)==2
            assert {p['direction'] for p in packets}=={'RX','TX'}
    finally:
        for sock in clients:sock.close()
        server.close()
    wait_until(lambda:writer.flush() and all(s['closed'] for s in read_history(db,'s')['items']))


def test_iec104_native_same_ip_reconnect_and_start_stop(history):
    from server.iec104_server import IEC104Server
    db,writer=history
    server=IEC104Server(ip='127.0.0.1',port=free_port())
    server.set_connection_history(writer,meta())
    clients=[]
    try:
        server.start()
        for _ in range(2):
            sock=socket.create_connection(('127.0.0.1',server.port),timeout=2);clients.append(sock)
            sock.sendall(bytes.fromhex('680407000000'))
            assert bytes.fromhex('68040b000000') in sock.recv(1024)
        wait_until(lambda:writer.flush() and read_history(db,'s')['total']==2)
        first=read_history(db,'s')['items']
        assert len({s['peer'] for s in first})==2
        assert all(':' in s['peer'] for s in first)
        clients[0].sendall(bytes.fromhex('680413000000'))
        wait_until(lambda:writer.flush() and any(s['state']=='inactive' for s in read_history(db,'s')['items']))
        clients[0].close()
        wait_until(lambda:writer.flush() and any(s['closed'] for s in read_history(db,'s')['items']))
        clients.append(socket.create_connection(('127.0.0.1',server.port),timeout=2))
        wait_until(lambda:writer.flush() and read_history(db,'s')['total']==3)
        assert len({s['id'] for s in read_history(db,'s')['items']})==3
        for row in first:
            packets=read_history(db,'s',row['id'],events=True,query={'kind':'packet'})['items']
            assert any('68 04 07 00 00 00'==p['hex'] for p in packets)
    finally:
        for sock in clients:sock.close()
        server.stop()
    assert writer.flush()
    assert all(s['closed'] for s in read_history(db,'s')['items'])

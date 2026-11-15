"""Real TCP evidence for slow Modbus commands and request identity."""
import concurrent.futures
import socket
import struct
import threading
import time
from contextlib import contextmanager

import pytest

from server.modbus_trans_server import ModbusTcpTransServer


def frame(tid, fc, address=0, value=1, unit=1):
    return struct.pack('>HHHBBHH', tid, 0, 6, unit, fc, address, value)


def receive(peer):
    def exact(count):
        data = b''
        while len(data) < count:
            chunk = peer.recv(count-len(data))
            if not chunk:
                raise EOFError('connection closed before reply')
            data += chunk
        return data
    header = exact(7)
    tid, pid, size, unit = struct.unpack('>HHHB', header)
    assert pid == 0
    return tid, unit, exact(size-1)


@contextmanager
def running(callback):
    server = ModbusTcpTransServer(port=0, points=[
        dict(kind='yc', pnt_no=1, address=0, value=37),
        dict(kind='yk', pnt_no=1, address=0),
    ], command_handler=callback)
    server.start()
    port = server.server.transport.sockets[0].getsockname()[1]
    try:
        yield server, port
    finally:
        server.close()


def connect(port):
    return socket.create_connection(('127.0.0.1', port), timeout=2)


def test_slow_write_does_not_block_other_client_or_ack_before_commit():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def command(*args):
        calls.append(args); entered.set()
        assert release.wait(3)
        return True
    with running(command) as (server, port), connect(port) as writer, connect(port) as reader:
        try:
            writer.sendall(frame(10, 5, value=0xff00))
            assert entered.wait(1)
            reader.settimeout(.3)
            reader.sendall(frame(11, 4))
            assert receive(reader) == (11, 1, b'\x04\x02\x00%')
            writer.settimeout(.05)
            with pytest.raises(socket.timeout):receive(writer)
        finally:
            release.set()
        writer.settimeout(2)
        assert receive(writer) == (10, 1, b'\x05\x00\x00\xff\x00')
        assert calls == [('yk', 1, True)]


def test_coalesced_and_fragmented_requests_keep_transaction_and_unit():
    calls = []
    def command(*args):
        calls.append(args); time.sleep(.03); return True
    with running(command) as (_, port), connect(port) as peer:
        first = frame(101, 5, value=0xff00)
        peer.sendall(first[:5])
        time.sleep(.02)
        peer.sendall(first[5:] + frame(102, 4, unit=2) + frame(103, 4) + frame(104, 5, value=0))
        replies = [receive(peer) for _ in range(4)]
        assert replies == [(101, 1, b'\x05\x00\x00\xff\x00'),
                           (102, 2, b'\x84\x0b'), (103, 1, b'\x04\x02\x00%'),
                           (104, 1, b'\x05\x00\x00\x00\x00')]
        assert calls == [('yk', 1, True), ('yk', 1, False)]


def test_multiple_clients_serialize_commands_and_close_drains_active_commit():
    entered, release = threading.Event(), threading.Event()
    active, maximum, calls = 0, 0, []
    lock = threading.Lock()
    def command(*args):
        nonlocal active, maximum
        with lock:active += 1; maximum = max(maximum, active)
        entered.set(); assert release.wait(3)
        time.sleep(.01); calls.append(args)
        with lock:active -= 1
        return True
    with running(command) as (server, port), connect(port) as a, connect(port) as b:
        a.sendall(frame(1, 5, value=0xff00)); assert entered.wait(1)
        b.sendall(frame(2, 5, value=0))
        with concurrent.futures.ThreadPoolExecutor() as pool:
            stopped = pool.submit(server.close)
            time.sleep(.1)
            assert not stopped.done()
            release.set(); stopped.result(3)
        assert maximum == 1 and active == 0
        assert calls[0] == ('yk', 1, True)
        assert not server.thread.is_alive()


def test_queue_full_returns_busy_without_executing_rejected_commands():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def command(*args):
        calls.append(args); entered.set(); assert release.wait(3); return True
    with running(command) as (server, port), connect(port) as peer:
        server._request_dispatch.peer_limit = 2
        try:
            peer.sendall(frame(1, 5, value=0xff00)); assert entered.wait(1)
            peer.sendall(frame(2, 5, value=0)+frame(3, 5, value=0xff00))
            assert receive(peer) == (3, 1, b'\x85\x06')
            assert len(calls) == 1
        finally:release.set()
        assert [receive(peer)[0] for _ in range(2)] == [1, 2]
        assert calls == [('yk', 1, True), ('yk', 1, False)]
        # Socket receive can precede the loop's final counter update.
        metrics = server._on_datastore(lambda: server.request_diagnostics)
        assert metrics['busy'] == 1 and metrics['peak'] == 2 and metrics['pending'] == 0
        assert len(metrics['recent']) == 2


def test_disconnected_peer_drops_queued_write_but_finishes_active_one():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def command(*args):
        calls.append(args); entered.set(); assert release.wait(3); return True
    with running(command) as (server, port):
        peer = connect(port)
        try:
            peer.sendall(frame(1, 5, value=0xff00)+frame(2, 5, value=0))
            assert entered.wait(1)
            peer.close()
            deadline = time.monotonic()+1
            while server.connections and time.monotonic()<deadline:time.sleep(.01)
        finally:release.set(); peer.close()
        server.close()
        assert calls == [('yk', 1, True)]
        metrics=server.request_diagnostics
        assert metrics['abandoned'] == 1 and metrics['pending'] == 0
        assert metrics['recent'][0]['peer_disconnected']


def test_failed_callback_keeps_register_and_other_client_available():
    def command(*args):raise OSError('simulated database failure')
    with running(command) as (server, port), connect(port) as peer:
        peer.sendall(frame(1, 5, value=0xff00)+frame(2, 1))
        assert receive(peer) == (1, 1, b'\x85\x04')
        assert receive(peer) == (2, 1, b'\x01\x01\x00')
        assert server.request_diagnostics['recent'][0]['exception_code'] == 4


@pytest.mark.parametrize('function,response',[(0x7f,b'\xff\x01'),(3,b'\x83\x03')])
def test_bad_pdu_does_not_discard_following_complete_request(function,response):
    with running(lambda *args:True) as (_,port),connect(port) as peer:
        bad=struct.pack('>HHHBB',11,0,2,1,function)
        peer.sendall(bad+frame(12,4))
        assert receive(peer)==(11,1,response)
        assert receive(peer)==(12,1,b'\x04\x02\x00%')


@pytest.mark.parametrize('pid,size',[(1,6),(0,0),(0,255)])
def test_invalid_mbap_disconnects_only_its_peer(pid,size):
    with running(lambda *args:True) as (_,port),connect(port) as bad,connect(port) as healthy:
        bad.sendall(struct.pack('>HHHB',1,pid,size,1))
        assert bad.recv(1)==b''
        healthy.sendall(frame(2,4));assert receive(healthy)[0]==2


def test_s_simulator_slow_database_commit_leaves_reads_live_and_sync_current(tmp_path):
    from scdfe_c.web.service import ScadaStore
    from server.modbus_mapped_server import ModbusMappedServer
    store=ScadaStore(tmp_path/'sim.sqlite3')
    with store.connection() as con:
        con.execute("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(1,1,'modbus-tcp')")
        con.execute('INSERT INTO fes_yc_define(rtu_id,pnt_no,value) VALUES(1,1,7)')
    engine=ModbusMappedServer(store=store,rtu_id=1,port=0,
        points=[dict(kind='yc',pnt_no=1,address=0,register_area='holding',data_type='int16')])
    original=engine._commands; entered,release=threading.Event(),threading.Event()
    def slow(commands):entered.set(); assert release.wait(3); original(commands)
    engine._commands=slow; engine.start()
    port=engine.server.transport.sockets[0].getsockname()[1]
    try:
        with connect(port) as writer,connect(port) as reader:
            try:
                writer.sendall(frame(1,6,value=19)); assert entered.wait(1)
                reader.settimeout(.3); reader.sendall(frame(2,3))
                assert receive(reader)==(2,1,b'\x03\x02\x00\x07')
                assert store.get_row('fes_yc_define',[1,1])['value']==7
            finally:release.set()
            assert receive(writer)==(1,1,b'\x06\x00\x00\x00\x13')
            time.sleep(.15)
            reader.sendall(frame(3,3)); assert receive(reader)==(3,1,b'\x03\x02\x00\x13')
            assert store.get_row('fes_yc_define',[1,1])['value']==19
            assert engine._on_datastore(lambda: engine.diagnostics)['modbus_requests']['completed']==3
    finally:release.set();engine.close();store.close()


def test_clients_share_one_serial_write_worker():
    lock=threading.Lock();active=maximum=0;calls=[]
    def command(*args):
        nonlocal active,maximum
        with lock:active+=1;maximum=max(maximum,active)
        time.sleep(.025)
        with lock:active-=1;calls.append(args)
        return True
    with running(command) as (server,port),concurrent.futures.ThreadPoolExecutor(4) as pool:
        def write(n):
            with connect(port) as peer:
                peer.sendall(frame(n,5,value=0xff00));return receive(peer)
        replies=list(pool.map(write,range(1,5)))
        assert [x[0] for x in replies]==[1,2,3,4]
        assert maximum==1 and len(calls)==4
        assert server._on_datastore(lambda: server.request_diagnostics)['pending']==0


def test_shared_queue_cap_rejects_before_callback():
    entered,release=threading.Event(),threading.Event();calls=[]
    def command(*args):calls.append(args);entered.set();assert release.wait(3);return True
    with running(command) as (server,port),connect(port) as a,connect(port) as b:
        server._request_dispatch.limit=1
        try:
            a.sendall(frame(1,5,value=0xff00));assert entered.wait(1)
            b.sendall(frame(2,5,value=0xff00));assert receive(b)==(2,1,b'\x85\x06')
            assert len(calls)==1
        finally:release.set()
        assert receive(a)[0]==1


@pytest.mark.parametrize('nested',[False,True])
def test_request_metrics_exposed_without_other_timing_samples(tmp_path,nested):
    import http.client,json
    from scdfe_t.web.server import make_server
    from core.runtime_status import RuntimeStatusStore
    server=make_server('127.0.0.1',0,db_path=tmp_path/'api.sqlite3')
    metrics=dict(pending=0,peak=3,busy=1,recent=[dict(transaction_id=9)])
    diagnostic={'modbus_requests':metrics}
    if nested:diagnostic={'engine':diagnostic}
    RuntimeStatusStore(server.trans_store.store,'scdfe_t').upsert(1,dict(running=1,diagnostic_json=diagnostic))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        c=http.client.HTTPConnection('127.0.0.1',server.server_port);c.request('GET','/api/diagnostics')
        response=c.getresponse();data=json.loads(response.read());c.close()
        assert response.status==200 and data['items'][0]['modbus_requests']==metrics
        assert data['items'][0]['latency']=={}
    finally:server.shutdown();server.server_close();thread.join(3)

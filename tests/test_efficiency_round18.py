"""Round 18: batch runtime writes and one-query transport configuration."""
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
import pytest

from core.runtime_status import RuntimeStatusStore, _write_snapshots
from scdfe_c.web.service import ScadaStore
from scdfe_s.web.protocol_service import ModbusTcpService
from scdfe_s.web.server import make_server, ScadaHandler


def test_runtime_batch_uses_one_executemany_call(tmp_path):
    store = ScadaStore(tmp_path/'batch.sqlite3')
    try:
        statuses = [RuntimeStatusStore(store, 'scdfe_s', write_interval_ms=0) for _ in range(3)]
        snapshots = [status._prepare(n, {'state': 'running', 'rx_count': n}, force=True)
                     for n, status in enumerate(statuses, 1)]
        original = store.connection
        class Proxy:
            def __init__(self, connection): self.connection = connection; self.executemany_calls = 0
            @property
            def in_transaction(self): return self.connection.in_transaction
            def execute(self, *args): return self.connection.execute(*args)
            def executemany(self, *args):
                self.executemany_calls += 1
                return self.connection.executemany(*args)
            def commit(self): return self.connection.commit()
            def rollback(self): return self.connection.rollback()
        proxy = None
        @contextmanager
        def leased():
            nonlocal proxy
            with original() as connection:
                proxy = Proxy(connection)
                yield proxy
        store.connection = leased
        assert _write_snapshots(store, snapshots) == 3
        assert proxy.executemany_calls == 1
        assert [row['rx_count'] for row in statuses[0].list()] == [1, 2, 3]
    finally:
        store.close()


def test_transport_config_joins_rtu_and_protocol_row_once(tmp_path):
    store = ScadaStore(tmp_path/'config.sqlite3')
    try:
        store.create_row('fes_rtu_define', {
            'rtu_id': 1, 'channel_id': 11, 'channel_type': 'modbus-tcp', 'name': '测试站'})
        store.create_row('fes_channel_modbus_tcp_define', {
            'channel_id': 11, 'remote_host': '127.0.0.1', 'remote_port': 1502,
            'unit_id': 1, 'response_timeout': 1, 'connect_timeout': 1,
            'retry_count': 0, 'keep_alive': 1})
        service = ModbusTcpService(store, config_path=tmp_path/'config.json')
        service._config['rtu_id'] = 1
        service._configured_fields.add('rtu_id')
        statements = []
        original = store.connection
        @contextmanager
        def traced():
            with original() as connection:
                connection.set_trace_callback(statements.append)
                try: yield connection
                finally: connection.set_trace_callback(None)
        store.connection = traced
        config = service.config()
        assert config['remote_port'] == 1502
        selects = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]
        assert len(selects) == 1, selects
    finally:
        store.close()


@pytest.fixture
def monitor(tmp_path):
    server = make_server('127.0.0.1', 0, db_path=tmp_path/'sim.sqlite3')
    with server.store.connection() as con:
        con.executemany('INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type,name) VALUES(?,?,?,?)',
            [(n,n,('iec104','modbus-tcp','can')[(n-1)%3],f'站{n}') for n in range(1,65)])
        con.executemany('INSERT INTO fes_channel_modbus_tcp_define(channel_id,remote_host,remote_port,unit_id) VALUES(?,?,?,?)',
            [(n,'127.0.0.1',1500+n,n) for n in range(2,65,3)])
    handler=object.__new__(ScadaHandler);handler.server=server
    yield handler,server.channel_services()
    server.server_close()


def test_many_rtus_use_bounded_config_queries_and_identical_config(monitor):
    handler,services=monitor
    expected={name:service.config() for name,service in services.items()}
    sql=[]
    with handler.store.connection() as con:con.set_trace_callback(sql.append)
    try:statuses,_=handler._monitor_statuses(services,{})
    finally:
        with handler.store.connection() as con:con.set_trace_callback(None)
    assert {name:status['config'] for name,status in statuses}==expected
    assert len([s for s in sql if s.startswith('SELECT')])==3
    assert sql.count('BEGIN')==1 and sql.count('BEGIN IMMEDIATE')==1


def test_engine_diagnostic_sample_is_shared_with_persisted_snapshot(monitor):
    handler,services=monitor;service=services['modbus-tcp-rtu-2']
    class Engine:
        connected=True
        count=0
        @property
        def diagnostics(self):
            self.count+=1
            return {'sample':self.count}
    engine=Engine();service._engine=engine
    try:
        statuses,_=handler._monitor_statuses({'modbus-tcp-rtu-2':service},{},2)
        row=service._runtime_status.list(channel_id=2)[0]
        assert engine.count==1
        assert row['diagnostic']['engine']==statuses[0][1]['diagnostic']=={'sample':1}
    finally:service._engine=None


def test_config_batch_preserves_overrides_missing_rows_and_rtu_binding(monitor):
    handler,services=monitor
    service=services['modbus-tcp-rtu-2'];service._config['remote_port']=9999
    service._configured_fields.add('remote_port')
    service._binding_active=True;service._bound_rtu_id=5
    chosen={'bound':service,'missing':services['can-rtu-3']}
    expected={name:s.config() for name,s in chosen.items()}
    statuses,_=handler._monitor_statuses(chosen,{})
    assert {name:status['config'] for name,status in statuses}==expected
    assert statuses[0][1]['rtu_id']==5 and statuses[0][1]['config']['remote_port']==9999
    assert statuses[0][1]['config']['unit_id']==5


def test_config_batch_chunks_read_one_snapshot_and_next_poll_sees_edits(monitor):
    handler,services=monitor
    chosen={name:s for name,s in services.items() if name.startswith('modbus-tcp')}
    other=handler.store.open_connection();reads=[]
    def trace(sql):
        if sql.startswith('SELECT') and 'fes_channel_modbus_tcp_define' in sql:
            reads.append(sql)
            if len(reads)==2:
                other.execute('UPDATE fes_channel_modbus_tcp_define SET remote_port=7777 WHERE channel_id=62')
    with handler.store.connection() as con:
        old=con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,4)
        con.set_trace_callback(trace)
    # Capture only: the deliberately tiny bind limit is below the UPSERT's
    # column count, so disable publication for this read-snapshot fixture.
    from unittest.mock import patch
    try:
        with patch('scdfe_s.web.server.write_runtime_status_batch',return_value=0):
            statuses,_=handler._monitor_statuses(chosen,{})
        assert len(reads)==6
        assert dict(statuses)['modbus-tcp-rtu-62']['config']['remote_port']==1562
    finally:
        with handler.store.connection() as con:
            con.set_trace_callback(None);con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,old)
        other.close()
    statuses,_=handler._monitor_statuses(chosen,{})
    assert dict(statuses)['modbus-tcp-rtu-62']['config']['remote_port']==7777


def test_selected_rtu_does_not_read_unrelated_parameter_rows(monitor):
    handler,services=monitor;sql=[]
    with handler.store.connection() as con:con.set_trace_callback(sql.append)
    try:statuses,_=handler._monitor_statuses({'one':services['modbus-tcp-rtu-2']},{},2)
    finally:
        with handler.store.connection() as con:con.set_trace_callback(None)
    assert statuses[0][1]['config']['remote_port']==1502
    reads=[s for s in sql if s.startswith('SELECT')]
    assert len(reads)==2 and not any('can_define' in s for s in reads)
    assert 'IN (2)' in reads[1]


def test_diagnostic_encoding_preserves_special_values_and_string_keys():
    from collections import UserDict, namedtuple
    from datetime import datetime, timezone
    import json
    from core.runtime_status import diagnostic_json
    Pair=namedtuple('Pair','a b');when=datetime(2026,11,15,tzinfo=timezone.utc)
    raw=UserDict({True: [1,None,False,'文本',2.5], None: {'bytes':b'\x01\xab','time':when},
        'floats':[float('nan'),float('inf'),float('-inf')], 'pair':Pair(1,2),
        'nested':UserDict({3:memoryview(b'\x02')})})
    assert json.loads(diagnostic_json(raw))=={'True':[1,None,False,'文本',2.5],
        'None':{'bytes':'01 ab','time':when.isoformat()},'floats':[None,None,None],
        'pair':[1,2],'nested':{'3':'02'}}


def test_invalid_config_still_aborts_monitor_publication(monitor):
    handler,services=monitor
    with handler.store.connection() as con:
        con.execute('UPDATE fes_channel_modbus_tcp_define SET remote_port=99999 WHERE channel_id=62')
    with pytest.raises(Exception,match='remote_port|端口'):
        handler._monitor_statuses(services,{})
    with handler.store.connection() as con:
        assert con.execute('SELECT count(*) FROM fes_channel_runtime_status').fetchone()[0]==0

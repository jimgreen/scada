"""Status traffic is sampled; lifecycle changes and transaction order are not."""
from contextlib import contextmanager
from types import SimpleNamespace
import sqlite3
import threading
import time
import pytest

from core import runtime_status as module
from core.runtime_status import RuntimeStatusStore
from scdfe_c.web.service import ScadaStore
from scdfe_s.web.server import make_server, ScadaHandler


@pytest.fixture
def store(tmp_path):
    data = ScadaStore(tmp_path/'status.sqlite3', connection_pool_size=2)
    yield data
    data.close()


def test_changing_traffic_is_sampled_using_monotonic_time(store, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    status = RuntimeStatusStore(store, 'scdfe_c')
    first = dict(state='running', running=1, rx_count=1, sampled_at=2000)
    assert status.upsert(1, first)
    for n in range(2, 30):
        assert not status.upsert(1, dict(first, rx_count=n, tx_count=n,
            process_heartbeat_time=n, diagnostic_json={'latency': n}, sampled_at=1000))
    assert status.list()[0]['rx_count'] == 1
    clock[0] += 1.001
    assert status.upsert(1, dict(first, rx_count=30, sampled_at=1000))
    assert status.list()[0]['rx_count'] == 30


@pytest.mark.parametrize('change', [dict(state='error'), dict(running=0),
    dict(connection_state='disconnected'), dict(process_status=5),
    dict(process_error='timeout'), dict(last_error='crc'), dict(error_count=1),
    dict(dropped_count=1), dict(reconnect_count=1), dict(process_stop_time=100),
    dict(process_pid=22), dict(process_exit_code=1)])
def test_lifecycle_and_failure_changes_bypass_sampling(store, change):
    status = RuntimeStatusStore(store, 'scdfe_t', write_interval_ms=60000)
    first = dict(state='running', running=1, connection_state='connected')
    assert status.upsert(1, first)
    assert status.upsert(1, dict(first, **change))
    row = status.list()[0]
    assert all(row[key] == value for key, value in change.items())


def test_outer_rollback_cannot_suppress_next_snapshot(store):
    status = RuntimeStatusStore(store, 'scdfe_t', write_interval_ms=60000)
    with store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        assert status.upsert(1, dict(state='running'), connection=con)
        con.rollback()
    assert status.upsert(1, dict(state='running'))
    assert len(status.list()) == 1


def test_older_concurrent_capture_cannot_overwrite_newer_state(store):
    status = RuntimeStatusStore(store, 'scdfe_s')
    original = store.connection
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []
    @contextmanager
    def held():
        if threading.current_thread().name == 'old-status':
            entered.set()
            assert release.wait(5)
        with original() as con:
            yield con
    store.connection = held
    def old():
        try:
            results.append(status.upsert(1, dict(state='running'), force=True))
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=old, name='old-status'); worker.start()
    try:
        assert entered.wait(2)
        assert status.upsert(1, dict(state='stopped'), force=True)
    finally:
        release.set(); worker.join(5)
    assert not worker.is_alive() and not errors
    assert status.list()[0]['state'] == 'stopped'
    assert results == [False]


@pytest.fixture
def monitor(tmp_path):
    server = make_server('127.0.0.1', 0, db_path=tmp_path/'s.sqlite3')
    for n, protocol in enumerate(('iec104', 'modbus-tcp', 'can'), 1):
        server.store.create_row('fes_rtu_define', dict(rtu_id=n, channel_id=n,
            channel_type=protocol, name=f'站{n}'))
    handler = object.__new__(ScadaHandler); handler.server = server
    yield handler, server.channel_services()
    server.server_close()


def test_repeated_monitor_reads_do_not_acquire_sqlite_write_lock(monitor, monkeypatch):
    handler, services = monitor
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=time.time, monotonic=lambda: 100.0))
    handler._monitor_statuses(services, {})
    sql = []
    with handler.store.connection() as con:
        con.set_trace_callback(sql.append)
        con.execute('PRAGMA busy_timeout=0')
    locked = sqlite3.connect(handler.store.db_path, isolation_level=None)
    locked.execute('BEGIN IMMEDIATE')
    try:
        for n in range(1, 11):
            for service in services.values():
                service._rx_count = n
            statuses, _ = handler._monitor_statuses(services, {})
            assert all(row['rx_count'] == n for _, row in statuses)
    finally:
        locked.rollback(); locked.close()
        with handler.store.connection() as con: con.set_trace_callback(None)
    assert 'BEGIN IMMEDIATE' not in sql
    assert not any(s.startswith('INSERT') for s in sql)


def test_failed_batch_retains_previous_rows_and_retries_latest(monitor, monkeypatch):
    handler, services = monitor; clock = [100.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    handler._monitor_statuses(services, {})
    for service in services.values(): service._rx_count = 50
    clock[0] += 2
    with handler.store.connection() as con:
        con.execute("CREATE TRIGGER fail_last BEFORE UPDATE ON fes_channel_runtime_status WHEN NEW.channel_id=3 BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(Exception, match='保存通道诊断快照失败'):
        handler._monitor_statuses(services, {})
    with handler.store.connection() as con:
        assert all(row[0] == 0 for row in con.execute('SELECT rx_count FROM fes_channel_runtime_status'))
        con.execute('DROP TRIGGER fail_last')
    handler._monitor_statuses(services, {})
    with handler.store.connection() as con:
        assert all(row[0] == 50 for row in con.execute('SELECT rx_count FROM fes_channel_runtime_status'))


def test_force_and_new_channel_publish_without_encoding_skipped_diagnostics(store, monkeypatch):
    status = RuntimeStatusStore(store, 'scdfe_c', write_interval_ms=60000)
    assert status.upsert(1, dict(state='running'))
    original = module.diagnostic_json
    def forbidden(value): raise AssertionError('encoded a skipped diagnostic')
    monkeypatch.setattr(module, 'diagnostic_json', forbidden)
    assert not status.upsert(1, dict(state='running', diagnostic_json={'rx':123}))
    monkeypatch.setattr(module, 'diagnostic_json', original)
    assert status.upsert(1, dict(state='running', rx_count=3), force=True)
    assert status.upsert(2, dict(state='running', rx_count=4))
    assert [row['rx_count'] for row in status.list()] == [3, 4]


def test_outer_writer_can_publish_while_other_call_waits_for_sqlite(store):
    status = RuntimeStatusStore(store, 'scdfe_t')
    original = store.connection
    waiting = threading.Event(); results = []; errors = []
    @contextmanager
    def traced():
        with original() as con:
            con.execute('PRAGMA busy_timeout=500')
            con.set_trace_callback(lambda sql: waiting.set() if sql == 'BEGIN IMMEDIATE' else None)
            try: yield con
            finally: con.set_trace_callback(None)
    store.connection = traced
    outer = sqlite3.connect(store.db_path, isolation_level=None)
    outer.execute('BEGIN IMMEDIATE')
    def waiting_writer():
        try: results.append(status.upsert(1, dict(state='running'), force=True))
        except BaseException as exc: errors.append(exc)
    thread = threading.Thread(target=waiting_writer); thread.start()
    try:
        assert waiting.wait(2)
        assert status.upsert(1, dict(state='stopped'), force=True, connection=outer)
        outer.commit()
    finally:
        outer.close(); thread.join(3)
    assert not thread.is_alive() and not errors
    assert results == [False]
    assert status.list()[0]['state'] == 'stopped'


def test_http_returns_live_counters_and_identifies_persisted_snapshot(monitor, monkeypatch):
    import http.client
    import json
    handler, services = monitor
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=time.time, monotonic=lambda: 100.0))
    thread = threading.Thread(target=handler.server.serve_forever); thread.start()
    def get():
        c = http.client.HTTPConnection('127.0.0.1', handler.server.server_port, timeout=3)
        try:
            c.request('GET', '/api/monitor/communications?rtu_id=1')
            r=c.getresponse(); assert r.status == 200
            return json.loads(r.read())['items'][0]
        finally: c.close()
    try:
        first = get()
        services['iec104-rtu-1']._rx_count = 123
        row = get()
        assert row['rx_count'] == 123
        assert row['runtime_status']['rx_count'] == first['runtime_status']['rx_count'] == 0
        assert row['runtime_status']['sampled_at'] == first['runtime_status']['sampled_at']
    finally: handler.server.shutdown(); thread.join(3)

"""Bounded writer latency, transactional point deltas and lossless UI cursors."""
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import scdfe_c as runtime
from scdfe_c.web.service import ScadaStore
from core.connection_history import ConnectionJournal, history_path, read_history


@pytest.fixture
def store(tmp_path):
    value = ScadaStore(tmp_path / 'data.sqlite3')
    for rid in (1, 2):
        value.create_row('fes_rtu_define', dict(rtu_id=rid, channel_id=rid, channel_type='iec104'))
        for kind in ('yc', 'yx'):
            for no in range(1, 4):
                value.create_row(f'fes_{kind}_define', dict(rtu_id=rid, pnt_no=no, value=0))
    yield value
    value.close()


def test_continuous_small_batches_commit_before_count_threshold(store, tmp_path):
    elapsed = [100.0]
    flushed = []
    original = runtime._flush_batch

    def flush(*args, **kwargs):
        flushed.append(elapsed[0])
        return original(*args, **kwargs)

    class Queue:
        count = 0
        acknowledged = 0

        def get(self, timeout):
            self.count += 1
            elapsed[0] += .02
            if self.count == 51:
                return runtime.BATCH_WRITER_STOP
            return dict(op='update', table='fes_yc_define',
                        key=dict(rtu_id=1, pnt_no=1), values=dict(value=self.count))

        def task_done(self):
            self.acknowledged += 1

        def get_nowait(self):
            # This fixture produces the next message after 20 ms; there is no
            # already queued burst for the writer to prefetch.
            from queue import Empty
            raise Empty

    queue = Queue()
    with patch.object(runtime.time, 'monotonic', side_effect=lambda: elapsed[0]), \
            patch.object(runtime, '_flush_batch', side_effect=flush):
        runtime._batch_writer_entry(str(store.db_path), str(store.catalog.directory),
                                    str(tmp_path / 'logs'), queue, batch_interval=.1, max_batch=500)
    assert flushed[0] - 100.02 < .15
    assert len(flushed) > 1
    assert queue.acknowledged == 51
    assert store.get_row('fes_yc_define', [1, 1])['value'] == 50


def test_monitor_delta_coalesces_latest_values_without_cross_rtu_scans(store):
    from core.monitor_changes import ensure_monitor_changes, read_monitor_changes
    with store.connection() as con:
        ensure_monitor_changes(con)
        cursor, rows, deleted = read_monitor_changes(con, 1, None)
        assert len(rows) == 6 and not deleted
        for value in range(1, 11):
            con.execute('UPDATE fes_yc_define SET value=? WHERE rtu_id=1 AND pnt_no=2', (value,))
        con.execute('UPDATE fes_yx_define SET status=128 WHERE rtu_id=2 AND pnt_no=1')
        current, rows, deleted = read_monitor_changes(con, 1, cursor)
        assert [(r[0], r[1], r[2], r[3]) for r in rows] == [('yc', 2, 10, 0)]
        assert current > cursor and not deleted
        assert con.execute('SELECT COUNT(*) FROM _scada_monitor_change').fetchone()[0] == 2
        con.execute('UPDATE fes_yc_define SET refresh_time=refresh_time+1 WHERE rtu_id=1')
        assert read_monitor_changes(con, 1, current) == (current, [], set())
        con.execute('UPDATE fes_yx_define SET status=16 WHERE rtu_id=1 AND pnt_no=3')
        latest, rows, _ = read_monitor_changes(con, 1, current)
        assert [(r[0], r[1], r[3]) for r in rows] == [('yx', 3, 16)]
        assert latest > current


def test_monitor_delta_rollback_delete_reinsert_and_independent_consumers(store):
    from core.monitor_changes import ensure_monitor_changes, read_monitor_changes
    with store.connection() as con:
        ensure_monitor_changes(con)
        first, _, _ = read_monitor_changes(con, 1, None)
        con.execute('SAVEPOINT mutation')
        con.execute('UPDATE fes_yc_define SET value=99 WHERE rtu_id=1 AND pnt_no=1')
        con.execute('ROLLBACK TO mutation'); con.execute('RELEASE mutation')
        assert read_monitor_changes(con, 1, first) == (first, [], set())
        con.execute('DELETE FROM fes_yc_define WHERE rtu_id=1 AND pnt_no=1')
        deleted_at, rows, deleted = read_monitor_changes(con, 1, first)
        assert rows == [] and deleted == {('yc', 1)}
        con.execute('INSERT INTO fes_yc_define(rtu_id,pnt_no,value) VALUES(1,1,17)')
        a = read_monitor_changes(con, 1, first)
        b = read_monitor_changes(con, 1, deleted_at)
        assert a == b and a[1][0][2] == 17 and not a[2]
        con.execute('UPDATE fes_yc_define SET rtu_id=2,pnt_no=4 WHERE rtu_id=1 AND pnt_no=1')
        assert read_monitor_changes(con, 1, a[0])[2] == {('yc', 1)}
        assert any(r[1] == 4 for r in read_monitor_changes(con, 2, None)[1])


def test_monitor_index_survives_store_restart_and_retains_cursor(store):
    from core.monitor_changes import ensure_monitor_changes, read_monitor_changes
    with store.connection() as con:
        ensure_monitor_changes(con)
        first, _, _ = read_monitor_changes(con, 1, None)
    store.update_row('fes_yc_define', [1, 2], {'value': 25})
    store.close()
    reopened = ScadaStore(store.db_path)
    try:
        with reopened.connection() as con:
            ensure_monitor_changes(con)
            current, rows, _ = read_monitor_changes(con, 1, first)
            assert current > first and [(r[0], r[1], r[2]) for r in rows] == [('yc', 2, 25)]
            from tools.simplify_schema import plan
            assert not any(plan(con, reopened.catalog).values())
    finally:
        reopened.close()


def test_monitor_coalescing_does_not_coalesce_soe(tmp_path):
    from scdfe_s.database import SimulatorStore
    from core.monitor_changes import ensure_monitor_changes, read_monitor_changes
    from tools.verify_iec104_soe import fill_one
    store = SimulatorStore(tmp_path / 'simulator.sqlite3')
    try:
        fill_one(store, 2404)
        with store.connection() as con:
            ensure_monitor_changes(con)
            first, _, _ = read_monitor_changes(con, 1, None)
            for value in (1, 0, 1):
                con.execute('UPDATE fes_yx_define SET value=?', (value,))
            current, rows, _ = read_monitor_changes(con, 1, first)
            assert current > first and len(rows) == 1 and rows[0][2] == 1
            assert [r[0] for r in con.execute('SELECT raw_state FROM _scada_iec104_soe ORDER BY event_id')] == [1, 0, 1]
        store.close()
        store = SimulatorStore(store.db_path)
        with store.connection() as con:
            assert con.execute('SELECT COUNT(*) FROM _scada_iec104_soe').fetchone()[0] == 3
    finally:
        store.close()


def test_connection_after_cursor_drains_oldest_unseen_first_without_gaps(tmp_path):
    db = tmp_path / 'data.sqlite3'
    writer = ConnectionJournal(history_path(db, 's'))
    try:
        ident = writer.open(dict(service='scdfe_s', protocol='IEC104', channel_id=1, rtu_id=1), 'peer', 'local')
        writer.flush()
        initial = read_history(db, 's', ident, events=True)
        cursor = initial['items'][0]['id']
        for n in range(250):
            writer.event(ident, 'log', f'event {n}')
        writer.flush()
        seen = []
        while True:
            page = read_history(db, 's', ident, events=True, query=dict(after=cursor, limit=100))
            ids = [row['id'] for row in page['items']]
            assert ids == sorted(ids, reverse=True)  # presentation remains newest first
            seen.extend(row['summary'] for row in reversed(page['items']))
            cursor = page['next_after']
            if not page['has_more']:
                break
        assert seen == [f'event {n}' for n in range(250)]
        assert read_history(db, 's', ident, events=True, query=dict(after=cursor))['items'] == []
    finally:
        writer.close()

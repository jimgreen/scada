"""Compare writer deadline and sparse S reads using disposable databases.

Run this same script with --root pointing at the baseline checkout and the
working tree. No operator database or protocol listener is touched.
"""
import argparse
import json
from pathlib import Path
from queue import Queue
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.root.resolve()))
    import scdfe_c as runtime
    from scdfe_c.web.service import ScadaStore
    from scdfe_s.web.iec104_service import IEC104Service

    def summary(values):
        return dict(median_ms=round(statistics.median(values), 4),
                    max_ms=round(max(values), 4), samples=len(values))

    result = dict(root=str(args.root.resolve()), rtus=16, points_per_rtu=1000, rounds=40)
    with tempfile.TemporaryDirectory(prefix='scada-round9-') as directory:
        store = ScadaStore(Path(directory)/'bench.sqlite3')
        with store.connection() as con:
            con.execute('BEGIN')
            con.executemany('INSERT INTO fes_rtu_define(rtu_id,channel_type,channel_id) VALUES(?, ?, ?)',
                            [(rid, 'iec104', rid) for rid in range(1, 17)])
            for kind in ('yc', 'yx'):
                con.executemany(f'INSERT INTO fes_{kind}_define(rtu_id,pnt_no,value,status) VALUES(?,?,0,0)',
                                [(rid, no) for rid in range(1, 17) for no in range(1, 501)])
            con.commit()
            services = []
            for rid in range(1, 17):
                cache = {(kind, no): (0, 0) for kind in ('yc', 'yx') for no in range(1, 501)}
                def update(rows, transmit, cache=cache):
                    cache.update({(kind, no): (value, quality) for kind, no, value, quality in rows})
                server = SimpleNamespace(running=True, rtu_id=rid, active_connection_count=0,
                    prepare_value=lambda k,n,v:v,
                    monitoring_snapshot=lambda keys, cache=cache:{key:cache.get(key) for key in keys},
                    update_monitoring=update)
                service = SimpleNamespace(_server=server, _lock=threading.RLock(),
                    _database_samples={}, _database_pending=set(), _soe_forwarder=None,
                    _packet_sink=SimpleNamespace(business=SimpleNamespace(rows=lambda *a, **kw:None)))
                IEC104Service._sync_database_values(service, con)
                services.append(service)
            sparse, writes, dense, dense_writes = [], [], [], []
            for dense_mode in (False, True):
                for cycle in range(40):
                    start = time.perf_counter()
                    if dense_mode:
                        con.execute('BEGIN')
                        con.execute('UPDATE fes_yc_define SET value=value+1')
                        con.commit()
                    else:
                        con.execute('UPDATE fes_yc_define SET value=value+1 WHERE rtu_id=1 AND pnt_no=1')
                    (dense_writes if dense_mode else writes).append((time.perf_counter()-start)*1000)
                    start = time.perf_counter()
                    changed = sum(IEC104Service._sync_database_values(s, con) for s in services)
                    (dense if dense_mode else sparse).append((time.perf_counter()-start)*1000)
                    assert changed == (8000 if dense_mode else 1), changed
            result.update(sparse_read=summary(sparse), sparse_write=summary(writes),
                          dense_read=summary(dense), dense_write=summary(dense_writes))
        store.close()
        # Time queue-to-commit separately from the large-store startup/migration.
        store = ScadaStore(Path(directory)/'writer.sqlite3')
        store.create_row('fes_rtu_define', dict(rtu_id=1, channel_type='iec104', channel_id=1))
        store.create_row('fes_yc_define', dict(rtu_id=1, pnt_no=1, value=0))
        queue, ready = Queue(), threading.Event()
        flushes, failures = [], []
        original = runtime._flush_batch
        def flush(*args, **kwargs):
            ok = original(*args, **kwargs)
            if ok and args[2]:
                flushes.append(time.perf_counter())
            return ok
        def run():
            try:
                runtime._batch_writer_entry(str(store.db_path), str(store.catalog.directory),
                                           str(Path(directory)/'logs'), queue, .1, 500, ready)
            except Exception as exc:
                failures.append(repr(exc))
        with patch.object(runtime, '_flush_batch', side_effect=flush):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            assert ready.wait(20), failures
            started = time.perf_counter()
            for i in range(40):
                queue.put(dict(op='update', table='fes_yc_define', key=dict(rtu_id=1,pnt_no=1), values=dict(value=i+1)))
                time.sleep(.02)
            queue.put(runtime.BATCH_WRITER_STOP)
            thread.join(15)
            assert not thread.is_alive() and not failures, failures
        assert queue.unfinished_tasks == 0
        assert store.get_row('fes_yc_define', [1,1])['value'] == 40
        result['continuous_writer'] = dict(messages=40, spacing_ms=20, interval_ms=100,
            first_commit_ms=round((flushes[0]-started)*1000, 3), batches=len(flushes), final_value=40)
        store.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

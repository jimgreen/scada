"""Probe whether replacing list_rows' count+page queries with one window query helps."""
from __future__ import annotations
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scdfe_c.web.seed_protocol_test_data import seed_protocol_test_data
from scdfe_c.web.service import ScadaStore


def run(connection, table, limit, offset, repeats, window):
    started = time.perf_counter()
    result = None
    for _ in range(repeats):
        if window:
            rows = connection.execute(
                f'SELECT *, COUNT(*) OVER() AS __total FROM "{table}" '
                f'ORDER BY "rtu_id" ASC, "pnt_no" ASC LIMIT ? OFFSET ?',
                (limit, offset),
            ).fetchall()
            total = rows[0]['__total'] if rows else connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
        else:
            total = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            rows = connection.execute(
                f'SELECT * FROM "{table}" ORDER BY "rtu_id" ASC, "pnt_no" ASC LIMIT ? OFFSET ?',
                (limit, offset),
            ).fetchall()
        result = (int(total), len(rows), dict(rows[0]) if rows else None)
    return (time.perf_counter() - started) * 1000, result


def main():
    results=[]
    with tempfile.TemporaryDirectory(prefix='scada-list-probe-') as folder:
        store=ScadaStore(Path(folder)/'x.db')
        seed_protocol_test_data(store, replicas=10)
        with store.connection() as c:
            for table in ('fes_rtu_define','fes_yc_define','fes_yx_define'):
                for limit,offset in ((50,0),(50,500),(500,0),(500,5000)):
                    for repeat in range(5):
                        order=('before','after') if repeat%2==0 else ('after','before')
                        for variant in order:
                            ms,result=run(c,table,limit,offset,100,variant=='after')
                            results.append({'table':table,'limit':limit,'offset':offset,'repeat':repeat+1,'variant':variant,'ms':ms,'result':result})
                            print(json.dumps(results[-1]),flush=True)
    med={}
    for row in results:
        key=f"{row['table']}:{row['limit']}:{row['offset']}"
        med.setdefault(key,{})
        med[key].setdefault(row['variant'],[]).append(row['ms'])
    med={k:{v:statistics.median(vals) for v,vals in d.items()} for k,d in med.items()}
    Path('output/database_list_round10_probe.json').write_text(json.dumps({'medians_ms':med,'results':results},indent=2)+'\n')
main()

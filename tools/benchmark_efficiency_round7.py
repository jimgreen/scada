"""Real S monitor HTTP comparisons on a temporary database, without transports."""
from pathlib import Path
import hashlib
import http.client
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scdfe_s.web.server import make_server, ScadaHandler

BASELINE = '0b0f5d6'
REPORT = ROOT/'reports/scdfe_efficiency_20261113_round7'


def main():
    module = types.ModuleType('scdfe_s.web._round7_before')
    module.__file__ = str(ROOT/'scdfe_s/web/server.py')
    sys.modules[module.__name__] = module
    exec(compile(subprocess.check_output(['git', 'show', f'{BASELINE}:scdfe_s/web/server.py'], cwd=ROOT), module.__file__, 'exec'), module.__dict__)
    old, new = module.ScadaHandler._monitor_control, ScadaHandler._monitor_control
    results = []
    for count in (1, 60, 300):
        with tempfile.TemporaryDirectory(prefix='scada-monitor-') as temporary:
            server = make_server('127.0.0.1', 0, db_path=Path(temporary)/'s.sqlite3')
            store = server.store
            with store.connection() as con:
                con.executemany('INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type,name) VALUES(?,?,?,?)',
                    [(n, n, ('iec104','modbus-tcp','can')[(n-1)%3], f'RTU{n}') for n in range(1, count+1)])
            services = server.channel_services()
            sql = []
            with store.connection() as con: con.set_trace_callback(sql.append)
            thread = threading.Thread(target=server.serve_forever); thread.start()
            samples = [[], []]; evidence = []
            def request():
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=20)
                try:
                    connection.request('GET', '/api/monitor/communications')
                    response = connection.getresponse(); value = json.loads(response.read())
                    assert response.status == 200, value
                    return value
                finally: connection.close()
            try:
                generation = 0
                for repeat in range(-2, 15):
                    for index in ((0, 1) if repeat%2 else (1, 0)):
                        generation += 1
                        for service in services.values(): service._rx_count = generation
                        sql.clear()
                        with patch.object(ScadaHandler, '_monitor_control', (old,new)[index]):
                            start = time.perf_counter_ns(); value = request(); elapsed = (time.perf_counter_ns()-start)/1e6
                        assert value['total'] == count
                        assert all(item['rx_count']==generation for item in value['items'])
                        fresh = all(item['runtime_status'] and item['runtime_status']['rx_count']==generation for item in value['items'])
                        if index: assert fresh
                        if repeat >= 0: samples[index].append(elapsed)
                        if repeat == 0: evidence.append(dict(variant=('before','after')[index], commits=sql.count('COMMIT'),
                            relation_reads=sum(q.startswith('SELECT channel_id, name FROM fes_rtu_define') for q in sql), same_refresh_diagnostics=fresh))
                a,b = map(statistics.median, samples)
                results.append(dict(channels=count, before_ms=a, after_ms=b, reduction_percent=100*(1-b/a), samples_ms=samples, evidence=evidence))
            finally:
                server.shutdown(); thread.join(3); server.server_close()
    files = ['scdfe_s/web/server.py','scdfe_s/web/iec104_service.py','scdfe_s/web/protocol_service.py']
    data = dict(baseline=BASELINE, results=results,
        source_sha256_lf={f:hashlib.sha256((ROOT/f).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for f in files},
        scope='Actual HTTP API, private SQLite WAL database; mixed IEC104/Modbus TCP/CAN services are stopped. No field transport or commands. 15 alternating pairs after two warmups; same DB and connection pool.')
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT/'backend.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps([{k:v for k,v in r.items() if k!='samples_ms'} for r in results],ensure_ascii=False))


if __name__ == '__main__': main()

"""Compare committed code on private SQLite files and Modbus TCP loopback."""
import hashlib
import json
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import runtime_status as current
from scdfe_c.web.service import ScadaStore
from server.modbus_trans_server import ModbusTcpTransServer
from pymodbus.client import ModbusTcpClient

BASELINE = '4f9aed9'
REPORT = ROOT/'reports/scdfe_efficiency_20261113_round6'


def historical(name, file):
    module = types.ModuleType(name)
    module.__file__ = str(ROOT/file)
    sys.modules[name] = module
    source = subprocess.check_output(['git', 'show', f'{BASELINE}:{file}'], cwd=ROOT)
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


def compare(before, after, repeats=21):
    samples = [[], []]
    for repeat in range(-2, repeats):
        for index in ((0, 1) if repeat % 2 else (1, 0)):
            start = time.perf_counter_ns()
            (before, after)[index]()
            elapsed = (time.perf_counter_ns()-start)/1e6
            if repeat >= 0:
                samples[index].append(elapsed)
    a, b = map(statistics.median, samples)
    return dict(before_ms=a, after_ms=b, reduction_percent=100*(1-b/a), samples_ms=samples)


def main():
    old = historical('core._round6_before', 'core/runtime_status.py')
    old_modbus = historical('server._round6_before', 'server/modbus_trans_server.py')
    results = []
    with tempfile.TemporaryDirectory(prefix='scada-round6-') as directory:
        store = ScadaStore(Path(directory)/'test.sqlite3', connection_pool_size=1)
        try:
            with store.connection() as con:
                con.executemany('INSERT INTO fes_channel_runtime_status(service_name,channel_id,state,diagnostic_json) VALUES(?,?,?,?)',
                    [('scdfe_c', n, 'running', '{"protocol":"modbus-tcp","rx":100}') for n in range(1, 5001)])
            for count in (50, 5000):
                ids = list(range(count, 0, -1))*2
                functions = [lambda m=m: m.runtime_status_rows(store, service_name='scdfe_c', channel_ids=ids) for m in (old, current)]
                assert functions[0]() == functions[1]()
                results.append(dict(name=f'diagnostics_{count}_channels_duplicate_selection', **compare(*functions)))
            writers = [module.RuntimeStatusStore(store, 'scdfe_c', write_interval_ms=0) for module in (old, current)]
            generation = 0
            def write(writer):
                nonlocal generation
                generation += 1
                with store.connection() as con:
                    con.execute('BEGIN IMMEDIATE')
                    for n in range(1, 501):
                        writer.upsert(n, dict(state='running', running=1, rx_count=generation,
                            sampled_at=generation, diagnostic_json={'rtt_ms':3}), force=True, connection=con)
                    con.commit()
            result = compare(lambda:write(writers[0]), lambda:write(writers[1]))
            with store.connection() as con:
                assert con.execute('SELECT COUNT(*) FROM fes_channel_runtime_status WHERE rx_count=?', (generation,)).fetchone()[0] == 500
            results.append(dict(name='500_channel_status_writes_one_transaction', **result))
        finally:
            store.close()
    servers, peers, calls = [], [], [0, 0]
    try:
        for index, cls in enumerate((old_modbus.ModbusTcpTransServer, ModbusTcpTransServer)):
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            def accepted(*args, i=index):
                calls[i] += 1
                return True
            server = cls(host='127.0.0.1', port=port, points=[dict(kind='yk', pnt_no=n+1,
                address=n, register_area='coil', data_type='bool') for n in range(1968)], command_handler=accepted)
            servers.append(server)
            server.start()
            client = ModbusTcpClient('127.0.0.1', port=port, timeout=3, retries=0)
            peers.append(client)
            assert client.connect()
        toggles = [False, False]
        def send(index):
            toggles[index] = not toggles[index]
            before = calls[index]
            response = peers[index].write_coils(0, [toggles[index]]*1968, device_id=1)
            assert not response.isError(), response
            assert calls[index]-before == 1968
        result = compare(lambda:send(0), lambda:send(1))
        for index, peer in enumerate(peers):
            response = peer.read_coils(0, count=1968, device_id=1)
            assert not response.isError() and response.bits[:1968] == [toggles[index]]*1968
        results.append(dict(name='modbus_fc15_1968_coils_tcp_round_trip', **result))
    finally:
        for peer in peers: peer.close()
        for server in servers: server.close()
    files = ['core/runtime_status.py', 'server/modbus_trans_server.py', 'scdfe_c/web/static/app.js',
             'scdfe_s/web/static/monitor.js', 'scdfe_t/web/static/processes.js']
    data = dict(baseline=BASELINE, python=platform.python_version(), platform=platform.platform(), results=results,
        source_sha256_lf={name:hashlib.sha256((ROOT/name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() for name in files},
        scope='Private SQLite and localhost Modbus TCP; callback accepts in memory. No writer IPC, physical device action, serial throughput or CAN hardware result. 21 alternating paired samples after two warmups.')
    REPORT.mkdir(exist_ok=True, parents=True)
    (REPORT/'backend.json').write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps([{k:v for k,v in row.items() if k!='samples_ms'} for row in results], ensure_ascii=False))


if __name__ == '__main__':
    main()

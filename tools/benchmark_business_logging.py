"""Compare full-detail logging and protocol/database work against a Git revision.

Uses only temporary logs/databases and loopback TCP. No records are sampled or
disabled. Baseline modules come from git, including their own log sanitizer.
"""
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
import argparse
import hashlib
import json
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scdfe_c
import core.communication_log as communication
from client import ModbusTcpMaster
from server.modbus_trans_server import ModbusTcpTransServer
from tests.test_protocol_interface_contracts import free_port
from scdfe_c.web.log_query import search_logs

SOURCES = ('core/scdfe_control.py', 'core/communication_log.py', 'scdfe_c/__init__.py')


def baseline(ref):
    modules, hashes = [], {}
    for i, path in enumerate(SOURCES):
        source = subprocess.check_output(['git', 'show', f'{ref}:{path}'], cwd=ROOT)
        hashes[path] = hashlib.sha256(source.replace(b'\r\n', b'\n')).hexdigest()
        name = f'scdfe_c._logging_benchmark_before_{i}'
        module = ModuleType(name)
        module.__file__, module.__package__ = str(ROOT/path), 'scdfe_c'
        sys.modules[name] = module
        exec(compile(source, module.__file__, 'exec'), module.__dict__)
        modules.append(module)
    old_control, old_communication, old_runtime = modules
    old_runtime.sanitize_log_message = old_control.sanitize_log_message
    return (old_runtime, old_communication), hashes


def normalized_logs(root):
    lines = []
    for path in sorted(root.rglob('*.log')):
        contexts = set()
        for line in path.read_text(encoding='utf8').splitlines():
            line = re.sub(r'^\d\d:\d\d:\d\d ', '', line)
            definition = re.match(r'\[日志\] 日志上下文 @(\S+) ',line)
            if definition:
                contexts.add(definition[1])
                continue
            reference = re.match(r'\[日志\] @(\S+) ',line)
            if reference:
                assert reference[1] in contexts, 'missing context definition'
                line = '[日志] ' + line[reference.end():]
            line = re.sub(r'事件时刻=\S+|批次=\S+|源刷新时刻=\S+|写库批次=\S+', '<time/id>', line)
            # Fixed identities intentionally move to context definitions. The
            # unit tests verify their exact mapping; compare event payloads here.
            line = re.sub(r'(?:线程|组件|RTU编号|通道编号|规约)=\S+', '',line)
            line = ' '.join(line.split())
            lines.append(line)
    return lines


def log_bytes(root):
    return sum(path.stat().st_size for path in root.rglob('*.log'))


def changed_points(runtime, com, root):
    logger = runtime.ScdfeLogger(root, process_name='benchmark')
    business = com.CommunicationLog(logger.business_lines, 组件='C', RTU编号=1, 通道编号=1)
    rows = [dict(四遥='YC', 点号=i, 旧值=i, 新值=i+.5, 新质量=i%2) for i in range(1000)]
    with logger.session():
        # Open the hourly file before measuring steady state.
        logger.normal('warmup')
        start = time.perf_counter_ns()
        business.rows('实时更新已入库', rows)
        elapsed = (time.perf_counter_ns()-start)/1e6
    lines = normalized_logs(root)[1:]
    assert len(lines) == 1000
    return elapsed, dict(records=len(lines), digest=hashlib.sha256('\n'.join(lines).encode()).hexdigest(),
                         stored_bytes=log_bytes(root))


def log_search(runtime, com, root):
    logger = runtime.ScdfeLogger(root,process_name='scdfe_c-rtu-1')
    business = com.CommunicationLog(logger.business_lines,组件='C',RTU编号=1,通道编号=8)
    with logger.session():
        business.rows('实时变化',({'点号':i,'值':i+.5,'质量':0} for i in range(10000)))
    filename = next(root.rglob('*.log')).relative_to(root).as_posix()
    query = dict(filename=filename,q='业务事件',limit='1000',record='runtime')
    records, cursor, pages = [],None,0
    start = time.perf_counter_ns()
    while True:
        result = search_logs(root,{**query,**({'cursor':cursor} if cursor else {})})
        records.extend(result['items']);pages+=1
        cursor = result['next_cursor']
        if not cursor: break
    elapsed = (time.perf_counter_ns()-start)/1e6
    points = [int(re.search(r'点号=(\d+)',row['text'])[1]) for row in records]
    assert points == list(reversed(range(10000)))
    return elapsed,dict(records=len(records),pages=pages,stored_bytes=log_bytes(root))


@dataclass
class Sample:
    kind: str
    point_no: int
    value: float
    quality: int = 0


class Queue:
    def __init__(self):
        self.messages, self.acknowledged = [], 0

    def put(self, message, **kwargs):
        self.messages.append(message)

    def task_done(self):
        self.acknowledged += 1


def database_pipeline(runtime, com, root):
    root.mkdir(parents=True)
    con = sqlite3.connect(root/'isolated.sqlite3', isolation_level=None)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    for kind in ('yc', 'yx'):
        extra = ',raw_value REAL,raw_status INT,raw_time REAL' if kind == 'yc' else ''
        con.execute(f'CREATE TABLE fes_{kind}_define(rtu_id INT,pnt_no INT,value REAL,status INT,'
                    f'refresh_time REAL,_version INT DEFAULT 1{extra},PRIMARY KEY(rtu_id,pnt_no))')
        con.executemany(f'INSERT INTO fes_{kind}_define(rtu_id,pnt_no) VALUES(?,?)',
                        ((r,n) for r in range(1,7) for n in range(1,101)))
    queue = Queue()
    workers = [runtime.ChannelWorker(None, runtime.ChannelSpec(r,r,'custom',None,None),
               log_dir=root/'logs',writer_queue=queue) for r in range(1,7)]
    logger = runtime.ScdfeLogger(root/'logs', process_name='scdfe_c-db-writer')
    samples = [[Sample(kind,n,(n+cycle if kind=='yc' else (n+cycle)%2),cycle%2)
                for kind in ('yc','yx') for n in range(1,101)] for cycle in range(3)]
    try:
        with ExitStack() as stack:
            for worker in workers:
                stack.enter_context(worker.logger.session())
                worker.logger.normal('warmup')
            stack.enter_context(logger.session()); logger.normal('warmup')
            started = time.perf_counter_ns()
            for cycle in samples:
                for worker in workers:
                    assert sum(worker._sync_measurements(cycle)) == 200
                pending, queue.messages = queue.messages, []
                assert runtime._flush_batch(None,logger,pending,queue,con) is True
            elapsed = (time.perf_counter_ns()-started)/1e6
        assert queue.acknowledged == 18
        final = []
        for kind in ('yc','yx'):
            rows = con.execute(f'SELECT rtu_id,pnt_no,value,status,_version FROM fes_{kind}_define ORDER BY rtu_id,pnt_no').fetchall()
            for r,n,v,s,version in rows:
                assert v == (n+2 if kind=='yc' else (n+2)%2) and s == 0 and version == 4
            final.extend(rows)
        lines = normalized_logs(root/'logs')
        assert sum('实时更新已入写库队列' in line for line in lines) == 3600
        assert sum('写库事务已提交' in line for line in lines) == 18
        return elapsed, dict(rtus=6,updates=3600,committed_receipts=18,records=len(lines),
                             final_digest=hashlib.sha256(repr(final).encode()).hexdigest(),
                             stored_bytes=log_bytes(root/'logs'))
    finally:
        con.close()


def modbus(runtime, com, root):
    with ExitStack() as stack:
        logger = runtime.ScdfeLogger(root,process_name='benchmark')
        stack.enter_context(logger.session())
        business = com.CommunicationLog(logger.business_lines,组件='benchmark')
        server = ModbusTcpTransServer(port=free_port(),points=[dict(kind='yc',pnt_no=i+1,
                    register_area='input',address=i,data_type='int16') for i in range(16)])
        sink = business.wrap_packet(lambda *args:None)
        server.set_packet_handler(sink)
        server.start();stack.callback(server.close)
        peer = ModbusTcpMaster('127.0.0.1',server.port)
        peer.set_packet_handler(sink)
        peer.connect();stack.callback(peer.close)
        for _ in range(10): assert peer.read_input_registers(0,16) == [0]*16
        before = len(normalized_logs(root))
        start = time.perf_counter_ns()
        for _ in range(100): assert peer.read_input_registers(0,16) == [0]*16
        elapsed = (time.perf_counter_ns()-start)/1e6/100
        lines = normalized_logs(root)[before:]
        assert sum('Modbus请求开始' in line for line in lines) == 100
        assert sum('Modbus读取完成' in line for line in lines) == 100
        assert sum('业务事件 Modbus请求 ' in line for line in lines) == 100
        assert sum('业务事件 Modbus响应 ' in line for line in lines) == 100
        return elapsed, dict(requests=100,registers=16,records=len(lines),stored_bytes=log_bytes(root))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref',default='fb8561f')
    parser.add_argument('--repeat',type=int,default=7)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.repeat < 3:
        parser.error('--repeat must be at least 3')
    before, hashes = baseline(args.baseline_ref)
    variants = dict(before=before,after=(scdfe_c,communication))
    report = dict(baseline=args.baseline_ref,python=sys.version,platform=platform.platform(),
                  scope='Full business logs enabled in both variants; temporary files, SQLite WAL and local TCP only. '
                        'Database case includes production producer + commit, excludes IPC and fixture setup. '
                        'Live services continue running; results include host contention.',
                  before_sha256=hashes,
                  benchmark_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  after_sha256={p:hashlib.sha256((ROOT/p).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in SOURCES},
                  results={})
    with tempfile.TemporaryDirectory(prefix='scada-log-cost-') as directory:
        for name,case in [('1000_changes_ms',changed_points),('6_rtu_database_ms',database_pipeline),
                          ('modbus_tcp_request_ms',modbus),('10000_log_query_ms',log_search)]:
            samples = dict(before=[],after=[]); checks = {}; sizes = dict(before=[],after=[]); pages = {}
            for repeat in range(-1,args.repeat):
                for variant in (('before','after') if repeat%2 else ('after','before')):
                    runtime,com = variants[variant]
                    with patch.dict(sys.modules,{'core.communication_log':com}):
                        elapsed,check = case(runtime,com,Path(directory)/f'{name}-{repeat}-{variant}')
                    checks[variant] = check
                    size = check.pop('stored_bytes')
                    if 'pages' in check:
                        pages[variant] = check.pop('pages')
                    if repeat >= 0:
                        samples[variant].append(elapsed)
                        sizes[variant].append(size)
                assert checks['before'] == checks['after'], (name,checks)
            medians = {v:statistics.median(values) for v,values in samples.items()}
            median_bytes = {v:statistics.median(values) for v,values in sizes.items()}
            result = dict(median_ms=medians,reduction_percent=100*(1-medians['after']/medians['before']),
                          samples_ms=samples,verified=checks['after'],median_log_bytes=median_bytes,
                          size_reduction_percent=100*(1-median_bytes['after']/median_bytes['before']),
                          log_bytes_samples=sizes,query_pages=pages)
            report['results'][name]=result
            print(json.dumps({name:result},ensure_ascii=False),flush=True)
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')


if __name__ == '__main__': main()

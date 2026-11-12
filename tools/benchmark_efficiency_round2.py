"""Reproducible local comparisons against the frozen working-tree baseline.

Uses temporary SQLite files, real c104 point objects without listening sockets,
and actual Modbus/CAN codecs. These are hot-path timings, not hardware throughput.
"""
from contextlib import contextmanager, ExitStack
from importlib.machinery import SourceFileLoader
import importlib.util
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import socket
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace, ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = ROOT / 'reports/scdfe_efficiency_20261112_round2'


def frozen(name, file):
    path = REPORT / 'before' / (file.replace('/', '__') + '.snapshot')
    expected = json.loads((REPORT/'baseline.json').read_text('utf-8-sig'))[file]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def measure(fn, repeats=31):
    for _ in range(3): fn()
    times=[]
    for _ in range(repeats):
        start=time.perf_counter_ns(); fn(); times.append((time.perf_counter_ns()-start)/1e6)
    return dict(median_ms=statistics.median(times),p95_ms=sorted(times)[int(.95*(len(times)-1))],samples_ms=times)


def paired_measure(functions, repeats=41):
    """Alternate order to control CPU/disk drift for small writer differences."""
    for fn in functions:
        for _ in range(3): fn()
    times=[[],[]]
    for repeat in range(repeats):
        for index in ((0,1) if repeat%2 else (1,0)):
            start=time.perf_counter_ns(); functions[index](); times[index].append((time.perf_counter_ns()-start)/1e6)
    return [dict(median_ms=statistics.median(t),p95_ms=sorted(t)[int(.95*(len(t)-1))],samples_ms=t) for t in times]


class Store:
    def __init__(self, con):
        self.con=con
        self.db_path=Path(con.execute('PRAGMA database_list').fetchone()[2])
    @contextmanager
    def connection(self):
        with self.con: yield self.con


def database(path):
    con=sqlite3.connect(path,isolation_level=None)
    con.execute('PRAGMA journal_mode=WAL'); con.execute('PRAGMA synchronous=NORMAL')
    for kind in ('yc','yx','yk','yt'):
        con.execute(f'CREATE TABLE fes_{kind}_define(rtu_id INT,pnt_no INT,value REAL,status INT,'
                    'refresh_time INT,PRIMARY KEY(rtu_id,pnt_no))')
    return con


def main():
    import scdfe_c
    from scdfe_t.trans import TransRuntime
    from scdfe_s.web.iec104_service import IEC104Service
    from server.iec104_server import IEC104Server
    from client.can import CanMaster, CanFrame
    from server.modbus_mapped_server import ModbusMappedServer
    before_t=frozen('scdfe_t._round2_before','scdfe_t/trans.py').TransRuntime
    before_c=frozen('scdfe_c._round2_before','scdfe_c/__init__.py')
    before_s=frozen('_round2_s_before','scdfe_s/web/iec104_service.py').IEC104Service
    before_iec=frozen('_round2_iec_before','server/iec104_server.py').IEC104Server
    before_can=frozen('_round2_can_before','client/can.py').CanMaster
    package=ModuleType('_round2_server'); package.__path__=[str(ROOT/'server')]
    sys.modules[package.__name__]=package
    frozen('_round2_server.modbus_trans_server','server/modbus_trans_server.py')
    before_modbus=frozen('_round2_server.modbus_mapped_server','server/modbus_mapped_server.py').ModbusMappedServer
    results=[]
    def add(name,before,after,**detail):
        row=dict(name=name,before=before,after=after,**detail)
        row['reduction_percent']=(1-after['median_ms']/before['median_ms'])*100
        results.append(row); print(json.dumps({k:v for k,v in row.items() if k not in ('before','after')},ensure_ascii=False),flush=True)

    with tempfile.TemporaryDirectory(prefix='scada-round2-') as tmp:
        con=database(Path(tmp)/'sources.sqlite3')
        try:
            con.execute('BEGIN')
            con.executemany('INSERT INTO fes_yc_define VALUES(?,?,?,?,?)',
                            [(r,n,r*100+n,r%2,1000+n) for r in range(1,201) for n in range(1,51)])
            con.commit()
            points=[SimpleNamespace(kind='yc',pnt_no=(r-1)*50+n,source_rtu_id=r,source_pnt_no=n)
                    for r in range(1,201) for n in range(1,51)]
            timings=[]; scans=[]; values=[]
            for cls in (before_t,TransRuntime):
                runtime=object.__new__(cls); runtime.store=Store(con)
                plan=runtime._source_plan(points)
                scans.append(len(plan)); values.append(runtime._read_sources(plan,True,True))
                timings.append(measure(lambda:runtime._read_sources(plan,True,True)))
            assert values[0]==values[1] and len(values[0])==10000
            add('T source read: 200 RTUs x 50 points',*timings,select_calls=scans,points=10000)
            small=[p for p in points if p.source_pnt_no<=5]
            timings=[]; scans=[]; values=[]
            for cls in (before_t,TransRuntime):
                runtime=object.__new__(cls); runtime.store=Store(con)
                plan=runtime._source_plan(small)
                scans.append(len(plan)); values.append(runtime._read_sources(plan,True,True))
                timings.append(measure(lambda:runtime._read_sources(plan,True,True)))
            assert values[0]==values[1] and len(values[0])==1000
            add('T source read: 200 RTUs x 5 points',*timings,select_calls=scans,points=1000)
        finally: con.close()

        timings=[]; native_reads=[]
        for variant,(service_cls,server_cls) in enumerate(((before_s,before_iec),(IEC104Service,IEC104Server))):
            con=database(Path(tmp)/f'iec-{variant}.sqlite3')
            con.execute('BEGIN'); con.executemany('INSERT INTO fes_yc_define VALUES(1,?,1,0,0)',[(n,) for n in range(1,4001)]); con.commit()
            native=server_cls(rtu_id=1,ip='127.0.0.1')
            for n in range(1,4001): native.add_point('yc',n,value=1)
            class LocalNative:
                running=True; active_connection_count=0; rtu_id=1
                def __getattr__(self,name): return getattr(native,name)
            service=object.__new__(service_cls)
            service._lock=threading.RLock(); service._server=LocalNative(); service._database_samples={}
            sync=lambda:service._sync_database_values(con)
            assert sync()==0
            timings.append(measure(sync))
            native_reads.append(4000 if variant==0 else 0)
            con.close(); native.stop()
        add('S IEC104: unchanged 4000 points after other RTU commit',*timings,native_point_reads=native_reads)

        def writer_fixture(variant,runtime,stack):
            con=sqlite3.connect(Path(tmp)/f'writer-{variant}.sqlite3',isolation_level=None)
            stack.callback(con.close)
            con.execute('PRAGMA journal_mode=WAL'); con.execute('PRAGMA synchronous=NORMAL')
            con.execute('CREATE TABLE p(rtu_id INT,pnt_no INT,value REAL,_version INT DEFAULT 0,PRIMARY KEY(rtu_id,pnt_no))')
            con.execute('BEGIN'); con.executemany('INSERT INTO p(rtu_id,pnt_no) VALUES(?,?)',[(r,n) for r in range(80) for n in range(20)]); con.commit()
            messages=[dict(op='batch',_preserve_versions=True,groups=[dict(table='p',key_names=('rtu_id','pnt_no'),value_names=('value',),_unique_rows=True,rows=[(r*100+n,r,n) for n in range(20)])]) for r in range(80)]
            class CountConnection:
                calls=0
                def __getattr__(self,name): return getattr(con,name)
                def executemany(self,sql,params): self.calls+=1; return con.executemany(sql,params)
            counted=CountConnection(); queue=SimpleNamespace(task_done=lambda:None)
            logger=SimpleNamespace(error=lambda msg:(_ for _ in ()).throw(AssertionError(msg)),alarm=print)
            def flush(): assert runtime._flush_batch(None,logger,messages,queue,counted)
            return flush,counted,con
        with ExitStack() as stack:
            fixtures=[writer_fixture(i,r,stack) for i,r in enumerate((before_c,scdfe_c))]
            timings=paired_measure([f[0] for f in fixtures])
            counts=[f[1].calls//44 for f in fixtures]
            final=[f[2].execute('SELECT * FROM p ORDER BY rtu_id,pnt_no').fetchall() for f in fixtures]
            assert final[0]==final[1]
        add('C writer: 80 RTUs x 20 version-preserving updates',*timings,executemany_calls=counts,rows=1600)

        timings=[]; outputs=[]
        mappings=[dict(kind='yc',pnt_no=n+1,frame_id=0x123,byte_offset=(n%8)*4,bit_length=32,
                       value_type='float',byte_order='big',scale=.5,offset=3) for n in range(32)]
        frames=[CanFrame(0x123,b'\x3f\x80\x00\x00'*8,is_fd=True,timestamp=1) for _ in range(200)]
        for cls in (before_can,CanMaster):
            master=cls(interface='virtual',channel='round2-codec',rtu_id=1); master.configure(mappings)
            timings.append(measure(lambda:master._decode_frames(frames)))
            outputs.append([tuple(v) for v in master._decode_frames(frames)]); master.close()
        assert outputs[0]==outputs[1]
        add('CAN FD: 200 frames x 32 mapped signals',*timings,decoded_values=6400)

        timings=[]; outputs=[]; encodes=[]; active_timings=[]
        for variant,cls in enumerate((before_modbus,ModbusMappedServer)):
            con=database(Path(tmp)/f'modbus-{variant}.sqlite3')
            con.execute('BEGIN'); con.executemany('INSERT INTO fes_yc_define VALUES(1,?,0,0,0)',[(n,) for n in range(1,1001)]); con.commit()
            with socket.socket() as port_socket:
                port_socket.bind(('127.0.0.1',0)); port=port_socket.getsockname()[1]
            engine=cls(store=Store(con),rtu_id=1,port=port,points=[dict(kind='yc',pnt_no=n,address=(n-1)*4,data_type='float32',register_area='input') for n in range(1,1001)])
            updates=[('yc',n,n/8) for n in range(1,1001)]
            count=[0]; encode=engine._encode
            def counted_encode(row,value): count[0]+=1; return encode(row,value)
            engine._encode=counted_encode
            def publish(): engine.publish_updates(engine.prepare_updates(updates))
            timings.append(measure(publish)); encodes.append(count[0]//34)
            engine.start()
            active_timings.append(measure(publish))
            from client.modbus_tcp import ModbusTcpMaster
            peer=ModbusTcpMaster('127.0.0.1',port)
            try:
                peer.connect()
                assert peer.read_input_registers(0,2)==engine._encode(engine._mappings[('yc',1)],1/8)
            finally: peer.close()
            outputs.append(engine.snapshot()); engine.close(); con.close()
        assert outputs[0]==outputs[1]
        add('S Modbus: prepare and publish 1000 points before start',*timings,encode_calls=encodes)
        add('S Modbus: publish 1000 points to active TCP server',*active_timings,wire_readback='loopback TCP read of first float32 verified')

    files=json.loads((REPORT/'baseline.json').read_text('utf-8-sig'))
    report=dict(timestamp=time.strftime('%Y-%m-%d %H:%M:%S'),python=sys.version,platform=platform.platform(),
                baseline_sha256=files,current_sha256={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files},
                scope='Temporary SQLite WAL; native c104 state; local Modbus/CAN codecs. No field hardware or sustained-load claim.',results=results)
    (REPORT/'runtime_comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__': main()

"""Twelve independent writers; C/S HTTP lists and T multi-table scans see committed frames."""
import json,sys,tempfile,threading,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen
out=Path(__file__).resolve().parent
sys.path.insert(0,str(out.parents[1]))
from scdfe_c.web.service import ScadaStore
from scdfe_c.web.server import make_server as cweb
from scdfe_s.web.server import make_server as sweb
from scdfe_t.trans import TransPoint,TransRuntime

def run():
    with tempfile.TemporaryDirectory(prefix='scdfemulti-wal-') as tmp:
        db=Path(tmp)/'fixture.sqlite3'
        store=ScadaStore(db,connection_pool_size=12)
        tr=TransRuntime(db); tr.store.connection_pool_size=1
        webs=[factory('127.0.0.1',0,db_path=db) for factory in (cweb,sweb)]
        for srv in webs:threading.Thread(target=srv.serve_forever,daemon=True).start()
        ids=tuple(range(1,13)); plans={}
        try:
            for cid in ids:
                store.create_row('fes_rtu_define',dict(rtu_id=cid,channel_id=cid,channel_type='iec104'))
                for k,n in [('yc',1),('yc',2),('yt',1)]:
                    store.create_row(f'fes_{k}_define',dict(rtu_id=cid,pnt_no=n,value=0))
                plans[cid]=tr._source_plan([TransPoint.from_row(dict(channel_id=cid,source_rtu_id=cid,source_pnt_no=1,pnt_no=1,kind=k)) for k in ('yc','yt')])
            gate=threading.Barrier(15); finish=threading.Event()
            iterations=80
            def writer(cid):
                gate.wait(8)
                for g in range(1,iterations+1):
                    v=1000*cid+g
                    changes=[('fes_yc_define',[cid,1],dict(value=v)),('fes_yc_define',[cid,2],dict(value=-v)),('fes_yt_define',[cid,1],dict(value=-v))]
                    assert store.update_rows(changes)==3
            def read(source):
                gate.wait(8)
                count=0
                while not finish.is_set() or count<len(ids):
                    cid=ids[count%len(ids)]
                    if source=='t':
                        sample=tr._read_sources(plans[cid],False)
                        pair=(sample['yc',1], sample['yt',1])
                    else:
                        srv=webs[0 if source=='c' else 1]
                        path=f'rtus/{cid}/points/yc' if source=='c' else f'channels/{cid}/points/yc'
                        with urlopen(f'http://127.0.0.1:{srv.server_port}/api/{path}',timeout=10) as response:
                            rows=json.load(response)['items']
                        assert all(row['rtu_id']==cid for row in rows)
                        pair=tuple(r['value'] for r in sorted(rows,key=lambda r:r['pnt_no']))
                    assert len(pair)==2 and pair[0]==-pair[1],(source,cid,pair)
                    assert pair[0]==0 or 1000*cid <pair[0]<=1000*cid+iterations
                    count+=1
                return count
            start=time.perf_counter()
            with ThreadPoolExecutor(max_workers=15) as pool:
                readers={src:pool.submit(read,src) for src in 'cst'}
                try:list(pool.map(writer,ids))
                finally:finish.set()
                checked={src:f.result() for src,f in readers.items()}
            for cid in ids:
                for k,n,sign in [('yc',1,1),('yc',2,-1),('yt',1,-1)]:
                    row=store.get_row(f'fes_{k}_define',[cid,n])
                    assert row['value']==sign*(1000*cid+iterations) and row['_version']==1+iterations
            return dict(writers=len(ids),committed_batches=len(ids)*iterations,point_updates=len(ids)*iterations*3,
                        consistent_reads=checked,seconds=round(time.perf_counter()-start,3))
        finally:
            for srv in webs:srv.shutdown();srv.server_close()
            tr.store.close();store.close()
if __name__=='__main__':
    report=run();(out/'contention.json').write_text(json.dumps(report,indent=2));print(report)

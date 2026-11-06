from pathlib import Path
import json, statistics, sys, tempfile, time
folder=Path(__file__).resolve().parent
sys.path.insert(0,str(folder.parents[1]))
from scdfe_c.web.service import ScadaStore
from core.web_logs import log_files
from datetime import datetime,timedelta
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp); store=ScadaStore(root/'bench.sqlite3', connection_pool_size=1)
    store.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='iec104'))
    with store.connection() as con:
        con.execute('BEGIN')
        con.executemany('INSERT INTO fes_yc_define(rtu_id,pnt_no,value) VALUES(1,?,0)',[(n,) for n in range(1,257)])
        con.commit()
    for n in range(48):
        date=datetime(2026,1,1)+timedelta(hours=n)
        path=root/'logs'/date.strftime('%Y/%m/%d/%H');path.mkdir(parents=True)
        for i in range(1,11):
            (path/f'scdfe_t-channel-{i}-{date:%Y%m%d-%H}.log').write_text('ok',encoding='utf-8')
    def batch():
        assert store.update_rows([('fes_yc_define',[1,n],{'value':n}) for n in range(1,257)])==256
    def logs():
        assert log_files(root/'logs','t',{})['total']==480
    def measure(fn):
        fn(); samples=[]
        for _ in range(9):
            start=time.perf_counter();fn();samples.append((time.perf_counter()-start)*1000)
        return round(statistics.median(samples),3)
    result={'update_256_ms':measure(batch),'list_480_files_ms':measure(logs)}
    store.close()
(folder/(sys.argv[1]+'.json')).write_text(json.dumps(result));print(result)

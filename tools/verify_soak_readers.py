"""Independent readers for a private tools.soak_multichannel run (pass its output directory)."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from time import monotonic, sleep, time
from urllib.parse import urlencode
from urllib.request import urlopen
from pymodbus.client import ModbusTcpClient


def verify(run):
    meta=json.loads((run/'ports.json').read_text(encoding='utf8'))
    info=json.loads((run/'events.jsonl').read_text(encoding='utf8').splitlines()[0])
    end=datetime.fromisoformat(info['at']).timestamp()+info['duration_seconds']-8
    root=Path(meta['root']);assert root.name.startswith('scada-three-web-')
    def get(f, path):
        with urlopen(f"http://127.0.0.1:{meta['web'][f]}/api/{path}",timeout=4) as r:return json.load(r)
    src=get('s','modbus-tcp-server/config')['item']['listen_port']
    forward={r['channel_id']:r['bind_port'] for r in get('t','trans/channels')['items']}
    def repeat(fn,interval):
        n=0;maximum=0;start=monotonic()
        while time()<end:
            t=monotonic();fn(n);maximum=max(maximum,monotonic()-t);n+=1;sleep(interval)
        return dict(count=n,seconds=round(monotonic()-start,2),max_ms=round(maximum*1000,2))
    def wire():
        with ModbusTcpClient('127.0.0.1',port=src,timeout=2,retries=0) as peer:
            def probe(_):
                r=peer.read_holding_registers(0,count=2,device_id=1)
                assert not r.isError() and r.registers[1]-r.registers[0]==29000,r
            return repeat(probe,.04)
    def db_reader():
        with closing(sqlite3.connect((root/'source/scada.sqlite3').as_uri()+'?mode=ro',uri=True)) as con:
            def probe(_):
                rows=list(con.execute('select pnt_no,value from fes_yc_define where rtu_id=37100 and pnt_no in (1,2) order by pnt_no'))
                assert len(rows)==2 and rows[1][1]-rows[0][1]==29000,rows
            return repeat(probe,.07)
    def reconnect():
        def probe(n):
            cid=(1,100)[n%2]
            with ModbusTcpClient('127.0.0.1',port=forward[cid],timeout=2,retries=0) as peer:
                r=peer.read_input_registers(0,count=1,device_id=1)
                assert not r.isError() and (1000<r.registers[0]<13000 if cid==1 else 30000<r.registers[0]<42000),r
        return repeat(probe,.4)
    def logs():
        matches=0;hours=set()
        def probe(n):
            nonlocal matches
            family='cst'[n%3];d=('RX','TX')[(n//3)%2]
            proc={'c':'scdfe_c-rtu-37101','s':'scdfe_s-modbus-tcp-server','t':'scdfe_t-channel-1'}[family]
            logroot=root/('source' if family=='s' else 'system')/'logs'
            file=max(logroot.rglob(proc+'-*.log'),key=lambda f:f.name)
            path='scdfe_c/logs/search' if family=='c' else 'log-files/search'
            params=dict(filename=file.relative_to(logroot).as_posix(),process=proc,record='packet',direction=d,limit=3)
            if family=='s':params['channel_id']=38100
            if family=='t':params['channel_id']=1
            data=get(family,path+'?'+urlencode(params))
            if len(data['items'])<3 or not data['next_cursor']:return  # a newly rotated file
            # Snapshot the bytes after page one. Locate its three-line anchor;
            # page two must be its immediate neighbours even as writers append.
            import re
            rx=re.compile(r'^\d\d:\d\d:\d\d \[(?:报文|日志)\] 报文 '+d+r' ')
            with file.open('rb') as f:
                size=f.seek(0,2);f.seek(max(0,size-524288))
                lines=[s for s in reversed(f.read().decode('utf8',errors='replace').splitlines()) if rx.match(s)]
            head=[v['text'] for v in data['items']]
            nxt=get(family,path+'?'+urlencode({**params,'cursor':data['next_cursor']}))
            tail=[v['text'] for v in nxt['items']]
            assert (any(lines[i:i+len(head)+len(tail)]==head+tail for i in range(len(lines))) if tail else lines[-len(head):]==head and not nxt["has_more"]), (family,d,head,tail)
            matches+=len(head)+len(tail);hours.add(str(file.parent))
        ans=repeat(probe,1.5);assert matches;return dict(**ans,matched_lines=matches,distinct_hour_directories=len(hours))
    with ThreadPoolExecutor(4) as pool:
        tasks={name:pool.submit(fn) for name,fn in [('S_wire',wire),('S_sqlite',db_reader),('T_reconnect',reconnect),('CST_log_pages',logs)]}
        result={name:f.result() for name,f in tasks.items()}
    assert all(row['count'] for row in result.values())
    (run/'independent.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run',type=Path);verify(p.parse_args().run.resolve())

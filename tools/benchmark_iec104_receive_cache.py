"""Compare preserved and current collectors after identical real TCP reception."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.metadata
from importlib.machinery import SourceFileLoader
import importlib.util
import json
import math
from pathlib import Path
import socket
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from client.iec104 import IEC104Master
from server.iec104_server import IEC104Server


def summary(values):
    ordered=sorted(values)
    return dict(samples=len(values),median_ms=round(statistics.median(values),6),
                p95_ms=round(ordered[math.ceil(.95*len(values))-1],6),max_ms=round(max(values),6))


def wait_for(predicate):
    deadline=time.monotonic()+12
    while not predicate():
        if time.monotonic()>deadline:raise AssertionError('real TCP data reception timed out')
        time.sleep(.005)


def received(peer, points, offset, quality):
    # Let native receive callbacks run before timing unchanged polling. Calling
    # the deliberately slower baseline collector here competes with callbacks
    # thousands of times and measures receive starvation instead of idle polls.
    with peer._lock:
        values = tuple(peer._received_values.values())
    return len(values) == points and all(
        s.value == s.point_no + offset and s.quality == quality for s in values)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=60);parser.add_argument('--points',type=int,nargs='+',default=[128,2048,8192])
    args=parser.parse_args()
    if args.rounds<1 or min(args.points)<1:parser.error('counts must be positive')
    loader=SourceFileLoader('_iec104_collector_before',str(args.before))
    spec=importlib.util.spec_from_loader(loader.name,loader);before=importlib.util.module_from_spec(spec)
    sys.modules[loader.name]=before;loader.exec_module(before)
    result=dict(timestamp=datetime.now(timezone.utc).isoformat(),c104=importlib.metadata.version('c104'),
                native_sha256=hashlib.sha256(Path(importlib.util.find_spec('c104._c104').origin).read_bytes()).hexdigest(),
                before_sha256=hashlib.sha256(args.before.read_bytes()).hexdigest(),
                after_sha256=hashlib.sha256((ROOT/'client/iec104.py').read_bytes()).hexdigest(),cases=[])
    for points in args.points:
        with ExitStack() as stack:
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            server=IEC104Server(ip='127.0.0.1',port=port,tick_rate_ms=50);stack.callback(server.stop)
            for no in range(1,points+1):server.add_point('yc',no,value=0)
            peers=[]; wire=[[],[]]
            for cls in (before.IEC104Master,IEC104Master):
                peer=cls('127.0.0.1',port,init='NONE');stack.callback(peer.close)
                peer.configure([dict(kind='yc',pnt_no=no) for no in range(1,points+1)]);peers.append(peer)
                capture=wire[len(peers)-1]
                peer.set_packet_handler(lambda direction,data,detail='',capture=capture:
                                        capture.append((direction,bytes(data))))
            server.start()
            for peer in peers:peer.connect(timeout=4)
            server.update_monitoring([('yc',no,float(no),0) for no in range(1,points+1)])
            try:
                wait_for(lambda:all(received(peer, points, 0, 0) for peer in peers))
            except Exception:
                result.update(passed=False,failed_points=points,initial_reception=[dict(
                    missing=[s.point_no for s in peer.collect_once() if s.value!=s.point_no or s.quality!=0],
                    received_objects=sum(b[7]&127 for direction,b in frames if direction=='RX' and len(b)>12 and not b[2]&1 and b[6]==13))
                    for peer,frames in zip(peers,wire)])
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                raise
            for peer in peers:
                for _ in range(10):peer.collect_once()
            samples=[[],[]]
            for repeat in range(args.rounds):
                for index in ((0,1) if repeat%2==0 else (1,0)):
                    begin=time.perf_counter()
                    for _ in range(10):values=peers[index].collect_once()
                    samples[index].append((time.perf_counter()-begin)*100)
                    assert len(values)==points and all(s.value==s.point_no and s.quality==0 for s in values)
            retained=peers[1].collect_once()
            assert all(a is b for a,b in zip(retained,peers[1].collect_once()))
            server.update_monitoring([('yc',no,float(no+1),128) for no in range(1,points+1)])
            try:
                wait_for(lambda:all(received(peer, points, 1, 128) for peer in peers))
            except Exception:
                result.update(passed=False,failed_points=points,phase='second_batch',
                    timings=[summary(v) for v in samples],reception=[dict(
                    connected=peer.connected,
                    missing=[dict(point=s.point_no,value=s.value,quality=s.quality) for s in peer.collect_once()
                             if s.value!=s.point_no+1 or s.quality!=128],
                    frames=[dict(direction=direction,hex=b.hex()) for direction,b in frames])
                    for peer,frames in zip(peers,wire)])
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                raise
            assert all(s.value==s.point_no and s.quality==0 for s in retained)
            item=dict(points=points,calls_per_sample=10,before=summary(samples[0]),after=summary(samples[1]))
            item['received_objects_per_client'] = [sum(b[7]&127 for direction,b in frames
                if direction=='RX' and len(b)>12 and not b[2]&1 and b[6]==13) for frames in wire]
            assert item['received_objects_per_client'] == [points*2, points*2]
            item['reduction_percent']=round(100*(1-item['after']['median_ms']/item['before']['median_ms']),2)
            result['cases'].append(item);print(json.dumps(item),flush=True)
    result['passed']=True;args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()

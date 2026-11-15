"""Measure reader isolation with a controlled slow command, on loopback TCP."""
import argparse
import concurrent.futures
import importlib.metadata
import json
from pathlib import Path
import socket
import statistics
import struct
import sys
import threading
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--delay-ms',type=float,default=120)
    args=parser.parse_args();sys.path.insert(0,str(args.root.resolve()))
    from server.modbus_trans_server import ModbusTcpTransServer
    entered=threading.Event();calls=[];observed=[]
    def command(*values):
        calls.append(values);entered.set();time.sleep(args.delay_ms/1000);return True
    server=ModbusTcpTransServer(port=0,points=[dict(kind='yc',pnt_no=1,address=0,value=37),
        dict(kind='yk',pnt_no=1,address=0)],command_handler=command)
    def request(tid,fc,value=1):return struct.pack('>HHHBBHH',tid,0,6,1,fc,0,value)
    def receive(peer):
        def exact(n):
            data=b''
            while len(data)<n:
                part=peer.recv(n-len(data))
                if not part:raise EOFError()
                data+=part
            return data
        head=exact(7);tid,_,n,unit=struct.unpack('>HHHB',head)
        return tid,unit,exact(n-1),time.perf_counter()
    def stats(values):
        return dict(median_ms=round(statistics.median(values),3),
                    p95_ms=round(sorted(values)[int((len(values)-1)*.95+.9999)],3),
                    samples_ms=[round(x,3) for x in values])
    server.start();port=server.server.transport.sockets[0].getsockname()[1]
    reads=[];writes=[];idle=[]
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=3) as writer, \
             socket.create_connection(('127.0.0.1',port),timeout=3) as reader, \
             concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            for n in range(14):
                entered.clear(); start=time.perf_counter();writer.sendall(request(n*2+1,5,0xff00))
                result=pool.submit(receive,writer);assert entered.wait(2)
                sent=time.perf_counter();reader.sendall(request(n*2+2,4));tid,unit,pdu,received=receive(reader)
                ack=result.result(3)
                assert (tid,unit,pdu)==(n*2+2,1,b'\x04\x02\x00%')
                assert ack[:3]==(n*2+1,1,b'\x05\x00\x00\xff\x00')
                if n>=2:
                    reads.append((received-sent)*1000);writes.append((ack[3]-start)*1000)
                    observed.append(dict(write_tid=n*2+1,read_tid=tid,write_send=start,
                        read_send=sent,read_receive=received,write_receive=ack[3]))
            for n in range(102):
                start=time.perf_counter();reader.sendall(request(n+100,4));reply=receive(reader)
                assert reply[:3]==(n+100,1,b'\x04\x02\x00%')
                if n>=2:idle.append((reply[3]-start)*1000)
    finally:server.close()
    assert len(calls)==14
    payload=dict(passed=True,root=str(args.root.resolve()),delay_ms=args.delay_ms,
        pymodbus=importlib.metadata.version('pymodbus'),read_under_write=stats(reads),
        write=stats(writes),idle_read=stats(idle),observed=observed,
        diagnostics=getattr(server,'request_diagnostics',{}))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(payload,indent=2),encoding='utf8')
    print(json.dumps({k:payload[k] for k in ('passed','delay_ms','read_under_write','write','idle_read') if k in payload}))


if __name__=='__main__':main()

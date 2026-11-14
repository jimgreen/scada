"""Independent raw TCP checks; record failed command contracts without hiding them."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from client.iec104 import IEC104Master, IEC104Error
from server.iec104_server import IEC104Server


class RawPeer:
    def __init__(self, sock):
        self.sock = sock; self.tx = 0; self.rx = 0; self.frames = []

    def send(self, frame):
        self.frames.append(dict(direction='TX', hex=frame.hex()))
        self.sock.sendall(frame)

    def u(self, value): self.send(bytes((0x68, 4, value, 0, 0, 0)))

    def asdu(self, data):
        self.send(bytes((0x68, len(data)+4)) + struct.pack('<HH', self.tx*2, self.rx*2) + data)
        self.tx = (self.tx+1) % 32768

    def receive(self, timeout=2):
        deadline = time.monotonic()+timeout
        def exact(size):
            value = b''
            while len(value)<size:
                self.sock.settimeout(max(.001, deadline-time.monotonic()))
                chunk = self.sock.recv(size-len(value))
                if not chunk: raise EOFError('peer closed')
                value += chunk
            return value
        header = exact(2)
        assert header[0]==0x68 and header[1]>=4, header.hex()
        frame = header + exact(header[1]); self.frames.append(dict(direction='RX', hex=frame.hex()))
        if not frame[2]&1:
            assert int.from_bytes(frame[2:4], 'little')>>1==self.rx, frame.hex()
            self.rx = (self.rx+1) % 32768
            self.send(b'\x68\x04\x01\x00'+struct.pack('<H', self.rx*2))
        return frame

    def until(self, predicate, timeout=2):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            frame = self.receive(deadline-time.monotonic())
            if predicate(frame): return frame
        raise TimeoutError('expected frame not received')

    def drain(self, seconds=.15):
        found=[]; deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try: found.append(self.receive(deadline-time.monotonic()))
            except socket.timeout: break
        return found


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    result=dict(timestamp=datetime.now(timezone.utc).isoformat(),checks=[],frames={})
    def check(name,passed,**details): result['checks'].append(dict(name=name,passed=bool(passed),**details))
    try:
        with ExitStack() as stack:
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1',0)); port=reservation.getsockname()[1]
            events=[]
            server=IEC104Server(ip='127.0.0.1',port=port,tick_rate_ms=50,t3=1)
            stack.callback(server.stop)
            server.add_point('yc',1,value=10)
            for kind,typ in (('yk',45),('yt',50)):
                for no in (1,2):
                    server.add_point(kind,no,data_type=typ,command_mode='DIRECT' if no==1 else 'SELECT_AND_EXECUTE',
                                     command_handler=lambda event:events.append(event) or True)
            server.start()
            sock=socket.create_connection(('127.0.0.1',port),timeout=3); stack.callback(sock.close)
            peer=RawPeer(sock)
            peer.u(7); check('STARTDT_ACT_CON',peer.until(lambda b:b[2]==0x0b)==bytes.fromhex('68040b000000'))
            peer.u(0x43); check('TESTFR_ACT_CON',peer.until(lambda b:b[2]==0x83)==bytes.fromhex('680483000000'))
            peer.u(0x13); check('STOPDT_ACT_CON',peer.until(lambda b:b[2]==0x23)==bytes.fromhex('680423000000'))
            server.update('yc',1,20,transmit=False)
            muted=peer.drain(.2); check('STOPDT_no_monitoring',not any(not b[2]&1 for b in muted))
            peer.u(7); peer.until(lambda b:b[2]==0x0b)
            mark=len(peer.frames)
            peer.asdu(bytes.fromhex('64010600010000000014'))
            peer.until(lambda b:len(b)>12 and not b[2]&1 and b[6]==100 and b[8]&63==10)
            data=[bytes.fromhex(r['hex']) for r in peer.frames[mark:] if r['direction']=='RX']
            check('STARTDT_resume_GI_latest_value',any(len(b)>19 and b[6]==13 and struct.unpack_from('<f',b,15)[0]==20 for b in data))
            idle=peer.until(lambda b:b[2]==0x43,timeout=4); peer.u(0x83)
            check('native_t3_TESTFR',idle==bytes.fromhex('680443000000'))
            for kind,typ,base in (('yk',45,0x6001),('yt',50,0x6201)):
                payload=lambda select: bytes((1 | (128 if select else 0),)) if kind=='yk' else struct.pack('<fB',12.5,128 if select else 0)
                def command(no,cot,select=False):
                    mark=len(peer.frames)
                    peer.asdu(bytes((typ,1,cot,0,1,0))+(base+no-1).to_bytes(3,'little')+payload(select))
                    peer.until(lambda b:len(b)>15 and not b[2]&1 and b[6]==typ)
                    peer.drain(.12)
                    return [bytes.fromhex(r['hex']) for r in peer.frames[mark:] if r['direction']=='RX'
                            and len(bytes.fromhex(r['hex']))>15 and bytes.fromhex(r['hex'])[6]==typ]
                direct=command(1,6)
                check(kind+'_direct_ACTCON',any(b[8]&127==7 for b in direct))
                canceled=command(1,8)
                check(kind+'_cancel_DEACTCON_9',any(b[8]&127==9 for b in canceled),observed_cots=[b[8]&127 for b in canceled])
                selected=command(2,6,True)
                check(kind+'_select_ACTCON',any(b[8]&127==7 and b[-1]&128 for b in selected))
                executed=command(2,6)
                terms=[b for b in executed if b[8]&63==10]
                check(kind+'_execute_ACTTERM_select_clear',bool(terms) and all(not b[-1]&128 for b in terms),frames=[b.hex() for b in terms])
            result['frames']['server_adapter']=peer.frames
        for kind,typ,base in (('yk',45,0x6001),('yt',50,0x6201)):
            with socket.socket() as listener, ExitStack() as stack, ThreadPoolExecutor(1) as pool:
                listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(3)
                master=IEC104Master('127.0.0.1',listener.getsockname()[1],init='NONE',command_timeout_ms=700)
                stack.callback(master.close);master.add_point(kind,1,data_type=typ)
                future=pool.submit(master.connect,3)
                sock,_=listener.accept();stack.callback(sock.close);peer=RawPeer(sock)
                peer.until(lambda b:b[2]==7);peer.u(0x0b);future.result(3)
                future=pool.submit(getattr(master,f'cancel_{kind}_point'),1,True if kind=='yk' else 12.5)
                request=peer.until(lambda b:len(b)>15 and not b[2]&1)
                assert request[8]&63==8,request.hex()
                answer=bytearray(request[6:]);answer[2]=9;peer.asdu(answer)
                accepted=True;error=None
                try:future.result(2)
                except IEC104Error as exc:accepted=False;error=str(exc)
                check('C_'+kind+'_accepts_standard_DEACTCON_9',accepted,error=error)
                result['frames']['C_'+kind]=peer.frames
        result['passed']=all(c['passed'] for c in result['checks'])
    except Exception as exc:
        result.update(passed=False,error=repr(exc));raise
    finally:
        (args.output/'wire_contracts.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='frames'},ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__=='__main__':sys.exit(main())

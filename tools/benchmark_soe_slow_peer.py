"""Compare fast-client delivery with one non-ACKing socket; private DB/TCP only."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import sys
import tempfile
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();sys.path.insert(0,str(args.root.resolve()))
    from scdfe_s.database import SimulatorStore
    from tools.verify_iec104_soe import fill_one,port
    from tools.verify_iec104_wire_contracts import RawPeer
    from core.iec104_soe import SoeForwarder,decode_events
    from server.iec104_server import IEC104Server
    from client.iec104 import IEC104Master
    with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
        data=SimulatorStore(Path(directory)/'s.sqlite3');stack.callback(data.close);fill_one(data,2404)
        with data.connection() as con:
            con.execute('BEGIN')
            for n in range(384):
                con.execute('UPDATE fes_yx_define SET value=? WHERE rtu_id=1 AND pnt_no=1',((n+1)%2,))
            con.commit()
        forward=SoeForwarder(data,'isolation','S',lambda row:[row],rtu_id=1);stack.callback(forward.close)
        server=IEC104Server(ip='127.0.0.1',port=port(),t1=10,tick_rate_ms=50)
        stack.callback(server.stop);server.add_point('yx',1,data_type=30);server.start()
        captured=[]
        def capture(direction,frame,detail=''):
            if direction=='RX' and len(frame)>12 and frame[2]&1==0 and frame[6]==30:
                captured.extend(decode_events(frame[6:]))
        fast=IEC104Master('127.0.0.1',server.port,init='NONE',w=1,t2=1)
        stack.callback(fast.close);fast.add_point('yx',1,data_type=30);fast.set_packet_handler(capture);fast.connect(timeout=3)
        sock=socket.create_connection(('127.0.0.1',server.port),timeout=3);stack.callback(sock.close)
        slow=RawPeer(sock);slow.u(7);slow.until(lambda b:b[2]==11)
        start=time.perf_counter();complete_ms=None
        while time.perf_counter()-start<2:
            forward.tick(server)
            if len(captured)==384 and complete_ms is None:
                complete_ms=round((time.perf_counter()-start)*1000,3)
            time.sleep(.005)
        states=[e['raw_state'] for e in captured]
        result=dict(root=str(args.root.resolve()),events=384,window_ms=2000,fast_received=len(captured),
                    ordered=states==[(n+1)%2 for n in range(len(states))],
                    complete_ms=complete_ms,durable_cursor=forward.last_event_id,
                    scope='real TCP, one non-ACKing client, 128-event batches, controlled 5 ms pump')
        assert result['ordered'] and result['durable_cursor']==0
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps(result))


if __name__=='__main__':main()

"""Isolated real Modbus C/S/T acceptance; no operator DBs or physical serial ports.

TCP uses production S services, C workers + writer process, T TCP runtimes.
RTU uses the production serial adapters with a socket:// byte transport.
This checks software RTU frames, not baud-rate timing or RS485 physics.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import datetime
import importlib.metadata
import json
import math
import multiprocessing as mp
from pathlib import Path
import shutil
import socket
import statistics
import struct
import sys
import tempfile
import threading
import time
import traceback
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from client.modbus_tcp import ModbusTcpMaster, ModbusError
from scdfe_c import ChannelSpec, ChannelWorker, _batch_writer_entry, BATCH_WRITER_STOP
from scdfe_c.web.service import ScadaStore, SCHEMA_DIR
from scdfe_s.database import SimulatorStore
from scdfe_s.web.protocol_service import ModbusTcpServerService, ModbusRtuServerService
from scdfe_t.trans import TransRuntime
from server.modbus_trans_server import ModbusTcpTransServer


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); return sock.getsockname()[1]


def wait_for(predicate,label,timeout=10):
    deadline=time.perf_counter()+timeout
    while time.perf_counter()<deadline:
        if predicate(): return time.perf_counter()
        time.sleep(.005)
    raise AssertionError(f'Timeout: {label}')


def stats(values):
    ordered=sorted(values)
    return dict(samples=len(values),median_ms=round(statistics.median(values),3),
                p95_ms=round(ordered[math.ceil(.95*len(values))-1],3),max_ms=round(max(values),3))


def crc(data):
    value=0xffff
    for byte in data:
        value ^= byte
        for _ in range(8): value=(value>>1)^0xa001 if value&1 else value>>1
    return value


class Wire:
    def __init__(self,rtu=False): self.frames=[]; self.rtu=rtu
    def __call__(self,direction,data,detail=''): self.frames.append((time.perf_counter(),direction,bytes(data)))

    def validate(self):
        count=Counter(); pending={}; buffers={'TX':b'','RX':b''}
        for _,direction,chunk in self.frames:
            if self.rtu and direction=='RX':
                # pymodbus traces its accumulated receive buffer, including
                # prior incomplete bytes, rather than only the new chunk.
                assert chunk.startswith(buffers[direction]),(buffers[direction].hex(),chunk.hex())
                buffers[direction]=chunk
            else:
                buffers[direction]+=chunk
            while len(buffers[direction]) >= (2 if self.rtu else 8):
                b=buffers[direction]
                if self.rtu:
                    fc=b[1]
                    if fc&128: length=5
                    elif direction=='RX' and fc in (1,2,3,4):
                        if len(b)<3: break
                        length=5+b[2]
                    elif direction=='TX' and fc in (15,16):
                        if len(b)<7: break
                        length=9+b[6]
                    else: length=8
                else: length=6+int.from_bytes(b[4:6],'big')
                if len(b)<length: break
                b,buffers[direction]=b[:length],b[length:]
                if self.rtu:
                    assert crc(b[:-2])==int.from_bytes(b[-2:],'little'),b.hex()
                    unit,fc=b[:2]
                else:
                    assert b[2:4]==b'\0\0' and 8<=length<=260,b.hex()
                    tid=int.from_bytes(b[:2],'big'); unit,fc=b[6:8]
                    if direction=='TX': pending[tid]=(unit,fc)
                    else: assert pending.pop(tid)==(unit,fc&127),b.hex()
                count[f'{direction}_FC{fc:02d}']+=1
        assert not any(buffers.values()),buffers
        return dict(count)


def define(store,ports,points,*,initial,serial=None):
    protocol='modbus-rtu' if serial else 'modbus-tcp'
    half=points//2
    with store.connection() as con:
        for rtu,listen in enumerate(ports,1):
            con.execute('INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type,process_enable) VALUES(?,?,?,0)',(rtu,rtu,protocol))
            if serial:
                con.execute("INSERT INTO fes_channel_modbus_rtu_define(channel_id,serial_port,baudrate,bytesize,parity,stopbits,unit_id,response_timeout,retry_count) VALUES(?,?,19200,8,'E',1,?,0.5,0)",(rtu,serial,rtu))
            else:
                con.execute('INSERT INTO fes_channel_modbus_tcp_define(channel_id,remote_host,remote_port,unit_id,response_timeout,connect_timeout,retry_count,keep_alive) VALUES(?,?,?,?,0.5,0.5,0,1)',(rtu,'127.0.0.1',listen,rtu))
            for kind in ('yc','yx','yk','yt'):
                for no in range(1,(points if kind in ('yc','yx') else 2)+1):
                    value=rtu*1000+no+.25 if initial and kind=='yc' else 0
                    con.execute(f'INSERT INTO fes_{kind}_define(rtu_id,pnt_no,value,status) VALUES(?,?,?,0)',(rtu,no,value))
            for fc,kind,base in [(1,'yx',0),(2,'yx',10001),(3,'yc',40001),(4,'yc',30001)]:
                con.execute('INSERT INTO fes_channel_modbus_define_call(id,channel_id,call_seq,irtuaddr,icode,iplace,address_base,inum) VALUES(?,?,?,?,?,?,?,?)',
                            (rtu*10+fc,rtu,fc,rtu,fc,base,base,half*(2 if kind=='yc' else 1)))
                for offset in range(half):
                    no=offset+1+(half if fc in (2,4) else 0)
                    con.execute('INSERT INTO fes_channel_modbus_define_data(id,channel_id,call_seq,cnt,if_yx,reg_addr,trans_type,fourbytedata_byteseq) VALUES(?,?,?,?,?,?,?,3)',
                                (rtu*10000+fc*1000+offset,rtu,fc,no,int(kind=='yx'),base+offset*(2 if kind=='yc' else 1),5 if kind=='yc' else 8))
            for ident,kind,no,fc,typ,addr,size in [(1,'yk',1,5,6,512,1),(2,'yk',2,15,6,513,1),(3,'yt',1,6,2,40513,1),(4,'yt',2,16,5,40515,2)]:
                con.execute('INSERT INTO fes_channel_modbus_define_ytyk(id,channel_id,cnt,ifyk,irtuaddr,icode,ytyk_type,reg_addr,address_base,fourbytedata_byteseq,multi_para) VALUES(?,?,?,?,?,?,?,?,?,3,?)',
                            (rtu*10+ident,rtu,no,int(kind=='yk'),rtu,fc,typ,addr,40001 if kind=='yt' else 0,size))
        con.commit()


def pipeline(channels,points,rounds,output,*,rtu=False):
    result=dict(upstream='RTU frames over socket://' if rtu else 'Modbus TCP',channels=channels,
                yc_per_rtu=points,yx_per_rtu=points,c_poll_ms=50,t_scan_ms=50,s_scan_ms=50,
                writer_batch_ms=100,writer_batch_limit=500,response_timeout_ms=500)
    wires=[]
    with tempfile.TemporaryDirectory(prefix='modbus-pipeline-') as directory, ExitStack() as stack:
        root=Path(directory)
        label='rtu' if rtu else f'tcp-{channels}ch'
        stack.callback(lambda: shutil.copytree(root/'logs',output/f'logs-{label}',dirs_exist_ok=True)
                       if (root/'logs').exists() else None)
        s=SimulatorStore(root/'scdfe_s.sqlite3',connection_pool_size=2)
        c=ScadaStore(root/'scada.sqlite3',connection_pool_size=2)
        stack.callback(s.close); stack.callback(c.close)
        s_ports=[port() for _ in range(channels)]; t_ports=[port() for _ in range(channels)]
        serial=f'socket://127.0.0.1:{s_ports[0]}' if rtu else None
        define(s,s_ports,points,initial=True,serial=serial)
        define(c,s_ports,points,initial=False,serial=serial)
        with c.connection() as con:
            for r,listen in enumerate(t_ports,1):
                con.execute("INSERT INTO fes_scdfe_t_define(channel_id,protocol,bind_host,bind_port,unit_id,scan_interval_ms) VALUES(?,'modbus-tcp','127.0.0.1',?,?,50)",(r,listen,r))
                for kind in ('yc','yx','yk','yt'):
                    for n in range(1,(points if kind in ('yc','yx') else 2)+1):
                        if kind in ('yc','yx'):
                            area=('holding' if n<=points//2 else 'input') if kind=='yc' else ('coil' if n<=points//2 else 'discrete')
                            address=((n-1)%(points//2))*(2 if kind=='yc' else 1)
                            dtype='float32' if kind=='yc' else 'bool'
                        else:
                            area='coil' if kind=='yk' else 'holding'
                            address=511+n if kind=='yk' else 512 if n==1 else 514
                            dtype='bool' if kind=='yk' else 'uint16' if n==1 else 'float32'
                        con.execute('INSERT INTO fes_scdfe_t_point_define(channel_id,kind,pnt_no,source_rtu_id,source_pnt_no,enabled,register_area,address,data_type) VALUES(?,?,?,?,?,1,?,?,?)',(r,kind,n,r,n,area,address,dtype))
            con.commit()
        ctx=mp.get_context('spawn'); queue=ctx.JoinableQueue(10000); ready=ctx.Event()
        writer=ctx.Process(target=_batch_writer_entry,args=(str(c.db_path),str(SCHEMA_DIR),str(root/'logs'),queue,.1,500,ready)); writer.start()
        def stop_writer():
            if writer.is_alive(): queue.put(BATCH_WRITER_STOP,timeout=5); writer.join(15)
            if writer.is_alive():
                writer.terminate(); writer.join(5); raise AssertionError('private writer stop timeout')
            assert writer.exitcode==0,writer.exitcode
            queue.close(); queue.join_thread()
        stack.callback(stop_writer); assert ready.wait(15)
        errors=[]; workers=[]; services=[]; peers=[]; events=[]; threads=[]
        def background(fn,event):
            def run():
                try: fn()
                except Exception: errors.append(traceback.format_exc())
            thread=threading.Thread(target=run,daemon=True); thread.start()
            events.append(event); threads.append(thread)
            def stop():
                event.set(); thread.join(15); assert not thread.is_alive(),'private runtime stop timeout'
            stack.callback(stop)
        for r in range(1,channels+1):
            service_cls=ModbusRtuServerService if rtu else ModbusTcpServerService
            service=service_cls(s,config_path=root/f's-{r}.json')
            config=dict(rtu_id=r,unit_id=r)
            config.update(dict(simulation=0,serial_port=serial) if rtu else dict(listen_port=s_ports[r-1]))
            service.configure(config); service.start(); services.append(service); stack.callback(service.close)
            store=ScadaStore(c.db_path,connection_pool_size=1); stack.callback(store.close)
            event=threading.Event()
            worker=ChannelWorker(store,ChannelSpec(r,r,'modbus-rtu' if rtu else 'modbus-tcp'),log_dir=root/'logs',poll_interval=.05,retry_interval=.1,stop_event=event,writer_queue=queue)
            wire=Wire(rtu); wires.append((f'C-{r}',wire)); original=worker.logger.packet
            def record(direction,data,detail='',w=wire,original=original): w(direction,data,detail); original(direction,data,detail)
            worker.logger.packet=record; workers.append(worker); background(worker.run,event)
        expected={(r,k,n):(r*1000+n+.25 if k=='yc' else 0) for r in range(1,channels+1) for k in ('yc','yx') for n in range(1,points+1)}
        def c_matches():
            with c.connection() as con:
                return all(row[2]==expected[(row[0],kind,row[1])] for kind in ('yc','yx')
                           for row in con.execute(f'SELECT rtu_id,pnt_no,value FROM fes_{kind}_define'))
        wait_for(c_matches,'C initial four-area read')
        for r in range(1,channels+1):
            event=threading.Event(); runtime=TransRuntime(c.db_path,channel_id=r,log_dir=root/'logs',stop_event=event)
            background(runtime.run,event)
            wait_for(lambda:runtime.server is not None and runtime.server.ready.is_set()
                     and runtime.server.connected,'T listener ready')
            peer=ModbusTcpMaster('127.0.0.1',t_ports[r-1],unit_id=r,timeout=.5,rtu_id=r)
            # Independent four-bank reads; verification does not reuse C decode plans.
            stack.callback(peer.close); peer.connect(); wire=Wire(); peer.set_packet_handler(wire)
            wires.append((f'T-{r}',wire)); peers.append(peer)
        def read_peer(peer):
            values={}; half=points//2
            for kind,method,first in [('yx',peer.read_coils,1),('yx',peer.read_discrete_inputs,half+1),
                                      ('yc',peer.read_holding_registers,1),('yc',peer.read_input_registers,half+1)]:
                raw=method(0,half*(2 if kind=='yc' else 1))
                decoded=[struct.unpack('>f',struct.pack('>HH',*raw[i:i+2]))[0] for i in range(0,len(raw),2)] if kind=='yc' else raw
                values.update({(kind,first+i):v for i,v in enumerate(decoded)})
            return values
        def peers_match():
            return all(v==expected[(r,k,n)] for r,peer in enumerate(peers,1) for (k,n),v in read_peer(peer).items())
        wait_for(peers_match,'initial T four banks')
        c_times=[]; e2e=[]; begin_all=time.perf_counter()
        for step in range(1,rounds+1):
            updates=[]
            for key in expected:
                r,k,n=key; value=r*1000+n+.25+step if k=='yc' else step%2
                expected[key]=value; updates.append((f'fes_{k}_define',[r,n],dict(value=value)))
            s.update_rows(updates); begin=time.perf_counter()
            c_end=wait_for(c_matches,'C updated values'); end=wait_for(peers_match,'T updated values')
            c_times.append((c_end-begin)*1000); e2e.append((end-begin)*1000)
        result.update(c_commit_latency=stats(c_times),end_to_end_latency=stats(e2e),
                      c_commit_samples_ms=c_times,end_to_end_samples_ms=e2e,workload_seconds=round(time.perf_counter()-begin_all,3))
        # Stable configured polls, without writes, benchmark the full C adapter.
        master=workers[0]._master
        poll_times=[]
        for _ in range(30):
            begin=time.perf_counter(); values=master.collect_once(); poll_times.append((time.perf_counter()-begin)*1000)
            assert len(values)==points*2
            assert all(v.value==expected[(1,v.kind,v.point_no)] for v in values)
        result['four_bank_poll']=stats(poll_times)
        command_times=[]; ack_times=[]
        for r,peer in enumerate(peers,1):
            operations=[(lambda:peer.write_single_coil(512,True),[('yk',1,1)]),
                        (lambda:peer.write_multiple_coils(512,[False,True]),[('yk',1,0),('yk',2,1)]),
                        (lambda:peer.write_single_register(512,1234),[('yt',1,1234)]),
                        (lambda:peer.write_multiple_registers(514,list(struct.unpack('>HH',struct.pack('>f',12.5)))),[('yt',2,12.5)])]
            for operation,changes in operations:
                begin=time.perf_counter(); assert operation()
                end=wait_for(lambda:all(s.get_row(f'fes_{k}_define',[r,n])['value']==v for k,n,v in changes),'T -> shared DB -> C -> S command')
                command_times.append((end-begin)*1000)
                def acknowledged():
                    rows=[c.get_row(f'fes_{k}_define',[r,n]) for k,n,_ in changes]
                    return all(row['cmd_time'] and (row['cmd_handle_time'] or 0)>=row['cmd_time'] and row['status']==1 for row in rows)
                ack_end=wait_for(acknowledged,'C command receipt committed before next command')
                ack_times.append((ack_end-begin)*1000)
            assert peer.read_coils(512,2)==[False,True]
            assert peer.read_holding_registers(512,1)==[1234]
            assert peer.read_holding_registers(514,2)==list(struct.unpack('>HH',struct.pack('>f',12.5)))
            for operation,code in [(lambda:peer.read_input_registers(4096,1),2),
                                   (lambda:peer.write_single_coil(0,True),2),
                                   (lambda:peer.write_single_register(514,0),3)]:
                try: operation()
                except ModbusError as exc: assert exc.exception_code==code,(exc.exception_code,code)
                else: raise AssertionError('expected Modbus exception')
            assert s.get_row('fes_yt_define',[r,2])['value']==12.5
        result['command_latency']=stats(command_times)
        result['command_receipt_latency']=stats(ack_times)
        result['checks']=['01/02/03/04 four banks and RTU isolation','reference addresses 10001/30001/40001 -> wire zero',
                          'autonomous S DB -> C writer -> T refresh','05/06/15/16 T->shared DB->C->S commands and readback',
                          'illegal address=02, protected measurement=02, partial float write=03',
                          'unchanged continued polls and complete float32 values']
        assert all(w.stats.errors==0 for w in workers),[vars(w.stats) for w in workers]
        result['errors_before_injected_disconnect']=0
        if not rtu:
            services[0].stop()
            wait_for(lambda:workers[0].stats.errors>0,'C sees upstream failure')
            changes=[]
            for key,value in expected.items():
                r,k,n=key
                if r==1:
                    value=value+50 if k=='yc' else 1-int(value); expected[key]=value
                    changes.append((f'fes_{k}_define',[r,n],dict(value=value)))
            s.update_rows(changes); begin=time.perf_counter(); services[0].start()
            wait_for(c_matches,'C reconnect'); end=wait_for(peers_match,'T reconnect recovery')
            result['restart_recovery_ms']=round((end-begin)*1000,3)
            result['checks'].append('S TCP stop/restart -> C reconnect -> T latest values')
        assert not errors,errors
        # Stop producers before validating packet pairs and taking final stats.
        for event in events: event.set()
        for thread in threads: thread.join(15); assert not thread.is_alive()
        result['workers']=[vars(w.stats).copy() for w in workers]
        result['wire']={name:wire.validate() for name,wire in wires}
        prefix='rtu' if rtu else f'tcp-{channels}ch'
        with (output/f'wire-{prefix}.jsonl').open('w',encoding='utf-8') as file:
            for name,wire in wires:
                for stamp,direction,data in wire.frames: file.write(json.dumps(dict(link=name,monotonic=stamp,direction=direction,hex=data.hex()))+'\n')
    result['passed']=True
    return result


def block_comparison(rounds=8):
    with ExitStack() as stack:
        server=ModbusTcpTransServer(host='127.0.0.1',port=port(),points=[dict(kind='yt',pnt_no=n+1,address=n,register_area='holding',data_type='uint16') for n in range(500)],
                                    point_values={('yt',n+1):n for n in range(500)},command_handler=lambda *args:True)
        server.start(); stack.callback(server.close)
        peer=ModbusTcpMaster('127.0.0.1',server.port,timeout=.5); peer.connect(); stack.callback(peer.close)
        wire=Wire(); peer.set_packet_handler(wire); result={m:dict(samples_ms=[],requests=[],adu_bytes=[]) for m in ('single','block')}
        for repeat in range(rounds):
            for mode in ('single','block'):
                mark=len(wire.frames); begin=time.perf_counter()
                size=1 if mode=='single' else 125
                values=[v for address in range(0,500,size) for v in peer.read_holding_registers(address,size)]
                elapsed=(time.perf_counter()-begin)*1000; assert values==list(range(500))
                frames=wire.frames[mark:]; result[mode]['samples_ms'].append(elapsed)
                result[mode]['requests'].append(sum(d=='TX' for _,d,_ in frames)); result[mode]['adu_bytes'].append(sum(len(b) for _,_,b in frames))
        for item in result.values(): item['latency']=stats(item['samples_ms'])
        result['wire_validation']=wire.validate(); result['registers']=500
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'reports/modbus_common_acceptance_20261113')
    parser.add_argument('--channels',type=int,nargs='+',default=[1,4]); parser.add_argument('--points',type=int,default=64)
    parser.add_argument('--rounds',type=int,default=12); parser.add_argument('--skip-rtu',action='store_true')
    parser.add_argument('--skip-comparison',action='store_true')
    args=parser.parse_args()
    if not 2<=args.points<=124 or args.points%2 or min(args.channels)<1 or max(args.channels)>16 or args.rounds<1:
        parser.error('points must be even in 2..124; channels 1..16; rounds positive')
    args.output.mkdir(parents=True,exist_ok=True)
    result=dict(timestamp=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),python=sys.version,
                libraries={n:importlib.metadata.version(n) for n in ('pymodbus','pyModbusTCP','pyserial')},pipelines=[])
    try:
        for count in args.channels:
            print(f'TCP {count} channels x {args.points*2} points',flush=True)
            item=pipeline(count,args.points,args.rounds,args.output); result['pipelines'].append(item)
            print(json.dumps(item),flush=True)
        if not args.skip_rtu:
            print('RTU socket byte transport, production serial adapters',flush=True)
            result['rtu']=pipeline(1,8,6,args.output,rtu=True); print(json.dumps(result['rtu']),flush=True)
        if not args.skip_comparison: result['comparison']=block_comparison()
        result['passed']=True
    except Exception:
        result['passed']=False; result['error']=traceback.format_exc(); raise
    finally: (args.output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__': main()

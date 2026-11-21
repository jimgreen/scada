"""Project-facing IEC104 client/server APIs, implemented using Python sockets."""
from collections import deque
from datetime import datetime, timezone
import errno
import logging
import math
import socket
import threading
import time

from .types import *
from .codec import (BASE, MONITOR, COMMAND, CP56, TIMED, INFO, SIZES, decode_asdu, decode_time,
                    encode_time, encode_information, header, pack_points)
from .transport import Link, Reactor, MAX_QUEUED_BYTES

LOG = logging.getLogger(__name__)
_MONITOR_COTS = frozenset({1, 2, 3, 5, *range(20, 37)})


def invoke(callback, *args, default=None):
    if callback is None: return default
    return callback(*args)


class Point:
    def __init__(self, station, io_address, type, report_ms=0, related_io_address=None,
                 related_io_autoreturn=False, command_mode=CommandMode.DIRECT):
        if not 0 <= int(io_address) <= 0xffffff: raise ValueError('IOA 超出范围')
        self.station=station;self.io_address=int(io_address);self.type=Type(type)
        self._type_int=int(self.type)
        self._base_type=BASE.get(self._type_int,self._type_int)
        self._is_timed=self._type_int in TIMED
        self._is_monitor=self._type_int in MONITOR
        self._info_cls=INFO[self._base_type]
        self._report_ms=int(report_ms);self.command_mode=command_mode
        self.related_io_address=related_io_address;self.related_io_autoreturn=related_io_autoreturn
        self._receive=None;self._before_read=None
        self._info=self._info_cls()
        self._lock=threading.RLock()
        self.received_at=None
        self._last_report=time.monotonic()

    @property
    def report_ms(self):
        return self._report_ms

    @report_ms.setter
    def report_ms(self, value):
        value=int(value)
        self._report_ms=value
        if self._is_monitor and self.station is not None:
            self.station._track_periodic(self)

    @property
    def info(self):
        with self._lock: return self._info

    @info.setter
    def info(self, value):
        if not isinstance(value,Information): raise TypeError('info 必须是 Information')
        base=self._base_type
        val=value.value
        if base in (1, 45) and not isinstance(val, bool):
            raise TypeError(f'IEC104 单点类型 {base} 要求 bool 值，实际收到 {type(val).__name__}')
        if base==3 and not isinstance(val, Double):
            raise TypeError(f'IEC104 双点遥信要求 Double 值，实际收到 {type(val).__name__}')
        if base==46:
            if not isinstance(val, Double) or val not in (Double.OFF, Double.ON):
                raise ValueError(f'IEC104 双点遥控仅允许 Double.OFF/Double.ON，实际收到 {val!r}')
        if base in (13, 50):
            if isinstance(val, bool) or not math.isfinite(float(val)):
                raise ValueError(f'IEC104 短浮点类型 {base} 要求有限数值')
        if not self._is_timed:
            value=Information(value.value,value.quality,None,value.qualifier,value.transient)
        with self._lock: self._info=value

    @property
    def value(self): return self.info.value

    @value.setter
    def value(self, value):
        base=self._base_type
        with self._lock:
            old=self._info
            constructor=self._info_cls
            stamp=datetime.now(timezone.utc) if self._is_timed else None
            if base in COMMAND:
                self._info=constructor(value,qualifier=old.qualifier,recorded_at=stamp)
            elif base==5:
                self._info=constructor(value,transient=old.transient,quality=old.quality,recorded_at=stamp)
            else:
                self._info=constructor(value,quality=old.quality,recorded_at=stamp)

    @property
    def quality(self): return self.info.quality

    @quality.setter
    def quality(self, value):
        with self._lock:
            old=self._info
            self._info=Information(old.value,Quality(value),old.recorded_at,old.qualifier,old.transient)

    @property
    def recorded_at(self): return self.info.recorded_at

    def on_receive(self, callback): self._receive=callback
    def on_before_read(self, callback): self._before_read=callback

    def transmit(self, cause=Cot.SPONTANEOUS):
        owner=self.station.owner
        if isinstance(owner,Server): return owner.transmit_batch(Batch(cause,[self]))
        return owner._point_command(self,cause)

    def _received(self, info, message, received_at=None):
        if not isinstance(info,Information): raise TypeError('info 必须是 Information')
        if not self._is_timed:
            info=Information(info.value,info.quality,None,info.qualifier,info.transient)
        with self._lock:
            old=self._info
            self._info=info
        self.received_at=received_at or datetime.now(timezone.utc)
        result=invoke(self._receive,self,old,message,default=ResponseState.SUCCESS)
        return result is not False and result != ResponseState.FAILURE


class Station:
    def __init__(self, owner, common_address):
        if not 1<=int(common_address)<=65534: raise ValueError('CA 超出范围')
        self.owner=owner;self.common_address=int(common_address)
        self._points={};self._periodic_points=set();self._lock=threading.RLock()

    @property
    def points(self):
        with self._lock: return list(self._points.values())

    def _track_periodic(self, point):
        with self._lock:
            if point._is_monitor and point._report_ms and self._points.get(point.io_address) is point:
                self._periodic_points.add(point)
            else:
                self._periodic_points.discard(point)

    def periodic_points(self):
        with self._lock:
            return tuple(self._periodic_points) if self._periodic_points else ()

    def add_point(self, io_address, type, **kwargs):
        with self._lock:
            if io_address in self._points: return None
            point=Point(self,io_address,type,**kwargs);self._points[io_address]=point
            if point._is_monitor and point._report_ms:
                self._periodic_points.add(point)
            return point

    def get_point(self, io_address):
        with self._lock: return self._points.get(int(io_address))

    def remove_point(self, io_address):
        with self._lock:
            point=self._points.pop(int(io_address),None)
            if point is not None:
                self._periodic_points.discard(point)
            return point

    def signal_initialized(self, cause=Coi.LOCAL_POWER_ON):
        if isinstance(self.owner,Server):
            return self.owner._broadcast([header(70,1,4,self.common_address)+b'\0\0\0'+bytes((int(cause),))])
        return False


class SoePeer:
    """One live socket's bounded SOE flight, independent of other recipients."""
    def __init__(self,server,link):
        self.server,self.link,self.goal=server,link,None
        self.key=link.ident
        self.address=str(link.peer)

    def submit_soe(self,asdus):
        if self.goal is not None:return False
        link=self.link
        if link.closed or not link.active:return False
        self.server._validate_soe(asdus)
        generation=link.generation
        serial=link.enqueue(asdus)
        if serial is None:return False
        self.goal=(generation,serial)
        return True

    def poll_soe(self):
        if self.goal is None:return 0
        generation,serial=self.goal
        if self.link.closed or self.link.generation!=generation:
            self.goal=None;return -1
        if self.link.acked_serial>=serial:
            self.goal=None;return 1
        return 0


class Server:
    def __init__(self, ip='0.0.0.0', port=2404, *, tick_rate_ms=100,
                 select_timeout_ms=10000, max_connections=0):
        self.ip,self.port=ip,int(port)
        self.tick_rate_ms=int(tick_rate_ms);self.select_timeout_ms=int(select_timeout_ms)
        self.max_connections=int(max_connections)
        self.protocol_parameters=ProtocolParameters()
        self._stations={};self._lock=threading.RLock()
        self._reactor=Reactor(self);self.is_running=False
        self._receive_raw=self._send_raw=self._connect=self._clock=self._unexpected=None
        self._trace_enabled=False;self._trace=deque();self._trace_dropped=0
        self._next_connection=0;self._next_periodic=0
        self._soe=None
        self._soe_peers={}
        self.last_error=None

    def add_station(self, common_address):
        with self._lock:
            if common_address in self._stations: return None
            station=Station(self,common_address);self._stations[common_address]=station
            return station

    def get_station(self, common_address): return self._stations.get(common_address)
    @property
    def stations(self): return list(self._stations.values())
    @property
    def open_connection_count(self): return sum(not x.closed for x in self._reactor.links)
    @property
    def active_connection_count(self): return sum(x.active and not x.closed for x in self._reactor.links)
    def on_connect(self, callback): self._connect=callback
    def on_receive_raw(self, callback): self._receive_raw=callback
    def on_send_raw(self, callback): self._send_raw=callback
    def on_clock_sync(self, callback): self._clock=callback
    def on_unexpected_message(self, callback): self._unexpected=callback

    def start(self):
        if self.is_running: return
        family=socket.AF_INET6 if ':' in self.ip else socket.AF_INET
        sock=socket.socket(family,socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind((self.ip,self.port));sock.listen(128)
            self._soe=None;self.is_running=True
            self._reactor.start(sock)
        except BaseException:
            sock.close();self.is_running=False;raise

    def stop(self):
        if self.is_running:
            self.is_running=False;self._reactor.stop()

    def _accept(self, sock, peer):
        try:
            if (self.max_connections and self.open_connection_count>=self.max_connections) or not invoke(self._connect,self,peer[0],default=True):
                sock.close();return
            self._next_connection+=1
            link=Link(self._reactor,sock,self);link.ident=self._next_connection
            self._reactor.add(link)
        except Exception:
            sock.close();LOG.exception('IEC104 connection admission failed')

    def _connected(self, link): self._record(link,10)
    def _active(self, link, active): self._record(link,12 if active else 13)
    def _closed(self, link, reason):
        self.last_error=reason
        link.selections.clear();self._record(link,11,reason.encode('utf8'))

    def _raw(self, link, direction, frame):
        self._record(link,1 if direction=='RX' else 2,frame)
        try: invoke(self._receive_raw if direction=='RX' else self._send_raw,self,frame)
        except Exception: LOG.exception('IEC104 raw observer failed')

    def enable_connection_trace(self, enabled): self._trace_enabled=bool(enabled)

    def _record(self, link, kind, data=b''):
        if not self._trace_enabled: return
        peer=link.peer or ('未知',0)
        host=f'[{peer[0]}]' if ':' in peer[0] else peer[0]
        with self._lock:
            if len(self._trace)>=32768: self._trace_dropped+=1
            else: self._trace.append((link.ident,kind,time.time_ns()//1_000_000,f'{host}:{peer[1]}',bytes(data),0))

    def drain_connection_trace(self):
        with self._lock:
            result=list(self._trace);self._trace.clear()
            if self._trace_dropped:
                result.insert(0,(0,99,0,'',b'',self._trace_dropped));self._trace_dropped=0
            return result

    def _broadcast(self, asdus):
        links=[x for x in self._reactor.links if x.active and not x.closed]
        # Updating a station without clients is valid; GI supplies its latest values.
        result=True
        for link in links:
            if link.enqueue(asdus) is None: result=False
        return result

    def transmit_batch(self, batch):
        return self._broadcast(pack_points(batch.points,batch.cause,self.protocol_parameters.max_asdu_length))

    def submit_soe(self, asdus):
        asdus=tuple(bytes(a) for a in asdus)
        if not asdus: return False
        self._validate_soe(asdus)
        with self._lock:
            if self._soe is not None: return False
            links=[x for x in self._reactor.links if x.active and not x.closed]
            if not links: return False
            total=sum(map(len,asdus))
            if any(x.pending_bytes+total>MAX_QUEUED_BYTES for x in links): return False
            goals=[]
            for link in links:
                serial=link.enqueue(asdus)
                if serial is None:
                    self._soe=[(link,-1,0)];return True
                goals.append((link,link.generation,serial))
            self._soe=goals
            return True

    def _validate_soe(self,asdus):
        from core.iec104_soe import decode_events
        for asdu in asdus:
            if len(asdu)>self.protocol_parameters.max_asdu_length or len(asdu)<17 or asdu[0] not in (30,31) or asdu[2]&127!=3:
                raise ValueError('SOE ASDU 无效')
            if not decode_events(asdu): raise ValueError('SOE ASDU 无事件')

    def soe_peers(self):
        with self._lock:
            active={x.ident:x for x in self._reactor.links if x.active and not x.closed}
            self._soe_peers={key:self._soe_peers.get(key) or SoePeer(self,link) for key,link in active.items()}
            return list(self._soe_peers.values())

    def poll_soe(self):
        with self._lock:
            if self._soe is None: return 0
            if any(x.closed or x.generation!=generation for x,generation,_ in self._soe):
                self._soe=None;return -1
            if all(x.acked_serial>=serial for x,_,serial in self._soe):
                self._soe=None;return 1
            return 0

    def _poll(self, now):
        if now<self._next_periodic: return
        self._next_periodic=now+self.tick_rate_ms/1000
        groups={}
        for station in self.stations:
            for point in station.periodic_points():
                if point._report_ms and now-point._last_report>=point._report_ms/1000:
                    groups.setdefault((station.common_address,point.type),[]).append(point)
                    point._last_report=now
        if groups:
            for points in groups.values(): self.transmit_batch(Batch(Cot.PERIODIC,points))

    def _reply(self, link, asdu, cot, *, negative=False):
        answer=asdu[:2]+bytes((int(cot) | (asdu[2]&128) | (64 if negative else 0),))+asdu[3:]
        if link.enqueue([answer]) is None: raise BufferError('命令响应队列已满')

    def _asdu(self, link, frame):
        asdu=frame[6:]
        if asdu[0] not in Type._value2member_map_:
            self._reply(link,asdu,44);return
        message,rows=decode_asdu(asdu,frame)
        station=self.get_station(message.common_address)
        if station is None:
            self._reply(link,asdu,46);invoke(self._unexpected,self,message,Umc.UNKNOWN_CA);return
        typ=int(message.type);cot=int(message.cot)
        if typ==100:
            if len(rows)!=1 or rows[0][0]!=0: self._reply(link,asdu,47);return
            if cot!=6: self._reply(link,asdu,45);return
            if rows[0][2]!=b'\x14': self._reply(link,asdu,7,negative=True);return
            points=sorted((p for p in station.points if int(p.type) in MONITOR),key=lambda p:(int(p.type),p.io_address))
            for point in points: invoke(point._before_read,point)
            self._reply(link,asdu,7)
            if link.enqueue(pack_points(points,Cot.INTERROGATED_BY_STATION,self.protocol_parameters.max_asdu_length,message.originator_address)) is None:
                raise BufferError('总召响应队列已满')
            self._reply(link,asdu,10)
        elif typ in (103,104,107):
            if cot!=6: self._reply(link,asdu,45);return
            if len(rows)!=1 or rows[0][0]!=0: self._reply(link,asdu,47);return
            success=True
            if typ==103:
                stamp=decode_time(rows[0][2])
                result=invoke(self._clock,self,link.peer[0],stamp,default=ResponseState.SUCCESS)
                success=result is not False and result!=ResponseState.FAILURE
            elif typ==104:
                success=rows[0][2]==b'\xaa\x55'
            self._reply(link,asdu,7,negative=not success)
        elif typ in COMMAND:
            if len(rows)!=1: raise ValueError('控制命令必须只有一个信息体')
            ioa,info,data=rows[0];point=station.get_point(ioa)
            if point is None: self._reply(link,asdu,47);return
            if point.type!=message.type: self._reply(link,asdu,44);return
            if cot not in (6,8): self._reply(link,asdu,45);return
            if (message.is_test or message.is_negative or
                    (typ in CP56 and (info.recorded_at is None or data[-5]&128))):
                self._reply(link,asdu,7 if cot==6 else 9,negative=True);return
            base=BASE.get(typ,typ)
            if (base==46 and int(info.value) not in (1,2)) or (base==45 and data[0]&2):
                self._reply(link,asdu,7 if cot==6 else 9,negative=True);return
            payload=bytearray(data[:SIZES[base]]);payload[-1]&=127
            key=(message.common_address,ioa)
            signature=(typ,message.originator_address,bytes(payload))
            selected=link.selections.get(key)
            if selected and selected[1]<time.monotonic():
                link.selections.pop(key,None);selected=None
            if message.is_select_command and cot==6:
                if point.command_mode!=CommandMode.SELECT_AND_EXECUTE:
                    self._reply(link,asdu,7,negative=True);return
                # A point can be reserved by only one live connection.
                for other in tuple(self._reactor.links):
                    choice=other.selections.get(key)
                    if other is not link and choice and choice[1]>=time.monotonic() and not other.closed:
                        self._reply(link,asdu,7,negative=True);return
                link.selections[key]=(signature,time.monotonic()+self.select_timeout_ms/1000)
                self._reply(link,asdu,7);return
            if point.command_mode==CommandMode.SELECT_AND_EXECUTE and (not selected or selected[0]!=signature):
                self._reply(link,asdu,7 if cot==6 else 9,negative=True);return
            if message.is_select_command and cot!=8:
                self._reply(link,asdu,9,negative=True);return
            old=point.info
            try: success=point._received(info,message)
            except Exception:
                LOG.exception('IEC104 command callback failed');success=False
            if not success: point.info=old
            elif cot==8:
                point.info=old;link.selections.pop(key,None)
            else:
                link.selections.pop(key,None)
            self._reply(link,asdu,7 if cot==6 else 9,negative=not success)
            if success and cot==6:
                self._reply(link,asdu,10)
                if point.related_io_autoreturn and point.related_io_address is not None:
                    related=station.get_point(point.related_io_address)
                    if related:
                        src_val=point.value
                        if related._base_type in (1, 3):
                            from core.iec104_values import binary_command_value
                            bit=bool(binary_command_value(src_val))
                            related.value=(Double.ON if bit else Double.OFF) if related._base_type==3 else bit
                        else:
                            related.value=src_val
                        related.transmit(Cot.SPONTANEOUS)
        else:
            self._reply(link,asdu,45)


class Client:
    def __init__(self, command_timeout_ms=10000, **kwargs):
        self.command_timeout_ms=int(command_timeout_ms)
        self.connections=[];self._reactor=Reactor(self);self.is_running=False

    def add_connection(self, ip, port=2404, init=Init.ALL):
        connection=Connection(self,ip,port,init)
        self.connections.append(connection);return connection

    def start(self):
        if not self.is_running:
            self.is_running=True;self._reactor.start()

    def stop(self):
        self.is_running=False;self._reactor.stop()

    def _poll(self, now):
        for connection in tuple(self.connections): connection._poll(now)


class Connection:
    def __init__(self, client, ip, port, init):
        self.client,self.ip,self.port,self.init=client,ip,int(port),init
        self.protocol_parameters=ProtocolParameters();self.originator_address=0
        self.state=ConnectionState.CLOSED
        self._link=None;self._stations={};self._next_retry=0
        self._receive_raw=self._send_raw=self._state_callback=self._unexpected=None
        self._command_lock=threading.RLock();self._pending=None;self._pending_lock=threading.Lock()
        self.last_error=None;self._initial_generation=None
        self._link_seq=0
        self._address_index=0

    @property
    def is_connected(self): return self.state in (ConnectionState.OPEN,ConnectionState.OPEN_MUTED)
    @property
    def is_muted(self): return self.state == ConnectionState.OPEN_MUTED
    @property
    def stations(self): return list(self._stations.values())
    def add_station(self, common_address):
        if common_address in self._stations: return None
        station=Station(self,common_address);self._stations[common_address]=station;return station
    def get_station(self, common_address): return self._stations.get(common_address)
    def on_state_change(self, callback): self._state_callback=callback
    def on_unexpected_message(self, callback): self._unexpected=callback
    def on_receive_raw(self, callback): self._receive_raw=callback
    def on_send_raw(self, callback): self._send_raw=callback

    def _state(self, state):
        self.state=state
        invoke(self._state_callback,self,state)

    def _poll(self, now):
        if (self._link is not None and not self._link.closed) or now<self._next_retry: return
        self._next_retry=now+.5
        sock=None
        try:
            addresses=socket.getaddrinfo(self.ip,self.port,0,socket.SOCK_STREAM)
            family,kind,proto,_,address=addresses[self._address_index % len(addresses)]
            self._address_index+=1
            sock=socket.socket(family,kind,proto);sock.setblocking(False)
            code=sock.connect_ex(address)
            pending={0,errno.EINPROGRESS,errno.EWOULDBLOCK,errno.EALREADY,10035}
            if code not in pending: raise OSError(code,'TCP 连接失败')
            self._state(ConnectionState.CLOSED_AWAIT_OPEN)
            self._link_seq+=1
            self._link=Link(self.client._reactor,sock,self,connecting=code!=0)
            self._link._seq=self._link_seq
            self.client._reactor.add(self._link)
        except OSError as exc:
            if sock: sock.close()
            self.last_error=str(exc);self._state(ConnectionState.CLOSED_AWAIT_OPEN)

    def _connected(self, link):
        self._state(ConnectionState.OPEN_MUTED)
        if self.init!=Init.MUTED: link.u(7,request=True)

    def _active(self, link, active):
        self._state(ConnectionState.OPEN if active else ConnectionState.OPEN_MUTED)
        link_token=getattr(link,'_seq',id(link))
        if active and self.init not in (Init.NONE,Init.MUTED) and self._initial_generation!=link_token:
            self._initial_generation=link_token
            threading.Thread(target=self._initialize,args=(link,),name='iec104-init',daemon=True).start()

    def _initialize(self, link):
        for station in self.stations:
            if self._link is not link or link.closed: return
            if self.init in (Init.ALL,Init.CLOCK_SYNC_ONLY): self.clock_sync(station.common_address)
            if self.init in (Init.ALL,Init.INTERROGATION_ONLY): self.interrogation(station.common_address)

    def _closed(self, link, reason):
        self.last_error=reason;self._next_retry=time.monotonic()+.5
        self._state(ConnectionState.CLOSED_AWAIT_OPEN if self.client.is_running else ConnectionState.CLOSED)
        with self._pending_lock:
            if self._pending:
                self._pending['ok']=False;self._pending['event'].set()

    def _raw(self, link, direction, frame):
        try: invoke(self._receive_raw if direction=='RX' else self._send_raw,self,frame)
        except Exception: LOG.exception('IEC104 client raw observer failed')

    @staticmethod
    def _pending_matches(frame, pending_asdu):
        """匹配主站控制/对时/总召确认帧；对时(103)和测试(107)允许 RTU 回填实际时钟。"""
        if len(frame) < 15 or len(pending_asdu) < 9:
            return False
        if frame[6:8] != pending_asdu[:2]:
            return False
        typ = pending_asdu[0]
        if typ in (103, 107):
            # 比对公共地址 CA 与信息体地址 IOA（跳过可能被 RTU 校准的时间戳与 OA 差异）
            return frame[10:15] == pending_asdu[4:9]
        return frame[9:] == pending_asdu[3:]

    def _asdu(self, link, frame):
        asdu = frame[6:]
        if len(asdu) >= 6 and asdu[0] not in Type._value2member_map_:
            # 未支持的合法标准 ASDU 类型仅触发异常回调，不抛出异常导致断链重连死循环。
            unknown_msg = IncomingMessage(
                asdu[0], asdu[2] & 63, int.from_bytes(asdu[4:6], 'little'),
                asdu[3], bool(asdu[2] & 64), False, frame,
            )
            invoke(self._unexpected, self, unknown_msg, Umc.UNKNOWN_TYPE_ID)
            return
        message,rows=decode_asdu(asdu,frame)
        station=self.get_station(message.common_address)
        if station is None:
            invoke(self._unexpected,self,message,Umc.UNKNOWN_CA);return
        if int(message.type) in MONITOR:
            if message.is_negative: return
            if int(message.cot) not in _MONITOR_COTS:
                invoke(self._unexpected,self,message,Umc.INVALID_COT);return
            with station._lock:
                resolved=[(ioa,info,station._points.get(ioa)) for ioa,info,_ in rows]
            received_at=datetime.now(timezone.utc)
            msg_type=message.type
            for ioa,info,point in resolved:
                message.io_address=ioa
                if point is None:
                    invoke(self._unexpected,self,message,Umc.UNKNOWN_IOA);continue
                if point.type!=msg_type:
                    invoke(self._unexpected,self,message,Umc.MISMATCHED_TYPE_ID);continue
                if not point._received(info,message,received_at=received_at): return False
        with self._pending_lock:
            pending=self._pending
            if pending and self._pending_matches(frame, pending['asdu']):
                if int(message.cot)==pending['expected'] or message.is_negative or int(message.cot) in (44,45,46,47):
                    pending['ok']=int(message.cot)==pending['expected'] and not message.is_negative
                    pending['event'].set()

    def _request(self, asdu, expected, wait=True):
        link=self._link
        if not link or not link.active or link.closed: return False
        if not wait: return link.enqueue([asdu]) is not None
        with self._command_lock:
            request={'asdu':asdu,'expected':int(expected),'event':threading.Event(),'ok':False}
            with self._pending_lock: self._pending=request
            try:
                if link.enqueue([asdu]) is None: return False
                return request['event'].wait(self.client.command_timeout_ms/1000) and request['ok']
            finally:
                with self._pending_lock: self._pending=None

    def _point_command(self, point, cause):
        if int(point.type) not in COMMAND: return False
        info=point.info
        def request(select):
            return header(point.type,1,cause,point.station.common_address,self.originator_address)+point.io_address.to_bytes(3,'little')+encode_information(point.type,info,select=select)
        with self._command_lock:
            progress = getattr(self,'command_progress',None)
            if int(cause)==6 and point.command_mode==CommandMode.SELECT_AND_EXECUTE:
                if progress: progress('select_sent')
                if not self._request(request(True),7): return False
                if progress: progress('select_confirmed')
            if progress: progress('execute_sent')
            return self._request(request(False),9 if int(cause)==8 else 7)

    def interrogation(self, common_address, cause=Cot.ACTIVATION, qualifier=Qoi.STATION, wait_for_response=True):
        asdu=header(100,1,cause,common_address,self.originator_address)+b'\0\0\0'+bytes((int(qualifier),))
        return self._request(asdu,10,wait_for_response)

    def clock_sync(self, common_address, wait_for_response=True):
        asdu=header(103,1,6,common_address,self.originator_address)+b'\0\0\0'+encode_time()
        return self._request(asdu,7,wait_for_response)

    def test(self, common_address, with_time=True, wait_for_response=True):
        typ=107 if with_time else 104
        data=b'\x01\0'+encode_time() if with_time else b'\xaa\x55'
        return self._request(header(typ,1,6,common_address,self.originator_address)+b'\0\0\0'+data,7,wait_for_response)

    def unmute(self): return bool(self._link and self._link.u(7,request=True))
    def mute(self): return bool(self._link and self._link.u(19,request=True))
    def test_fr(self, wait_for_response=True):
        link=self._link
        if not link or link.closed or link.connecting: return False
        link._testfr_event.clear()
        if not link.u(67,request=True): return False
        if not wait_for_response: return True
        deadline=time.monotonic()+self.client.command_timeout_ms/1000
        while True:
            if link.closed: return False
            with link.lock:
                if 67 not in link.pending_u: return True
            remaining=deadline-time.monotonic()
            if remaining<=0: return False
            link._testfr_event.wait(min(remaining,0.05))
            link._testfr_event.clear()

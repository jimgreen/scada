"""Selector-driven IEC104 APCI. One reactor per server/client, bounded queues."""
from collections import deque
import errno
import logging
import selectors
import socket
import struct
import threading
import time

from core.iec104_protocol import parse_apdu

LOG = logging.getLogger(__name__)
MAX_QUEUED_BYTES = 4 * 1024 * 1024
_STRUCT_H = struct.Struct('<H')
_STRUCT_HH = struct.Struct('<HH')


class Link:
    def __init__(self, reactor, sock, owner, *, connecting=False):
        self.reactor, self.sock, self.owner = reactor, sock, owner
        self.parameters = owner.protocol_parameters
        self.lock = threading.RLock()
        self.connecting = connecting
        self.closed = False
        self.active = False
        self.generation = 0
        self.send_sequence = self.receive_sequence = 0
        self.ack_sequence = 0
        self.pending = deque()
        self.pending_bytes = 0
        self.serial = self.acked_serial = 0
        self.outstanding = deque()
        self.output = deque()
        self.output_offset = 0
        self.input = bytearray()
        self.received_unacked = 0
        self.first_unacked = None
        self.last_activity = time.monotonic()
        self.connect_started = self.last_activity
        self.pending_u = {}
        self.peer = None
        self.ident = id(self)
        self.selections = {}
        self._selector_events = None
        self._testfr_event = threading.Event()
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def established(self):
        self.connecting = False
        self.peer = self.sock.getpeername()
        self.owner._connected(self)

    def enqueue(self, asdus):
        asdus = tuple(bytes(a) for a in asdus)
        total = sum(map(len, asdus))
        with self.lock:
            if self.closed or not self.active or self.pending_bytes+total > MAX_QUEUED_BYTES:
                return None
            if any(not 6 <= len(a) <= self.parameters.max_asdu_length for a in asdus):
                raise ValueError('ASDU 超过配置长度限制')
            for asdu in asdus:
                self.serial += 1
                self.pending.append((asdu,self.serial))
            self.pending_bytes += total
            serial = self.serial
        self.reactor.wake()
        return serial

    def u(self, code, *, request=False):
        with self.lock:
            if self.closed: return False
            self.output.append(bytes((0x68,4,code,0,0,0)))
            if request:
                self.pending_u.setdefault(code,time.monotonic())
        self.reactor.wake()
        return True

    def _s(self):
        self.output.append(b'\x68\x04\x01\x00'+_STRUCT_H.pack(self.receive_sequence*2))
        self.received_unacked = 0
        self.first_unacked = None

    def set_active(self, active):
        with self.lock:
            if self.active == active:
                return
            self.active = active
            self.generation += 1
        self.owner._active(self, active)

    def _ack(self, sequence):
        delta = (sequence-self.ack_sequence)&32767
        if delta > len(self.outstanding):
            raise ValueError(f'异常确认序号 {sequence}，已发送至 {self.send_sequence}')
        for _ in range(delta):
            _,_,serial = self.outstanding.popleft()
            self.acked_serial = serial
        self.ack_sequence = sequence

    def receive(self):
        data = self.sock.recv(65536)
        if not data:
            self.close('对端关闭 TCP 连接')
            return
        buf = self.input
        buf.extend(data)
        offset = 0
        total = len(buf)
        try:
            while total - offset >= 2 and not self.closed:
                length = buf[offset + 1]
                if buf[offset] != 0x68 or not 4 <= length <= 253:
                    raise ValueError('无效 IEC104 起始字节或长度')
                size = length + 2
                if total - offset < size:
                    break
                frame = bytes(buf[offset:offset + size])
                offset += size
                self.last_activity = time.monotonic()
                self.owner._raw(self, 'RX', frame)
                parsed = parse_apdu(frame, max_asdu_length=self.parameters.max_asdu_length)
                if parsed.frame_type != 'I' and size != 6:
                    raise ValueError('S/U 帧不能携带 ASDU')
                if parsed.frame_type == 'I':
                    if not self.active or frame[4] & 1 or size < 12:
                        raise ValueError('未激活的数据帧或无效 I 帧')
                    if parsed.send_sequence != self.receive_sequence:
                        raise ValueError(f'异常发送序号 {parsed.send_sequence}，期望 {self.receive_sequence}')
                    with self.lock:
                        self._ack(parsed.receive_sequence)
                        self.receive_sequence = (self.receive_sequence + 1) & 32767
                    # Application persistence completes before an APCI ACK can leave.
                    if self.owner._asdu(self, frame) is False:
                        raise ValueError('接收回调失败，未确认该帧')
                    with self.lock:
                        self.received_unacked += 1
                        if self.first_unacked is None:
                            self.first_unacked = time.monotonic()
                        if self.received_unacked >= self.parameters.receive_window_size:
                            self._s()
                elif parsed.frame_type == 'S':
                    with self.lock:
                        self._ack(parsed.receive_sequence)
                else:
                    code = frame[2]
                    if code == 7:
                        self.u(11)
                        self.set_active(True)
                    elif code == 11:
                        with self.lock:
                            self.pending_u.pop(7, None)
                        self.set_active(True)
                    elif code == 19:
                        with self.lock:
                            if self.received_unacked:
                                self._s()
                        self.u(35)
                        self.set_active(False)
                    elif code == 35:
                        with self.lock:
                            self.pending_u.pop(19, None)
                        self.set_active(False)
                    elif code == 67:
                        self.u(131)
                    elif code == 131:
                        with self.lock:
                            self.pending_u.pop(67, None)
                        self._testfr_event.set()
        finally:
            if offset:
                del buf[:offset]

    def tick(self, now):
        if self.closed: return
        p=self.parameters
        with self.lock:
            if self.connecting:
                if now-self.connect_started>p.message_timeout:
                    raise TimeoutError('TCP 连接超时')
                return
            if self.outstanding and now-self.outstanding[0][1]>p.message_timeout:
                raise TimeoutError('t1：I 帧确认超时')
            if self.pending_u and any(now-since>p.message_timeout for since in self.pending_u.values()):
                raise TimeoutError('t1：U 帧确认超时')
            if self.received_unacked and now-self.first_unacked>=p.confirm_interval:
                self._s()
            if now-self.last_activity>=p.keep_alive_interval and 67 not in self.pending_u:
                self.u(67,request=True)
            while self.active and self.pending and len(self.outstanding)<p.send_window_size:
                asdu,serial=self.pending.popleft();self.pending_bytes-=len(asdu)
                frame=bytes((0x68,len(asdu)+4))+_STRUCT_HH.pack(self.send_sequence*2,self.receive_sequence*2)+asdu
                self.outstanding.append((self.send_sequence,now,serial))
                self.send_sequence=(self.send_sequence+1)&32767
                self.output.append(frame)
                self.received_unacked=0;self.first_unacked=None

    def send(self):
        sent_frames=[]
        with self.lock:
            budget=65536
            while self.output and budget>0:
                if len(self.output)==1:
                    frame=self.output[0]
                    try: count=self.sock.send(memoryview(frame)[self.output_offset:])
                    except BlockingIOError: break
                    if count==0: raise ConnectionError('TCP 发送返回 0')
                    budget-=count;self.output_offset+=count;self.last_activity=time.monotonic()
                    if self.output_offset==len(frame):
                        self.output.popleft();self.output_offset=0;sent_frames.append(frame)
                    break
                # Coalesce queued APDUs into one TCP write to avoid per-frame
                # syscall and TCP_NODELAY packet overhead during bursts.
                chunk=bytearray()
                offset=self.output_offset
                for frame in self.output:
                    part=memoryview(frame)[offset:]
                    offset=0
                    if chunk and len(chunk)+len(part)>budget:
                        break
                    chunk.extend(part)
                    if len(chunk)>=budget:
                        break
                try: count=self.sock.send(chunk)
                except BlockingIOError: break
                if count==0: raise ConnectionError('TCP 发送返回 0')
                budget-=count;self.last_activity=time.monotonic()
                remaining=count
                while remaining and self.output:
                    frame=self.output[0]
                    need=len(frame)-self.output_offset
                    if remaining>=need:
                        remaining-=need
                        self.output.popleft();self.output_offset=0;sent_frames.append(frame)
                    else:
                        self.output_offset+=remaining
                        remaining=0
                if count<len(chunk):
                    break
        for frame in sent_frames: self.owner._raw(self,'TX',frame)

    def close(self, reason):
        if self.closed: return
        self.closed=True;self.active=False;self.generation+=1;self._selector_events=None
        self._testfr_event.set()
        try: self.reactor.selector.unregister(self.sock)
        except (KeyError,ValueError,AttributeError): pass
        try: self.sock.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        self.sock.close()
        with self.lock:
            self.pending.clear();self.output.clear();self.pending_bytes=0
        self.owner._closed(self,reason)


class Reactor:
    def __init__(self, owner):
        self.owner=owner
        self.links=[]
        self.selector=None
        self.listener=None
        self.stopped=threading.Event()
        self.thread=None
        self.reader=self.writer=None

    def wake(self):
        if self.writer:
            try: self.writer.send(b'\0')
            except (OSError,BlockingIOError): pass

    def add(self, link):
        self.links.append(link)
        events=selectors.EVENT_READ | (selectors.EVENT_WRITE if link.connecting else 0)
        self.selector.register(link.sock,events,link)
        link._selector_events=events
        if not link.connecting: link.established()

    def start(self, listener=None):
        self.selector=selectors.DefaultSelector()
        self.reader,self.writer=socket.socketpair()
        self.reader.setblocking(False);self.writer.setblocking(False)
        self.selector.register(self.reader,selectors.EVENT_READ,'wake')
        self.listener=listener
        if listener:
            listener.setblocking(False)
            self.selector.register(listener,selectors.EVENT_READ,'listen')
        self.stopped.clear()
        self.thread=threading.Thread(target=self.run,name='iec104-python',daemon=True)
        self.thread.start()

    def _next_timeout(self, now):
        """根据活动链路定时器与宿主周期任务动态计算下一次 select 超时，避免空闲 20ms 高频空转。"""
        next_deadline = now + 0.25
        next_periodic = getattr(self.owner, '_next_periodic', 0)
        if next_periodic and next_periodic > 0:
            next_deadline = min(next_deadline, next_periodic)
        connections = getattr(self.owner, 'connections', None)
        if connections:
            for conn in tuple(connections):
                link = getattr(conn, '_link', None)
                if link is None or link.closed:
                    retry_at = getattr(conn, '_next_retry', 0)
                    if retry_at:
                        next_deadline = min(next_deadline, retry_at)
        for link in tuple(self.links):
            if link.closed:
                continue
            p = link.parameters
            if link.connecting:
                next_deadline = min(next_deadline, link.connect_started + p.message_timeout)
                continue
            if link.active and link.pending and len(link.outstanding) < p.send_window_size:
                return 0.005
            if link.outstanding:
                next_deadline = min(next_deadline, link.outstanding[0][1] + p.message_timeout)
            if link.pending_u:
                next_deadline = min(next_deadline, min(link.pending_u.values()) + p.message_timeout)
            if link.received_unacked and link.first_unacked is not None:
                next_deadline = min(next_deadline, link.first_unacked + p.confirm_interval)
            if 67 not in link.pending_u:
                next_deadline = min(next_deadline, link.last_activity + p.keep_alive_interval)
        return max(0.005, min(0.25, next_deadline - now))

    def run(self):
        try:
            while not self.stopped.is_set():
                now=time.monotonic()
                self.owner._poll(now)
                for link in tuple(self.links):
                    if link.closed:
                        self.links.remove(link);continue
                    try:
                        link.tick(now)
                        events=selectors.EVENT_READ | (selectors.EVENT_WRITE if link.output or link.connecting else 0)
                        if events!=link._selector_events:
                            self.selector.modify(link.sock,events,link)
                            link._selector_events=events
                    except (OSError,ValueError,TimeoutError) as exc:
                        link.close(str(exc))
                timeout = self._next_timeout(time.monotonic())
                for key,mask in self.selector.select(timeout):
                    if key.data=='wake':
                        try:
                            while self.reader.recv(4096): pass
                        except BlockingIOError: pass
                        continue
                    if key.data=='listen':
                        try:
                            while True:
                                sock,peer=self.listener.accept()
                                self.owner._accept(sock,peer)
                        except BlockingIOError: pass
                        continue
                    link=key.data
                    if link.closed: continue
                    try:
                        if link.connecting:
                            error=link.sock.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR)
                            if error: raise OSError(error,'TCP 连接失败')
                            link.established()
                        if mask&selectors.EVENT_READ: link.receive()
                        if mask&selectors.EVENT_WRITE and not link.closed: link.send()
                    except Exception as exc:
                        link.close(str(exc))
        except Exception:
            LOG.exception('IEC104 reactor failed')
        finally:
            self.owner.is_running=False
            for link in tuple(self.links): link.close('服务停止')
            self.links.clear()
            if self.listener: self.listener.close()
            for sock in (self.reader,self.writer):
                if sock: sock.close()
            self.reader=self.writer=None
            self.selector.close()

    def stop(self):
        self.stopped.set();self.wake()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(10)
            if self.thread.is_alive(): raise TimeoutError('IEC104 接收回调未退出')

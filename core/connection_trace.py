"""Attach observers to a native connection or an individual asyncio protocol."""
import logging
import threading
import time


LOG = logging.getLogger(__name__)


def configure_history(server, db_path, component, *, protocol, channel_id=None, rtu_id=None):
    if hasattr(server, 'set_connection_history'):
        from core.packet_logging import packet_log_switch
        server._connection_history_settings = (db_path, component, {
            'service':f'scdfe_{component}', 'protocol':protocol,
            'channel_id':channel_id, 'rtu_id':rtu_id,
            'packet_enabled':(packet_log_switch(db_path,component).for_channel(channel_id).enabled
                              if channel_id is not None else lambda:False)})


class ConnectionObserver:
    def __init__(self, native, history, metadata, local):
        if not hasattr(native, 'enable_connection_trace'):
            raise RuntimeError('IEC104 引擎缺少连接观察接口')
        self.native, self.history, self.metadata, self.local = native, history, metadata, local
        self.sessions = {}
        self.stop_event = threading.Event()
        native.enable_connection_trace(True)
        self.thread = threading.Thread(target=self._run, name='iec104-connections', daemon=True)

    def start(self):
        self.thread.start()

    def drain(self):
        for key, kind, stamp, peer, data, dropped in self.native.drain_connection_trace():
            if kind == 99:
                self.history.dropped += dropped
                for ident in self.sessions.values():
                    self.history.event(ident,'log',f'连接观察队列溢出：服务共丢失 {dropped} 条记录；统计不完整')
                    self.history.state(ident,'unknown','队列溢出后连接状态待确认')
                continue
            at = stamp/1000
            if key not in self.sessions:
                self.sessions[key] = self.history.open(self.metadata,peer,self.local,at)
                if kind != 10:
                    self.history.event(self.sessions[key],'log','接入事件缺失：从首次可观察事件开始记录',at=at)
            ident = self.sessions[key]
            if kind in (1,2):
                self.history.event(ident,'packet','IEC104','RX' if kind==1 else 'TX',data,at)
            elif kind in (11,12,13):
                state, reason = {11:('closed',data.decode('utf8','replace') if data else '客户端连接关闭'),
                                 12:('active','STARTDT：数据传输已激活'),
                                 13:('inactive','STOPDT：数据传输未激活')}[kind]
                self.history.state(ident,state,reason,at)
                if kind == 11:
                    del self.sessions[key]

    def _run(self):
        while not self.stop_event.wait(.1):
            try:
                self.drain()
            except Exception:
                LOG.exception('IEC104 连接观察失败')

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(5)
        self.drain()
        for ident in self.sessions.values():
            self.history.state(ident,'closed','服务停止；连接结束')
        self.sessions.clear()
        self.native.enable_connection_trace(False)
        self.history.flush()


class ClientTrace:
    def __init__(self, history, metadata, peer, local):
        self.history, self.metadata = history, metadata
        self.ident = history.open(metadata,peer,local)
        self.buffers = {'RX':bytearray(), 'TX':bytearray()}
        self.closed = False

    def packet(self, direction, data):
        protocol = self.metadata['protocol']
        if protocol != 'Modbus-TCP':
            for start in range(0, len(data), 65536):
                self.history.event(self.ident,'packet',protocol+' TCP片段',direction,data[start:start+65536])
            return
        buffer = self.buffers[direction]
        buffer.extend(data)
        while len(buffer) >= 7:
            size = int.from_bytes(buffer[4:6],'big') + 6
            if buffer[2:4] != b'\0\0' or not 8 <= size <= 260:
                self.history.event(self.ident,'packet','Modbus-TCP 无效流片段',direction,buffer)
                buffer.clear()
                return
            if len(buffer) < size:
                return
            frame = bytes(buffer[:size]); del buffer[:size]
            self.history.event(self.ident,'packet',protocol,direction,frame)

    def close(self, reason):
        if self.closed:
            return
        self.closed = True
        for direction, buffer in self.buffers.items():
            if buffer:
                self.history.event(self.ident,'packet','连接结束时未完成的TCP帧',direction,buffer)
        self.history.state(self.ident,'closed',reason)


class ObservedTransport:
    def __init__(self, transport, trace):
        self.transport, self.trace = transport, trace

    def __getattr__(self, name):
        return getattr(self.transport,name)

    def write(self, data):
        result = self.transport.write(data)
        self.trace.packet('TX',data)
        return result

    def writelines(self, lines):
        self.write(b''.join(lines))


def observe_protocol(protocol, history, metadata):
    made, received, lost = protocol.connection_made, protocol.data_received, protocol.connection_lost
    trace = None
    def connection_made(transport):
        nonlocal trace
        trace = ClientTrace(history,metadata,transport.get_extra_info('peername'),transport.get_extra_info('sockname'))
        return made(ObservedTransport(transport,trace))
    def data_received(data):
        if trace:
            trace.packet('RX',data)
        return received(data)
    def connection_lost(exc):
        if trace:
            trace.close(f'连接断开：{exc}' if exc else '连接关闭（对端关闭或服务结束）')
        return lost(exc)
    protocol.connection_made, protocol.data_received, protocol.connection_lost = connection_made,data_received,connection_lost
    return protocol

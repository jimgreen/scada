"""Per-connection, observation-only tracing of actual protocol bytes."""
import logging
import time
from datetime import datetime, timezone
from itertools import count

LOG = logging.getLogger(__name__)
_packet_ids = count(1)


def packet_lines(direction, data, detail=""):
    """Keep every byte; split large TCP chunks into bounded readable lines."""
    payload = bytes(data)
    detail = str(detail).replace("\r", " ").replace("\n", " ")
    if "IEC104 APDU" in detail:
        try:
            from core.iec104_protocol import describe_apdu
            detail = f"{detail} {describe_apdu(payload)}"
        except (ImportError, ValueError):
            detail = f"{detail} IEC104 invalid-apdu"
    count = max(1, (len(payload) + 1023) // 1024)
    packet_id = next(_packet_ids)
    for part in range(count):
        chunk = payload[part * 1024:(part + 1) * 1024]
        yield (f"报文 {direction} {detail} frame={packet_id} bytes={len(payload)} "
               f"part={part + 1}/{count} HEX={chunk.hex(' ')}")


def service_packet_recorder(record, rtu_id, channel_id):
    """Keep delayed callbacks tied to the transport that received the frame."""
    prefix = '' if channel_id is None else f'通道={channel_id} '
    suffix = '' if rtu_id is None else f' RTU {rtu_id}'
    from core.communication_log import CommunicationLog
    owner = getattr(record, '__self__', None)
    file_log = getattr(owner, '_file_log', None)
    if file_log is not None:
        logger = file_log._logger_for(channel_id)
        def write_many(lines):
            # Also populate the bounded communication-test preview. Release
            # its lock before file I/O; _record acquires these in that order.
            stamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            with owner._log_lock:
                owner._logs.extend({'time': stamp, 'level': 'INFO', 'message': line} for line in lines)
            logger.business_lines(lines)
    else:
        def write_many(lines):
            for line in lines:
                record('INFO', line, channel_id=channel_id)
    business = CommunicationLog(write_many, 组件='S', RTU编号=rtu_id, 通道编号=channel_id)
    def receive(direction, data, detail=''):
        business.packet(direction, data, detail)
        for line in packet_lines(direction, data, prefix + detail + suffix):
            record('PACKET', line, channel_id=channel_id)
    receive.business = business
    return receive


class PacketTrace:
    """Adapters expose a callback installed before connect/start by their owner."""
    PACKET_PROTOCOL = ""

    def set_packet_handler(self, handler):
        if handler is not None and not callable(handler):
            raise TypeError("packet handler must be callable")
        self._packet_handler = handler

    def _business_logger(self):
        return getattr(getattr(self, '_packet_handler', None), 'business', None)

    def _business(self, action, rows=None, **fields):
        logger = self._business_logger()
        if logger is not None:
            # User-provided observers are isolated just like packet callbacks.
            try:
                logger.rows(action, [{}] if rows is None else rows, **fields)
            except Exception:
                now = time.monotonic()
                if now >= getattr(self, '_next_business_error', 0):
                    self._next_business_error = now + 60
                    LOG.exception('通信业务观察回调失败')

    def _trace_modbus_pdu(self, sending, pdu):
        # Library PDU callback is after framing/CRC validation. Never decode
        # arbitrary stream chunks as requests, or correlate across TCP clients.
        if self._business_logger() is None:
            return pdu
        fields = dict(方向='TX' if sending else 'RX', 规约=self.PACKET_PROTOCOL,
                      链路=getattr(self, '_packet_endpoint', ''),
                      站号=getattr(pdu, 'dev_id', None), 事务标识=getattr(pdu, 'transaction_id', None),
                      功能码=pdu.function_code, 异常码=getattr(pdu, 'exception_code', None))
        is_read_response = sending and pdu.function_code in {1,2,3,4}
        if not is_read_response and pdu.function_code < 128:
            fields['PDU地址0基'] = getattr(pdu, 'address', None)
            fields['PDU数量'] = getattr(pdu, 'count', None)
        fields['数据区'] = {1:'线圈',2:'离散输入',3:'保持寄存器',4:'输入寄存器',
                           5:'线圈',6:'保持寄存器',15:'线圈',16:'保持寄存器'}.get(pdu.function_code & 127, '其他')
        # encode() is the library's own typed representation, not a new codec.
        try:
            fields['PDU数据'] = pdu.encode().hex(' ')
        except Exception:
            fields['PDU数据'] = '库未提供编码'
        values = getattr(pdu, 'registers', None) or getattr(pdu, 'bits', None)
        rows = ({'响应内偏移' if is_read_response else '数据偏移': i, '原始值': list(values[i:i+64])}
                for i in range(0,len(values),64)) if values else None
        if is_read_response:
            fields['地址说明'] = '读响应不含地址，需同连接TID请求对应；位响应末尾可能含填充位'
        self._business('Modbus响应' if sending else 'Modbus请求', rows=rows, **fields)
        return pdu

    def _packet(self, direction, data, detail=""):
        handler = getattr(self, "_packet_handler", None)
        if handler is not None:
            try:
                handler(direction, bytes(data), f"{self.PACKET_PROTOCOL} {detail}".strip())
            except Exception:
                # A full log disk must not alter a frame or disconnect a channel.
                now = time.monotonic()
                if now >= getattr(self, "_next_packet_error", 0):
                    self._next_packet_error = now + 60
                    LOG.exception("报文日志写入失败")

    def _trace_packet(self, sending, data):
        self._packet("TX" if sending else "RX", data, getattr(self, "_packet_endpoint", ""))
        return data  # pymodbus trace hooks may transform bytes; ours never do.

    def _trace_can(self, direction, message):
        self._business('CAN错误帧' if message.is_error_frame else 'CAN帧',
            方向=direction, 接口=getattr(self, 'interface', ''), 总线=getattr(self, 'channel', ''),
            帧ID=f'0x{message.arbitration_id:X}', DLC=message.dlc, 数据长度=len(message.data),
            帧格式='扩展帧' if message.is_extended_id else '标准帧', FD=int(message.is_fd),
            RTR=int(message.is_remote_frame), BRS=int(getattr(message, 'bitrate_switch', False)),
            ESI=int(getattr(message, 'error_state_indicator', False)),
            驱动时标=getattr(message, 'timestamp', None), 数据=bytes(message.data).hex(' '),
            确认范围='驱动提交，不代表ACK或设备执行' if direction == 'TX' else '驱动接收')
        self._packet(direction, message.data,
                     f"ID=0x{message.arbitration_id:X} DLC={message.dlc} "
                     f"EXT={int(message.is_extended_id)} FD={int(message.is_fd)} "
                     f"RTR={int(message.is_remote_frame)} ERR={int(message.is_error_frame)} "
                     f"BRS={int(getattr(message, 'bitrate_switch', False))} "
                     f"ESI={int(getattr(message, 'error_state_indicator', False))}")

    def _trace_can_state(self, bus):
        if self._business_logger() is None or time.monotonic() < getattr(self, '_next_can_state', 0):
            return
        self._next_can_state = time.monotonic() + 1
        try:
            state = getattr(bus, 'state', None)
            state = getattr(state, 'name', str(state)) if state is not None else '驱动未提供'
        except Exception:
            state = '驱动未提供'
        if state != getattr(self, '_last_can_state', None):
            self._business('CAN驱动总线状态', 旧状态=getattr(self, '_last_can_state', '未知'),
                           新状态=state, 来源='python-can bus.state；非物理层测量')
            self._last_can_state = state


class _TracedTransport:
    def __init__(self, transport, trace):
        self._transport, self._trace = transport, trace

    def __getattr__(self, name):
        return getattr(self._transport, name)

    def write(self, data):
        result = self._transport.write(data)
        self._trace("TX", data, f"TCP peer={self._transport.get_extra_info('peername')}")
        return result

    def writelines(self, lines):
        self.write(b"".join(lines))


def trace_stream_protocol(protocol, trace):
    """Wrap one asyncua protocol instance, never patch a library globally.

    TCP callbacks represent stream chunks, including handshakes and encrypted
    bytes as actually transported. Their boundaries need not be UA messages.
    """
    made, received = protocol.connection_made, protocol.data_received
    peer = None

    def connection_made(transport):
        nonlocal peer
        peer = transport.get_extra_info("peername")
        return made(_TracedTransport(transport, trace))

    def data_received(data):
        trace("RX", data, f"TCP peer={peer}")
        return received(data)

    protocol.connection_made = connection_made
    protocol.data_received = data_received
    return protocol


def trace_stream_server_loop(loop, trace):
    """Install a protocol factory wrapper on a server's private event loop."""
    create_server = loop.create_server

    async def traced_server(factory, *args, **kwargs):
        return await create_server(lambda: trace_stream_protocol(factory(), trace), *args, **kwargs)

    loop.create_server = traced_server


def trace_bacnet_app(app, trace):
    """Observe encoded BACnet/IP BVLL datagrams at this app's UDP boundary."""
    server = app.normal.server
    indication, confirmation = server.indication, server.confirmation

    async def send(pdu):
        data, peer = bytes(pdu.pduData), str(pdu.pduDestination)
        result = await indication(pdu)
        trace("TX", data, f"UDP peer={peer}")
        return result

    async def receive(pdu):
        trace("RX", pdu.pduData, f"UDP peer={pdu.pduSource}")
        return await confirmation(pdu)

    server.indication, server.confirmation = send, receive

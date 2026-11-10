"""Per-connection, observation-only tracing of actual protocol bytes."""
import logging
import time
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
    def receive(direction, data, detail=''):
        for line in packet_lines(direction, data, prefix + detail + suffix):
            record('PACKET', line, channel_id=channel_id)
    return receive


class PacketTrace:
    """Adapters expose a callback installed before connect/start by their owner."""
    PACKET_PROTOCOL = ""

    def set_packet_handler(self, handler):
        if handler is not None and not callable(handler):
            raise TypeError("packet handler must be callable")
        self._packet_handler = handler

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
        self._packet(direction, message.data,
                     f"ID=0x{message.arbitration_id:X} DLC={message.dlc} "
                     f"EXT={int(message.is_extended_id)} FD={int(message.is_fd)} "
                     f"RTR={int(message.is_remote_frame)}")


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

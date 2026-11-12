"""CAN 主站适配层，底层使用 ``python-can``。

不在这里假设 CANopen/J1939 等上层协议；接口传递原始 CAN/CAN-FD 帧。
真实设备只需选择 python-can 支持的 interface/channel/bitrate，例如
``socketcan``、``pcan``、``vector`` 或 ``kvaser``。测试可使用 ``virtual``。
"""
from collections import deque
from collections.abc import Mapping
import math
import struct
import threading
import time
from typing import NamedTuple
from core.packet_trace import PacketTrace

try:
    import can
except ImportError as exc:  # pragma: no cover
    can = None
    _CAN_IMPORT_ERROR = exc
else:
    _CAN_IMPORT_ERROR = None

__all__ = ['CanMaster', 'CanFrame', 'CanValue', 'CanError', 'CanTimeout', 'CAN_AVAILABLE']
CAN_AVAILABLE = can is not None


class CanError(RuntimeError):
    """CAN 设备、帧参数或过滤配置错误。"""


class CanTimeout(CanError, TimeoutError):
    """在指定期限内未收到匹配的 CAN 应答。"""


class CanFrame(NamedTuple):
    """不可变的独立帧快照；data 始终从驱动缓冲区复制为 bytes。"""
    arbitration_id: int
    data: bytes
    is_extended_id: bool = False
    is_remote_frame: bool = False
    is_fd: bool = False
    bitrate_switch: bool = False
    timestamp: float | None = None
    dlc: int | None = None
    is_error_frame: bool = False

    @classmethod
    def from_message(cls, message):
        return cls(message.arbitration_id, bytes(message.data), message.is_extended_id,
                    message.is_remote_frame, message.is_fd, message.bitrate_switch,
                    message.timestamp, message.dlc, message.is_error_frame)


class CanValue(NamedTuple):
    """A four遥 value decoded from one configured CAN frame."""
    rtu_id: int | None
    point_no: int
    kind: str
    value: object
    quality: int
    frame: CanFrame
    received_at: float


def _require_can():
    if can is None:
        raise CanError('缺少依赖 python-can，请执行: python -m pip install -r requirements.txt') from _CAN_IMPORT_ERROR


def _seconds(value, name, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or (positive and value == 0)):
        raise ValueError(f'{name} 必须是{"正" if positive else "非负"}有限数')
    return float(value)


def _count(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'{name} 必须是正整数')
    return value


class _CanTransport(PacketTrace):
    """同步 CAN/CAN-FD 传输；由公开的 ``CanMaster`` 组合使用。"""
    PACKET_PROTOCOL = "CAN"

    def __init__(self, *, interface='virtual', channel=None, bitrate=500000,
                 data_bitrate=None, fd=False, receive_own_messages=False,
                 sample_point=None, data_sample_point=None,
                 bitrate_switch=False, receive_error_frames=False,
                 restart_on_error=False, bus_off_recovery_ms=0,
                 auto_start=True, filters=None, **kwargs):
        _require_can()
        if not isinstance(interface, str) or not interface:
            raise ValueError('interface 必须是 python-can 支持的字符串')
        if bitrate is not None and (not isinstance(bitrate, int) or bitrate <= 0):
            raise ValueError('bitrate 必须是正整数')
        if sample_point is not None and (not isinstance(sample_point, (int, float)) or not 0 < sample_point <= 100):
            raise ValueError('sample_point 必须在 0 到 100 之间')
        if data_sample_point is not None and (not isinstance(data_sample_point, (int, float)) or not 0 < data_sample_point <= 100):
            raise ValueError('data_sample_point 必须在 0 到 100 之间')
        if isinstance(bus_off_recovery_ms, bool) or not isinstance(bus_off_recovery_ms, int) or bus_off_recovery_ms < 0:
            raise ValueError('bus_off_recovery_ms 必须是非负整数')
        self.interface, self.channel, self.bitrate, self.fd = interface, channel, bitrate, fd
        self.sample_point = sample_point
        self.data_sample_point = data_sample_point
        self.bitrate_switch = bool(bitrate_switch)
        self.receive_error_frames = bool(receive_error_frames)
        self.restart_on_error = bool(restart_on_error)
        self.bus_off_recovery_ms = bus_off_recovery_ms
        options = dict(interface=interface, channel=channel, bitrate=bitrate,
                       receive_own_messages=receive_own_messages, **kwargs)
        if data_bitrate is not None:
            options['data_bitrate'] = data_bitrate
        if sample_point is not None:
            options['sample_point'] = sample_point
        if data_sample_point is not None:
            options['data_sample_point'] = data_sample_point
        if receive_error_frames:
            options['receive_error_frames'] = True
        if restart_on_error:
            options['restart_ms'] = bus_off_recovery_ms
        if fd:
            options['fd'] = True
        options = {k: v for k, v in options.items() if v is not None}
        if filters is not None:
            options['can_filters'] = self._normal_filters(filters)
        self._bus_options = dict(options)
        self.bus = None
        if auto_start:
            self.connect()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    @property
    def connected(self):
        return self.bus is not None

    def _reopen(self):
        try:
            self.bus = can.Bus(**self._bus_options)
            send = self.bus.send
            def traced_send(message, *args, **kwargs):
                result = send(message, *args, **kwargs)
                self._trace_can("TX", message)
                return result
            self.bus.send = traced_send
        except Exception as exc:
            raise CanError(f'打开 CAN 总线失败: {exc}') from exc

    def connect(self):
        if self.bus is None:
            self._reopen()
        return self

    @staticmethod
    def _validate_id(arbitration_id, extended):
        limit = 0x1FFFFFFF if extended else 0x7FF
        if (isinstance(arbitration_id, bool) or not isinstance(arbitration_id, int)
                or not 0 <= arbitration_id <= limit):
            raise ValueError(f'arbitration_id 必须在 0..{limit:#x}')

    def message(self, arbitration_id, data=b'', *, is_extended_id=False,
                is_remote_frame=False, is_fd=None, bitrate_switch=False, dlc=None):
        self._validate_id(arbitration_id, is_extended_id)
        data = bytes(data)
        if is_remote_frame and data:
            raise ValueError('远程帧不能携带 data')
        if len(data) > (64 if (self.fd if is_fd is None else is_fd) else 8):
            raise ValueError('经典 CAN 最多 8 字节，CAN-FD 最多 64 字节')
        if dlc is not None and (isinstance(dlc, bool) or not isinstance(dlc, int)):
            raise ValueError('dlc 必须是整数')
        return can.Message(arbitration_id=arbitration_id, data=data, dlc=dlc, check=True,
                           is_extended_id=is_extended_id,
                           is_remote_frame=is_remote_frame,
                           is_fd=self.fd if is_fd is None else is_fd,
                           bitrate_switch=bitrate_switch)

    def send(self, arbitration_id, data=b'', *, timeout=None, **kwargs):
        if not self.connected:
            raise CanError('CAN 总线未打开')
        message = self.message(arbitration_id, data, **kwargs)
        try:
            self.bus.send(message, timeout=timeout)
        except Exception as exc:
            raise CanError(f'发送 CAN 帧失败: {exc}') from exc
        return CanFrame.from_message(message)

    def recv(self, timeout=1.0):
        if not self.connected:
            raise CanError('CAN 总线未打开')
        if timeout is not None:
            _seconds(timeout, 'timeout')
        return self._recv_frame(timeout)

    def _recv_frame(self, timeout):
        """Receive after the public entry point checked state and timeout."""
        try:
            message = self.bus.recv(timeout)
        except Exception as exc:
            raise CanError(f'接收 CAN 帧失败: {exc}') from exc
        if message is not None:
            self._trace_can("RX", message)
        return None if message is None else CanFrame.from_message(message)

    def receive(self, *, timeout=1.0, count=None):
        start = time.monotonic()
        received = 0
        while count is None or received < count:
            remaining = timeout
            if timeout is not None:
                remaining = max(0.0, timeout - (time.monotonic() - start))
            frame = self.recv(remaining)
            if frame is None:
                break
            received += 1
            yield frame

    @classmethod
    def _normal_filters(cls, filters):
        normal = []
        for item in filters:
            if not isinstance(item, dict) or 'can_id' not in item or 'can_mask' not in item:
                raise ValueError('每个 CAN filter 必须包含 can_id 和 can_mask')
            extended = bool(item.get('extended', False))
            cls._validate_id(item['can_id'], extended)
            cls._validate_id(item['can_mask'], extended)
            normal.append({'can_id': item['can_id'], 'can_mask': item['can_mask'],
                           'extended': extended})
        return normal

    def set_filters(self, filters):
        """filters 采用 python-can 的 [{can_id, can_mask, extended}] 结构，重连时保留。"""
        normal = self._normal_filters(filters)
        if not self.connected:
            raise CanError('CAN 总线未打开')
        self.bus.set_filters(normal)
        self._bus_options['can_filters'] = normal

    def send_periodic(self, arbitration_id, data=b'', period=1.0, duration=None, **kwargs):
        _seconds(period, 'period', positive=True)
        if duration is not None:
            _seconds(duration, 'duration', positive=True)
        if not self.connected:
            raise CanError('CAN 总线未打开')
        message = self.message(arbitration_id, data, **kwargs)
        try:
            # When tracing, use python-can's thread scheduler so our bus.send
            # wrapper observes every successful send, including periodic ones.
            trace_options = ({"modifier_callback": lambda msg: None}
                             if getattr(self, "_packet_handler", None) is not None else {})
            return self.bus.send_periodic(message, period, duration=duration, **trace_options)
        except Exception as exc:
            raise CanError(f'启动周期发送失败: {exc}') from exc

    def close(self):
        if self.bus is not None:
            self.bus.shutdown()
            self.bus = None


class CanMaster(_CanTransport):
    """原始 CAN/CAN-FD 的应用控制端，不假设 CANopen/J1939 的节点语义。

    默认延迟打开硬件，connect/with 后才通信。request 按显式响应ID、帧类型
    和可选 matcher 匹配；未匹配报文保留供 recv/collect_once 读取。
    同一实例的接收/请求串行执行，应用不应再直接用 bus.recv 或另建 Notifier。
    """
    _STORE_RTU_PROJECTION = 'rtu_id,channel_id,channel_type'
    _STORE_PARAMETER_PROJECTION = (
        'channel_id,interface,channel,bitrate,data_bitrate,fd,receive_own_messages,'
        'sample_point,data_sample_point,bitrate_switch,receive_error_frames,'
        'restart_on_error,bus_off_recovery_ms'
    )
    _STORE_POINT_PROJECTION = (
        'channel_id,kind,pnt_no,frame_id,tx_frame_id,response_id,is_extended_id,is_fd,'
        'byte_offset,bit_offset,bit_length,value_type,byte_order,scale,offset,'
        'quality_byte_offset,quality_mask,tx_data_hex,response_data_hex,dlc,period_ms,'
        'bitrate_switch,enabled,dbc_message_name,dbc_signal_name,unit,is_signed'
    )

    def __init__(self, *, rtu_id=None, auto_start=False, **kwargs):
        if rtu_id is not None:
            _count(rtu_id, 'rtu_id')
        self.rtu_id = rtu_id
        self._receive_lock = threading.RLock()
        self._send_lock = threading.RLock()
        self._pending = deque()
        self._frame_schedule = None
        self._points: list[dict] = []
        self._point_index: dict[tuple[str, int], dict] = {}
        self._frame_index: dict[tuple[int, bool], tuple[dict, ...]] = {}
        self._frame_decoders = {}
        self._frame_word_orders = {}
        super().__init__(auto_start=auto_start, **kwargs)

    @classmethod
    def from_channel(cls, channel, *, interface, **kwargs):
        """协议参数中的 channel 为驱动通道名，interface/bitrate 由调用者指定。"""
        def value(name):
            return channel.get(name) if isinstance(channel, Mapping) else getattr(channel, name, None)
        kwargs.setdefault('rtu_id', value('rtu_id'))
        return cls(interface=interface, channel=value('channel'), **kwargs)

    @classmethod
    def from_store(cls, store, rtu_id, **kwargs):
        """从当前 RTU 记录和 ``fes_channel_can_define`` 参数表构造主站。"""
        with store.connection() as connection:
            connection.execute('BEGIN')
            rtu = connection.execute(
                f'SELECT {cls._STORE_RTU_PROJECTION} FROM fes_rtu_define WHERE rtu_id=?', (rtu_id,),
            ).fetchone()
            if rtu is None:
                raise ValueError(f'RTU {rtu_id} 不存在')
            if rtu['channel_type'] != 'can':
                raise ValueError(f'RTU {rtu_id} 不是 CAN 通道')
            channel_id = int(rtu['channel_id'])
            rows = [dict(row) for row in connection.execute(
                f'SELECT {cls._STORE_PARAMETER_PROJECTION} '
                'FROM fes_channel_can_define WHERE channel_id=? LIMIT 2', (channel_id,),
            )]
            point_rows = [dict(row) for row in connection.execute(
                f'SELECT {cls._STORE_POINT_PROJECTION} '
                'FROM fes_channel_can_point_define WHERE channel_id=? AND COALESCE(enabled, 1)=1 '
                'ORDER BY kind, pnt_no', (channel_id,)
            )]
        if len(rows) > 1:
            raise ValueError(f'RTU {rtu_id} 需要唯一 CAN 参数定义，实际找到 {len(rows)} 条')
        parameter = rows[0] if rows else {}
        interface = kwargs.pop('interface', parameter.get('interface'))
        bus_channel = kwargs.pop('channel', parameter.get('channel'))
        if not interface:
            raise ValueError('CAN 参数需要 interface，例如 virtual、socketcan 或 pcan')
        for name in ('bitrate', 'data_bitrate', 'fd', 'receive_own_messages',
                     'sample_point', 'data_sample_point', 'bitrate_switch',
                     'receive_error_frames', 'restart_on_error', 'bus_off_recovery_ms'):
            if parameter.get(name) is not None and name not in kwargs:
                kwargs[name] = parameter[name]
        if 'filters' not in kwargs:
            # The point-map is already the authoritative receive-address
            # definition.  Push exact IDs into python-can so unrelated bus
            # traffic is rejected by the driver/kernel before it reaches the
            # Python receive queue.  Response IDs are included because command
            # requests may wait for them even when no four遥 point uses that
            # frame as a normal measurement.
            filters = cls._filters_from_points(point_rows)
            if filters:
                kwargs['filters'] = filters
        kwargs.setdefault('rtu_id', rtu_id)
        master = cls(interface=interface, channel=bus_channel, **kwargs)
        try:
            if point_rows:
                master.configure(point_rows)
            from core.can_schedule import CanFrameSchedule, load_frame_definitions
            master._frame_schedule = CanFrameSchedule(lambda *a, **kw: master.send(*a, timeout=.5, **kw),
                                                      load_frame_definitions(store, rtu_id), fd=master.fd)
            if master.connected:
                master._frame_schedule.start()
        except Exception:
            master.close()
            raise
        return master

    @classmethod
    def _filters_from_points(cls, points):
        """Build exact python-can filters from persisted point mappings."""
        filters = {}
        for row in points:
            extended = bool(row.get('is_extended_id', False))
            mask = 0x1FFFFFFF if extended else 0x7FF
            for name in ('frame_id', 'response_id'):
                frame_id = row.get(name)
                if frame_id is None:
                    continue
                frame_id = int(frame_id)
                cls._validate_id(frame_id, extended)
                filters[(frame_id, extended)] = {
                    'can_id': frame_id, 'can_mask': mask, 'extended': extended,
                }
        return list(filters.values())

    @staticmethod
    def _point_kind(kind):
        if kind not in {'yc', 'yx', 'yk', 'yt'}:
            raise ValueError('CAN 点 kind 必须是 yc/yx/yk/yt')
        return kind

    @staticmethod
    def _hex_bytes(value, name):
        if value in (None, ''):
            return bytearray()
        if not isinstance(value, str):
            raise ValueError(f'{name} 必须是十六进制字符串')
        try:
            compact = ''.join(value.split())
            if len(compact) % 2:
                raise ValueError
            return bytearray.fromhex(compact)
        except ValueError as exc:
            raise ValueError(f'{name} 不是有效十六进制字符串') from exc

    @classmethod
    def _validate_point(cls, row):
        row = dict(row)
        kind = cls._point_kind(row.get('kind'))
        point_no = row.get('pnt_no')
        if isinstance(point_no, bool) or not isinstance(point_no, int) or point_no < 1:
            raise ValueError('CAN 点号必须是正整数')
        frame_id = row.get('frame_id')
        if frame_id is None and kind in {'yc', 'yx'}:
            raise ValueError(f'CAN {kind}/{point_no} 缺少 frame_id')
        if frame_id is not None:
            frame_id = int(frame_id)
        extended = bool(row.get('is_extended_id', False))
        if frame_id is not None:
            cls._validate_id(frame_id, extended)
        for id_name in ('tx_frame_id', 'response_id'):
            if row.get(id_name) is not None:
                cls._validate_id(int(row[id_name]), extended)
        byte_offset = int(row.get('byte_offset') or 0)
        bit_offset = int(row.get('bit_offset') or 0)
        bit_length = int(row.get('bit_length') or (1 if kind == 'yx' else 8))
        if byte_offset < 0 or bit_offset < 0 or bit_offset > 7 or bit_length < 1 or bit_length > 64:
            raise ValueError(f'CAN {kind}/{point_no} 的字节/位范围无效')
        if (row.get('byte_order') or 'big') not in {'big', 'little'}:
            raise ValueError('CAN byte_order 必须为 big 或 little')
        value_type = str(row.get('value_type') or ('bool' if kind == 'yx' else 'float' if kind in {'yc', 'yt'} else 'uint')).lower()
        if value_type not in {'bool', 'uint', 'int', 'float'}:
            raise ValueError('CAN value_type 必须是 bool/uint/int/float')
        if value_type == 'float' and (bit_offset or bit_length not in (32, 64)):
            raise ValueError('CAN float 必须使用 bit_offset=0 且 bit_length=32 或 64')
        cls._hex_bytes(row.get('tx_data_hex'), 'tx_data_hex')
        cls._hex_bytes(row.get('response_data_hex'), 'response_data_hex')
        dlc = row.get('dlc')
        if dlc is not None:
            if (isinstance(dlc, bool) or not isinstance(dlc, int)
                    or not 0 <= dlc <= (64 if row.get('is_fd') else 8)):
                raise ValueError('CAN dlc 超出当前帧格式范围')
        period_ms = row.get('period_ms')
        if (period_ms is not None and
                (isinstance(period_ms, bool) or not isinstance(period_ms, int) or period_ms < 0)):
            raise ValueError('CAN period_ms 必须是非负整数')
        row.update(kind=kind, pnt_no=point_no, frame_id=frame_id,
                   byte_offset=byte_offset, bit_offset=bit_offset,
                   bit_length=bit_length, byte_order=str(row.get('byte_order') or 'big'),
                   value_type=value_type, scale=float(row.get('scale') if row.get('scale') is not None else 1.0),
                   offset=float(row.get('offset') if row.get('offset') is not None else 0.0))
        if row['scale'] == 0 or not math.isfinite(row['scale']) or not math.isfinite(row['offset']):
            raise ValueError('CAN scale 必须非零且 scale/offset 必须有限')
        return row

    def configure(self, points):
        checked, seen = [], set()
        frame_index = {}
        for point in points:
            row = self._validate_point(point)
            key = (row['kind'], row['pnt_no'])
            if key in seen:
                raise ValueError(f'重复 CAN 点 {key}')
            seen.add(key)
            checked.append(row)
            frame_id = row.get('frame_id')
            if frame_id is not None:
                frame_index.setdefault((int(frame_id), bool(row.get('is_extended_id', False))), []).append(row)
        decoders = {key: tuple(self._compile_decoder(row) for row in rows)
                    for key, rows in frame_index.items()}
        # Dense frames share one full-payload integer per byte order. Sparse
        # frames keep the existing slice decoder to avoid preparation overhead.
        word_orders = {key: tuple(dict.fromkeys(row['byte_order'] for row in rows
                                               if row['value_type'] != 'float'))
                       for key, rows in frame_index.items()
                       if sum(row['value_type'] != 'float' for row in rows) >= 4}
        with self._receive_lock:
            self._points = checked
            self._point_index = {(row['kind'], row['pnt_no']): row for row in checked}
            self._frame_index = {
                key: tuple(rows) for key, rows in frame_index.items()
            }
            self._frame_decoders = decoders
            self._frame_word_orders = word_orders
        return self

    @staticmethod
    def _compile_decoder(row):
        """Resolve offsets, masks and codecs once per configuration."""
        start, bits, offset = row['byte_offset'], row['bit_length'], row['bit_offset']
        end = start + (offset + bits + 7) // 8
        order, typ = row['byte_order'], row['value_type']
        shift = (end - start) * 8 - offset - bits if order == 'big' else offset
        mask, sign, wrap = (1 << bits) - 1, 1 << (bits - 1), 1 << bits
        scale, bias = row['scale'], row['offset']
        unpack = struct.Struct(('>' if order == 'big' else '<') +
                               ('f' if bits == 32 else 'd')).unpack_from if typ == 'float' else None
        q_offset = row.get('quality_byte_offset')
        q_offset = -1 if q_offset is None else int(q_offset)
        q_mask = int(row.get('quality_mask') or 0xFF)
        point_no, kind = row['pnt_no'], row['kind']
        little = order == 'little'
        whole_shift = start * 8 + offset + (0 if little else bits)

        def decode(frame, rtu_id, words=None):
            raw = frame.data
            if end > len(raw):
                raise ValueError(f"CAN 帧 {frame.arbitration_id:#x} 数据长度不足")
            if unpack is not None:
                value = unpack(raw, start)[0] * scale + bias
            else:
                if words is None:
                    number = (int.from_bytes(raw[start:end], order) >> shift) & mask
                else:
                    number = (words[order] >> (whole_shift if little else len(raw)*8-whole_shift)) & mask
                if typ == 'bool':
                    value = bool(number)
                else:
                    if typ == 'int' and number & sign:
                        number -= wrap
                    value = number * scale + bias
            quality = raw[q_offset] & q_mask if 0 <= q_offset < len(raw) else 0
            return CanValue(rtu_id, point_no, kind, value, quality, frame,
                            frame.timestamp or time.time())
        return decode

    @staticmethod
    def _extract_value(frame, row):
        raw = frame.data
        start = int(row['byte_offset'])
        bits = int(row['bit_length'])
        offset = int(row['bit_offset'])
        needed = (offset + bits + 7) // 8
        if start + needed > len(raw):
            raise ValueError(f"CAN 帧 {frame.arbitration_id:#x} 数据长度不足")
        order = row.get('byte_order', 'big')
        chunk = raw[start:start + needed]
        number = int.from_bytes(chunk, order, signed=False)
        shift = (len(chunk) * 8 - offset - bits) if order == 'big' else offset
        number = (number >> shift) & ((1 << bits) - 1)
        value_type = row['value_type']
        if value_type == 'bool':
            value = bool(number)
        elif value_type == 'int':
            value = number - (1 << bits) if number & (1 << (bits - 1)) else number
        elif value_type == 'float':
            width = bits // 8
            if offset or bits not in (32, 64):
                value = float(number)
            else:
                value = struct.unpack('>f' if order == 'big' else '<f', chunk[-width:] if order == 'big' else chunk[:width])[0] if width == 4 else struct.unpack('>d' if order == 'big' else '<d', chunk[-width:] if order == 'big' else chunk[:width])[0]
        else:
            value = number
        return value if value_type == 'bool' else value * row['scale'] + row['offset']

    @classmethod
    def _decode_point(cls, frame, row, rtu_id):
        if row.get('frame_id') is None or frame.arbitration_id != row['frame_id'] or frame.is_extended_id != bool(row.get('is_extended_id', False)):
            return None
        value = cls._extract_value(frame, row)
        q_offset = row.get('quality_byte_offset')
        quality = 0
        if q_offset is not None and 0 <= int(q_offset) < len(frame.data):
            quality = int(frame.data[int(q_offset)]) & int(row.get('quality_mask') or 0xFF)
        return CanValue(rtu_id, row['pnt_no'], row['kind'], value, quality, frame, frame.timestamp or time.time())

    def connect(self):
        with self._receive_lock, self._send_lock:
            result = super().connect()
            if self._frame_schedule is not None:
                self._frame_schedule.start()
            return result

    def close(self):
        if self._frame_schedule is not None:
            self._frame_schedule.stop()
        with self._receive_lock, self._send_lock:
            try:
                super().close()
            finally:
                self._pending.clear()

    def schedule_frames(self, **selection):
        if self._frame_schedule is None:
            raise ValueError('尚未加载 CAN 报文定义')
        return self._frame_schedule.trigger(**selection)

    @property
    def diagnostics(self):
        return {'frame_schedule': self._frame_schedule.status() if self._frame_schedule else None}

    def send(self, arbitration_id, data=b'', *, timeout=None, **kwargs):
        if timeout is not None:
            _seconds(timeout, 'timeout')
        with self._send_lock:
            return super().send(arbitration_id, data, timeout=timeout, **kwargs)

    def recv(self, timeout=1.0):
        if timeout is not None:
            _seconds(timeout, 'timeout')
        with self._receive_lock:
            if not self.connected:
                raise CanError('CAN 总线未打开')
            if self._pending:
                return self._pending.popleft()
            return self._recv_frame(timeout)

    def request(self, arbitration_id, data=b'', *, response_id, timeout=1.0,
                response_extended=False, matcher=None, **kwargs):
        """发一次请求并等待数据应答；超时抛 CanTimeout，不自动重发。

        response_extended 显式区分11/29位ID，matcher(frame)可检查序号或数据。
        CAN 本身没有事务号；同ID旧帧的区分须由设备协议的序号匹配承担。
        硬件过滤器必须允许响应ID；已暂存的非匹配帧不会被新请求作为应答。
        """
        _seconds(timeout, 'timeout', positive=True)
        self._validate_id(response_id, response_extended)
        if matcher is not None and not callable(matcher):
            raise TypeError('matcher 必须可调用')
        self.message(arbitration_id, data, **kwargs)  # 发送前校验全部参数
        with self._receive_lock:
            deadline = time.monotonic() + timeout
            self.send(arbitration_id, data, timeout=timeout, **kwargs)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                frame = super().recv(remaining)
                if frame is None:
                    break
                candidate = (frame.arbitration_id == response_id
                             and frame.is_extended_id == response_extended
                             and not frame.is_remote_frame and not frame.is_error_frame)
                try:
                    matches = candidate and (matcher is None or matcher(frame))
                except Exception:
                    self._pending.append(frame)
                    raise
                if matches:
                    return frame
                self._pending.append(frame)
        raise CanTimeout(f'等待 CAN 应答 {response_id:#x} 超时（{timeout} 秒）')

    def collect_once(self, *, timeout=0.1, max_frames=100):
        """在整批 timeout 内读取最多 max_frames 帧；无数据返回空列表。"""
        _seconds(timeout, 'timeout')
        _count(max_frames, 'max_frames')
        with self._receive_lock:
            if not self.connected:
                raise CanError('CAN 总线未打开')
            # Keep the hot loop local-variable only.  A zero timeout is used by
            # drain-style polling and has no deadline to enforce, so avoid a
            # monotonic clock call for every frame in that common path.
            frames = []
            append = frames.append
            pending = self._pending
            popleft = pending.popleft
            receive_frame = self._recv_frame
            if timeout == 0:
                for _ in range(max_frames):
                    frame = popleft() if pending else receive_frame(0.0)
                    if frame is None:
                        break
                    append(frame)
                return self._decode_frames(frames)

            monotonic = time.monotonic
            deadline = monotonic() + timeout
            for index in range(max_frames):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    if index:
                        break
                    remaining = 0.0
                frame = popleft() if pending else receive_frame(remaining)
                if frame is None:
                    break
                append(frame)
            return self._decode_frames(frames)

    def _decode_frames(self, frames):
        """Decode configured four遥 mappings; without mappings preserve raw frames."""
        frame_index = self._frame_decoders
        if not frame_index:
            return frames
        decoded = []
        if not self._frame_word_orders:
            for frame in frames:
                for decode in frame_index.get((frame.arbitration_id, bool(frame.is_extended_id)), ()):
                    value = decode(frame, self.rtu_id)
                    if value is not None:
                        decoded.append(value)
            return decoded
        for frame in frames:
            key = (frame.arbitration_id, bool(frame.is_extended_id))
            orders = self._frame_word_orders.get(key)
            words = {order: int.from_bytes(frame.data, order) for order in orders} if orders else None
            for decode in frame_index.get(key, ()):
                try:
                    value = decode(frame, self.rtu_id, words)
                except ValueError:
                    # A frame with the right ID but a short payload is a
                    # malformed sample; leave it to the caller as a protocol
                    # error rather than silently writing a wrong point.
                    raise
                if value is not None:
                    decoded.append(value)
        return decoded

    def _point_definition(self, kind, point_no, points=None):
        if points is None:
            point = self._point_index.get((kind, int(point_no)))
            if point is None:
                raise ValueError(f'点号 {point_no} 没有唯一的 CAN {kind} 点定义')
            return point
        rows = [self._validate_point(row) for row in points]
        matches = [row for row in rows if row['kind'] == kind and row['pnt_no'] == int(point_no)]
        if len(matches) != 1:
            raise ValueError(f'点号 {point_no} 没有唯一的 CAN {kind} 点定义')
        return matches[0]

    def _command_frame(self, row, value, *, wait_response=False, response_timeout=1.0):
        tx_id = row.get('tx_frame_id')
        if tx_id is None:
            raise ValueError(f"CAN {row['kind']}/{row['pnt_no']} 缺少 tx_frame_id")
        payload = self._hex_bytes(row.get('tx_data_hex'), 'tx_data_hex')
        if not payload:
            payload = bytearray(row.get('dlc') or (8 if bool(row.get('is_fd')) is False else 64))
        if row.get('dlc') is not None and len(payload) < int(row['dlc']):
            payload.extend(b'\x00' * (int(row['dlc']) - len(payload)))
        bits = int(row['bit_length'])
        start, offset = int(row['byte_offset']), int(row['bit_offset'])
        order = row.get('byte_order', 'big')
        needed = (offset + bits + 7) // 8
        if start + needed > len(payload):
            payload.extend(b'\x00' * (start + needed - len(payload)))
        def send_payload(payload):
            if not wait_response:
                return self.send(int(tx_id), payload,
                                 is_extended_id=bool(row.get('is_extended_id', False)),
                                 is_fd=bool(row.get('is_fd', False)),
                                 bitrate_switch=(bool(row['bitrate_switch'])
                                                 if row.get('bitrate_switch') is not None
                                                 else self.bitrate_switch))
            response_id = row.get('response_id')
            if response_id is None:
                raise ValueError(f"CAN {row['kind']}/{row['pnt_no']} 未配置 response_id")
            expected = self._hex_bytes(row.get('response_data_hex'), 'response_data_hex')
            matcher = None
            if expected:
                def match_response(frame):
                    return bytes(frame.data[:len(expected)]) == bytes(expected)
                matcher = match_response
            return self.request(
                int(tx_id), payload, response_id=int(response_id),
                response_extended=bool(row.get('is_extended_id', False)),
                timeout=response_timeout, matcher=matcher,
                is_extended_id=bool(row.get('is_extended_id', False)),
                is_fd=bool(row.get('is_fd', False)),
                bitrate_switch=(bool(row['bitrate_switch'])
                                if row.get('bitrate_switch') is not None
                                else self.bitrate_switch),
            )

        if row['value_type'] == 'float' and not offset and bits in (32, 64):
            width = bits // 8
            encoded_bytes = struct.pack(
                ('>' if order == 'big' else '<') + ('f' if bits == 32 else 'd'),
                float(value),
            )
            payload[start:start + width] = encoded_bytes
            return send_payload(payload)
        if row['kind'] == 'yk':
            encoded = 1 if bool(value) else 0
        else:
            encoded = int(round((float(value) - row['offset']) / row['scale']))
            if encoded < 0:
                encoded &= (1 << bits) - 1
        current = int.from_bytes(payload[start:start + needed], order)
        shift = (needed * 8 - offset - bits) if order == 'big' else offset
        mask = ((1 << bits) - 1) << shift
        current = (current & ~mask) | ((encoded << shift) & mask)
        payload[start:start + needed] = current.to_bytes(needed, order)
        return send_payload(payload)

    def send_yk_point(self, point_no, close, points=None, *, wait_response=False,
                      response_timeout=1.0):
        row = point_no if isinstance(point_no, Mapping) else self._point_definition('yk', point_no, points)
        return self._command_frame(row, bool(close), wait_response=wait_response,
                                   response_timeout=response_timeout)

    def send_yt_point(self, point_no, value, points=None, *, wait_response=False,
                      response_timeout=1.0):
        row = point_no if isinstance(point_no, Mapping) else self._point_definition('yt', point_no, points)
        return self._command_frame(row, value, wait_response=wait_response,
                                   response_timeout=response_timeout)

    def poll_loop(self, callback, *, timeout=0.1, max_frames=100, interval=0.0,
                  stop_event=None, max_cycles=None):
        """阻塞式接收轮询；每批回调（含空批）后等待 interval，错误直接抛出。

        stop_event 可中断批次间等待；当前接收批次最多等待 timeout 秒。
        """
        if not callable(callback):
            raise TypeError('callback 必须可调用')
        _seconds(timeout, 'timeout')
        _seconds(interval, 'interval')
        _count(max_frames, 'max_frames')
        if timeout == 0 and interval == 0:
            raise ValueError('timeout 和 interval 不能同时为0，以免空轮询占用CPU')
        if max_cycles is not None:
            _count(max_cycles, 'max_cycles')
        if stop_event is not None and not all(
                callable(getattr(stop_event, name, None)) for name in ('is_set', 'wait')):
            raise TypeError('stop_event 必须提供 is_set()/wait()')
        event = stop_event if stop_event is not None else threading.Event()
        cycles = 0
        while not event.is_set():
            callback(self.collect_once(timeout=timeout, max_frames=max_frames))
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            event.wait(interval)
        return cycles

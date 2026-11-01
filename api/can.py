"""CAN 主站适配层，底层使用 ``python-can``。

不在这里假设 CANopen/J1939 等上层协议；接口传递原始 CAN/CAN-FD 帧。
真实设备只需选择 python-can 支持的 interface/channel/bitrate，例如
``socketcan``、``pcan``、``vector`` 或 ``kvaser``。测试可使用 ``virtual``。
"""
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
import math
import threading
import time

try:
    import can
except ImportError as exc:  # pragma: no cover
    can = None
    _CAN_IMPORT_ERROR = exc
else:
    _CAN_IMPORT_ERROR = None

__all__ = ['CanMaster', 'CanFrame', 'CanError', 'CanTimeout', 'CAN_AVAILABLE']
CAN_AVAILABLE = can is not None


class CanError(RuntimeError):
    """CAN 设备、帧参数或过滤配置错误。"""


class CanTimeout(CanError, TimeoutError):
    """在指定期限内未收到匹配的 CAN 应答。"""


@dataclass(frozen=True)
class CanFrame:
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


class _CanTransport:
    """同步 CAN/CAN-FD 传输；由公开的 ``CanMaster`` 组合使用。"""

    def __init__(self, *, interface='virtual', channel=None, bitrate=500000,
                 data_bitrate=None, fd=False, receive_own_messages=False,
                 auto_start=True, filters=None, **kwargs):
        _require_can()
        if not isinstance(interface, str) or not interface:
            raise ValueError('interface 必须是 python-can 支持的字符串')
        if bitrate is not None and (not isinstance(bitrate, int) or bitrate <= 0):
            raise ValueError('bitrate 必须是正整数')
        self.interface, self.channel, self.bitrate, self.fd = interface, channel, bitrate, fd
        options = dict(interface=interface, channel=channel, bitrate=bitrate,
                       receive_own_messages=receive_own_messages, **kwargs)
        if data_bitrate is not None:
            options['data_bitrate'] = data_bitrate
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
        try:
            message = self.bus.recv(timeout)
        except Exception as exc:
            raise CanError(f'接收 CAN 帧失败: {exc}') from exc
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
            return self.bus.send_periodic(message, period, duration=duration)
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

    def __init__(self, *, rtu_id=None, auto_start=False, **kwargs):
        if rtu_id is not None:
            _count(rtu_id, 'rtu_id')
        self.rtu_id = rtu_id
        self._receive_lock = threading.RLock()
        self._send_lock = threading.RLock()
        self._pending = deque()
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
                'SELECT * FROM fes_rtu_define WHERE rtu_id=?', (rtu_id,),
            ).fetchone()
            if rtu is None:
                raise ValueError(f'RTU {rtu_id} 不存在')
            if rtu['channel_type'] != 'can':
                raise ValueError(f'RTU {rtu_id} 不是 CAN 通道')
            rows = [dict(row) for row in connection.execute(
                'SELECT * FROM fes_channel_can_define WHERE rtu_id=?', (rtu_id,),
            )]
        if len(rows) > 1:
            raise ValueError(f'RTU {rtu_id} 需要唯一 CAN 参数定义，实际找到 {len(rows)} 条')
        parameter = rows[0] if rows else {}
        interface = kwargs.pop('interface', parameter.get('interface'))
        bus_channel = kwargs.pop('channel', parameter.get('channel'))
        if not interface:
            raise ValueError('CAN 参数需要 interface，例如 virtual、socketcan 或 pcan')
        for name in ('bitrate', 'data_bitrate', 'fd', 'receive_own_messages'):
            if parameter.get(name) is not None and name not in kwargs:
                kwargs[name] = parameter[name]
        kwargs.setdefault('rtu_id', rtu_id)
        return cls(interface=interface, channel=bus_channel, **kwargs)

    def connect(self):
        with self._receive_lock, self._send_lock:
            return super().connect()

    def close(self):
        with self._receive_lock, self._send_lock:
            try:
                super().close()
            finally:
                self._pending.clear()

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
            return super().recv(timeout)

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
            deadline = time.monotonic() + timeout
            frames = []
            while len(frames) < max_frames:
                remaining = max(0.0, deadline - time.monotonic())
                if timeout > 0 and frames and remaining == 0:
                    break
                frame = self.recv(remaining)
                if frame is None:
                    break
                frames.append(frame)
            return frames

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

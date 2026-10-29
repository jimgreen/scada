"""IEC 60870-5-104 服务端 API，底层使用 ``c104.Server``。

该模块对应旧工程 ``CRtu104`` 的 RTU 侧能力：提供公共地址、四遥点表映射、
遥信/遥测主动上送、遥控/遥调命令回调和站点初始化通知。链路状态机、APDU、
总召唤响应、选择/执行确认和 TCP 监听全部交给 c104/lib60870-C。

点号映射沿用客户端 API：``IOA = 起始地址 + 点号 - 1``，默认起始地址为
遥测 0x4001、遥信 0x01、遥控 0x6001、遥调 0x6201。
"""
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import threading

from .iec104 import _require_c104, c104

__all__ = ['IEC104Server', 'IEC104ServerError', 'IEC104Command']


class IEC104ServerError(RuntimeError):
    """服务端未启动、点表冲突、底层库拒绝操作或用户回调失败。"""


@dataclass(frozen=True)
class IEC104Command:
    """已收到的遥控/遥调命令快照。"""

    kind: str
    point_no: int
    value: object
    selected: bool
    cause: object
    originator_address: int
    received_at: datetime


def field(row, name, default=None):
    value = row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)
    return default if value is None else value


def attr_value(obj, name):
    """兼容 c104 不同 wheel 版本的属性/无参方法暴露方式。"""
    value = getattr(obj, name)
    return value() if callable(value) else value


class IEC104Server:
    """单站 IEC104 服务端。

    ``start`` 才会打开监听 socket；构造和点表注册不会绑定端口，便于在启动
    前检查点表。默认只监听本机所有地址 ``0.0.0.0:2404``，生产环境建议由
    部署配置明确指定 ``ip``、``port`` 和 ``max_connections``。
    """

    DEFAULT_BASE = {'yc': 0x4001, 'yx': 0x01, 'yk': 0x6001, 'yt': 0x6201}
    TYPE_NAMES = {
        'yx': ('M_SP_NA_1', 'M_DP_NA_1'),
        'yc': ('M_ME_NC_1', 'M_ME_NB_1', 'M_ME_NA_1'),
        'yk': ('C_SC_NA_1', 'C_DC_NA_1'),
        'yt': ('C_SE_NC_1', 'C_SE_NB_1', 'C_SE_NA_1'),
    }

    def __init__(self, *, ip='0.0.0.0', port=2404, common_address=1,
                 ycbegin_addr=0x4001, yxbegin_addr=1, ykbegin_addr=0x6001,
                 ytbegin_addr=0x6201, tick_rate_ms=100, select_timeout_ms=10000,
                 max_connections=0):
        _require_c104()
        if not isinstance(ip, str) or not ip:
            raise ValueError('ip 必须是非空字符串')
        if not 1 <= int(port) <= 65535:
            raise ValueError('port 必须在 1..65535')
        if not 1 <= int(common_address) <= 65534:
            raise ValueError('common_address 必须在 1..65534')
        if int(tick_rate_ms) < 1 or int(select_timeout_ms) < 1:
            raise ValueError('tick_rate_ms 和 select_timeout_ms 必须为正数')
        if int(max_connections) < 0:
            raise ValueError('max_connections 不能为负数')
        self.ip, self.port = ip, int(port)
        self.common_address = int(common_address)
        self.base = {
            'yc': int(ycbegin_addr) or self.DEFAULT_BASE['yc'],
            'yx': int(yxbegin_addr) or self.DEFAULT_BASE['yx'],
            'yk': int(ykbegin_addr) or self.DEFAULT_BASE['yk'],
            'yt': int(ytbegin_addr) or self.DEFAULT_BASE['yt'],
        }
        for kind, address in self.base.items():
            if not 1 <= address <= 0xFFFFFF:
                raise ValueError(f'{kind} 起始地址必须在 1..0xffffff')
        kwargs = dict(ip=ip, port=self.port, tick_rate_ms=int(tick_rate_ms),
                      select_timeout_ms=int(select_timeout_ms),
                      max_connections=int(max_connections))
        self._server = c104.Server(**kwargs)
        self._station = self._server.add_station(common_address=self.common_address)
        if self._station is None:
            raise IEC104ServerError(f'创建公共地址 {self.common_address} 失败')
        self._points = {}
        self._point_types = {}
        self._handlers = {}
        self._commands = {}
        self._errors = []
        self._lock = threading.RLock()

    @property
    def running(self):
        return bool(self._server.is_running)

    @property
    def server(self):
        """底层 c104.Server，供需要高级能力的调用者使用。"""
        return self._server

    @property
    def station(self):
        return self._station

    @property
    def open_connection_count(self):
        return int(self._server.open_connection_count)

    @property
    def active_connection_count(self):
        return int(self._server.active_connection_count)

    @property
    def last_error(self):
        return self._errors[-1] if self._errors else None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        if not self.running:
            try:
                self._server.start()
            except Exception as exc:
                raise IEC104ServerError(f'启动 IEC104 服务失败 {self.ip}:{self.port}: {exc}') from exc
        return self

    def stop(self):
        if self.running:
            self._server.stop()

    def set_connect_handler(self, handler: Callable[[str], bool]):
        """设置连接准入回调；返回 False 会拒绝客户端连接。"""
        if not callable(handler):
            raise TypeError('handler 必须可调用')

        def callback(server: c104.Server, ip: str) -> bool:
            return bool(handler(ip))
        self._server.on_connect(callback)

    def set_raw_handlers(self, *, on_receive=None, on_send=None):
        """设置原始 APDU 调试回调；回调参数为 bytes，不应在回调中阻塞。"""
        if on_receive is not None:
            if not callable(on_receive):
                raise TypeError('on_receive 必须可调用')

            def receive(server: c104.Server, data: bytes) -> None:
                on_receive(data)
            self._server.on_receive_raw(receive)
        if on_send is not None:
            if not callable(on_send):
                raise TypeError('on_send 必须可调用')

            def send(server: c104.Server, data: bytes) -> None:
                on_send(data)
            self._server.on_send_raw(send)

    def set_clock_sync_handler(self, handler):
        """设置对时回调；返回 False 或 FAILURE 会拒绝对时。"""
        if not callable(handler):
            raise TypeError('handler 必须可调用')

        def callback(server: c104.Server, ip: str,
                     date_time: datetime) -> c104.ResponseState:
            result = handler(ip, date_time)
            if result is False or result == c104.ResponseState.FAILURE:
                return c104.ResponseState.FAILURE
            return c104.ResponseState.SUCCESS
        self._server.on_clock_sync(callback)

    def point_address(self, kind, point_no):
        if kind not in self.base:
            raise ValueError('kind 必须是 yc/yx/yk/yt')
        if not isinstance(point_no, int) or point_no < 1:
            raise ValueError('点号必须是正整数')
        address = self.base[kind] + point_no - 1
        if address > 0xFFFFFF:
            raise ValueError('信息体地址超过 24 位')
        return address

    def point_number(self, kind, io_address):
        if kind not in self.base:
            raise ValueError('kind 必须是 yc/yx/yk/yt')
        number = int(io_address) - self.base[kind] + 1
        if number < 1:
            raise ValueError('信息体地址早于配置起始地址')
        return number

    def _type(self, kind, data_type=None):
        if kind not in self.TYPE_NAMES:
            raise ValueError('kind 必须是 yc/yx/yk/yt')
        if data_type is None:
            data_type = 1 if kind in ('yx', 'yc', 'yk') else 3
        data_type = int(data_type)
        choices = self.TYPE_NAMES[kind]
        if kind == 'yx':
            index = 1 if data_type == 2 else 0
        elif kind == 'yc':
            index = {1: 0, 2: 1, 3: 2}.get(data_type)
        elif kind == 'yk':
            index = 1 if data_type == 2 else 0
        else:
            index = {1: 2, 2: 1, 3: 0}.get(data_type)
        if index is None:
            raise ValueError(f'{kind} 不支持 data_type={data_type}')
        return getattr(c104.Type, choices[index])

    @staticmethod
    def _plain(value):
        if isinstance(value, (bool, int, float, str, bytes)) or value is None:
            return value
        name = value.__class__.__name__
        if name in {'Int16', 'UInt16', 'UInt5', 'UInt7', 'Int7', 'Double'}:
            return int(value)
        if name == 'NormalizedFloat':
            return float(value)
        if name == 'Byte32':
            return bytes(value)
        return value

    @staticmethod
    def _as_value(kind, data_type, value):
        if kind == 'yx':
            if int(data_type or 1) == 2:
                if isinstance(value, c104.Double):
                    return value
                return c104.Double.ON if int(value) else c104.Double.OFF
            return bool(value)
        if kind == 'yc':
            typ = int(data_type or 1)
            if typ == 1:
                return float(value)
            if typ == 2:
                return c104.Int16(int(value))
            if typ == 3:
                return c104.NormalizedFloat(float(value))
        if kind == 'yk':
            if int(data_type or 1) == 2:
                return value if isinstance(value, c104.Double) else (c104.Double.ON if int(value) else c104.Double.OFF)
            return bool(value)
        if kind == 'yt':
            typ = int(data_type or 3)
            if typ == 1:
                return c104.NormalizedFloat(float(value))
            if typ == 2:
                return c104.Int16(int(value))
            if typ == 3:
                return float(value)
        raise ValueError(f'不支持 {kind} data_type={data_type}')

    @staticmethod
    def _cause(cause):
        if isinstance(cause, str):
            cause = getattr(c104.Cot, cause.upper(), None)
        if cause is None:
            raise ValueError('cause 必须是 c104.Cot 或其名称')
        return cause

    def add_point(self, kind, point_no, *, data_type=None, value=None, report_ms=0,
                  related_io_address=None, related_io_autoreturn=False,
                  command_mode='DIRECT', command_handler=None):
        """注册一个四遥点；命令点可通过 command_handler 接收遥控/遥调。"""
        ioa = self.point_address(kind, point_no)
        typ = self._type(kind, data_type)
        mode = command_mode if not isinstance(command_mode, str) else getattr(
            c104.CommandMode, command_mode.upper(), None)
        if mode is None:
            raise ValueError(f'未知 command_mode={command_mode!r}')
        if kind in ('yk', 'yt') and command_handler is None:
            command_handler = self._default_command_handler
        with self._lock:
            old = self._station.get_point(io_address=ioa)
            if old is not None:
                if old.type != typ:
                    raise IEC104ServerError(f'点 {kind}/{point_no} 已存在且类型不同')
                point = old
            else:
                point = self._station.add_point(
                    io_address=ioa, type=typ, report_ms=int(report_ms),
                    related_io_address=related_io_address,
                    related_io_autoreturn=bool(related_io_autoreturn),
                    command_mode=mode)
            if point is None:
                raise IEC104ServerError(f'添加点 {kind}/{point_no} ({ioa}) 失败')
            if value is not None:
                try:
                    point.value = self._as_value(kind, data_type, value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f'点 {kind}/{point_no} 初值无效: {exc}') from exc
            self._points[(kind, point_no)] = point
            self._point_types[(kind, point_no)] = (int(data_type) if data_type is not None else None)
            if command_handler is not None and kind in ('yk', 'yt'):
                self._handlers[(kind, point_no)] = command_handler
                point.on_receive(self._make_command_callback(kind, point_no))
            return point

    def add_points(self, definitions):
        """批量注册字典或 dataclass 定义，字段名沿用 IEC104 通道/点表。"""
        result = []
        for row in definitions:
            kind = field(row, 'kind')
            if kind is None:
                raise ValueError('批量点定义缺少 kind')
            result.append(self.add_point(
                kind, int(field(row, 'pnt_no')),
                data_type=field(row, 'data_type'), value=field(row, 'value'),
                report_ms=int(field(row, 'report_ms', 0))))
        return result

    def _default_command_handler(self, event):
        return True

    def _make_command_callback(self, kind, point_no):
        def callback(point: c104.Point, previous_info: c104.Information,
                     message: c104.IncomingMessage) -> c104.ResponseState:
            try:
                event = IEC104Command(
                    kind=kind, point_no=point_no, value=self._plain(point.value),
                    selected=bool(attr_value(message, 'is_select_command')),
                    cause=attr_value(message, 'cot'),
                    originator_address=int(attr_value(message, 'originator_address')),
                    received_at=datetime.now(timezone.utc))
                with self._lock:
                    self._commands[(kind, point_no)] = event
                    handler = self._handlers.get((kind, point_no), self._default_command_handler)
                result = handler(event)
                if result is False or result == c104.ResponseState.FAILURE:
                    return c104.ResponseState.FAILURE
                return c104.ResponseState.SUCCESS
            except Exception as exc:  # callback must not escape into the c104 thread
                with self._lock:
                    self._errors.append(exc)
                return c104.ResponseState.FAILURE
        return callback

    def set_command_handler(self, kind, point_no, handler: Callable[[IEC104Command], object]):
        if kind not in ('yk', 'yt'):
            raise ValueError('只有 yk/yt 支持命令回调')
        if not callable(handler):
            raise TypeError('handler 必须可调用')
        key = (kind, int(point_no))
        if key not in self._points:
            raise IEC104ServerError(f'点 {kind}/{point_no} 尚未注册')
        self._handlers[key] = handler
        self._points[key].on_receive(self._make_command_callback(*key))

    def command(self, kind, point_no):
        return self._commands.get((kind, int(point_no)))

    def update(self, kind, point_no, value, *, cause='SPONTANEOUS', transmit=True):
        """更新并主动上送遥信/遥测；命令点只允许通过客户端命令改变。"""
        if kind not in ('yx', 'yc'):
            raise ValueError('服务端 update 只允许 yx/yc，yk/yt 应由客户端下发')
        key = (kind, int(point_no))
        point = self._points.get(key)
        if point is None:
            raise IEC104ServerError(f'点 {kind}/{point_no} 尚未注册')
        data_type = self._point_types.get(key)
        point.value = self._as_value(kind, data_type, value)
        if transmit and not point.transmit(self._cause(cause)):
            raise IEC104ServerError(f'点 {kind}/{point_no} 上送失败')
        return self._plain(point.value)

    def transmit_many(self, kind, values, *, cause='SPONTANEOUS'):
        """批量更新同一类型点并用 c104.Batch 一次上送。"""
        if kind not in ('yx', 'yc'):
            raise ValueError('批量上送只允许 yx/yc')
        points = []
        for point_no, value in values.items():
            key = (kind, int(point_no))
            point = self._points.get(key)
            if point is None:
                raise IEC104ServerError(f'点 {kind}/{point_no} 尚未注册')
            point.value = self._as_value(kind, self._point_types.get(key), value)
            points.append(point)
        if not points:
            return True
        if any(point.type != points[0].type for point in points[1:]):
            raise IEC104ServerError('c104.Batch 要求同一站点、同一信息体类型')
        if not self._server.transmit_batch(c104.Batch(self._cause(cause), points)):
            raise IEC104ServerError('IEC104 批量上送失败')
        return True

    def signal_initialized(self, cause='LOCAL_POWER_ON'):
        if isinstance(cause, str):
            cause = getattr(c104.Coi, cause.upper(), None)
        if cause is None:
            raise ValueError('cause 必须是 c104.Coi 或其名称')
        self._station.signal_initialized(cause)

    def snapshot(self):
        with self._lock:
            return {key: self._plain(point.value) for key, point in self._points.items()}

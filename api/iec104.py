"""IEC 60870-5-104 主站适配层，底层使用 ``c104`` (iec104-python)。

旧 scdfe_c 的 IEC104 地址约定保留在这里：遥测/遥信/遥控/遥调点号均按
``起始地址 + 点号 - 1`` 映射到 information object address，默认起始地址
分别为 0x4001/0x01/0x6001/0x6201。帧编解码、链路启动、I/S/U 计数和
命令确认由 c104/lib60870-C 负责，本文件只负责业务点表适配。
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time

try:
    import c104
except ImportError as exc:  # pragma: no cover - exercised in dependency checks
    c104 = None
    _C104_IMPORT_ERROR = exc
else:
    _C104_IMPORT_ERROR = None

__all__ = ['IEC104Master', 'IEC104Error', 'IEC104Value', 'C104_AVAILABLE']
C104_AVAILABLE = c104 is not None


class IEC104Error(RuntimeError):
    """IEC104 连接、点类型或命令被底层库拒绝。"""


@dataclass(frozen=True)
class IEC104Value:
    rtu_id: int | None
    point_no: int
    kind: str
    value: object
    quality: int | None
    recorded_at: datetime | None
    received_at: datetime


def field(row, name, default=None):
    value = row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)
    return default if value is None else value


def _require_c104():
    if c104 is None:
        raise IEC104Error('缺少依赖 c104，请执行: python -m pip install -r requirements.txt') from _C104_IMPORT_ERROR


class IEC104Master:
    """一个远端 RTU 的 IEC104 主站。

    ``connect()`` 启动 c104 的后台线程并连接；它不是阻塞式 socket 接口。
    ``collect_once()`` 读取已由 c104 接收并更新的点，实际接收由底层线程完成。
    """

    DEFAULT_BASE = {'yc': 0x4001, 'yx': 0x01, 'yk': 0x6001, 'yt': 0x6201}
    TYPE_NAMES = {
        'yx': ('M_SP_NA_1', 'M_DP_NA_1'),
        'yc': ('M_ME_NC_1', 'M_ME_NB_1', 'M_ME_NA_1'),
        'yk': ('C_SC_NA_1', 'C_DC_NA_1'),
        'yt': ('C_SE_NC_1', 'C_SE_NB_1', 'C_SE_NA_1'),
    }

    def __init__(self, host, port=2404, *, common_address=1, rtu_id=None,
                 master_address=1, ycbegin_addr=0x4001, yxbegin_addr=1,
                 ykbegin_addr=0x6001, ytbegin_addr=0x6201,
                 yx_data_type=1, yc_data_type=1, yk_execute_type=1,
                 yt_execute_type=2, command_timeout_ms=10000,
                 init='ALL'):
        _require_c104()
        if not isinstance(host, str) or not host:
            raise ValueError('host 必须是非空字符串')
        if not 1 <= int(port) <= 65535:
            raise ValueError('port 必须在 1..65535')
        if not 1 <= int(common_address) <= 65534:
            raise ValueError('common_address 必须在 1..65534')
        self.host, self.port = host, int(port)
        self.common_address, self.rtu_id = int(common_address), rtu_id
        self.master_address = int(master_address)
        self.base = {'yc': int(ycbegin_addr) or self.DEFAULT_BASE['yc'],
                     'yx': int(yxbegin_addr) or self.DEFAULT_BASE['yx'],
                     'yk': int(ykbegin_addr) or self.DEFAULT_BASE['yk'],
                     'yt': int(ytbegin_addr) or self.DEFAULT_BASE['yt']}
        for kind, base in self.base.items():
            if not 1 <= base <= 0xFFFFFF:
                raise ValueError(f'{kind} 起始地址必须在 1..0xffffff')
        self.yx_data_type = int(yx_data_type)
        self.yc_data_type = int(yc_data_type)
        self.yk_execute_type = int(yk_execute_type)
        self.yt_execute_type = int(yt_execute_type)
        self._client = c104.Client(command_timeout_ms=int(command_timeout_ms))
        init_enum = getattr(c104.Init, str(init).upper(), None) if isinstance(init, str) else init
        if init_enum is None:
            raise ValueError(f'未知 c104 初始化模式 {init!r}')
        self._connection = self._client.add_connection(host, port=self.port, init=init_enum)
        if self._connection is None:
            raise IEC104Error(f'创建 {host}:{port} 连接失败')
        self._station = self._connection.add_station(common_address=self.common_address)
        if self._station is None:
            raise IEC104Error(f'创建公共地址 {self.common_address} 失败')
        self._points = {}
        self._values = {}
        self._lock = threading.RLock()
        self._started = False
        self._point_types = {}
        self._configured_points = []

    @classmethod
    def from_channel(cls, channel, definition=None, **kwargs):
        """从合并后的 fes_rtu_define 与 IEC104 参数行创建。"""
        definition = definition or {}
        kwargs.setdefault('rtu_id', field(channel, 'rtu_id'))
        kwargs.setdefault('master_address', field(definition, 'rtu_addr', 1))
        kwargs.setdefault('common_address', kwargs.pop('common_address', field(definition, 'rtu_addr', 1)))
        for name in ('ycbegin_addr', 'yxbegin_addr', 'ykbegin_addr', 'ytbegin_addr',
                     'yx_data_type', 'yc_data_type', 'yk_execute_type', 'yt_execute_type'):
            if name not in kwargs:
                kwargs[name] = field(definition, name, None)
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        return cls(field(channel, 'chan_addr'), field(channel, 'chan_port', 2404), **kwargs)

    @classmethod
    def from_store(cls, store, rtu_id, *, point_map=None, **kwargs):
        """从当前 RTU、IEC104 通道参数和四遥表构造主站。

        四遥表没有统一的 IEC104 类型列，``point_map`` 可按 ``(kind, pnt_no)``
        提供 ``data_type`` 或完整字典；未提供时使用通道默认类型。
        """
        with store.connection() as connection:
            connection.execute('BEGIN')
            rtu = connection.execute(
                'SELECT * FROM fes_rtu_define WHERE rtu_id=?', (rtu_id,),
            ).fetchone()
            if rtu is None:
                raise ValueError(f'RTU {rtu_id} 不存在')
            channel_type = rtu['channel_type']
            if channel_type != 'iec104':
                raise ValueError(f'RTU {rtu_id} 不是 IEC104 通道')
            definitions = [dict(row) for row in connection.execute(
                'SELECT * FROM fes_channel_iec104_define WHERE rtu_id=? ORDER BY id', (rtu_id,),
            )]
            points = []
            for table, kind in (('fes_yc_define', 'yc'), ('fes_yx_define', 'yx'),
                                ('fes_yk_define', 'yk'), ('fes_yt_define', 'yt')):
                points.extend(dict(row, kind=kind) for row in connection.execute(
                    f'SELECT * FROM {table} WHERE rtu_id=? ORDER BY pnt_no', (rtu_id,),
                ))
        if len(definitions) != 1:
            raise ValueError(f'RTU {rtu_id} 需要唯一 IEC104 通道定义，实际找到 {len(definitions)} 条')
        defaults = {
            'yc': field(definitions[0], 'yc_data_type', 1),
            'yx': field(definitions[0], 'yx_data_type', 1),
            'yk': 2 if field(definitions[0], 'yk_excute_type', 1) == 2 else 1,
            'yt': 3,
        }
        for row in points:
            mapped = point_map.get((row['kind'], row['pnt_no'])) if isinstance(point_map, Mapping) else None
            if isinstance(mapped, Mapping):
                row.update(mapped)
            elif mapped is not None:
                row['data_type'] = mapped
            row.setdefault('data_type', defaults[row['kind']])
        master = cls.from_channel(dict(rtu), definitions[0], **kwargs)
        master.configure(points)
        return master

    @property
    def connected(self):
        state = self._connection.is_connected
        return bool(state() if callable(state) else state)

    @property
    def connection(self):
        """底层 c104.Connection；需要高级功能时可直接使用。"""
        return self._connection

    def connect(self, timeout=10.0):
        if not self._started:
            self._client.start()
            self._started = True
        deadline = time.monotonic() + timeout
        while not self.connected and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.connected:
            raise IEC104Error(f'IEC104 连接超时 {self.host}:{self.port}')
        return self

    def close(self):
        if self._started:
            self._client.stop()
            self._started = False

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

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
        if data_type is None:
            data_type = self.yx_data_type if kind == 'yx' else self.yc_data_type if kind == 'yc' else 3
        choices = self.TYPE_NAMES[kind]
        if kind == 'yx':
            return getattr(c104.Type, choices[1 if int(data_type or 1) == 2 else 0])
        if kind == 'yc':
            return getattr(c104.Type, choices[{1: 0, 2: 1, 3: 2}.get(int(data_type or 1), 0)])
        if kind == 'yk':
            return getattr(c104.Type, choices[1 if int(data_type or 1) == 2 else 0])
        if kind == 'yt':
            return getattr(c104.Type, choices[{1: 2, 2: 1, 3: 0}.get(int(data_type or 3), 0)])
        raise ValueError('未知点类型')

    def add_point(self, kind, point_no, *, data_type=None, report_ms=0):
        """注册远端点；收到报文时 c104 自动更新 Point.info/value。"""
        ioa = self.point_address(kind, point_no)
        typ = self._type(kind, data_type)
        with self._lock:
            point = self._station.get_point(io_address=ioa)
            if point is not None and point.type != typ:
                raise IEC104Error(
                    f'点 {kind}/{point_no} 已注册为 {point.type.name}，不能改成 {typ.name}')
            if point is None:
                point = self._station.add_point(io_address=ioa, type=typ, report_ms=report_ms)
            if point is None:
                raise IEC104Error(f'添加点 {kind}/{point_no} ({ioa}) 失败')
            self._points[(kind, point_no)] = point
            self._point_types[(kind, point_no)] = typ
            return point

    def configure(self, points):
        rows = [dict(row) if isinstance(row, Mapping) else vars(row).copy() for row in points]
        checked = []
        seen = set()
        for row in rows:
            kind = field(row, 'kind')
            point_no = field(row, 'pnt_no')
            if kind not in self.TYPE_NAMES or not isinstance(point_no, int) or point_no < 1:
                raise ValueError('IEC104 点必须包含 kind=yc/yx/yk/yt 和正整数 pnt_no')
            key = (kind, point_no)
            if key in seen:
                raise ValueError(f'重复 IEC104 点 {key}')
            seen.add(key)
            self.add_point(kind, point_no, data_type=field(row, 'data_type'),
                           report_ms=field(row, 'report_ms', 0))
            checked.append((kind, point_no))
        self._configured_points = checked
        return self

    def _point(self, kind, point_no, data_type=None):
        return self._points.get((kind, point_no)) or self.add_point(kind, point_no, data_type=data_type)

    def general_interrogation(self, *, qualifier='STATION', wait_for_response=True):
        q = getattr(c104.Qoi, qualifier.upper()) if isinstance(qualifier, str) else qualifier
        if not self.connected:
            raise IEC104Error('未连接，请先 connect()')
        ok = self._connection.interrogation(self.common_address, cause=c104.Cot.ACTIVATION,
                                             qualifier=q, wait_for_response=wait_for_response)
        if not ok:
            raise IEC104Error('总召唤未被 c104 接受或确认')
        return True

    def clock_sync(self, *, wait_for_response=True):
        if not self.connected:
            raise IEC104Error('未连接，请先 connect()')
        if not self._connection.clock_sync(self.common_address, wait_for_response=wait_for_response):
            raise IEC104Error('对时命令失败')
        return True

    def _write_yk_command(self, point_no, value, *, double=False, select_execute=None, qualifier=None):
        point = self._point('yk', point_no, data_type=2 if double else 1)
        mode = c104.CommandMode.SELECT_AND_EXECUTE if select_execute else c104.CommandMode.DIRECT
        point.command_mode = mode
        if double:
            state = c104.Double.ON if bool(value) else c104.Double.OFF
            point.info = c104.DoubleCmd(state=state, qualifier=qualifier or c104.Qoc.NONE)
        else:
            point.info = c104.SingleCmd(on=bool(value), qualifier=qualifier or c104.Qoc.NONE)
        if not point.transmit(c104.Cot.ACTIVATION):
            raise IEC104Error(f'遥控点 {point_no} 未被 c104 接受')
        return self.value('yk', point_no)

    def _write_yt_command(self, point_no, value, *, data_type=None, qualifier=0):
        # 旧 CRtu104::TxdData_2SETPNT 使用 0x32（短浮点）设点；
        # 因而遥调未显式指定类型时沿用短浮点，而非遥测配置。
        data_type = 3 if data_type is None else int(data_type)
        point = self._point('yt', point_no, data_type=data_type)
        point.command_mode = (c104.CommandMode.SELECT_AND_EXECUTE
                              if self.yt_execute_type == 1 else c104.CommandMode.DIRECT)
        q = c104.UInt7(int(qualifier))
        if data_type == 1:
            point.info = c104.NormalizedCmd(target=c104.NormalizedFloat(float(value)), qualifier=q)
        elif data_type == 2:
            point.info = c104.ScaledCmd(target=c104.Int16(int(value)), qualifier=q)
        else:
            point.info = c104.ShortCmd(target=float(value), qualifier=q)
        if not point.transmit(c104.Cot.ACTIVATION):
            raise IEC104Error(f'遥调点 {point_no} 未被 c104 接受')
        return self.value('yt', point_no)

    @staticmethod
    def _plain(value):
        if isinstance(value, (bool, int, float, str, bytes)) or value is None:
            return value
        # c104 的固定宽度数值是 pybind11 对象：它支持 int()/float()，
        # 但没有可供业务代码读取的 .value 属性。
        if value.__class__.__name__ in {'Int16', 'UInt16', 'UInt5', 'UInt7', 'Int7'}:
            return int(value)
        if value.__class__.__name__ in {'NormalizedFloat'}:
            return float(value)
        if value.__class__.__name__ == 'Double':
            return int(value)
        if value.__class__.__name__ == 'Byte32':
            return bytes(value)
        return value

    def value(self, kind, point_no):
        point = self._point(kind, point_no)
        value = self._plain(point.value)
        quality = getattr(point.quality, 'value', None) if point.quality is not None else None
        recorded = point.recorded_at
        result = IEC104Value(self.rtu_id, point_no, kind, value, quality, recorded,
                             datetime.now(timezone.utc))
        with self._lock:
            self._values[(kind, point_no)] = result
        return result

    def _poll(self, points=None):
        """读取点当前值；points 可为 [(kind, point_no), ...]，不主动造帧。"""
        points = list(points) if points is not None else list(self._points)
        return [self.value(kind, number) for kind, number in points]

    def collect_once(self, points=None):
        if not self.connected:
            raise IEC104Error('未连接，请先 connect()')
        return self._poll(points if points is not None else self._configured_points)

    def poll_loop(self, callback, *, interval=1.0, stop_event=None, max_cycles=None):
        if not callable(callback):
            raise TypeError('callback 必须可调用')
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 0:
            raise ValueError('interval 必须是非负数')
        if max_cycles is not None and (isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1):
            raise ValueError('max_cycles 必须是正整数')
        event = stop_event if stop_event is not None else threading.Event()
        cycles = 0
        while not event.is_set():
            callback(self.collect_once())
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            event.wait(interval)
        return cycles

    def send_yk_point(self, point_no, value, *, double=False, select_execute=None, qualifier=None):
        return self._write_yk_command(point_no, value, double=double,
                                       select_execute=select_execute, qualifier=qualifier)

    def send_yt_point(self, point_no, value, *, data_type=None, qualifier=0):
        return self._write_yt_command(point_no, value, data_type=data_type, qualifier=qualifier)

    def snapshot(self):
        with self._lock:
            return dict(self._values)

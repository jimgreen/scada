"""IEC 60870-5-104 服务端 API，底层使用 ``c104.Server``。

该模块对应旧工程 ``CRtu104`` 的 RTU 侧能力：提供公共地址、四遥点表映射、
遥信/遥测主动上送、遥控/遥调命令回调和站点初始化通知。链路状态机、APDU、
总召唤响应、选择/执行确认和 TCP 监听全部交给 c104/lib60870-C。

点号映射沿用主站 API：``IOA = 起始地址 + 点号 - 1``，默认起始地址为
遥测 0x4001、遥信 0x01、遥控 0x6001、遥调 0x6201。
"""
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import threading

from core.iec104_values import binary_command_value, double_to_binary
from core.iec104_protocol import IEC104ProtocolConfig, IEC104ProtocolError, parse_apdu
from core.packet_trace import PacketTrace

try:
    import c104
except ImportError as exc:  # pragma: no cover - dependency check path
    c104 = None
    _C104_IMPORT_ERROR = exc
else:
    _C104_IMPORT_ERROR = None

__all__ = ['IEC104Server', 'IEC104ServerError', 'IEC104Command']


class IEC104ServerError(RuntimeError):
    """服务端未启动、点表冲突、底层库拒绝操作或用户回调失败。"""


def _require_c104():
    if c104 is None:
        raise IEC104ServerError(
            '缺少依赖 c104，请执行: python -m pip install -r requirements.txt'
        ) from _C104_IMPORT_ERROR


@dataclass(frozen=True)
class IEC104Command:
    """已收到的业务命令快照；单双点遥控均为 False/True。"""

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


class IEC104Server(PacketTrace):
    """单站 IEC104 服务端。

    ``start`` 才会打开监听 socket；构造和点表注册不会绑定端口，便于在启动
    前检查点表。默认只监听本机所有地址 ``0.0.0.0:2404``，生产环境建议由
    部署配置明确指定 ``ip``、``port`` 和 ``max_connections``。
    """

    DEFAULT_BASE = {'yc': 0x4001, 'yx': 0x01, 'yk': 0x6001, 'yt': 0x6201}
    TYPE_NAMES = {
        'yx': ('M_SP_NA_1', 'M_DP_NA_1', 'M_ST_NA_1'),
        'yc': ('M_ME_NC_1', 'M_ME_NB_1', 'M_ME_NA_1'),
        'yk': ('C_SC_NA_1', 'C_DC_NA_1'),
        'yt': ('C_SE_NC_1', 'C_SE_NB_1', 'C_SE_NA_1'),
    }
    TYPE_IDS = {
        'yx': {
            1: 'M_SP_NA_1', 2: 'M_SP_TA_1', 30: 'M_SP_TB_1',
            3: 'M_DP_NA_1', 4: 'M_DP_TA_1', 31: 'M_DP_TB_1',
            5: 'M_ST_NA_1', 6: 'M_ST_TA_1', 32: 'M_ST_TB_1',
        },
        'yc': {
            9: 'M_ME_NA_1', 10: 'M_ME_TA_1', 34: 'M_ME_TD_1', 21: 'M_ME_ND_1',
            11: 'M_ME_NB_1', 12: 'M_ME_TB_1', 35: 'M_ME_TE_1',
            13: 'M_ME_NC_1', 14: 'M_ME_TC_1', 36: 'M_ME_TF_1',
        },
        'yk': {
            45: 'C_SC_NA_1', 58: 'C_SC_TA_1',
            46: 'C_DC_NA_1', 59: 'C_DC_TA_1',
        },
        'yt': {
            48: 'C_SE_NA_1', 61: 'C_SE_TA_1',
            49: 'C_SE_NB_1', 62: 'C_SE_TB_1',
            50: 'C_SE_NC_1', 63: 'C_SE_TC_1',
        },
    }
    IEC_TYPE_SELECTORS = {
        'yx': {1: 1, 3: 2},
        'yc': {9: 3, 11: 2, 13: 1},
        'yk': {45: 1, 46: 2},
        'yt': {48: 1, 49: 2, 50: 3},
    }
    _STORE_RTU_PROJECTION = 'rtu_id,channel_id,channel_type'
    _STORE_DEFINITION_PROJECTION = (
        'channel_id,rtu_addr,ycbegin_addr,yxbegin_addr,ykbegin_addr,ytbegin_addr,'
                'yx_data_type,yk_excute_type,yc_data_type,yt_excute_type,'
        'k,w,t1,t2,t3,max_asdu_length'
    )

    def __init__(self, *, ip='0.0.0.0', port=2404, common_address=1,
                 ycbegin_addr=0x4001, yxbegin_addr=1, ykbegin_addr=0x6001,
                 ytbegin_addr=0x6201, tick_rate_ms=100, select_timeout_ms=10000,
                 max_connections=0, rtu_id=None, k=12, w=8, t1=15.0,
                 t2=10.0, t3=20.0, max_asdu_length=249):
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
        self.tick_rate_ms = int(tick_rate_ms)
        self.rtu_id = rtu_id
        self.common_address = int(common_address)
        self.protocol = IEC104ProtocolConfig(
            k=int(k), w=int(w), t1=float(t1), t2=float(t2), t3=float(t3),
            max_asdu_length=int(max_asdu_length),
        )
        self.base = {
            'yc': int(ycbegin_addr) or self.DEFAULT_BASE['yc'],
            'yx': int(yxbegin_addr) or self.DEFAULT_BASE['yx'],
            'yk': int(ykbegin_addr) or self.DEFAULT_BASE['yk'],
            'yt': int(ytbegin_addr) or self.DEFAULT_BASE['yt'],
        }
        for kind, address in self.base.items():
            if not 1 <= address <= 0xFFFFFF:
                raise ValueError(f'{kind} 起始地址必须在 1..0xffffff')
        kwargs = dict(ip=ip, port=self.port, tick_rate_ms=self.tick_rate_ms,
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
        self._unexpected_messages = []
        self._lock = threading.RLock()
        self.protocol.apply(self._server.protocol_parameters)
        self._server.on_unexpected_message(self._on_unexpected_message)

    @classmethod
    def from_channel(cls, channel, definition=None, **kwargs):
        """从 RTU 行和 IEC104 参数行构造服务端，但不启动监听。"""
        definition = definition or {}
        def value(row, name, default=None):
            return field(row, name, default)
        kwargs.setdefault('rtu_id', value(channel, 'rtu_id'))
        kwargs.setdefault('common_address', value(definition, 'rtu_addr', 1))
        for name in ('k', 'w', 't1', 't2', 't3', 'max_asdu_length'):
            if name not in kwargs and value(definition, name) is not None:
                kwargs[name] = value(definition, name)
        for name in ('ycbegin_addr', 'yxbegin_addr', 'ykbegin_addr', 'ytbegin_addr'):
            if name not in kwargs and value(definition, name) is not None:
                kwargs[name] = value(definition, name)
        return cls(**kwargs)

    @classmethod
    def from_store(cls, store, rtu_id, *, point_map=None, command_handlers=None, **kwargs):
        """按 RTU、IEC104 参数和 IEC104 四遥辅助表构造服务端。

        ``fes_channel_iec104_point_define`` 是信息体地址、类型标识、上送
        周期和命令方式的唯一规约映射来源；四遥定义表只提供点号和初值。
        ``point_map`` 可用于一次性的运行时覆盖，``command_handlers`` 的键
        为 ``(kind, pnt_no)``。
        """
        with store.connection() as connection:
            connection.execute('BEGIN')
            rtu = connection.execute(
                f'SELECT {cls._STORE_RTU_PROJECTION} FROM fes_rtu_define WHERE rtu_id=?', (rtu_id,),
            ).fetchone()
            if rtu is None:
                raise ValueError(f'RTU {rtu_id} 不存在')
            if rtu['channel_type'] != 'iec104':
                raise ValueError(f'RTU {rtu_id} 不是 IEC104 通道')
            channel_id = int(rtu['channel_id'])
            definitions = [dict(row) for row in connection.execute(
                f'SELECT {cls._STORE_DEFINITION_PROJECTION} '
                'FROM fes_channel_iec104_define WHERE channel_id=? ORDER BY id LIMIT 2', (channel_id,),
            )]
            mappings = [dict(row) for row in connection.execute(
                'SELECT channel_id, kind, pnt_no, io_address, type_id, time_tag, report_ms, '
                'command_mode, qualifier, related_io_address, related_io_autoreturn '
                'FROM fes_channel_iec104_point_define WHERE channel_id=? '
                'ORDER BY kind, pnt_no', (channel_id,),
            )]
            points = []
            for table, kind in (('fes_yc_define', 'yc'), ('fes_yx_define', 'yx'),
                                ('fes_yk_define', 'yk'), ('fes_yt_define', 'yt')):
                points.extend(dict(row, kind=kind) for row in connection.execute(
                    f'SELECT pnt_no, value FROM {table} WHERE rtu_id=? ORDER BY pnt_no', (rtu_id,),
                ))
        if len(definitions) != 1:
            raise ValueError(f'RTU {rtu_id} 需要唯一 IEC104 通道定义，实际找到 {len(definitions)} 条')
        mapping_index = {(row['kind'], int(row['pnt_no'])): row for row in mappings}
        handler_map = command_handlers or {}
        prepared = []
        for point in points:
            key = (point['kind'], int(point['pnt_no']))
            row = dict(point)
            mapped = mapping_index.get(key)
            if mapped:
                for name in ('io_address', 'type_id', 'time_tag', 'report_ms', 'command_mode',
                             'qualifier', 'related_io_address', 'related_io_autoreturn'):
                    if mapped.get(name) is not None:
                        row[name] = mapped[name]
            override = point_map.get(key) if isinstance(point_map, Mapping) else None
            if isinstance(override, Mapping):
                row.update(override)
            elif override is not None:
                row['io_address'] = override
            prepared.append(row)
        server = cls.from_channel(dict(rtu), definitions[0], **kwargs)
        for row in prepared:
            kind, point_no = row['kind'], int(row['pnt_no'])
            handler = handler_map.get((kind, point_no))
            server.add_point(
                kind, point_no,
                data_type=field(row, 'data_type', field(row, 'type_id')),
                time_tag=field(row, 'time_tag', 'NONE'),
                value=field(row, 'value'), report_ms=int(field(row, 'report_ms', 0) or 0),
                io_address=field(row, 'io_address'),
                related_io_address=field(row, 'related_io_address'),
                related_io_autoreturn=bool(field(row, 'related_io_autoreturn', False)),
                command_mode=field(row, 'command_mode', 'DIRECT'),
                command_handler=handler,
            )
        return server

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
    def protocol_parameters(self):
        return self._server.protocol_parameters

    @property
    def protocol_config(self):
        return self.protocol.as_dict()

    @property
    def active_connection_count(self):
        return int(self._server.active_connection_count)

    @property
    def last_error(self):
        return self._errors[-1] if self._errors else None

    @property
    def protocol_errors(self):
        with self._lock:
            return list(self._errors)

    @property
    def unexpected_messages(self):
        with self._lock:
            return list(self._unexpected_messages)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    @property
    def soe_point_keys(self):
        return {key for key, point in self._points.items()
                if key[0] == 'yx' and point.type.value in (30,31)}

    def require_soe_support(self):
        if not hasattr(self._server, 'submit_soe'):
            raise IEC104ServerError('可靠 SOE 发送需要 c104 2.2.1+scada.3，请安装 requirements.txt 中的本地补丁包')

    def submit_soe(self, events):
        from core.iec104_soe import encode_events
        self.require_soe_support()
        asdus = encode_events(events, max_length=self.protocol.max_asdu_length)
        return bool(self._server.submit_soe(asdus))

    def poll_soe(self):
        self.require_soe_support()
        return int(self._server.poll_soe())

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
                self._inspect_apdu(data)
                on_receive(data)
            self._server.on_receive_raw(receive)
        if on_send is not None:
            if not callable(on_send):
                raise TypeError('on_send 必须可调用')

            def send(server: c104.Server, data: bytes) -> None:
                self._inspect_apdu(data)
                on_send(data)
            self._server.on_send_raw(send)

    def _inspect_apdu(self, data):
        try:
            parse_apdu(data, max_asdu_length=self.protocol.max_asdu_length)
        except IEC104ProtocolError as exc:
            with self._lock:
                self._errors.append(exc)
                del self._errors[:-1000]

    def _on_unexpected_message(self, server: c104.Server,
                               message: c104.IncomingMessage,
                               cause: c104.Umc) -> None:
        item = {
            'common_address': int(getattr(message, 'common_address', 0)),
            'io_address': int(getattr(message, 'io_address', 0)),
            'type': str(getattr(message, 'type', '')),
            'cause': str(cause),
        }
        with self._lock:
            self._unexpected_messages.append(item)

    def set_packet_handler(self, handler):
        super().set_packet_handler(handler)
        self.PACKET_PROTOCOL = "IEC104 APDU"
        self.set_raw_handlers(on_receive=lambda data: self._packet("RX", data),
                              on_send=lambda data: self._packet("TX", data))

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

    def _type(self, kind, data_type=None, time_tag=None):
        if kind not in self.TYPE_NAMES:
            raise ValueError('kind 必须是 yc/yx/yk/yt')
        if data_type is None:
            data_type = 1 if kind in ('yx', 'yc', 'yk') else 3
        data_type = self._canonical_type_id(kind, data_type, time_tag)
        from core.protocol_capabilities import validate_iec104_runtime_type
        validate_iec104_runtime_type(data_type)
        try:
            return getattr(c104.Type, self.TYPE_IDS[kind][data_type])
        except KeyError as exc:
            raise ValueError(f'{kind} 不支持 data_type={data_type}') from exc

    @classmethod
    def _canonical_type_id(cls, kind, data_type, time_tag=None):
        data_type = int(data_type)
        compact = {
            'yx': {1: 1, 2: 3, 3: 3},
            'yc': {1: 13, 2: 11, 3: 9},
            'yk': {1: 45, 2: 46},
            'yt': {1: 48, 2: 49, 3: 50},
        }
        canonical = compact.get(kind, {}).get(data_type, data_type)
        if not time_tag or str(time_tag).upper() in {'', 'NONE'}:
            return canonical
        tag = str(time_tag).upper().replace('-', '')
        if tag == 'CP24TIME2A' and kind in {'yk', 'yt'}:
            raise ValueError('IEC104 控制命令不使用 CP24Time2a；请使用 CP56Time2a')
        tagged = {
            'CP24TIME2A': {
                'yx': {1: 2, 3: 4, 5: 6}, 'yc': {9: 10, 11: 12, 13: 14},
            },
            'CP56TIME2A': {
                'yx': {1: 30, 3: 31, 5: 32}, 'yc': {9: 34, 11: 35, 13: 36},
                'yk': {45: 58, 46: 59}, 'yt': {48: 61, 49: 62, 50: 63},
            },
        }
        return tagged.get(tag, {}).get(kind, {}).get(canonical, canonical)

    @staticmethod
    def _plain(value):
        if isinstance(value, (bool, int, float, str, bytes)) or value is None:
            return value
        name = value.__class__.__name__
        if name == 'Double':
            return double_to_binary(value)
        if name in {'Int16', 'UInt16', 'UInt5', 'UInt7', 'Int7'}:
            return int(value)
        if name == 'NormalizedFloat':
            return float(value)
        if name == 'Byte32':
            return bytes(value)
        return value

    @staticmethod
    def _as_value(kind, data_type, value):
        data_type = IEC104Server._canonical_type_id(
            kind, data_type or (1 if kind in ('yx', 'yc', 'yk') else 3)
        )
        if kind == 'yx':
            if int(data_type) in {3, 4, 31}:
                if isinstance(value, c104.Double):
                    return value
                return c104.Double.ON if int(value) else c104.Double.OFF
            if int(data_type) in {5, 6, 32}:
                return c104.Int7(int(value))
            return bool(value)
        if kind == 'yc':
            typ = int(data_type)
            if typ in {13, 14, 36}:
                return float(value)
            if typ in {11, 12, 35}:
                return c104.Int16(int(value))
            if typ in {9, 10, 21, 34}:
                return c104.NormalizedFloat(float(value))
        if kind == 'yk':
            value = binary_command_value(value)
            if int(data_type) in {46, 59}:
                return c104.Double.ON if value else c104.Double.OFF
            return value
        if kind == 'yt':
            typ = int(data_type)
            if typ in {48, 61}:
                return c104.NormalizedFloat(float(value))
            if typ in {49, 62}:
                return c104.Int16(int(value))
            if typ in {50, 63}:
                return float(value)
        raise ValueError(f'不支持 {kind} data_type={data_type}')

    @staticmethod
    def _cause(cause):
        if isinstance(cause, str):
            cause = getattr(c104.Cot, cause.upper(), None)
        if cause is None:
            raise ValueError('cause 必须是 c104.Cot 或其名称')
        return cause

    def add_point(self, kind, point_no, *, data_type=None, time_tag='NONE', value=None, report_ms=0,
                  io_address=None,
                  related_io_address=None, related_io_autoreturn=False,
                  command_mode='DIRECT', command_handler=None):
        """注册一个四遥点；命令点可通过 command_handler 接收遥控/遥调。"""
        ioa = self.point_address(kind, point_no) if io_address is None else int(io_address)
        if not 1 <= ioa <= 0xFFFFFF:
            raise ValueError('信息体地址必须在 1..0xffffff')
        report_ms = int(report_ms or 0)
        if report_ms < 0 or (report_ms and report_ms % self.tick_rate_ms):
            raise ValueError('report_ms 必须为 0 或 tick_rate_ms 的正整数倍')
        typ = self._type(kind, data_type, time_tag)
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
                    io_address=ioa, type=typ, report_ms=report_ms,
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
            self._point_types[(kind, point_no)] = (
                self._canonical_type_id(kind, data_type)
                if data_type is not None else None
            )
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
                data_type=field(row, 'data_type', field(row, 'type_id')), time_tag=field(row, 'time_tag', 'NONE'), value=field(row, 'value'),
                report_ms=int(field(row, 'report_ms', 0)),
                io_address=field(row, 'io_address'),
                related_io_address=field(row, 'related_io_address'),
                related_io_autoreturn=bool(field(row, 'related_io_autoreturn', False)),
                command_mode=field(row, 'command_mode', 'DIRECT')))
        return result

    def _default_command_handler(self, event):
        return True

    def _make_command_callback(self, kind, point_no):
        def callback(point: c104.Point, previous_info: c104.Information,
                     message: c104.IncomingMessage) -> c104.ResponseState:
            try:
                value = self._plain(point.value)
                if kind == 'yk':
                    value = binary_command_value(value)
                event = IEC104Command(
                    kind=kind, point_no=point_no, value=value,
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

    def set_monitor_report_interval(self, milliseconds):
        """Change native periodic YC/YX reports without replacing the station."""
        period = int(milliseconds)
        if period <= 0:
            raise ValueError('周期上送间隔必须为正整数')
        period = ((period + self.tick_rate_ms - 1) // self.tick_rate_ms) * self.tick_rate_ms
        with self._lock:
            for (kind, _), point in self._points.items():
                if kind in ('yc', 'yx'):
                    point.report_ms = period
        return period

    def command(self, kind, point_no):
        return self._commands.get((kind, int(point_no)))

    def update(self, kind, point_no, value, *, cause='SPONTANEOUS', transmit=True,
               quality=None):
        """更新并主动上送遥信/遥测；命令点只允许通过客户端命令改变。"""
        if kind not in ('yx', 'yc'):
            raise ValueError('服务端 update 只允许 yx/yc，yk/yt 应由客户端下发')
        return self.set_value(kind, point_no, value, cause=cause,
                              transmit=transmit, quality=quality)

    def set_value(self, kind, point_no, value, *, cause='SPONTANEOUS', transmit=False,
                  quality=None):
        """设置任意四遥点的当前值。

        IEC 104 服务端只能主动发送遥信和遥测监视量。遥控、遥调点的值
        通常由客户端命令改变，因此它们默认只更新站内值，不发送控制报文。
        ``transmit=True`` 仅用于需要显式测试底层信息体发送的场景。
        ``quality`` 可为质量位整数或 c104.Quality；仅适用于 yc/yx，省略时
        保留现有质量。质量和业务值在上送前一起更新，支持仅质量变化的报告。
        """
        if kind not in self.base:
            raise ValueError('kind 必须是 yc/yx/yk/yt')
        key = (kind, int(point_no))
        point = self._points.get(key)
        if point is None:
            raise IEC104ServerError(f'点 {kind}/{point_no} 尚未注册')
        if quality is not None:
            if kind not in ('yc', 'yx'):
                raise ValueError('只有 yc/yx 监视量支持质量标志')
            quality = c104.Quality(quality)
        point.value = self._as_value(kind, self._point_types.get(key), value)
        if quality is not None:
            point.quality = quality
        if transmit and not point.transmit(self._cause(cause)):
            raise IEC104ServerError(f'点 {kind}/{point_no} 上送失败')
        return self._plain(point.value)

    def prepare_value(self, kind, point_no, value):
        """Return the station's business value without changing it or sending an ASDU."""
        key = (kind, int(point_no))
        if key not in self._points:
            raise IEC104ServerError(f'点 {kind}/{point_no} 尚未注册')
        native = self._as_value(kind, self._point_types[key], value)
        # c104 stores short floating-point information in single precision.
        # Return exactly what a later native snapshot will expose.
        if isinstance(native, float):
            native = c104.ShortInfo(native).value
        return self._plain(native)

    def update_monitoring(self, updates, *, transmit=True, cause='SPONTANEOUS', isolate_errors=False):
        """Publish (kind, number, value, quality) samples grouped by native type.

        Validate the complete input before mutation. A native send failure is
        reported to the caller, which must retry the unconfirmed samples.
        lib60870 splits each type group to its native ASDU size limit.
        With isolate_errors, return errors by point key and continue unrelated
        points/type groups. Successful native submission is not peer execution.
        """
        cot = self._cause(cause)
        with self._lock:
            prepared, seen, errors = [], set(), {}
            for kind, no, value, quality in updates:
                key = (kind, int(no))
                if key in seen:
                    raise IEC104ServerError(f'重复的监视点 {kind}/{no}')
                seen.add(key)
                try:
                    if kind not in ('yc', 'yx') or key not in self._points:
                        raise IEC104ServerError(f'无效的监视点 {kind}/{no}')
                    prepared.append((key, self._points[key], self._as_value(kind, self._point_types[key], value),
                                     None if quality is None else c104.Quality(quality)))
                except Exception as exc:
                    if not isolate_errors:
                        raise
                    errors[key] = exc
            groups = {}
            for key, point, value, quality in prepared:
                try:
                    point.value = value
                    if quality is not None:
                        point.quality = quality
                    if transmit:
                        groups.setdefault(point.type, []).append((key, point))
                except Exception as exc:
                    if not isolate_errors:
                        raise
                    errors[key] = exc
            for entries in groups.values():
                try:
                    if not self._server.transmit_batch(c104.Batch(cot, [point for _, point in entries])):
                        raise IEC104ServerError('IEC104 变化量批量上送失败')
                except Exception as exc:
                    if not isolate_errors:
                        raise
                    errors.update((key, exc) for key, _ in entries)
        return errors if isolate_errors else len(prepared)

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

    def transmit_full(self, values=None, *, cause='INTERROGATED_BY_STATION', transmit=True):
        """更新并批量发送全站遥信/遥测快照。

        ``values`` 使用 ``{'yx': {1: True}, 'yc': {1: 12.5}}`` 形式；
        未提供的监视量保留当前值。不同类型标识会自动拆成多个批次，
        因为 ``c104.Batch`` 要求同一批次使用同一种信息体类型。遥控、
        遥调点只更新站内当前值，不会伪造一条发往客户端的控制报文。
        返回本次处理的点数和实际发送的批次数。
        """
        incoming = {} if values is None else values
        if not isinstance(incoming, Mapping):
            raise ValueError('values 必须是点值对象')
        groups = {}
        with self._lock:
            prepared = {}
            for kind, supplied in incoming.items():
                if kind not in self.base:
                    raise ValueError(f'未知四遥类型: {kind}')
                if not isinstance(supplied, Mapping):
                    raise ValueError(f'{kind} 必须是点号到值的对象')
                for no, value in supplied.items():
                    key = (kind, int(no))
                    if key not in self._points or key in prepared:
                        raise IEC104ServerError(f'无效或重复的点 {kind}/{no}')
                    prepared[key] = self._as_value(kind, self._point_types[key], value)
            for key, native in prepared.items():
                self._points[key].value = native
            updated = len(self._points)
            if transmit:
                for (kind, _), point in self._points.items():
                    if kind in ('yx', 'yc'):
                        groups.setdefault(point.type, []).append(point)
        sent_batches = 0
        for points in groups.values():
            if points and transmit and not self._server.transmit_batch(c104.Batch(self._cause(cause), points)):
                raise IEC104ServerError('IEC104 全量上送失败')
            if points and transmit:
                sent_batches += 1
        return {'updated': updated, 'sent_batches': sent_batches}

    def signal_initialized(self, cause='LOCAL_POWER_ON'):
        if isinstance(cause, str):
            cause = getattr(c104.Coi, cause.upper(), None)
        if cause is None:
            raise ValueError('cause 必须是 c104.Coi 或其名称')
        self._station.signal_initialized(cause)

    @property
    def point_count(self):
        with self._lock:
            return len(self._points)

    def snapshot(self):
        with self._lock:
            return {key: self._plain(point.value) for key, point in self._points.items()}

    def monitoring_snapshot(self, keys=None):
        """Read each monitoring value and quality from one information object."""
        with self._lock:
            result = {}
            points = self._points.items() if keys is None else ((key,self._points[key]) for key in keys if key in self._points)
            for key, point in points:
                if key[0] not in {'yc', 'yx'}:
                    continue
                info = point.info
                result[key] = (self._plain(info.value), int(info.quality or 0))
            return result

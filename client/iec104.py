"""IEC 60870-5-104 主站适配层，底层使用 ``c104`` (iec104-python)。

旧 scdfe_c 的 IEC104 地址约定保留在这里：遥测/遥信/遥控/遥调点号均按
``起始地址 + 点号 - 1`` 映射到 information object address，默认起始地址
分别为 0x4001/0x01/0x6001/0x6201。帧编解码、链路启动、I/S/U 计数和
命令确认由 c104/lib60870-C 负责，本文件只负责业务点表适配。
"""
from collections.abc import Mapping
from datetime import datetime, timezone
import threading
import time
from typing import NamedTuple

from core.iec104_values import binary_command_value, double_to_binary
from core.iec104_protocol import IEC104ProtocolConfig, IEC104ProtocolError, parse_apdu
from core.packet_trace import PacketTrace

try:
    import c104
except ImportError as exc:  # pragma: no cover - exercised in dependency checks
    c104 = None
    _C104_IMPORT_ERROR = exc
else:
    _C104_IMPORT_ERROR = None

__all__ = ['IEC104Master', 'IEC104Error', 'IEC104Value', 'C104_AVAILABLE']
C104_AVAILABLE = c104 is not None


_PLAIN_CONVERTERS = {}
_PLAIN_INT_NAMES = {'Int16', 'UInt16', 'UInt5', 'UInt7', 'Int7'}


def _plain_identity(value):
    return value


class IEC104Error(RuntimeError):
    """IEC104 连接、点类型或命令被底层库拒绝。"""


class IEC104Value(NamedTuple):
    """不可变业务值快照；双点 OFF/ON 为 False/True，未知态为 None。"""

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


class IEC104Master(PacketTrace):
    """一个远端 RTU 的 IEC104 主站。

    ``connect()`` 启动 c104 的后台线程并连接；它不是阻塞式 socket 接口。
    ``collect_once()`` 读取已由 c104 接收并更新的点，实际接收由底层线程完成。
    """

    DEFAULT_BASE = {'yc': 0x4001, 'yx': 0x01, 'yk': 0x6001, 'yt': 0x6201}
    PACKET_PROTOCOL = "IEC104 APDU"
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
    # Public point tables may use the compact 1/2/3 selectors used by the
    # legacy project or the IEC 60870-5-104 type identifiers themselves.
    IEC_TYPE_SELECTORS = {
        'yx': {1: 1, 3: 2},
        'yc': {9: 3, 11: 2, 13: 1},
        'yk': {45: 1, 46: 2},
        'yt': {48: 1, 49: 2, 50: 3},
    }
    _STORE_RTU_PROJECTION = 'rtu_id,channel_id,channel_type'
    _STORE_DEFINITION_PROJECTION = (
        'channel_id,rtu_addr,ycbegin_addr,yxbegin_addr,ykbegin_addr,ytbegin_addr,'
        'yx_data_type,yk_excute_type,yc_data_type,yt_excute_type,remote_host,remote_port,'
        'k,w,t1,t2,t3,max_asdu_length'
    )

    def __init__(self, host, port=2404, *, common_address=1, rtu_id=None,
                 master_address=1, ycbegin_addr=0x4001, yxbegin_addr=1,
                 ykbegin_addr=0x6001, ytbegin_addr=0x6201,
                 yx_data_type=1, yc_data_type=1, yk_execute_type=1,
                  yt_execute_type=2, command_timeout_ms=10000, k=12, w=8,
                  t1=15.0, t2=10.0, t3=20.0, max_asdu_length=249,
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
        self.protocol = IEC104ProtocolConfig(
            k=int(k), w=int(w), t1=float(t1), t2=float(t2), t3=float(t3),
            max_asdu_length=int(max_asdu_length),
        )
        self._client = c104.Client(command_timeout_ms=int(command_timeout_ms))
        init_enum = getattr(c104.Init, str(init).upper(), None) if isinstance(init, str) else init
        if init_enum is None:
            raise ValueError(f'未知 c104 初始化模式 {init!r}')
        self._start_muted = init_enum == c104.Init.MUTED
        self._connection = self._client.add_connection(host, port=self.port, init=init_enum)
        if self._connection is None:
            raise IEC104Error(f'创建 {host}:{port} 连接失败')
        self.protocol.apply(self._connection.protocol_parameters)
        self._station = self._connection.add_station(common_address=self.common_address)
        if self._station is None:
            raise IEC104Error(f'创建公共地址 {self.common_address} 失败')
        self._points = {}
        self._values = {}
        self._lock = threading.RLock()
        self._started = False
        self._point_types = {}
        self._configured_points = []
        self._point_entries = []
        self._point_entry_index = {}
        self._configured_entries = []
        self._point_definitions: dict[tuple[str, int], dict] = {}
        # IEC104 point types normally stay stable after registration.  Cache
        # the converter after the first sample so the hot polling path does
        # not repeat the generic isinstance/type/name dispatch for every
        # sample.  The fallback path refreshes this cache if a custom/test
        # point supplies a different Python value type later.
        self._point_value_converters = {}
        self._protocol_errors = []
        self._connection_states = []
        self._unexpected_messages = []
        self._connection.on_state_change(self._on_state_change)
        self._connection.on_unexpected_message(self._on_unexpected_message)

    def _on_state_change(self, connection: c104.Connection,
                         state: c104.ConnectionState) -> None:
        with self._lock:
            self._connection_states.append({
                'state': getattr(state, 'name', str(state)),
                'time': datetime.now(timezone.utc),
            })
            del self._connection_states[:-1000]

    def _on_unexpected_message(self, connection: c104.Connection,
                               message: c104.IncomingMessage,
                               cause: c104.Umc) -> None:
        item = {
            'common_address': int(getattr(message, 'common_address', 0)),
            'io_address': int(getattr(message, 'io_address', 0)),
            'type': str(getattr(message, 'type', '')),
            'cause': str(cause),
            'time': datetime.now(timezone.utc),
        }
        with self._lock:
            self._unexpected_messages.append(item)
            del self._unexpected_messages[:-1000]

    @classmethod
    def from_channel(cls, channel, definition=None, **kwargs):
        """从合并后的 fes_rtu_define 与 IEC104 参数行创建。"""
        definition = definition or {}
        kwargs.setdefault('rtu_id', field(channel, 'rtu_id'))
        kwargs.setdefault('master_address', field(definition, 'rtu_addr', 1))
        kwargs.setdefault('common_address', kwargs.pop('common_address', field(definition, 'rtu_addr', 1)))
        for name in ('ycbegin_addr', 'yxbegin_addr', 'ykbegin_addr', 'ytbegin_addr',
                     'yx_data_type', 'yc_data_type'):
            if name not in kwargs:
                kwargs[name] = field(definition, name, None)
        for source, target in (('yk_excute_type', 'yk_execute_type'),
                               ('yt_excute_type', 'yt_execute_type')):
            if target not in kwargs:
                kwargs[target] = field(definition, source, None)
        for name in ('k', 'w', 't1', 't2', 't3', 'max_asdu_length'):
            if name not in kwargs:
                kwargs[name] = field(definition, name, None)
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        host = kwargs.pop('remote_host', None) or field(definition, 'remote_host')
        port = kwargs.pop('remote_port', None) or field(definition, 'remote_port', 2404)
        if not host:
            raise ValueError('IEC104 参数需要 remote_host')
        return cls(host, port, **kwargs)

    @classmethod
    def from_store(cls, store, rtu_id, *, point_map=None, **kwargs):
        """从当前 RTU、IEC104 通道参数和四遥表构造主站。

        四遥表没有统一的 IEC104 类型列，``point_map`` 可按 ``(kind, pnt_no)``
        提供 ``data_type`` 或完整字典；未提供时使用通道默认类型。
        """
        with store.connection() as connection:
            connection.execute('BEGIN')
            rtu = connection.execute(
                f'SELECT {cls._STORE_RTU_PROJECTION} FROM fes_rtu_define WHERE rtu_id=?', (rtu_id,),
            ).fetchone()
            if rtu is None:
                raise ValueError(f'RTU {rtu_id} 不存在')
            channel_type = rtu['channel_type']
            if channel_type != 'iec104':
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
                    f'SELECT pnt_no FROM {table} WHERE rtu_id=? ORDER BY pnt_no', (rtu_id,),
                ))
        if len(definitions) != 1:
            raise ValueError(f'RTU {rtu_id} 需要唯一 IEC104 通道定义，实际找到 {len(definitions)} 条')
        defaults = {
            'yc': field(definitions[0], 'yc_data_type', 1),
            'yx': field(definitions[0], 'yx_data_type', 1),
            'yk': 2 if field(definitions[0], 'yk_excute_type', 1) == 2 else 1,
            'yt': 3,
        }
        mapping_index = {(row['kind'], int(row['pnt_no'])): row for row in mappings}
        for row in points:
            mapped = mapping_index.get((row['kind'], int(row['pnt_no'])))
            if mapped:
                # Protocol point definitions are authoritative for address and
                # type.  Keep the four遥 row as the source of name/value limits.
                for name in ('io_address', 'type_id', 'time_tag', 'report_ms',
                             'command_mode', 'qualifier', 'related_io_address',
                             'related_io_autoreturn'):
                    if mapped.get(name) is not None:
                        row[name] = mapped[name]
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
    def protocol_parameters(self):
        return self._connection.protocol_parameters

    @property
    def protocol_config(self):
        return self.protocol.as_dict()

    @property
    def protocol_errors(self):
        with self._lock:
            return list(self._protocol_errors)

    @property
    def connection_states(self):
        with self._lock:
            return list(self._connection_states)

    @property
    def unexpected_messages(self):
        with self._lock:
            return list(self._unexpected_messages)

    @property
    def diagnostics(self):
        return {
            'protocol_errors': self.protocol_errors,
            'connection_states': self.connection_states,
            'unexpected_messages': self.unexpected_messages,
        }

    def set_packet_handler(self, handler):
        super().set_packet_handler(handler)
        def receive(connection: c104.Connection, data: bytes) -> None:
            self._inspect_apdu(data)
            self._packet("RX", data, f"peer={connection.ip}:{connection.port}")
        def send(connection: c104.Connection, data: bytes) -> None:
            self._inspect_apdu(data)
            self._packet("TX", data, f"peer={connection.ip}:{connection.port}")
        self._connection.on_receive_raw(receive)
        self._connection.on_send_raw(send)

    def _inspect_apdu(self, data):
        try:
            parse_apdu(data, max_asdu_length=self.protocol.max_asdu_length)
        except IEC104ProtocolError as exc:
            with self._lock:
                self._protocol_errors.append(str(exc))
                del self._protocol_errors[:-1000]

    @property
    def connection(self):
        """底层 c104.Connection；需要高级功能时可直接使用。"""
        return self._connection

    def connect(self, timeout=10.0):
        if not self._started:
            self._client.start()
            self._started = True
        deadline = time.monotonic() + timeout
        muted_since = None
        requested_start = False
        # is_connected also includes OPEN_MUTED: the TCP socket can be
        # connected before the peer confirms STARTDT. A GI issued there can
        # time out. Only a deliberately muted client may return in that state.
        while True:
            state = self._connection.state
            if state == c104.ConnectionState.OPEN or (
                self._start_muted and state == c104.ConnectionState.OPEN_MUTED
            ):
                return self
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IEC104Error(f'IEC104 连接超时 {self.host}:{self.port} ({state.name})')
            if state == c104.ConnectionState.OPEN_MUTED:
                # Let c104 perform its normal automatic handshake. On a cold
                # connection it can remain muted without sending STARTDT. In
                # that case request data transfer once, never replay an ASDU.
                now = time.monotonic()
                if muted_since is None:
                    muted_since = now
                elif not requested_start and now - muted_since >= .25:
                    requested_start = True
                    self._connection.unmute()
            else:
                muted_since = None
                requested_start = False
            time.sleep(min(.02, remaining))

    def close(self):
        if self._started:
            self._client.stop()
            self._started = False

    def test_command(self, *, with_time=True, wait_for_response=True):
        """Send the ASDU application test command (104/107), not U TESTFR."""
        if not self.connected:
            raise IEC104Error('未连接，请先 connect()')
        if not self._connection.test(
                self.common_address, with_time=bool(with_time),
                wait_for_response=bool(wait_for_response)):
            raise IEC104Error('IEC104 应用测试命令失败')
        return True

    def test_fr(self, **kwargs):
        """U TESTFR is managed by the native connection's t3 timer."""
        raise NotImplementedError('当前驱动仅自动发送 U TESTFR；应用测试请调用 test_command()')

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

    def _type(self, kind, data_type=None, time_tag=None):
        if data_type is None:
            if kind == 'yx':
                data_type = self.yx_data_type
            elif kind == 'yc':
                data_type = self.yc_data_type
            elif kind == 'yk':
                data_type = self.yk_execute_type
            else:
                data_type = self.yt_execute_type
        data_type = self._canonical_type_id(kind, data_type, time_tag)
        from core.protocol_capabilities import validate_iec104_runtime_type
        validate_iec104_runtime_type(data_type)
        try:
            return getattr(c104.Type, self.TYPE_IDS[kind][data_type])
        except KeyError as exc:
            raise ValueError(f'{kind} 不支持 data_type={data_type}') from exc

    @staticmethod
    def _canonical_type_id(kind, data_type, time_tag=None):
        data_type = int(data_type)
        compact = {
            # Legacy point tables used 1=single and 2=double.  Keep both 2
            # and 3 as the compact double selector; explicit CP24/CP56
            # choices use their full IEC type IDs in the mapping table.
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

    def add_point(self, kind, point_no, *, data_type=None, time_tag='NONE', report_ms=0,
                  io_address=None):
        """注册远端点；收到报文时 c104 自动更新 Point.info/value。"""
        ioa = self.point_address(kind, point_no) if io_address is None else int(io_address)
        if not 1 <= ioa <= 0xFFFFFF:
            raise ValueError('信息体地址必须在 1..0xffffff')
        typ = self._type(kind, data_type, time_tag)
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
            key = (kind, point_no)
            if key not in self._point_entry_index:
                self._point_entries.append((kind, point_no, point))
                self._point_entry_index[key] = point
            return point

    def configure(self, points):
        rows = [dict(row) if isinstance(row, Mapping) else vars(row).copy() for row in points]
        checked = []
        seen = set()
        self._point_definitions = {}
        for row in rows:
            kind = field(row, 'kind')
            point_no = field(row, 'pnt_no')
            if kind not in self.TYPE_NAMES or not isinstance(point_no, int) or point_no < 1:
                raise ValueError('IEC104 点必须包含 kind=yc/yx/yk/yt 和正整数 pnt_no')
            key = (kind, point_no)
            if key in seen:
                raise ValueError(f'重复 IEC104 点 {key}')
            seen.add(key)
            self.add_point(kind, point_no, data_type=field(row, 'data_type', field(row, 'type_id')),
                           time_tag=field(row, 'time_tag', 'NONE'),
                           report_ms=field(row, 'report_ms', 0),
                           io_address=field(row, 'io_address'))
            self._point_definitions[key] = row
            checked.append((kind, point_no))
        self._configured_points = checked
        self._configured_entries = [(kind, point_no, self._points[(kind, point_no)])
                                    for kind, point_no in checked]
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

    def _write_yk_command(self, point_no, value, *, double=None, select_execute=None,
                          qualifier=None, recorded_at=None):
        value = binary_command_value(value)
        definition = self._point_definitions.get(('yk', int(point_no)), {})
        type_id = int(field(definition, 'data_type', field(definition, 'type_id', 1)) or 1)
        canonical_type = self._canonical_type_id('yk', type_id, field(definition, 'time_tag'))
        if double is None:
            double = canonical_type in {46, 59}
        if select_execute is None:
            mode = str(field(definition, 'command_mode', '') or '').upper()
            select_execute = mode in {'SELECT_AND_EXECUTE', 'SELECT-AND-EXECUTE', 'SELECT'}
        if qualifier is None:
            qualifier = field(definition, 'qualifier')
        wire_type = canonical_type if canonical_type in {45, 46, 58, 59} else (46 if double else 45)
        point = self._point('yk', point_no, data_type=wire_type)
        mode = c104.CommandMode.SELECT_AND_EXECUTE if select_execute else c104.CommandMode.DIRECT
        point.command_mode = mode
        qoc = c104.Qoc.NONE if qualifier is None else (
            qualifier if isinstance(qualifier, c104.Qoc) else c104.Qoc(int(qualifier)))
        if double:
            state = c104.Double.ON if bool(value) else c104.Double.OFF
            point.info = c104.DoubleCmd(state=state, qualifier=qoc, recorded_at=recorded_at)
        else:
            point.info = c104.SingleCmd(on=bool(value), qualifier=qoc, recorded_at=recorded_at)
        if not point.transmit(c104.Cot.ACTIVATION):
            raise IEC104Error(f'遥控点 {point_no} 未被 c104 接受')
        return self.value('yk', point_no)

    def _write_yt_command(self, point_no, value, *, data_type=None, qualifier=None,
                           recorded_at=None, cause=None):
        # 旧 CRtu104::TxdData_2SETPNT 使用 0x32（短浮点）设点；
        # 因而遥调未显式指定类型时沿用短浮点，而非遥测配置。
        definition = self._point_definitions.get(('yt', int(point_no)), {})
        data_type = field(definition, 'data_type', field(definition, 'type_id', 3)) if data_type is None else data_type
        data_type = 3 if data_type is None else int(data_type)
        data_type = self._canonical_type_id('yt', data_type, field(definition, 'time_tag'))
        if qualifier is None:
            qualifier = field(definition, 'qualifier', 0)
        point = self._point('yt', point_no, data_type=data_type)
        mode = str(field(definition, 'command_mode', '') or '').upper()
        point.command_mode = (c104.CommandMode.SELECT_AND_EXECUTE
                              if (mode in {'SELECT_AND_EXECUTE', 'SELECT-AND-EXECUTE', 'SELECT'}
                                  or (not mode and self.yt_execute_type == 1))
                              else c104.CommandMode.DIRECT)
        q = c104.UInt7(int(qualifier))
        if data_type in {48, 61}:
            point.info = c104.NormalizedCmd(
                target=c104.NormalizedFloat(float(value)), qualifier=q,
                recorded_at=recorded_at)
        elif data_type in {49, 62}:
            point.info = c104.ScaledCmd(
                target=c104.Int16(int(value)), qualifier=q,
                recorded_at=recorded_at)
        elif data_type in {50, 63}:
            point.info = c104.ShortCmd(target=float(value), qualifier=q,
                                       recorded_at=recorded_at)
        else:
            raise ValueError(f'yt 不支持 data_type={data_type}')
        if not point.transmit(c104.Cot.ACTIVATION if cause is None else cause):
            raise IEC104Error(f'遥调点 {point_no} 未被 c104 接受')
        return self.value('yt', point_no)

    @staticmethod
    def _converter_for(value):
        """Return the stable Python value type and its plain-value converter."""
        if isinstance(value, (bool, int, float, str, bytes)) or value is None:
            return type(value), _plain_identity
        value_type = type(value)
        converter = _PLAIN_CONVERTERS.get(value_type)
        if converter is None:
            name = value_type.__name__
            if name == 'Double':
                converter = double_to_binary
            elif name in _PLAIN_INT_NAMES:
                converter = int
            elif name == 'NormalizedFloat':
                converter = float
            elif name == 'Byte32':
                converter = bytes
            else:
                converter = _plain_identity
            _PLAIN_CONVERTERS[value_type] = converter
        return value_type, converter

    @classmethod
    def _plain(cls, value):
        _, converter = cls._converter_for(value)
        return converter(value)

    def _cached_plain(self, key, value):
        value_type = type(value)
        cached = self._point_value_converters.get(key)
        if cached is not None and cached[0] is value_type:
            return cached[1](value)
        value_type, converter = self._converter_for(value)
        self._point_value_converters[key] = (value_type, converter)
        return converter(value)

    def _value_from_point(self, kind, point_no, point, received_at=None):
        """从已解析的 c104 Point 生成快照，供批量轮询绕过字典查找。"""
        # Hold one information object for value/quality/time. Separate Point
        # getters repeatedly cross pybind11 and may see different updates.
        info = point.info
        value = self._cached_plain((kind, point_no), info.value)
        point_quality = info.quality
        quality = point_quality.value if point_quality is not None else None
        if value is None and isinstance(info.value, c104.Double):
            # Neither an intermediate switch position nor an indeterminate
            # double state is a valid OFF/ON sample, even if QDS says good.
            quality = (quality or 0) | int(c104.Quality.Invalid.value)
        recorded = info.recorded_at
        result = IEC104Value(self.rtu_id, point_no, kind, value, quality, recorded,
                             received_at if received_at is not None else datetime.now(timezone.utc))
        return result

    def value(self, kind, point_no):
        point = self._point(kind, point_no)
        result = self._value_from_point(kind, point_no, point)
        with self._lock:
            self._values[(kind, point_no)] = result
        return result

    def _poll(self, points=None):
        """读取点当前值；points 可为 [(kind, point_no), ...]，不主动造帧。"""
        if points is None:
            entries = self._point_entries
        else:
            entries = [(kind, number, self._point(kind, number))
                       for kind, number in points]
        return self._poll_entries(entries)

    def _poll_entries(self, entries):
        # One receive timestamp describes this coherent snapshot.  Calling
        # datetime.now(timezone.utc) once per point was a measurable cost for
        # large IEC104 point tables and did not add useful precision because
        # c104 already stores the point's source timestamp in ``recorded_at``.
        received_at = datetime.now(timezone.utc)
        values = [self._value_from_point(kind, number, point, received_at)
                  for kind, number, point in entries]
        with self._lock:
            self._values.update({(value.kind, value.point_no): value for value in values})
        return values

    def collect_once(self, points=None):
        if not self.connected:
            raise IEC104Error('未连接，请先 connect()')
        return (self._poll_entries(self._configured_entries)
                if points is None else self._poll(points))

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

    def send_yk_point(self, point_no, value, *, double=None, select_execute=None,
                      qualifier=None, recorded_at=None):
        return self._write_yk_command(point_no, value, double=double,
                                       select_execute=select_execute, qualifier=qualifier,
                                       recorded_at=recorded_at)

    def send_yt_point(self, point_no, value, *, data_type=None, qualifier=None, recorded_at=None):
        return self._write_yt_command(point_no, value, data_type=data_type,
                                       qualifier=qualifier, recorded_at=recorded_at)

    def cancel_yt_point(self, point_no, value, *, data_type=None, qualifier=None, recorded_at=None):
        """Send a set-point deactivation, preserving the configured type/time tag."""
        return self._write_yt_command(point_no, value, data_type=data_type,
                                     qualifier=qualifier, recorded_at=recorded_at,
                                     cause=c104.Cot.DEACTIVATION)

    def cancel_yk_point(self, point_no, value, *, double=None, qualifier=None,
                        recorded_at=None):
        """Cancel a pending control using the IEC 104 deactivation cause."""
        value = binary_command_value(value)
        definition = self._point_definitions.get(('yk', int(point_no)), {})
        type_id = int(field(definition, 'data_type', field(definition, 'type_id', 1)) or 1)
        canonical_type = self._canonical_type_id('yk', type_id, field(definition, 'time_tag'))
        double = canonical_type in {46, 59} if double is None else bool(double)
        wire_type = canonical_type if canonical_type in {45, 46, 58, 59} else (46 if double else 45)
        point = self._point('yk', point_no, data_type=wire_type)
        mode = str(field(definition, 'command_mode', '') or '').upper()
        point.command_mode = (c104.CommandMode.SELECT_AND_EXECUTE
                              if mode in {'SELECT_AND_EXECUTE', 'SELECT-AND-EXECUTE', 'SELECT'}
                              else c104.CommandMode.DIRECT)
        if qualifier is None:
            qualifier = field(definition, 'qualifier')
        qoc = c104.Qoc.NONE if qualifier is None else (
            qualifier if isinstance(qualifier, c104.Qoc) else c104.Qoc(int(qualifier)))
        if double:
            point.info = c104.DoubleCmd(
                state=c104.Double.ON if value else c104.Double.OFF,
                qualifier=qoc, recorded_at=recorded_at)
        else:
            point.info = c104.SingleCmd(on=bool(value), qualifier=qoc,
                                        recorded_at=recorded_at)
        if not point.transmit(c104.Cot.DEACTIVATION):
            raise IEC104Error(f'撤销遥控点 {point_no} 未被 c104 接受')
        return self.value('yk', point_no)

    def snapshot(self):
        with self._lock:
            return dict(self._values)

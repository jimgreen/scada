"""Modbus TCP 主站；协议通信交给 pyModbusTCP。

参考 scdfe_c/CRtuModbusTcpEx.cpp 的召唤、数据解释、遥调遥控字段。
地址使用线上零基地址，例如文档 40001 通常传 0；不会自动减 40001。
本文件的私有协议层为 TCP/RTU 共用的应用层点表适配，不实现协议帧。
"""
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import struct
import threading

__all__ = ['ModbusTcpMaster', 'ModbusError', 'ModbusValue', 'decode_point']


class ModbusError(RuntimeError):
    """通信失败或设备异常；写失败不代表设备一定没有执行，不自动重发。"""

    def __init__(self, message, *, exception_code=None):
        super().__init__(message)
        self.exception_code = exception_code


@dataclass(frozen=True)
class ModbusValue:
    rtu_id: int | None
    call_seq: int
    point_no: int
    kind: str
    value: int | float | bool
    received_at: datetime


def field(row, name, default=None):
    """同时接受 dict 和 dbdef/def 下生成的数据类；None 按缺省处理。"""
    value = row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)
    return default if value is None else value


def integer(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f'{name} 必须是 {lo}..{hi} 的整数，实际为 {value!r}')
    return value


def boolean(value):
    if not isinstance(value, (bool, int)) or value not in (0, 1):
        raise ValueError('开关值必须是 bool 或 0/1')
    return bool(value)


def _order4(raw, seq):
    # 与旧代码 Change4ByteDataSeq 一致：输入/输出为 Windows 小端内存字节。
    permutations = ((0, 1, 2, 3), (2, 3, 0, 1), (1, 0, 3, 2), (3, 2, 1, 0))
    integer(seq, 'fourbytedata_byteseq', 0, 3)
    return bytes(raw[i] for i in permutations[seq])


def decode_point(raw: bytes, row):
    """解析单点寄存器数据，支持 trans_type=1..11 和现有字节序编号。

    1/2=int16/uint16, 3/4=int32/uint32, 5=float32, 6=byte,
    7=int8, 8=bit, 9=bits, 10/11=int64/uint64。
    四字节编号 0/1/2/3 对应通常线上标记 DCBA/BADC/CDAB/ABCD。
    """
    typ = integer(field(row, 'trans_type'), 'trans_type', 1, 11)
    size = {1: 2, 2: 2, 3: 4, 4: 4, 5: 4, 6: 2, 7: 1,
            8: 2, 9: 2, 10: 8, 11: 8}[typ]
    if len(raw) < size:
        raise ValueError(f'点 {field(row, "cnt")} 数据不足：需要 {size} 字节，得到 {len(raw)}')
    raw = raw[:size]
    little = field(row, 'twobytedata_byteseq', 0) > 0
    if typ in (1, 2):
        return int.from_bytes(raw, 'little' if little else 'big', signed=typ == 1)
    if typ in (3, 4, 5):
        raw = _order4(raw, field(row, 'fourbytedata_byteseq', 0))
        return struct.unpack('<' + {3: 'i', 4: 'I', 5: 'f'}[typ], raw)[0]
    if typ in (10, 11):
        return int.from_bytes(raw, 'big', signed=typ == 10)
    if typ == 6:
        return raw[0 if little else 1]
    if typ == 7:
        return int.from_bytes(raw, 'big', signed=True)
    offset = integer(field(row, 'offset_bit', 0), 'offset_bit', 0, 15)
    width = 1 if typ == 8 else integer(field(row, 'all_bit'), 'all_bit', 1, 16 - offset)
    value = (int.from_bytes(raw, 'little' if little else 'big') >> offset) & ((1 << width) - 1)
    if typ == 8:
        return bool(value) ^ bool(field(row, 'if_reverse', 0))
    return value


class _ModbusOperations:
    """共用的读写/点表 API；子类 _invoke 调用第三方 TCP 或串口库。"""

    def _init_operations(self, unit_id, rtu_id):
        self.unit_id = integer(unit_id, 'unit_id', 1, 247)
        self.rtu_id = rtu_id
        self._lock = threading.RLock()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def _unit(self, unit_id):
        return self.unit_id if unit_id is None else integer(unit_id, 'unit_id', 1, 247)

    @staticmethod
    def _range(address, count, limit):
        integer(address, 'address', 0, 65535)
        integer(count, 'count', 1, limit)
        if address + count > 65536:
            raise ValueError('地址范围超过 65535')

    def read(self, function_code, address, count=1, *, unit_id=None):
        """功能码 1/2 返回 bool 列表，3/4 返回 uint16 列表。"""
        names = {1: 'read_coils', 2: 'read_discrete_inputs',
                 3: 'read_holding_registers', 4: 'read_input_registers'}
        if function_code not in names:
            raise ValueError('读取功能码必须是 1/2/3/4')
        self._range(address, count, 2000 if function_code < 3 else 125)
        return self._invoke(names[function_code], address, count, self._unit(unit_id))

    def read_coils(self, address, count=1, *, unit_id=None):
        return self.read(1, address, count, unit_id=unit_id)

    def read_discrete_inputs(self, address, count=1, *, unit_id=None):
        return self.read(2, address, count, unit_id=unit_id)

    def read_holding_registers(self, address, count=1, *, unit_id=None):
        return self.read(3, address, count, unit_id=unit_id)

    def read_input_registers(self, address, count=1, *, unit_id=None):
        return self.read(4, address, count, unit_id=unit_id)

    def write_single_coil(self, address, value, *, unit_id=None):
        self._range(address, 1, 1)
        return self._invoke('write_single_coil', address, boolean(value), self._unit(unit_id))

    def write_single_register(self, address, value, *, unit_id=None):
        self._range(address, 1, 1)
        integer(value, 'value', 0, 65535)
        return self._invoke('write_single_register', address, value, self._unit(unit_id))

    def write_multiple_coils(self, address, values, *, unit_id=None):
        values = [boolean(v) for v in values]
        self._range(address, len(values), 1968)
        return self._invoke('write_multiple_coils', address, values, self._unit(unit_id))

    def write_multiple_registers(self, address, values, *, unit_id=None):
        values = [integer(v, 'register', 0, 65535) for v in values]
        self._range(address, len(values), 123)
        return self._invoke('write_multiple_registers', address, values, self._unit(unit_id))

    def _collect_modbus(self, calls, points):
        """按 fes_channel_modbus_define_call/data 采集一轮；调用者负责周期和入库。

        rtu_id 仅用于筛选业务点表，irtuaddr 才是线上从站地址。
        不向数据库写入数据；失败抛异常，不把失败点伪装成零值。
        """
        calls, points = list(calls), list(points)
        if self.rtu_id is not None:
            calls = [r for r in calls if field(r, 'rtu_id') == self.rtu_id]
            points = [r for r in points if field(r, 'rtu_id') == self.rtu_id]
        ids = {field(r, 'rtu_id') for r in calls + points}
        if len(ids) > 1:
            raise ValueError('一个客户端只采集一个 RTU；请指定 rtu_id 过滤点表')
        seen = set()
        plan = []
        for call in sorted(calls, key=lambda r: field(r, 'call_seq', 0)):
            seq = integer(field(call, 'call_seq'), 'call_seq', 0, 2**31 - 1)
            if seq in seen:
                raise ValueError(f'重复 call_seq={seq}')
            seen.add(seq)
            address = field(call, 'iplace')
            count = field(call, 'inum')
            fc = field(call, 'icode')
            if fc not in (1, 2, 3, 4):
                raise ValueError(f'不支持召唤功能码 {fc}')
            self._range(address, count, 2000 if fc < 3 else 125)
            unit = self._unit(field(call, 'irtuaddr'))
            matched = [p for p in points if field(p, 'call_seq') == seq]
            for p in matched:
                integer(field(p, 'cnt'), 'cnt', 0, 2**31 - 1)
                offset = integer(field(p, 'reg_addr'), 'reg_addr', address, address + count - 1) - address
                if fc >= 3:
                    # 先验证整点宽度/位号，再进行通信。
                    decode_point(bytes((count - offset) * 2), p)
                elif field(p, 'trans_type', 8) != 8:
                    raise ValueError('线圈/离散输入点只支持 trans_type=8；reg_addr 是位地址')
            plan.append((call, matched, address, count, fc, unit))
        if any(field(p, 'call_seq') not in seen for p in points):
            raise ValueError('存在没有对应召唤行的数据点')
        result = []
        for call, matched, address, count, fc, unit in plan:
            values = self.read(fc, address, count, unit_id=unit)
            raw = b''.join(v.to_bytes(2, 'big') for v in values) if fc >= 3 else b''
            now = datetime.now(timezone.utc)
            for p in matched:
                offset = field(p, 'reg_addr') - address
                value = (decode_point(raw[offset * 2:], p) if fc >= 3
                         else bool(values[offset]) ^ bool(field(p, 'if_reverse', 0)))
                kind = 'yx' if field(p, 'if_yx', fc < 3) else 'yc'
                result.append(ModbusValue(field(call, 'rtu_id'), field(call, 'call_seq'),
                                          field(p, 'cnt'), kind, bool(value) if kind == 'yx' else value, now))
        return result

    def _check_definition(self, row, is_yk):
        if self.rtu_id is not None and field(row, 'rtu_id') != self.rtu_id:
            raise ValueError('命令所属 RTU 与客户端不一致')
        if field(row, 'ifyk', int(is_yk)) != int(is_yk):
            raise ValueError('ifyk 与命令类型不一致')

    def _write_yt_definition(self, definition, value):
        """按 fes_channel_modbus_define_ytyk 下发设点，支持类型 1..5。"""
        self._check_definition(definition, False)
        typ = integer(field(definition, 'ytyk_type'), 'ytyk_type', 1, 5)
        factor = field(definition, 'multi_para', 1.0)
        value = float(value) * (factor if abs(factor) > 1e-7 else 1.0)
        if not math.isfinite(value):
            raise ValueError('设点值必须有限')
        fmt = {1: 'h', 2: 'H', 3: 'i', 4: 'I', 5: 'f'}[typ]
        try:
            if typ <= 2:
                raw = struct.pack(('<' if field(definition, 'twobytedata_byteseq', 0) > 0 else '>') + fmt, int(value))
            else:
                raw = _order4(struct.pack('<' + fmt, value if typ == 5 else int(value)),
                              field(definition, 'fourbytedata_byteseq', 0))
        except (struct.error, OverflowError) as exc:
            raise ValueError('设点值超出配置的数据类型范围') from exc
        words = [int.from_bytes(raw[i:i+2], 'big') for i in range(0, len(raw), 2)]
        fc = field(definition, 'icode', 0) or (6 if len(words) == 1 else 16)
        return self._write_words(definition, fc, words)

    def _write_words(self, row, fc, words):
        kwargs = dict(unit_id=field(row, 'irtuaddr', self.unit_id))
        if fc == 6 and len(words) == 1:
            return self.write_single_register(field(row, 'reg_addr'), words[0], **kwargs)
        if fc == 16:
            return self.write_multiple_registers(field(row, 'reg_addr'), words, **kwargs)
        raise ValueError('寄存器命令只支持 06（单寄存器）或 16（多寄存器）')

    def _write_yk_definition(self, definition, close, *, current_value=None):
        """标准 05 线圈或 06/16 寄存器遥控；特殊位控制显式传入当前寄存器值。

        current_value 的新鲜度由调用者保证；跨设备读改写不具备原子性。
        不支持旧设备在 05 中携带非 FF00/0000 的私有编码。
        """
        self._check_definition(definition, True)
        on = boolean(close) ^ bool(field(definition, 'ifreverse', 0))
        fc = field(definition, 'icode', 0) or 5
        typ = field(definition, 'ytyk_type', 6)
        if fc == 5:
            if typ == 7:
                raise ValueError('特殊位控制必须明确配置功能码 06 或 16')
            return self.write_single_coil(field(definition, 'reg_addr'), on,
                                          unit_id=field(definition, 'irtuaddr', self.unit_id))
        little = field(definition, 'twobytedata_byteseq', 0) > 0
        if typ == 7:
            bit = integer(field(definition, 'special_yk_bit'), 'special_yk_bit', 0, 15)
            value = integer(current_value, 'current_value', 0, 65535)
            value = (value | (1 << bit)) if on else (value & ~(1 << bit))
        elif typ == 6:
            bit = integer(field(definition, 'special_yk_bit', 0), 'special_yk_bit', 0, 15)
            if field(definition, 'send_ff_replace_01', 0):
                value = 0xFF if on else 0
            else:
                value = (1 << bit) if on else 0
        elif typ in (1, 2):
            value = 0xFF00 if on else 0
        else:
            raise ValueError('遥控只支持 ytyk_type=1/2/6/7')
        raw = value.to_bytes(2, 'little' if little else 'big')
        return self._write_words(definition, fc, [int.from_bytes(raw, 'big')])


class _ModbusTcpTransport(_ModbusOperations):
    """每个连接串行请求；无自动重试，无后台采集；可使用 with。"""

    def __init__(self, host, port=502, *, unit_id=1, timeout=3.0, rtu_id=None):
        from pyModbusTCP.client import ModbusClient
        self._init_operations(unit_id, rtu_id)
        self.host, self.port = host, integer(port, 'port', 1, 65535)
        self.native = ModbusClient(host=host, port=port, unit_id=unit_id,
                                   timeout=timeout, auto_open=False, auto_close=False)

    @property
    def connected(self):
        return self.native.is_open

    def connect(self):
        with self._lock:
            try:
                opened = self.native.open()
            except OSError as exc:
                raise ModbusError(f'TCP 连接失败 {self.host}:{self.port}: {exc}') from exc
            if not opened:
                raise ModbusError(f'TCP 连接失败 {self.host}:{self.port}: {self.native.last_error_as_txt}')
        return self

    def close(self):
        with self._lock:
            self.native.close()

    def _invoke(self, name, address, argument, unit_id):
        with self._lock:
            if not self.connected:
                raise ModbusError('未连接，请先 connect() 或使用 with')
            self.native.unit_id = unit_id
            result = getattr(self.native, name)(address, argument)
            if result is None or result is False:
                raise ModbusError(f'{name} 失败: {self.native.last_error_as_txt}; '
                                  f'{self.native.last_except_as_txt}',
                                  exception_code=self.native.last_except or None)
            return result


class _ModbusMasterOperations:
    """TCP/RTU 主站共用的配置、采集和按点号控制；不创建传输连接。"""

    def _init_master(self, calls, points, definitions):
        if self.rtu_id is not None:
            integer(self.rtu_id, 'rtu_id', 1, 2**63 - 1)
        self._calls = []
        self._points = []
        self._definitions = []
        self.configure(calls or (), points or (), definitions or ())

    @classmethod
    def from_store(cls, store, rtu_id, *, unit_id=None, **kwargs):
        """从 ``ScadaStore`` 读取通道、召唤、数据点和遥控遥调配置。

        ``fes_rtu_define`` 同时保存唯一通道的 TCP 目标或串口名；Modbus 专用表保存从站号和
        点表。每个召唤行自己的 ``irtuaddr`` 仍优先于默认值。
        """
        integer(rtu_id, 'rtu_id', 1, 2**63 - 1)
        # 同一只读事务加载完整配置，避免分页截断或跨表读取不同版本。
        with store.connection() as connection:
            connection.execute('BEGIN')
            rtu = connection.execute(
                'SELECT * FROM fes_rtu_define WHERE rtu_id=?', (rtu_id,),
            ).fetchone()
            if rtu is None:
                raise ValueError(f'RTU {rtu_id} 不存在')
            channel_type = rtu['channel_type']
            if channel_type != cls.CHANNEL_TYPE:
                raise ValueError(f'RTU {rtu_id} 不是 {cls.PROTOCOL_NAME} 通道 {cls.CHANNEL_TYPE}')
            tables = {}
            for table in ('fes_channel_modbus_define',
                          'fes_channel_modbus_define_call', 'fes_channel_modbus_define_data',
                          'fes_channel_modbus_define_ytyk'):
                tables[table] = [dict(row) for row in connection.execute(
                    f'SELECT * FROM {table} WHERE rtu_id=? ORDER BY id', (rtu_id,),
                )]
            rows = [dict(row) for row in connection.execute(
                f'SELECT * FROM {cls.PARAMETER_TABLE} WHERE rtu_id=?', (rtu_id,),
            )]
            if len(rows) > 1:
                raise ValueError(f'RTU {rtu_id} 需要唯一 {cls.PARAMETER_TABLE} 参数定义，实际找到 {len(rows)} 条')
            parameter = rows[0] if rows else None
        channel = dict(rtu)
        if parameter:
            # 协议专用参数表是 Web 当前配置入口；RTU 通用记录仍作为缺省值。
            address = field(parameter, 'remote_host', field(parameter, 'serial_port'))
            if address:
                channel['chan_addr'] = address
            port = field(parameter, 'remote_port')
            if port is not None:
                channel['chan_port'] = port
            if unit_id is None and field(parameter, 'unit_id') is not None:
                unit_id = field(parameter, 'unit_id')
            for name in ('baudrate', 'bytesize', 'parity', 'stopbits'):
                if field(parameter, name) is not None and name not in kwargs:
                    kwargs[name] = field(parameter, name)
            if field(parameter, 'response_timeout') is not None and 'timeout' not in kwargs:
                kwargs['timeout'] = field(parameter, 'response_timeout')
        if not field(channel, 'chan_addr'):
            raise ValueError(f'RTU {rtu_id} 尚未配置唯一通道')
        modbus_channels = tables['fes_channel_modbus_define']
        if unit_id is None and len(modbus_channels) > 1:
            raise ValueError('存在多个 Modbus 从站定义，请指定 unit_id')
        if unit_id is None and len(modbus_channels) == 1:
            unit_id = field(modbus_channels[0], 'irtuaddr', 1)
        calls = tables['fes_channel_modbus_define_call']
        points = tables['fes_channel_modbus_define_data']
        definitions = tables['fes_channel_modbus_define_ytyk']
        if not calls and points:
            raise ValueError(f'RTU {rtu_id} 有数据点但没有召唤定义')
        if unit_id is None:
            return cls.from_channel(
                channel, rtu_id=rtu_id, calls=calls, points=points,
                definitions=definitions, **kwargs,
            )
        return cls.from_channel(
            channel, rtu_id=rtu_id, unit_id=unit_id,
            calls=calls, points=points, definitions=definitions, **kwargs,
        )

    def _scoped_rows(self, rows):
        rows = list(rows)
        if self.rtu_id is not None:
            rows = [row for row in rows if field(row, 'rtu_id') == self.rtu_id]
        return rows

    def configure(self, calls, points, definitions=()):
        """复制并校验配置；支持 dict/dataclass，失败时保留上一份配置。"""
        calls, points, definitions = [deepcopy(self._scoped_rows(rows))
                                      for rows in (calls, points, definitions)]
        self._validate_poll_plan(calls, points)
        ids = {field(row, 'rtu_id') for row in calls + points + definitions}
        if len(ids) > 1:
            raise ValueError('一个主站只配置一个 RTU；请指定 rtu_id 过滤点表')
        seen = set()
        for row in definitions:
            key = (integer(field(row, 'ifyk'), 'ifyk', 0, 1),
                   integer(field(row, 'cnt'), 'cnt', 0, 2**31 - 1))
            self._unit(field(row, 'irtuaddr'))
            self._range(field(row, 'reg_addr'), 1, 1)
            if key in seen:
                raise ValueError(f'重复遥控/遥调定义: {key}')
            seen.add(key)
        with self._lock:
            self._calls, self._points, self._definitions = calls, points, definitions
        return self

    def _validate_poll_plan(self, calls, points):
        """验证点表关联和帧范围，不进行网络请求。"""
        seen = set()
        call_by_seq = {}
        for call in calls:
            seq = integer(field(call, 'call_seq'), 'call_seq', 0, 2**31 - 1)
            if seq in seen:
                raise ValueError(f'重复 call_seq={seq}')
            seen.add(seq)
            address = field(call, 'iplace')
            count = field(call, 'inum')
            fc = field(call, 'icode')
            if fc not in (1, 2, 3, 4):
                raise ValueError(f'不支持召唤功能码 {fc}')
            integer(fc, 'icode', 1, 4)
            self._unit(field(call, 'irtuaddr'))
            _ModbusOperations._range(address, count, 2000 if fc < 3 else 125)
            call_by_seq[seq] = (address, count, fc)
        point_keys = set()
        for point in points:
            seq = integer(field(point, 'call_seq'), 'call_seq', 0, 2**31 - 1)
            if seq not in call_by_seq:
                raise ValueError(f'数据点没有对应召唤行: call_seq={seq}')
            address, count, fc = call_by_seq[seq]
            number = integer(field(point, 'cnt'), 'cnt', 0, 2**31 - 1)
            key = (bool(field(point, 'if_yx', fc < 3)), number)
            if key in point_keys:
                raise ValueError(f'重复遥测/遥信点号: {key}')
            point_keys.add(key)
            reg_addr = field(point, 'reg_addr')
            integer(reg_addr, 'reg_addr', address, address + count - 1)
            if fc >= 3:
                # 验证类型、字节序和点宽度；这里不访问设备。
                decode_point(bytes((address + count - reg_addr) * 2), point)
            elif field(point, 'trans_type', 8) != 8:
                raise ValueError('线圈/离散输入点只支持 trans_type=8；reg_addr 是位地址')
        ids = {field(row, 'rtu_id') for row in [*calls, *points]}
        if len(ids) > 1:
            raise ValueError('一个主站只采集一个 RTU；请指定 rtu_id 过滤点表')
        return call_by_seq

    def collect_once(self, calls=None, points=None):
        """采集一轮已配置点表；返回 ``ModbusValue`` 列表。"""
        with self._lock:
            if calls is None:
                calls = self._calls
            if points is None:
                points = self._points
            calls = self._scoped_rows(calls)
            if not calls:
                raise ValueError('尚未配置本 RTU 的 Modbus 召唤行')
            return self._collect_modbus(calls, points)

    def poll_loop(self, callback: Callable[[list[ModbusValue]], object], *,
                  interval=1.0, stop_event=None, max_cycles=None):
        """每轮完成后等待 interval 秒，直到事件、次数或异常终止。

        回调在主站调用线程内执行，收到的列表属于本轮快照。回调异常会原样
        抛出并停止循环；通信异常同样不会被吞掉，避免把旧值误当成新值。
        ``stop_event`` 使用 ``threading.Event`` 或兼容 is_set()/wait() 的对象。
        等待间隔可以立即停止；进行中的请求仍受 timeout 限制。
        """
        if not callable(callback):
            raise TypeError('callback 必须可调用')
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or interval < 0):
            raise ValueError('interval 必须是非负有限数')
        if max_cycles is not None:
            integer(max_cycles, 'max_cycles', 1, 2**31 - 1)
        if stop_event is not None and not all(
                callable(getattr(stop_event, name, None)) for name in ('is_set', 'wait')):
            raise TypeError('stop_event 必须提供 is_set()/wait()')
        event = stop_event if stop_event is not None else threading.Event()
        cycles = 0
        while not event.is_set():
            callback(self.collect_once())
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            event.wait(interval)
        return cycles

    def _point_definition(self, point_no, definitions, is_yk):
        integer(point_no, 'point_no', 0, 2**31 - 1)
        if definitions is None:
            definitions = self._definitions
        scoped = self._scoped_rows(definitions)
        if len({field(row, 'rtu_id') for row in scoped}) > 1:
            raise ValueError('多个 RTU 的命令定义需要指定 rtu_id')
        matches = [row for row in scoped
                   if field(row, 'cnt') == point_no and field(row, 'ifyk') == is_yk]
        if len(matches) != 1:
            raise ValueError(f'点号 {point_no} 没有唯一的 Modbus 命令定义')
        return matches[0]

    def send_yk_point(self, point_no, close, definitions=None, *, current_value=None):
        """按点号选择遥控定义并下发，返回底层写操作结果。"""
        with self._lock:
            return self._write_yk_definition(self._point_definition(point_no, definitions, 1), close,
                                             current_value=current_value)

    def send_yt_point(self, point_no, value, definitions=None):
        """按点号选择遥调定义并下发，返回底层写操作结果。"""
        with self._lock:
            return self._write_yt_definition(self._point_definition(point_no, definitions, 0), value)


class ModbusTcpMaster(_ModbusMasterOperations, _ModbusTcpTransport):
    """基于 pyModbusTCP 的 SCADA 主站，配置采集及遥控遥调由共用层提供。

    calls/points/definitions 分别对应 fes_channel_modbus_define_call/data/ytyk。
    collect_once 返回 ModbusValue，poll_loop 在调用线程中执行回调。
    """

    CHANNEL_TYPE = 'modbus-tcp'
    PROTOCOL_NAME = 'Modbus TCP'
    PARAMETER_TABLE = 'fes_channel_modbus_tcp_define'

    def __init__(self, host, port=502, *, unit_id=1, timeout=3.0, rtu_id=None,
                 calls=None, points=None, definitions=None):
        super().__init__(host, port, unit_id=unit_id, timeout=timeout, rtu_id=rtu_id)
        self._init_master(calls, points, definitions)

    @classmethod
    def from_channel(cls, channel, **kwargs):
        """通道地址/端口决定 TCP 目标，irtuaddr（若有）提供默认从站号。"""
        kwargs.setdefault('rtu_id', field(channel, 'rtu_id'))
        kwargs.setdefault('unit_id', field(channel, 'irtuaddr', 1))
        return cls(field(channel, 'chan_addr'), field(channel, 'chan_port', 502), **kwargs)

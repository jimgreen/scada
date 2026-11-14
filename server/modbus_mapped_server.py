"""S server engineering-value adapter using the shared Modbus wire codec."""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from contextlib import closing

from pymodbus.constants import ExcCodes
from pymodbus.simulator import DataType, SimData

from client.modbus_tcp import field, integer, normalize_modbus_address
from .modbus_trans_server import ModbusTcpTransServer


class _PreparedUpdates(tuple):
    """Immutable engineering samples with their validated wire representation."""
    def __new__(cls, owner, samples, ready):
        batch = super().__new__(cls, samples)
        batch._owner = owner
        batch._ready = tuple(ready)
        return batch


def _numeric_mapping(row, typ):
    types = {1:'int16', 2:'uint16', 3:'int32', 4:'uint32', 5:'float32', 10:'int64', 11:'uint64'}
    if typ not in types:
        raise ValueError(f'S Modbus 工程点暂不支持寄存器位/字节切片类型 {typ}，不能回退为原始寄存器')
    byte_order, word_order = 'big', 'big'
    if typ in (1, 2):
        byte_order = 'little' if field(row, 'twobytedata_byteseq', 0) else 'big'
    if typ in (3, 4, 5):
        order = int(field(row, 'fourbytedata_byteseq', 0))
        if order not in range(4):
            raise ValueError('四字节字序必须在 0..3')
        byte_order, word_order = [('little','big'), ('little','little'),
                                 ('big','little'), ('big','big')][order]
    return dict(data_type=types[typ], byte_order=byte_order, word_order=word_order)


def _sample_mapping(row):
    typ = int(row['trans_type'])
    little = bool(field(row, 'twobytedata_byteseq', 0))
    if typ == 6:
        return dict(data_type='uint16', slice_shift=8 if little else 0, slice_width=8)
    if typ == 7:
        # The acquisition codec's int8 uses the first (high) wire byte.
        return dict(data_type='uint16', slice_shift=8, slice_width=8, slice_signed=True)
    if typ in (8, 9):
        shift = integer(field(row, 'offset_bit', 0), 'offset_bit', 0, 15)
        width = 1 if typ == 8 else integer(field(row, 'all_bit'), 'all_bit', 1, 16-shift)
        return dict(data_type='uint16', slice_shift=shift, slice_width=width,
                    byte_order='little' if little else 'big',
                    reverse=bool(field(row, 'if_reverse', 0)) if typ == 8 else False)
    return _numeric_mapping(row, typ)


def _control_mapping(row):
    fc = field(row, 'icode', 0) or 5
    typ = field(row, 'ytyk_type', 6)
    reverse = bool(field(row, 'ifreverse', 0))
    if fc in (5, 15) and typ in (1, 2, 6):
        return dict(data_type='bool', reverse=reverse), 'coil'
    if fc not in (6, 16) or typ not in (1, 2, 6, 7):
        raise ValueError('遥控需配置 05/15 线圈或 06/16 寄存器，ytyk_type=1/2/6/7；类型7仅寄存器')
    codec = dict(data_type='uint16', reverse=reverse,
                 byte_order='little' if field(row, 'twobytedata_byteseq', 0) else 'big')
    if typ in (6, 7):
        bit = integer(field(row, 'special_yk_bit', 0), 'special_yk_bit', 0, 15)
        if typ == 7:
            codec.update(slice_shift=bit, slice_width=1)
        else:
            codec['yk_on_word'] = 0xFF if field(row, 'send_ff_replace_01', 0) else 1 << bit
    else:
        codec['yk_on_word'] = 0xFF00
    return codec, 'holding'


def simulator_mappings(store, rtu_id, unit_id):
    """Load the channel's persisted C/S mapping contract in one read snapshot."""
    if rtu_id is None:
        return []
    with store.connection() as con:
        con.execute('BEGIN')
        rtu = con.execute('SELECT channel_id FROM fes_rtu_define WHERE rtu_id=?', (rtu_id,)).fetchone()
        if rtu is None:
            raise ValueError(f'RTU {rtu_id} 不存在')
        channel = rtu[0]
        calls = {r['call_seq']: dict(r) for r in con.execute(
            'SELECT * FROM fes_channel_modbus_define_call WHERE channel_id=?', (channel,))}
        samples = [dict(r) for r in con.execute(
            'SELECT * FROM fes_channel_modbus_define_data WHERE channel_id=?', (channel,))]
        commands = [dict(r) for r in con.execute(
            'SELECT * FROM fes_channel_modbus_define_ytyk WHERE channel_id=?', (channel,))]
    mappings = []
    for row in samples:
        call = calls.get(row['call_seq'])
        if call is None:
            raise ValueError('Modbus 数据点缺少召唤定义')
        if int(field(call, 'irtuaddr', unit_id)) != unit_id:
            raise ValueError('S Modbus 服务的点映射必须使用同一站号')
        fc = int(call['icode'])
        area = {1:'coil', 2:'discrete', 3:'holding', 4:'input'}.get(fc)
        if area is None:
            raise ValueError('读取功能码必须为 01/02/03/04')
        kind = 'yx' if field(row, 'if_yx', fc < 3) else 'yc'
        address = normalize_modbus_address(row['reg_addr'], field(call, 'address_base', 0))
        start = normalize_modbus_address(call['iplace'], field(call, 'address_base', 0))
        if fc < 3:
            if row['trans_type'] != 8:
                raise ValueError('Modbus 位区只支持 trans_type=8')
            codec = dict(data_type='bool', reverse=bool(field(row, 'if_reverse', 0)))
        else:
            codec = _sample_mapping(row)
        size = ModbusTcpTransServer._TYPE_SIZES.get(codec['data_type'], 1)
        if address < start or address + size > start + int(call['inum']):
            raise ValueError('数据点编码范围超出召唤定义')
        mappings.append(dict(kind=kind, pnt_no=row['cnt'], address=address,
                             register_area=area, **codec))
    for row in commands:
        if int(field(row, 'irtuaddr', unit_id)) != unit_id:
            raise ValueError('S Modbus 服务的命令映射必须使用同一站号')
        kind = 'yk' if row['ifyk'] else 'yt'
        if kind == 'yk':
            codec, area = _control_mapping(row)
        else:
            codec, area = _numeric_mapping(row, row['ytyk_type']), 'holding'
            factor = float(field(row, 'multi_para', 1))
            codec['scale'] = 1 / factor if abs(factor) > 1e-7 else 1
        mappings.append(dict(kind=kind, pnt_no=row['cnt'], register_area=area,
                             address=normalize_modbus_address(row['reg_addr'], field(row,'address_base',0)), **codec))
    return mappings


class ModbusMappedServer(ModbusTcpTransServer):
    """TCP/RTU simulator, with database-owned values and command receipts."""
    # S simulates the device's writable holding/coil areas. T measurements
    # remain read-only: only commands may be forwarded to the acquisition side.
    ALLOW_MEASUREMENT_WRITES = True
    def __init__(self, *, store, rtu_id, points, **kwargs):
        self.store, self.rtu_id = store, int(rtu_id)
        values = {}
        with store.connection() as con:
            con.execute('BEGIN')
            for kind in ('yc','yx','yk','yt'):
                values.update({(kind,int(r[0])): r[1] if r[1] is not None else 0 for r in con.execute(
                    f'SELECT pnt_no,value FROM fes_{kind}_define WHERE rtu_id=?', (rtu_id,))})
        missing = {(p['kind'], p['pnt_no']) for p in points} - values.keys()
        if missing:
            raise ValueError(f'Modbus 映射缺少四遥定义: {sorted(missing)}')
        self._sync_stop = threading.Event()
        self._sync_thread = None
        self._address_rows = {}
        self._wire_values = {}
        self._write_generation = 0
        self._special_register_codecs = any('slice_width' in row or 'yk_on_word' in row for row in points)
        self._database_samples = {k:v for k,v in values.items() if k[0] in {'yc','yx'}}
        self._business_samples = {}
        super().__init__(points=points, point_values=values, command_handler=self._command, **kwargs)
        # Mappings are fixed for the service lifetime. Keep ordinary channels
        # on the original codec/publication path, without per-point slicing checks.
        if not any('slice_width' in row for row in self._mappings.values()):
            self._write_local = super()._write_local
            if not self._special_register_codecs:
                self._decode = super()._decode

    @staticmethod
    def _swap_word(row, word):
        return ((word & 255) << 8) | (word >> 8) if row.get('byte_order') == 'little' else word

    def _wire_mask(self, row):
        if 'slice_width' not in row:
            return 0xFFFF
        mask = ((1 << row['slice_width']) - 1) << row['slice_shift']
        return self._swap_word(row, mask)

    def _validate_mapping_overlap(self, row):
        if 'slice_width' in row:
            shift = integer(row['slice_shift'], 'slice_shift', 0, 15)
            integer(row['slice_width'], 'slice_width', 1, 16-shift)
        for offset in range(row['size']):
            for previous in self._address_rows.get((row['register_area'], row['address'] + offset), ()):
                if (row['size'] != 1 or previous['size'] != 1 or
                        row['register_area'] in {'coil', 'discrete'} or
                        self._wire_mask(row) & self._wire_mask(previous)):
                    raise ValueError(f"Modbus 地址/位掩码冲突 {row['register_area']}/{row['address'] + offset}")

    def _add_mapping(self, source, values):
        super()._add_mapping(source, values)
        row = self._mappings[(str(source['kind']).lower(), self._point_no(source))]
        for offset in range(row['size']):
            self._address_rows.setdefault((row['register_area'], row['address'] + offset), []).append(row)

    @staticmethod
    def _bit_value(row, value):
        if value not in (0, 1):
            raise ValueError('Modbus 位值必须为 0/1')
        return bool(value) ^ bool(row.get('reverse'))

    def _simdata(self):
        blocks = [[], [], [], []]
        self._sim_blocks = {}
        for row in self._mappings.values():
            area = row['register_area']
            value = self._values[(row['kind'], row['pnt_no'])]
            raw = [self._bit_value(row, value)] if area in {'coil', 'discrete'} else self._encode(row, value)
            for offset, word in enumerate(raw):
                slot = (area, row['address'] + offset)
                self._wire_values[slot] = self._wire_values.get(slot, 0) | word
        for index, area in enumerate(('coil', 'discrete', 'holding', 'input')):
            seen = set()
            for row in self._mappings.values():
                if row['register_area'] != area or row['address'] in seen:
                    continue
                raw = [self._wire_values[(area, row['address'] + offset)] for offset in range(row['size'])]
                bit = area in {'coil', 'discrete'}
                block = SimData(address=row['address'], count=1, values=bool(raw[0]) if bit else raw,
                                datatype=DataType.BITS if bit else DataType.REGISTERS, readonly=area == 'input')
                blocks[index].append(block)
                for offset in range(row['size']):
                    self._sim_blocks[(area, row['address'] + offset)] = block
                    seen.add(row['address'] + offset)
            if not blocks[index]:
                bit = area in {'coil', 'discrete'}
                blocks[index].append(SimData(address=0, count=1, values=False if bit else [0],
                                            datatype=DataType.BITS if bit else DataType.REGISTERS))
            blocks[index].sort(key=lambda block: block.address)
        return tuple(blocks)

    def _encode(self, row, value):
        if self._special_register_codecs and ('yk_on_word' in row or 'slice_width' in row):
            return self._encode_special(row, value)
        if not math.isfinite(float(value)):
            raise ValueError('Modbus 值必须为有限数字')
        if row['data_type'].startswith('uint') and (value-row['offset'])/(row['scale'] or 1) < 0:
            raise ValueError('无符号寄存器不能表示负数')
        return super()._encode(row,value)

    def _encode_special(self, row, value):
        if not math.isfinite(float(value)):
            raise ValueError('Modbus 值必须为有限数字')
        if 'yk_on_word' in row:
            return [self._swap_word(row, row['yk_on_word'] if self._bit_value(row, value) else 0)]
        if 'slice_width' in row:
            width = row['slice_width']
            number = int(value)
            signed = bool(row.get('slice_signed'))
            lower, upper = (-(1 << (width-1)), (1 << (width-1))-1) if signed else (0, (1 << width)-1)
            if number != value or not lower <= number <= upper:
                raise ValueError(f'切片值必须为 {lower}..{upper} 的整数')
            if row.get('reverse'):
                number ^= 1
            return [self._swap_word(row, (number & ((1 << width)-1)) << row['slice_shift'])]
        raise ValueError('未知 Modbus 切片编码')

    def _decode(self, row, values):
        if 'yk_on_word' in row:
            word = self._swap_word(row, int(values[0]))
            if word not in (0, row['yk_on_word']):
                raise ValueError('寄存器遥控值与配置的合/分编码不符')
            return bool(word) ^ bool(row.get('reverse'))
        if 'slice_width' in row:
            width = row['slice_width']
            value = (self._swap_word(row, int(values[0])) >> row['slice_shift']) & ((1 << width)-1)
            if row.get('slice_signed') and value & (1 << (width-1)):
                value -= 1 << width
            return value ^ 1 if row.get('reverse') else value
        return super()._decode(row, values)

    def _command(self, kind, no, value):
        self._commands([(kind, no, value)])
        return True

    def _commands(self, commands):
        now = int(time.time()*1000)
        changes = []
        for kind, no, value in commands:
            if not math.isfinite(float(value)):
                raise ValueError('Modbus 值必须为有限数字')
            fields = dict(value=int(value) if kind in {'yx','yk'} else value, status=0, refresh_time=now)
            if kind in {'yk','yt'}:
                fields.update(status=1, cmd_handle_time=now)
            changes.append((f'fes_{kind}_define', [self.rtu_id,no], fields))
        self.store.update_rows(changes)
        self._business('Modbus写入已提交模拟数据库', rows=(dict(四遥=kind.upper(), 点号=no,
            值=value, 质量=1 if kind in {'yk','yt'} else 0, 刷新时刻=now,
            指令接收时刻=now if kind in {'yk','yt'} else None) for kind,no,value in commands),
            RTU编号=self.rtu_id, 站号=self.unit_id)

    async def _action(self, function_code, start_address, address, count, current_registers, set_values):
        if set_values is None or function_code not in {5, 6, 15, 16}:
            return None
        area = 'coil' if function_code in {5, 15} else 'holding'
        candidates = {}
        for offset in range(len(set_values)):
            rows = self._address_rows.get((area, address + offset))
            if not rows:
                return ExcCodes.ILLEGAL_ADDRESS
            for row in rows:
                candidates[(row['kind'], row['pnt_no'])] = row
        decoded = []
        try:
            for key, row in candidates.items():
                begin = row['address'] - address
                if begin < 0 or begin + row['size'] > len(set_values):
                    return ExcCodes.ILLEGAL_VALUE
                raw = set_values[begin:begin + row['size']]
                value = self._bit_value(row, raw[0]) if area == 'coil' else self._decode(row, raw)
                if not math.isfinite(float(value)):
                    return ExcCodes.ILLEGAL_VALUE
                decoded.append((*key, value))
        except (ValueError, OverflowError):
            return ExcCodes.ILLEGAL_VALUE
        try:
            # All mapped points in one request commit or roll back together.
            self._business('Modbus写请求解码', rows=(dict(四遥=kind.upper(), 点号=no, 工程值=value)
                for kind,no,value in decoded), RTU编号=self.rtu_id, 站号=self.unit_id,
                功能码=function_code, 数据区=area, 起始地址0基=address, 数量=len(set_values))
            self._commands(decoded)
        except Exception as exc:
            self._business('Modbus写请求未提交数据库', RTU编号=self.rtu_id, 站号=self.unit_id,
                功能码=function_code, 起始地址0基=address, 异常码=4, 错误=str(exc))
            self._log('ERROR', f'Modbus 批量写库失败: {exc}')
            return ExcCodes.DEVICE_FAILURE
        for kind, no, value in decoded:
            self._values[(kind, no)] = value
            if kind in {'yc', 'yx'}:
                self._database_samples[(kind, no)] = value
        self._wire_values.update({(area, address + i): word for i, word in enumerate(set_values)})
        self._write_generation += 1
        return None

    def _write_local(self, runtime, row, area, raw):
        sliced = 'slice_width' in row
        if sliced:
            mask = self._wire_mask(row)
            raw = [(self._wire_values[(area, row['address'])] & ~mask) | (raw[0] & mask)]
        written = super()._write_local(runtime, row, area, raw)
        if written and sliced:
            self._wire_values.update({(area, row['address'] + i): word for i, word in enumerate(raw)})
        return written

    def _prepare_sample(self, kind, point_no, value):
        key = (kind, int(point_no))
        row = self._mappings[key]
        area = row['register_area']
        if not math.isfinite(float(value)):
            raise ValueError('Modbus 值必须为有限数字')
        if area in {'coil', 'discrete'}:
            if value not in (0,1):
                raise ValueError('Modbus 位值必须为 0/1')
            raw = [self._bit_value(row, value)]
        else:
            raw = self._encode(row,value)
        return key, row, area, raw

    def prepare_updates(self, updates):
        batch, ready, seen = [], [], set()
        for kind, no, value in updates:
            key, row, area, raw = self._prepare_sample(kind, int(no), value)
            if key in seen:
                raise ValueError('批次点号不能重复')
            seen.add(key)
            value = self._bit_value(row, raw[0]) if area in {'coil','discrete'} else self._decode(row,raw)
            batch.append((*key,value))
            ready.append((key, row, area, raw, value))
        return _PreparedUpdates(self, batch, ready)

    def publish_updates(self, batch):
        failed = (self._publish_ready(batch._ready)
                  if isinstance(batch, _PreparedUpdates) and batch._owner is self
                  else self.update_many(batch))
        if failed:
            raise next(iter(failed.values()))

    def update_point(self, kind, point_no, value):
        batch = self.prepare_updates([(kind,point_no,value)])
        self.publish_updates(batch)
        return batch[0][2]

    def snapshot(self):
        return self._on_datastore(lambda: [dict(kind=k, pnt_no=n, value=v)
                                          for (k,n),v in self._values.items()])

    def _sync(self):
        with closing(sqlite3.connect(str(self.store.db_path), timeout=2, isolation_level=None)) as con:
            version = None
            while not self._sync_stop.wait(.05):
                try:
                    generation = self._write_generation
                    current = con.execute('PRAGMA data_version').fetchone()[0]
                    if current == version:
                        continue
                    changed, details, states = [], [], {}
                    con.execute('BEGIN')
                    try:
                        for kind in ('yc','yx'):
                            for no,value,status,stamp in con.execute(f'SELECT pnt_no,value,status,refresh_time FROM fes_{kind}_define WHERE rtu_id=?', (self.rtu_id,)):
                                value = 0 if value is None else value
                                if (kind,no) in self._mappings and value != self._database_samples.get((kind,no)):
                                    changed.append((kind,no,value))
                                if (kind,no) in self._mappings:
                                    states[(kind,no)] = (value,status)
                                    previous = self._business_samples.get((kind,no))
                                    if previous != (value,status):
                                        row = self._mappings[(kind,no)]
                                        details.append(dict(四遥=kind.upper(), 点号=no, 数据区=row['register_area'],
                                            地址0基=row['address'], 数据类型=row['data_type'], 字节序=row['byte_order'],
                                            字序=row['word_order'], 旧值=previous[0] if previous else '初值',
                                            新值=value, 旧质量=previous[1] if previous else '初值', 新质量=status,
                                            源刷新时刻=stamp, 质量说明='本地状态，Modbus报文不携带质量位'))
                    finally:
                        con.rollback()
                    batch = self.prepare_updates(changed)
                    def publish_if_current():
                        # A remote write can commit while this thread is reading
                        # SQLite. Never overwrite its newer wire value with that
                        # older scan; retry from the database on the next tick.
                        if generation != self._write_generation:
                            return False
                        self.publish_updates(batch)
                        self._database_samples.update({(kind,no):value for kind,no,value in changed})
                        self._business_samples.update(states)
                        self._business('数据库变化已同步Modbus映射，等待对端读取', rows=details,
                                       RTU编号=self.rtu_id, 站号=self.unit_id)
                        return True
                    if self._on_datastore(publish_if_current):
                        version = current
                except Exception as exc:
                    self._log('ERROR', f'Modbus 数据库同步失败: {exc}')

    def start(self):
        super().start()
        self._sync_stop.clear()
        self._sync_thread = threading.Thread(target=self._sync, name='modbus-db-sync', daemon=True)
        self._sync_thread.start()

    def close(self):
        self._sync_stop.set()
        if self._sync_thread is not None:
            self._sync_thread.join(3)
            self._sync_thread = None
        super().close()

    @property
    def diagnostics(self):
        return dict(value_mode='engineering_mapping', mapped_points=len(self._mappings),
                    database_sync_interval_ms=50, command_confirmation_scope='simulator_database')

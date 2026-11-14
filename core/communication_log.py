"""Observation-only business logs: point changes and actual IEC104 ASDUs.

No database reads, no sampling of changed points, no alteration of protocol
callbacks. One frame/batch is written together; unchanged polling is silent.
"""
from datetime import datetime
from itertools import count
from functools import wraps
import logging
import os
import struct
import threading
import time

from core.iec104_protocol import parse_apdu

LOG = logging.getLogger(__name__)
_batches = count(1)
_COT = {1: '周期', 2: '背景扫描', 3: '突发/变位', 5: '请求', 6: '激活',
        7: '激活确认', 8: '撤销', 9: '撤销确认', 10: '激活终止', 20: '站总召返回',
        44: '未知类型', 45: '未知传送原因', 46: '未知公共地址', 47: '未知信息体地址'}
# kind, encoding, value+quality/qualifier bytes, time bytes
_TYPES = {1: ('yx', 'single', 1, 0), 2: ('yx', 'single', 1, 3),
          3: ('yx', 'double', 1, 0), 4: ('yx', 'double', 1, 3),
          5: ('yc', 'step', 2, 0), 6: ('yc', 'step', 2, 3),
          9: ('yc', 'normalized', 3, 0), 10: ('yc', 'normalized', 3, 3),
          11: ('yc', 'scaled', 3, 0), 12: ('yc', 'scaled', 3, 3),
          13: ('yc', 'float', 5, 0), 14: ('yc', 'float', 5, 3),
          21: ('yc', 'normalized', 2, 0),
          30: ('yx', 'single', 1, 7), 31: ('yx', 'double', 1, 7),
          32: ('yc', 'step', 2, 7), 34: ('yc', 'normalized', 3, 7),
          35: ('yc', 'scaled', 3, 7), 36: ('yc', 'float', 5, 7),
          45: ('yk', 'single', 1, 0), 46: ('yk', 'double', 1, 0),
          47: ('yk', 'step-command', 1, 0),
          48: ('yt', 'normalized', 3, 0), 49: ('yt', 'scaled', 3, 0),
          50: ('yt', 'float', 5, 0),
          58: ('yk', 'single', 1, 7), 59: ('yk', 'double', 1, 7),
          60: ('yk', 'step-command', 1, 7), 61: ('yt', 'normalized', 3, 7),
          62: ('yt', 'scaled', 3, 7), 63: ('yt', 'float', 5, 7)}


def _clock(raw):
    if not raw:
        return {}
    ms = int.from_bytes(raw[:2], 'little')
    minute = raw[2] & 63
    result = {'时标类型': 'CP24Time2a' if len(raw) == 3 else 'CP56Time2a',
              '时标原始': raw.hex(), '时标无效': int(bool(raw[2] & 128))}
    if len(raw) == 3:
        result['源时标'] = f'分={minute} 秒={ms/1000:.3f}（不含日期和小时）'
    else:
        result['源时标'] = (f'{2000+(raw[6]&127):04d}-{raw[5]&15:02d}-{raw[4]&31:02d}'
                          f'T{raw[3]&31:02d}:{minute:02d}:{ms//1000:02d}.{ms%1000:03d}')
        result['夏令时'] = int(bool(raw[3] & 128))
        try:
            datetime(2000+(raw[6]&127), raw[5]&15, raw[4]&31, raw[3]&31,
                     minute, ms//1000, ms%1000*1000)
        except ValueError:
            result['时标无效'] = 1
    if ms >= 60000 or minute > 59:
        result['时标无效'] = 1
    return result


def decode_asdu(asdu):
    """Return header and all objects, rejecting incomplete/ambiguous layouts."""
    if len(asdu) < 6:
        raise ValueError('ASDU头不足6字节')
    typ, vsq, cot, oa = asdu[:4]
    size, sequence = vsq & 127, bool(vsq & 128)
    header = {'类型标识': typ, 'COT': cot & 63, '原因': _COT.get(cot & 63, f'原因{cot & 63}'),
              'CA': int.from_bytes(asdu[4:6], 'little'), 'OA': oa,
              '否定确认': int(bool(cot & 64)), '测试': int(bool(cot & 128)), '点数': size}
    if 21 <= cot & 63 <= 36:
        header['原因'] = f'分组召唤返回{(cot & 63)-20}'
    if typ == 100:
        if size != 1 or len(asdu) != 10:
            raise ValueError('总召信息体长度无效')
        return header, [{'IOA': int.from_bytes(asdu[6:9], 'little'), 'QOI': asdu[9]}]
    if typ not in _TYPES:
        return header, [{'未解码信息体': asdu[6:].hex(' ')}]
    kind, encoding, width, time_bytes = _TYPES[typ]
    if not size or len(asdu) != 6 + (3 if sequence else 3*size) + (width+time_bytes)*size:
        raise ValueError('ASDU信息体数量/长度不一致')
    rows, offset, base = [], 6, 0
    for index in range(size):
        if not sequence or index == 0:
            base = int.from_bytes(asdu[offset:offset+3], 'little')
            offset += 3
        address = base + index if sequence else base
        if address > 0xffffff:
            raise ValueError('连续IOA溢出')
        raw = asdu[offset:offset+width]
        offset += width
        row = {'四遥': kind.upper(), 'IOA': address}
        if encoding == 'single':
            row['值'] = raw[0] & 1
        elif encoding in {'double', 'step-command'}:
            row['值'] = raw[0] & 3
            row['原始状态'] = '双点' if encoding == 'double' else '调节步进'
        elif encoding == 'step':
            row['值'] = (raw[0] & 127) - (128 if raw[0] & 64 else 0)
            row['过渡态'] = int(bool(raw[0] & 128))
        elif encoding == 'float':
            row['值'] = struct.unpack('<f', raw[:4])[0]
        else:
            number = int.from_bytes(raw[:2], 'little', signed=True)
            row['值'] = number / 32768 if encoding == 'normalized' else number
        if kind in {'yk', 'yt'}:
            row['选择'] = int(bool(raw[-1] & 128))
            row['限定词'] = raw[-1] & 127
        else:
            quality = (raw[0] & 240 if kind == 'yx' else raw[-1] if width > 2 or encoding == 'step' else 0)
            row['质量'] = f'0x{quality:02X}'
        row.update(_clock(asdu[offset:offset+time_bytes]))
        offset += time_bytes
        rows.append(row)
    return header, rows


class CommunicationLog:
    def __init__(self, write_many, **context):
        self.write_many, self.context = write_many, context
        self.point_map = {}
        self._next_error = 0

    @staticmethod
    def _text(value):
        return ' '.join(str(value).splitlines())[:1500]

    def rows(self, action, rows, **common):
        """Write every row, flushing in bounded groups without dropping points."""
        try:
            stamp = datetime.now().astimezone().isoformat(timespec='milliseconds')
            batch = next(_batches)
            prefix = (f'业务事件 {action} 事件时刻={stamp} 批次={os.getpid()}:{batch} '
                      f'线程={threading.current_thread().name} ')
            prefix += ' '.join(f'{k}={self._text(v)}' for k, v in {**self.context, **common}.items())
            lines = []
            for row in rows:
                lines.append(prefix + ' ' + ' '.join(f'{k}={self._text(v)}' for k, v in row.items()))
                if len(lines) >= 128:
                    self.write_many(lines)
                    lines = []
            if lines:
                self.write_many(lines)
        except Exception:
            if time.monotonic() >= self._next_error:
                self._next_error = time.monotonic() + 60
                LOG.exception('通信业务日志写入失败；通信业务继续执行')

    def event(self, action, **fields):
        self.rows(action, [fields])

    def soe(self, action, events):
        self.rows(action, ({'SOE事件': row.get('event_id', '接收批次'),
            '源RTU': row.get('rtu_id'), '源点号': row.get('pnt_no'),
            '转发点号': row.get('target_pnt_no'), 'CA': row.get('common_address'),
            'IOA': row.get('io_address'), '值': row.get('raw_state'),
            '质量': row.get('quality'), 'COT': row.get('cot'),
            '源时标': row.get('event_time_ms'), 'CP56原始': row.get('cp56_hex')}
            for row in events))

    def bind_points(self, adapter):
        self.point_map = {(kind.upper(), int(point.io_address)): no
                          for (kind, no), point in getattr(adapter, '_points', {}).items()}

    def packet(self, direction, data, detail=''):
        if 'IEC104' not in detail.upper():
            return
        try:
            frame = parse_apdu(data)
            common = {'方向': direction, '链路': detail,
                      '发送序号': frame.send_sequence, '接收序号': frame.receive_sequence}
            if frame.frame_type == 'U':
                self.event('链路控制', **common, 功能=frame.u_function)
            elif frame.frame_type == 'I':
                header, rows = decode_asdu(bytes(data)[6:])
                action = '总召' if header['类型标识'] == 100 else '数据报文'
                if header['类型标识'] in _TYPES:
                    kind = _TYPES[header['类型标识']][0]
                    action = '指令报文' if kind in {'yk', 'yt'} else header['原因']
                for row in rows:
                    key = (row.get('四遥'), row.get('IOA'))
                    if key in self.point_map:
                        row['点号'] = self.point_map[key]
                self.rows(action, rows, **common, **header)
        except Exception as exc:
            self.event('报文业务解析失败', 方向=direction, 错误=str(exc), 原始=bytes(data).hex(' '))

    def wrap_packet(self, sink):
        def observe(direction, data, detail=''):
            self.packet(direction, data, detail)
            return sink(direction, data, detail)
        observe.business = self
        return observe


_MODBUS_FUNCTIONS = {'read_coils': (1, '线圈'), 'read_discrete_inputs': (2, '离散输入'),
    'read_holding_registers': (3, '保持寄存器'), 'read_input_registers': (4, '输入寄存器'),
    'write_single_coil': (5, '线圈'), 'write_single_register': (6, '保持寄存器'),
    'write_multiple_coils': (15, '线圈'), 'write_multiple_registers': (16, '保持寄存器')}


def trace_modbus_operation(invoke):
    """Observe the adapter's validated operation, never parse TCP/RTU chunks.

    A local operation ID links requests/results; TCP TID is reported only after
    the library allocated it. Read words/bits are logged in bounded slices so
    the string safety limit cannot truncate a large coil response.
    """
    @wraps(invoke)
    def observed(self, name, address, argument, unit_id):
        if self._business_logger() is None:
            return invoke(self, name, address, argument, unit_id)
        # Same lock as transport: another call cannot replace the native TID.
        with self._lock:
            fc, area = _MODBUS_FUNCTIONS[name]
            reading = name.startswith('read_')
            size = argument if reading else len(argument) if isinstance(argument, (list, tuple)) else 1
            common = dict(操作编号=f'{os.getpid()}:{next(_batches)}', 规约=self.PACKET_PROTOCOL,
                          链路=getattr(self, '_packet_endpoint', ''), 站号=unit_id,
                          功能码=fc, 数据区=area, 起始地址0基=address, 数量=size)
            proposed = argument if isinstance(argument, (list, tuple)) else [argument]
            request_rows = None if reading else (
                {'地址0基': address+i, '待写原始值': list(proposed[i:i+64])}
                for i in range(0, len(proposed), 64))
            self._business('Modbus请求开始', rows=request_rows, **common)
            started = time.monotonic()
            try:
                result = invoke(self, name, address, argument, unit_id)
            except Exception as exc:
                self._business('Modbus请求失败', **common, 错误=str(exc),
                               异常码=getattr(exc, 'exception_code', None),
                               耗时毫秒=round((time.monotonic()-started)*1000, 3))
                raise
            if self.PACKET_PROTOCOL == 'Modbus-TCP':
                common['事务标识'] = getattr(self.native, '_transaction_id', None)
            values = result if reading else argument
            values = list(values) if isinstance(values, (list, tuple)) else [values]
            self._business('Modbus读取完成' if reading else 'Modbus写入收到正常响应',
                rows=({'地址0基': address+i, '原始值': values[i:i+64]} for i in range(0, len(values), 64)),
                **common, 耗时毫秒=round((time.monotonic()-started)*1000, 3),
                确认范围='协议响应，不代表设备物理动作完成')
            return result
    return observed

"""Strict ASDU codec. CP56 uses UTC calendar fields; CP24 needs an hour reference."""
from datetime import datetime, timezone, timedelta
import struct

from .types import (Information, Quality, Double, Type, Cot, IncomingMessage,
                    SingleInfo, DoubleInfo, StepInfo, NormalizedInfo, ScaledInfo,
                    ShortInfo, SingleCmd, DoubleCmd, NormalizedCmd, ScaledCmd, ShortCmd)

BASE = {2:1,4:3,6:5,10:9,12:11,14:13,21:9,
        30:1,31:3,32:5,34:9,35:11,36:13,58:45,59:46,61:48,62:49,63:50}
CP24 = frozenset({2,4,6,10,12,14})
CP56 = frozenset({30,31,32,34,35,36,58,59,61,62,63})
TIMED = CP24 | CP56
MONITOR = frozenset({1,2,3,4,5,6,9,10,11,12,13,14,21,30,31,32,34,35,36})
COMMAND = frozenset({45,46,48,49,50,58,59,61,62,63})
SIZES = {1:1,3:1,5:2,9:3,11:3,13:5,45:1,46:1,48:3,49:3,50:5,
         70:1,100:1,103:7,104:2,107:9}
INFO = {1:SingleInfo,3:DoubleInfo,5:StepInfo,9:NormalizedInfo,11:ScaledInfo,
        13:ShortInfo,45:SingleCmd,46:DoubleCmd,48:NormalizedCmd,49:ScaledCmd,50:ShortCmd}
_SIZE_BY_TYPE = {
    typ: (2 if typ == 21 else SIZES[BASE.get(typ, typ)]) + (3 if typ in CP24 else 7 if typ in CP56 else 0)
    for typ in (*SIZES, *BASE)
}
_STRUCT_HB = struct.Struct('<HB')
_STRUCT_h = struct.Struct('<h')
_STRUCT_f = struct.Struct('<f')
_STRUCT_fB = struct.Struct('<fB')
_STRUCT_hB = struct.Struct('<hB')


def size_of(typ):
    return _SIZE_BY_TYPE[int(typ)]


def encode_time(stamp=None, *, short=False):
    stamp = stamp or datetime.now(timezone.utc)
    # Naive values intentionally mean UTC calendar, matching retained SOE bytes.
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)
    if not 2000 <= stamp.year <= 2099:
        raise ValueError('IEC104 时间年份必须在 2000..2099')
    raw = _STRUCT_HB.pack(stamp.second*1000+stamp.microsecond//1000, stamp.minute)
    if short:
        return raw
    return raw+bytes((stamp.hour,stamp.day | stamp.isoweekday()<<5,stamp.month,stamp.year-2000))


def decode_time(data, *, reference=None):
    length = len(data)
    if length not in (3,7):
        raise ValueError('时间标签长度无效')
    ms = data[0] | (data[1] << 8)
    if ms >= 60000 or data[2]&63 >= 60:
        raise ValueError('时间标签分秒无效')
    if length == 7:
        return datetime(2000+(data[6]&127),data[5]&15,data[4]&31,data[3]&31,
                        data[2]&63,ms//1000,ms%1000*1000,tzinfo=timezone.utc)
    ref = reference or datetime.now(timezone.utc)
    ref = ref.replace(tzinfo=timezone.utc) if ref.tzinfo is None else ref.astimezone(timezone.utc)
    stamp = ref.replace(minute=data[2]&63,second=ms//1000,microsecond=ms%1000*1000)
    return min((stamp-timedelta(hours=1),stamp,stamp+timedelta(hours=1)),key=lambda x:abs((x-ref).total_seconds()))


def encode_information(typ, info, *, select=False):
    typ = int(typ); base = BASE.get(typ,typ)
    value = info.value
    quality = int(info.quality or 0)
    if base in (1,3):
        data = bytes(((int(value) if base == 3 else int(bool(value))) | (quality & 240),))
    elif base == 5:
        data = bytes(((int(value)&127) | (128 if info.transient else 0),quality))
    elif base in (9,11):
        n = int(round(float(value)*32768)) if base == 9 else int(value)
        n = min(n,32767) if base == 9 else n
        data = _STRUCT_h.pack(n) if typ == 21 else _STRUCT_hB.pack(n,quality)
    elif base == 13:
        data = _STRUCT_fB.pack(float(value),quality)
    elif base in (45,46):
        n = int(bool(value)) if base == 45 else int(value)
        data = bytes((n | int(info.qualifier)<<2 | (128 if select else 0),))
    elif base in (48,49,50):
        qualifier = int(info.qualifier) | (128 if select else 0)
        if base == 48:
            data = _STRUCT_hB.pack(min(32767,round(float(value)*32768)),qualifier)
        elif base == 49:
            data = _STRUCT_hB.pack(int(value),qualifier)
        else:
            data = _STRUCT_fB.pack(float(value),qualifier)
    else:
        raise ValueError(f'不支持编码类型 {typ}')
    if typ in TIMED:
        data += encode_time(info.recorded_at,short=typ in CP24)
    return data


def decode_information(typ, data, *, reference=None):
    typ = int(typ); base = BASE.get(typ,typ)
    if len(data) != _SIZE_BY_TYPE[typ]:
        raise ValueError('信息体长度无效')
    stamp, invalid = None, 0
    if typ in TIMED:
        time_data = data[-(3 if typ in CP24 else 7):]
        invalid = 128 if time_data[2]&128 else 0
        try:
            stamp = decode_time(time_data,reference=reference)
        except ValueError:
            invalid = 128  # Retain invalid-clock events in message.raw.
    if base in (1,3):
        return INFO[base](bool(data[0]&1) if base==1 else Double(data[0]&3),Quality((data[0]&240)|invalid),stamp)
    if base == 5:
        value = data[0]&127
        return StepInfo(value-128 if value&64 else value,bool(data[0]&128),Quality(data[1]|invalid),stamp)
    if base in (9,11,13):
        value = (_STRUCT_f if base==13 else _STRUCT_h).unpack_from(data)[0]
        if base==9: value /= 32768
        q = 0 if typ==21 else data[4 if base==13 else 2]
        return INFO[base](value,Quality(q|invalid),stamp)
    if base in (45,46):
        return INFO[base](bool(data[0]&1) if base==45 else Double(data[0]&3),(data[0]>>2)&31,stamp)
    if base in (48,49,50):
        value = (_STRUCT_f if base==50 else _STRUCT_h).unpack_from(data)[0]
        if base==48: value /= 32768
        return INFO[base](value,data[4 if base==50 else 2]&127,stamp)
    return Information(bytes(data),None,stamp)


def header(typ, count, cause, ca, oa=0, *, sequence=False):
    if not 0 <= int(oa) <= 255 or not 1 <= int(ca) <= 65535 or not 1 <= count <= 127:
        raise ValueError('ASDU 地址或数量无效')
    return bytes((int(typ),count | (128 if sequence else 0),int(cause),int(oa)))+int(ca).to_bytes(2,'little')


def decode_asdu(asdu, raw=b''):
    if len(asdu)<6 or not asdu[1]&127:
        raise ValueError('ASDU 头或信息体数量无效')
    typ,vsq,cot,oa=asdu[:4]
    try: size=size_of(typ)
    except KeyError: raise ValueError(f'未知 ASDU 类型 {typ}') from None
    count,sequence=vsq&127,bool(vsq&128)
    if len(asdu)!=6+(3 if sequence else 3*count)+size*count:
        raise ValueError('ASDU 信息体总长度无效')
    msg=IncomingMessage(raw,Type(typ),Cot(cot&63) if (cot&63) in Cot._value2member_map_ else cot&63,
                        int.from_bytes(asdu[4:6],'little'),oa,is_negative=bool(cot&64),is_test=bool(cot&128))
    rows=[]; offset=6; address=0
    for index in range(count):
        if not sequence or index==0:
            address=int.from_bytes(asdu[offset:offset+3],'little');offset+=3
        ioa=address+index if sequence else address
        if ioa>0xffffff: raise ValueError('IOA 溢出')
        data=asdu[offset:offset+size];offset+=size
        info=decode_information(typ,data)
        rows.append((ioa,info,data))
    msg.io_address=rows[0][0]
    if typ in COMMAND:
        msg.is_select_command=bool(rows[0][2][SIZES[BASE.get(typ,typ)]-1]&128)
    return msg,rows


def pack_points(points, cause, limit=249, oa=0):
    """Snapshot values before queuing; split by CA/type and configured ASDU limit."""
    batches=[]; group=[]; previous=None; size=6
    for point in points:
        key=(point.station.common_address,int(point.type))
        data=point.io_address.to_bytes(3,'little')+encode_information(point.type,point.info)
        if 6+len(data)>limit: raise ValueError('ASDU 上限不能容纳一个信息体')
        if group and (key!=previous or size+len(data)>limit or len(group)==127):
            batches.append(header(previous[1],len(group),cause,previous[0],oa)+b''.join(group))
            group=[];size=6
        previous=key;group.append(data);size+=len(data)
    if group:
        batches.append(header(previous[1],len(group),cause,previous[0],oa)+b''.join(group))
    return batches

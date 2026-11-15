"""IEC104 values used by the project adapters (no native extension)."""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, IntEnum, IntFlag
import math
import struct


Type = IntEnum('Type', {
    'M_SP_NA_1':1, 'M_SP_TA_1':2, 'M_DP_NA_1':3, 'M_DP_TA_1':4,
    'M_ST_NA_1':5, 'M_ST_TA_1':6, 'M_ME_NA_1':9, 'M_ME_TA_1':10,
    'M_ME_NB_1':11, 'M_ME_TB_1':12, 'M_ME_NC_1':13, 'M_ME_TC_1':14,
    'M_ME_ND_1':21, 'M_SP_TB_1':30, 'M_DP_TB_1':31, 'M_ST_TB_1':32,
    'M_ME_TD_1':34, 'M_ME_TE_1':35, 'M_ME_TF_1':36, 'M_EI_NA_1':70,
    'C_SC_NA_1':45, 'C_DC_NA_1':46, 'C_SE_NA_1':48, 'C_SE_NB_1':49,
    'C_SE_NC_1':50, 'C_SC_TA_1':58, 'C_DC_TA_1':59, 'C_SE_TA_1':61,
    'C_SE_TB_1':62, 'C_SE_TC_1':63, 'C_IC_NA_1':100, 'C_CS_NA_1':103,
    'C_TS_NA_1':104, 'C_TS_TA_1':107,
})
Cot = IntEnum('Cot', {'PERIODIC':1, 'BACKGROUND_SCAN':2, 'SPONTANEOUS':3,
    'INITIALIZED':4, 'REQUEST':5, 'ACTIVATION':6, 'ACTIVATION_CON':7,
    'DEACTIVATION':8, 'DEACTIVATION_CON':9, 'ACTIVATION_TERMINATION':10,
    'INTERROGATED_BY_STATION':20, **{f'INTERROGATED_BY_GROUP_{n}':20+n for n in range(1,17)},
    'UNKNOWN_TYPE_ID':44, 'UNKNOWN_COT':45, 'UNKNOWN_CA':46, 'UNKNOWN_IOA':47})
Qoi = IntEnum('Qoi', {'STATION':20, **{f'GROUP_{n}':20+n for n in range(1,17)}})
Coi = IntEnum('Coi', {'LOCAL_POWER_ON':0, 'LOCAL_MANUAL_RESET':1, 'REMOTE_RESET':2})
Qoc = IntEnum('Qoc', {'NONE':0, 'SHORT_PULSE':1, 'LONG_PULSE':2, 'PERSISTENT':3})


class Quality(IntFlag):
    Good = 0
    Overflow = 1
    Blocked = 16
    Substituted = 32
    NonTopical = 64
    Invalid = 128


class Double(Enum):
    INTERMEDIATE = 0
    OFF = 1
    ON = 2
    INDETERMINATE = 3

    def __int__(self):
        return self.value


class CommandMode(Enum):
    DIRECT = 0
    SELECT_AND_EXECUTE = 1


class ResponseState(Enum):
    SUCCESS = 1
    FAILURE = 2
    NONE = 3


class Init(Enum):
    NONE = 0
    ALL = 1
    MUTED = 2
    INTERROGATION_ONLY = 3
    CLOCK_SYNC_ONLY = 4


class ConnectionState(Enum):
    CLOSED = 0
    CLOSED_AWAIT_OPEN = 1
    OPEN_MUTED = 2
    OPEN = 3


class Umc(Enum):
    UNKNOWN_TYPE_ID = 44
    INVALID_COT = 45
    UNKNOWN_CA = 46
    UNKNOWN_IOA = 47
    MISMATCHED_TYPE_ID = 48
    INVALID_SEQUENCE = 49


class _Integer(int):
    minimum, maximum = 0, 65535

    def __new__(cls, value=0):
        number = int(value)
        if not cls.minimum <= number <= cls.maximum:
            raise ValueError(f'{cls.__name__} 超出范围: {value}')
        return int.__new__(cls, number)

    @property
    def value(self):
        return int(self)


class Int16(_Integer):
    minimum, maximum = -32768, 32767


class UInt16(_Integer):
    pass


class UInt5(_Integer):
    maximum = 31


class UInt7(_Integer):
    maximum = 127


class Int7(_Integer):
    minimum, maximum = -64, 63


class NormalizedFloat(float):
    def __new__(cls, value=0):
        number = float(value)
        if not math.isfinite(number) or not -1 <= number <= 1:
            raise ValueError('归一化值必须在 -1..1')
        # The representable positive maximum is 32767/32768.
        return float.__new__(cls, min(32767, round(number*32768))/32768)


@dataclass(frozen=True)
class Information:
    value: object
    quality: Quality | None = Quality.Good
    recorded_at: datetime | None = None
    qualifier: int = 0
    transient: bool = False


class SingleInfo(Information):
    def __init__(self, on=False, quality=Quality.Good, recorded_at=None):
        super().__init__(bool(on), Quality(quality), recorded_at)


class DoubleInfo(Information):
    def __init__(self, state=Double.INTERMEDIATE, quality=Quality.Good, recorded_at=None):
        super().__init__(Double(state), Quality(quality), recorded_at)


class StepInfo(Information):
    def __init__(self, position=0, transient=False, quality=Quality.Good, recorded_at=None):
        super().__init__(Int7(position), Quality(quality), recorded_at, transient=bool(transient))


class NormalizedInfo(Information):
    def __init__(self, actual=0, quality=Quality.Good, recorded_at=None):
        super().__init__(NormalizedFloat(actual), Quality(quality), recorded_at)


class ScaledInfo(Information):
    def __init__(self, actual=0, quality=Quality.Good, recorded_at=None):
        super().__init__(Int16(actual), Quality(quality), recorded_at)


def float32(value):
    return struct.unpack('<f', struct.pack('<f', float(value)))[0]


class ShortInfo(Information):
    def __init__(self, actual=0, quality=Quality.Good, recorded_at=None):
        super().__init__(float32(actual), Quality(quality), recorded_at)


class SingleCmd(Information):
    def __init__(self, on=False, qualifier=0, recorded_at=None):
        super().__init__(bool(on), None, recorded_at, int(UInt5(qualifier)))


class DoubleCmd(Information):
    def __init__(self, state=Double.OFF, qualifier=0, recorded_at=None):
        super().__init__(Double(state), None, recorded_at, int(UInt5(qualifier)))


class NormalizedCmd(Information):
    def __init__(self, target=0, qualifier=0, recorded_at=None):
        super().__init__(NormalizedFloat(target), None, recorded_at, int(UInt7(qualifier)))


class ScaledCmd(Information):
    def __init__(self, target=0, qualifier=0, recorded_at=None):
        super().__init__(Int16(target), None, recorded_at, int(UInt7(qualifier)))


class ShortCmd(Information):
    def __init__(self, target=0, qualifier=0, recorded_at=None):
        super().__init__(float32(target), None, recorded_at, int(UInt7(qualifier)))


@dataclass
class ProtocolParameters:
    send_window_size: int = 12
    receive_window_size: int = 8
    message_timeout: float = 15
    confirm_interval: float = 10
    keep_alive_interval: float = 20
    max_asdu_length: int = 249


@dataclass
class IncomingMessage:
    raw: bytes
    type: object
    cot: object
    common_address: int
    originator_address: int
    io_address: int = 0
    is_select_command: bool = False
    is_negative: bool = False
    is_test: bool = False


@dataclass
class Batch:
    cause: object
    points: list

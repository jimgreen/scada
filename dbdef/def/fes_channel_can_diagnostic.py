# -*- coding: utf-8 -*-
"""Generated dataclass definition from dbdef/json/fes_channel_can_diagnostic.json."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional

@dataclass
class fes_channel_can_diagnostic:
    """CAN运行诊断快照表。"""

    __tablename__: ClassVar[str] = 'fes_channel_can_diagnostic'
    __table_name_chn__: ClassVar[str] = 'CAN运行诊断快照表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('channel_id',)

    channel_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属通道', 'eng_name': 'channel_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )

    bus_state: Optional[str] = field(
        default='unknown',
        metadata={'chn_name': '总线状态', 'eng_name': 'bus_state', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': 'CAN总线状态', 'allowed_values': ['unknown', 'error_active', 'error_passive', 'bus_off'], 'default': 'unknown'},
    )

    error_state: Optional[str] = field(
        default='unknown',
        metadata={'chn_name': '错误状态', 'eng_name': 'error_state', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': 'unknown'},
    )

    tx_error_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '发送错误计数', 'eng_name': 'tx_error_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    rx_error_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '接收错误计数', 'eng_name': 'rx_error_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    error_frame_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '错误帧数', 'eng_name': 'error_frame_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    ack_error_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': 'ACK错误数', 'eng_name': 'ack_error_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    crc_error_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': 'CRC错误数', 'eng_name': 'crc_error_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    stuff_error_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '位填充错误数', 'eng_name': 'stuff_error_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    bit_error_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '位错误数', 'eng_name': 'bit_error_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    form_error_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '格式错误数', 'eng_name': 'form_error_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    bus_load_pct: Optional[float] = field(
        default=0.0,
        metadata={'chn_name': '总线负载率（百分比）', 'eng_name': 'bus_load_pct', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': '', 'default': 0.0},
    )

    avg_latency_ms: Optional[float] = field(
        default=0.0,
        metadata={'chn_name': '平均报文延迟（毫秒）', 'eng_name': 'avg_latency_ms', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': '', 'default': 0.0},
    )

    max_latency_ms: Optional[float] = field(
        default=0.0,
        metadata={'chn_name': '最大报文延迟（毫秒）', 'eng_name': 'max_latency_ms', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': '', 'default': 0.0},
    )

    dropped_frame_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '丢帧数', 'eng_name': 'dropped_frame_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    received_frame_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '接收报文数', 'eng_name': 'received_frame_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    transmitted_frame_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '发送报文数', 'eng_name': 'transmitted_frame_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    auto_recovery_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '自动恢复次数', 'eng_name': 'auto_recovery_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    temperature_c: Optional[float] = field(
        default=None,
        metadata={'chn_name': '采集温度（摄氏度）', 'eng_name': 'temperature_c', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    emi_level: Optional[float] = field(
        default=None,
        metadata={'chn_name': '电磁干扰指标', 'eng_name': 'emi_level', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    termination_ohms: Optional[float] = field(
        default=None,
        metadata={'chn_name': '终端电阻（欧姆）', 'eng_name': 'termination_ohms', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    waveform_ref: Optional[str] = field(
        default=None,
        metadata={'chn_name': '物理层波形引用', 'eng_name': 'waveform_ref', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    last_error: Optional[str] = field(
        default='',
        metadata={'chn_name': '最后错误', 'eng_name': 'last_error', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''},
    )

    last_error_time: Optional[int] = field(
        default=0,
        metadata={'chn_name': '最后错误时间', 'eng_name': 'last_error_time', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    sampled_at: Optional[int] = field(
        default=0,
        metadata={'chn_name': '采样时间', 'eng_name': 'sampled_at', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

__all__ = ['fes_channel_can_diagnostic']

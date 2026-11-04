# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_channel_can_point_define
Chinese name: CAN四遥报文解析表
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_can_point_define:
    """CAN四遥报文解析表。"""

    __tablename__: ClassVar[str] = 'fes_channel_can_point_define'
    __table_name_chn__: ClassVar[str] = 'CAN四遥报文解析表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('rtu_id', 'kind', 'pnt_no')

    rtu_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属RTU', 'eng_name': 'rtu_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )

    kind: Optional[str] = field(
        default=None,
        metadata={'chn_name': '点类型', 'eng_name': 'kind', 'primary_key': True, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    pnt_no: Optional[int] = field(
        default=None,
        metadata={'chn_name': '点号', 'eng_name': 'pnt_no', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    frame_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '接收帧ID', 'eng_name': 'frame_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': ''},
    )

    tx_frame_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '发送帧ID', 'eng_name': 'tx_frame_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': ''},
    )

    response_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '应答帧ID', 'eng_name': 'response_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': ''},
    )

    is_extended_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '扩展帧', 'eng_name': 'is_extended_id', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

    is_fd: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'CAN-FD', 'eng_name': 'is_fd', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

    byte_offset: Optional[int] = field(
        default=None,
        metadata={'chn_name': '字节偏移', 'eng_name': 'byte_offset', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    bit_offset: Optional[int] = field(
        default=None,
        metadata={'chn_name': '位偏移', 'eng_name': 'bit_offset', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    bit_length: Optional[int] = field(
        default=None,
        metadata={'chn_name': '位长度', 'eng_name': 'bit_length', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    value_type: Optional[str] = field(
        default=None,
        metadata={'chn_name': '值类型', 'eng_name': 'value_type', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    byte_order: Optional[str] = field(
        default='big',
        metadata={'chn_name': '字节序', 'eng_name': 'byte_order', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': 'big'},
    )

    scale: Optional[float] = field(
        default=1.0,
        metadata={'chn_name': '乘系数', 'eng_name': 'scale', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': '', 'default': 1.0},
    )

    offset: Optional[float] = field(
        default=0.0,
        metadata={'chn_name': '加偏移', 'eng_name': 'offset', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': '', 'default': 0.0},
    )

    quality_byte_offset: Optional[int] = field(
        default=None,
        metadata={'chn_name': '质量字节偏移', 'eng_name': 'quality_byte_offset', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    quality_mask: Optional[int] = field(
        default=None,
        metadata={'chn_name': '质量码掩码', 'eng_name': 'quality_mask', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    tx_data_hex: Optional[str] = field(
        default=None,
        metadata={'chn_name': '发送报文模板HEX', 'eng_name': 'tx_data_hex', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    response_data_hex: Optional[str] = field(
        default=None,
        metadata={'chn_name': '应答匹配HEX', 'eng_name': 'response_data_hex', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

__all__ = ['fes_channel_can_point_define']

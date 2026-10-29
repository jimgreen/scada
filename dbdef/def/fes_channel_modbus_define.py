# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_channel_modbus_define
Chinese name: MODBUS通道定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_modbus_define:
    """MODBUS通道定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_modbus_define'
    __table_name_chn__: ClassVar[str] = 'MODBUS通道定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('id',)

    id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '标识', 'eng_name': 'id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': ''},
    )

    code: Optional[str] = field(
        default=None,
        metadata={'chn_name': '英文标识', 'eng_name': 'code', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    name: Optional[str] = field(
        default=None,
        metadata={'chn_name': '中文名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    rdfid: Optional[str] = field(
        default=None,
        metadata={'chn_name': 'RDFID', 'eng_name': 'rdfid', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    rtu_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属RTU', 'eng_name': 'rtu_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    irtuaddr: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'modbus通道的子站地址', 'eng_name': 'irtuaddr', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    iplace: Optional[int] = field(
        default=None,
        metadata={'chn_name': '每帧召唤的起始地址', 'eng_name': 'iplace', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    inum: Optional[int] = field(
        default=None,
        metadata={'chn_name': '每帧召唤的寄存器个数', 'eng_name': 'inum', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    icode: Optional[int] = field(
        default=None,
        metadata={'chn_name': '每帧召唤的功能码', 'eng_name': 'icode', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    ioffset: Optional[int] = field(
        default=None,
        metadata={'chn_name': '以字节为单位的偏移地址', 'eng_name': 'ioffset', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    ibitnum: Optional[int] = field(
        default=None,
        metadata={'chn_name': '一个寄存器占用的字节个数', 'eng_name': 'ibitnum', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

__all__ = ['fes_channel_modbus_define']

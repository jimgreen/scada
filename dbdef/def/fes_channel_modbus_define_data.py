# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_channel_modbus_define_data
Chinese name: MODBUS数据定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_modbus_define_data:
    """MODBUS数据定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_modbus_define_data'
    __table_name_chn__: ClassVar[str] = 'MODBUS数据定义表'
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

    rtu_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属RTU', 'eng_name': 'rtu_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    call_seq: Optional[int] = field(
        default=None,
        metadata={'chn_name': '召唤序号', 'eng_name': 'call_seq', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    if_yx: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否遥信', 'eng_name': 'if_yx', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

    cnt: Optional[int] = field(
        default=None,
        metadata={'chn_name': '点号', 'eng_name': 'cnt', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    trans_type: Optional[int] = field(
        default=None,
        metadata={'chn_name': '解释方式', 'eng_name': 'trans_type', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': 'MODBUS数据解析类型'},
    )

    reg_addr: Optional[int] = field(
        default=None,
        metadata={'chn_name': '寄存器地址', 'eng_name': 'reg_addr', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    offset_bit: Optional[int] = field(
        default=None,
        metadata={'chn_name': '偏移位数', 'eng_name': 'offset_bit', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    all_bit: Optional[int] = field(
        default=None,
        metadata={'chn_name': '占用总位数', 'eng_name': 'all_bit', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    if_reverse: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥信是否取反', 'eng_name': 'if_reverse', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

    twobytedata_byteseq: Optional[int] = field(
        default=None,
        metadata={'chn_name': '两字节数据字节顺序', 'eng_name': 'twobytedata_byteseq', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

    fourbytedata_byteseq: Optional[int] = field(
        default=None,
        metadata={'chn_name': '四字节数据字节顺序', 'eng_name': 'fourbytedata_byteseq', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

__all__ = ['fes_channel_modbus_define_data']

# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_channel_modbus_define_ytyk
Chinese name: MODBUS遥调遥控定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_modbus_define_ytyk:
    """MODBUS遥调遥控定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_modbus_define_ytyk'
    __table_name_chn__: ClassVar[str] = 'MODBUS遥调遥控定义表'
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

    ifyk: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否遥控', 'eng_name': 'ifyk', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

    rtu_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属RTU', 'eng_name': 'rtu_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    cnt: Optional[int] = field(
        default=None,
        metadata={'chn_name': '点号', 'eng_name': 'cnt', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    irtuaddr: Optional[int] = field(
        default=None,
        metadata={'chn_name': '子站地址', 'eng_name': 'irtuaddr', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    reg_addr: Optional[int] = field(
        default=None,
        metadata={'chn_name': '寄存器地址', 'eng_name': 'reg_addr', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    icode: Optional[int] = field(
        default=None,
        metadata={'chn_name': '功能码', 'eng_name': 'icode', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    ytyk_type: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调遥控数据类型', 'eng_name': 'ytyk_type', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': 'MODBUS遥调遥控类型'},
    )

    ifreverse: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥信是否取反', 'eng_name': 'ifreverse', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

    twobytedata_byteseq: Optional[int] = field(
        default=None,
        metadata={'chn_name': '两字节数据字节顺序', 'eng_name': 'twobytedata_byteseq', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

    fourbytedata_byteseq: Optional[int] = field(
        default=None,
        metadata={'chn_name': '四字节数据字节顺序', 'eng_name': 'fourbytedata_byteseq', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

    multi_para: Optional[float] = field(
        default=None,
        metadata={'chn_name': '乘系数', 'eng_name': 'multi_para', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    special_yk_yccnt: Optional[int] = field(
        default=None,
        metadata={'chn_name': '特殊遥控取遥测号', 'eng_name': 'special_yk_yccnt', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    special_yk_bit: Optional[int] = field(
        default=None,
        metadata={'chn_name': '特殊遥控第几位', 'eng_name': 'special_yk_bit', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

    send_ff_replace_01: Optional[int] = field(
        default=None,
        metadata={'chn_name': '下发0xFF替换0x01', 'eng_name': 'send_ff_replace_01', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

__all__ = ['fes_channel_modbus_define_ytyk']

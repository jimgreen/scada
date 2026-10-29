# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_channel_iec104_define
Chinese name: IEC104通道定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_iec104_define:
    """IEC104通道定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_iec104_define'
    __table_name_chn__: ClassVar[str] = 'IEC104通道定义表'
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

    rtu_addr: Optional[int] = field(
        default=None,
        metadata={'chn_name': '主站地址', 'eng_name': 'rtu_addr', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    ycbegin_addr: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥测起始地址', 'eng_name': 'ycbegin_addr', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    yxbegin_addr: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥信起始地址', 'eng_name': 'yxbegin_addr', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    ykbegin_addr: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控起始地址', 'eng_name': 'ykbegin_addr', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    ytbegin_addr: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调起始地址', 'eng_name': 'ytbegin_addr', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    yx_data_type: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥信数据类型', 'eng_name': 'yx_data_type', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': 'iec104遥信类型'},
    )

    yk_excute_type: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控执行类型', 'eng_name': 'yk_excute_type', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': 'iec104命令类型'},
    )

    yc_data_type: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥测数据类型', 'eng_name': 'yc_data_type', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': 'iec104遥测类型'},
    )

    yt_excute_type: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调执行类型', 'eng_name': 'yt_excute_type', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': 'iec104命令类型'},
    )

    call_alldata_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '总召唤时间（秒）', 'eng_name': 'call_alldata_time', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    if_autosend_alldata: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否自动发送全数据', 'eng_name': 'if_autosend_alldata', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

__all__ = ['fes_channel_iec104_define']

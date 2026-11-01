# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_yx_define
Chinese name: 遥信定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_yx_define:
    """遥信定义表。"""

    __tablename__: ClassVar[str] = 'fes_yx_define'
    __table_name_chn__: ClassVar[str] = '遥信定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('rtu_id', 'pnt_no',)

    rtu_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属RTU', 'eng_name': 'rtu_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )

    pnt_no: Optional[int] = field(
        default=None,
        metadata={'chn_name': '点号', 'eng_name': 'pnt_no', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
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

    name_0: Optional[str] = field(
        default=None,
        metadata={'chn_name': '0值描述', 'eng_name': 'name_0', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    name_1: Optional[str] = field(
        default=None,
        metadata={'chn_name': '1值描述', 'eng_name': 'name_1', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    yk_pnt: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控点号', 'eng_name': 'yk_pnt', 'primary_key': False, 'data_type': 'int', 'disp_type': 'SHORT', 'disp_menu': ''},
    )

    yx_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥信ID', 'eng_name': 'yx_id', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    iflock: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否闭锁', 'eng_name': 'iflock', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

    ifreverse: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否取反', 'eng_name': 'ifreverse', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

    ifalarm: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否变位报警', 'eng_name': 'ifalarm', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '遥信越限报警类型'},
    )

    locked_value: Optional[int] = field(
        default=None,
        metadata={'chn_name': '闭锁值', 'eng_name': 'locked_value', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

    value: Optional[int] = field(
        default=None,
        metadata={'chn_name': '值', 'eng_name': 'value', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

    status: Optional[int] = field(
        default=None,
        metadata={'chn_name': '状态', 'eng_name': 'status', 'primary_key': False, 'data_type': 'int', 'disp_type': '多选菜单', 'disp_menu': '遥信状态'},
    )

    ifdata_curb: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否数据抑制', 'eng_name': 'ifdata_curb', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    refresh_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '刷新时间', 'eng_name': 'refresh_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    lock_begintime: Optional[int] = field(
        default=None,
        metadata={'chn_name': '闭锁开始时间', 'eng_name': 'lock_begintime', 'primary_key': False, 'data_type': 'int', 'disp_type': 'DATETIME', 'disp_menu': ''},
    )

    lock_holdtime: Optional[int] = field(
        default=None,
        metadata={'chn_name': '闭锁持续时间', 'eng_name': 'lock_holdtime', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    pdr_type: Optional[int] = field(
        default=None,
        metadata={'chn_name': '事故追忆类型', 'eng_name': 'pdr_type', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    yx_id_1: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥信ID_1', 'eng_name': 'yx_id_1', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    yx_id_2: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥信ID_2', 'eng_name': 'yx_id_2', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    yx_id_3: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥信ID_3', 'eng_name': 'yx_id_3', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    yk_rtu: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控通道号', 'eng_name': 'yk_rtu', 'primary_key': False, 'data_type': 'int', 'disp_type': 'SHORT', 'disp_menu': ''},
    )

    if_serious_alm: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否严重告警', 'eng_name': 'if_serious_alm', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

    dev_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '设备ID', 'eng_name': 'dev_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )

__all__ = ['fes_yx_define']

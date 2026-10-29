# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_yk_define
Chinese name: 遥控定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_yk_define:
    """遥控定义表。"""

    __tablename__: ClassVar[str] = 'fes_yk_define'
    __table_name_chn__: ClassVar[str] = '遥控定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('rtu_id', 'pnt_no')

    rtu_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属RTU', 'eng_name': 'rtu_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )

    pnt_no: Optional[int] = field(
        default=None,
        metadata={'chn_name': '点号', 'eng_name': 'pnt_no', 'primary_key': True, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': ''},
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
        metadata={'chn_name': '0值名称', 'eng_name': 'name_0', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    name_1: Optional[str] = field(
        default=None,
        metadata={'chn_name': '1值名称', 'eng_name': 'name_1', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    max_wait_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '指令最长等待时间', 'eng_name': 'max_wait_time', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    icode: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控功能码', 'eng_name': 'icode', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    yk_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控ID', 'eng_name': 'yk_id', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    yk_dir: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控方向', 'eng_name': 'yk_dir', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    value: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控值', 'eng_name': 'value', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

    status: Optional[int] = field(
        default=None,
        metadata={'chn_name': '状态', 'eng_name': 'status', 'primary_key': False, 'data_type': 'int', 'disp_type': '多选菜单', 'disp_menu': '遥信状态'},
    )

    refresh_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控时间0', 'eng_name': 'refresh_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    man_disable: Optional[int] = field(
        default=None,
        metadata={'chn_name': '人工禁止遥控', 'eng_name': 'man_disable', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    man_disable_tm: Optional[int] = field(
        default=None,
        metadata={'chn_name': '人工禁止遥控时刻', 'eng_name': 'man_disable_tm', 'primary_key': False, 'data_type': 'int', 'disp_type': 'DATETIME', 'disp_menu': ''},
    )

    auto_disable: Optional[int] = field(
        default=None,
        metadata={'chn_name': '自动禁止遥控', 'eng_name': 'auto_disable', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    auto_disable_tm: Optional[int] = field(
        default=None,
        metadata={'chn_name': '自动禁止遥控时刻', 'eng_name': 'auto_disable_tm', 'primary_key': False, 'data_type': 'int', 'disp_type': 'DATETIME', 'disp_menu': ''},
    )

    man_disable_last_tm: Optional[int] = field(
        default=None,
        metadata={'chn_name': '人工禁止遥控闭锁时间(秒)', 'eng_name': 'man_disable_last_tm', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    refresh_time1: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控时间1', 'eng_name': 'refresh_time1', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    refresh_time2: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控时间2', 'eng_name': 'refresh_time2', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    refresh_time3: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控时间3', 'eng_name': 'refresh_time3', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    cmd_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '本地下发命令时间', 'eng_name': 'cmd_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    cmd_handle_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '本地下发命令时间', 'eng_name': 'cmd_handle_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    cmd_check_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '五防定义表ID', 'eng_name': 'cmd_check_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': ''},
    )

__all__ = ['fes_yk_define']

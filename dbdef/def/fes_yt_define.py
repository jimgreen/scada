# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_yt_define
Chinese name: 遥调定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_yt_define:
    """遥调定义表。"""

    __tablename__: ClassVar[str] = 'fes_yt_define'
    __table_name_chn__: ClassVar[str] = '遥调定义表'
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

    raw_base: Optional[float] = field(
        default=None,
        metadata={'chn_name': '生数据基值', 'eng_name': 'raw_base', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    raw_scale: Optional[float] = field(
        default=None,
        metadata={'chn_name': '生数据比例', 'eng_name': 'raw_scale', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    max_wait_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '指令最长等待时间', 'eng_name': 'max_wait_time', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    icode: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调功能码', 'eng_name': 'icode', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    yt_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调ID', 'eng_name': 'yt_id', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    is_step: Optional[float] = field(
        default=None,
        metadata={'chn_name': '是否离散调节', 'eng_name': 'is_step', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    step: Optional[float] = field(
        default=None,
        metadata={'chn_name': '离散调节步长', 'eng_name': 'step', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    yt_type: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调类型', 'eng_name': 'yt_type', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    raw_value: Optional[float] = field(
        default=None,
        metadata={'chn_name': '生数据遥调值', 'eng_name': 'raw_value', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    raw_status: Optional[int] = field(
        default=None,
        metadata={'chn_name': '生数据状态', 'eng_name': 'raw_status', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    raw_max: Optional[float] = field(
        default=None,
        metadata={'chn_name': '生数据最大值', 'eng_name': 'raw_max', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    raw_min: Optional[float] = field(
        default=None,
        metadata={'chn_name': '生数据最小值', 'eng_name': 'raw_min', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    raw_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '生数据下发时刻', 'eng_name': 'raw_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    value: Optional[float] = field(
        default=None,
        metadata={'chn_name': '遥调值', 'eng_name': 'value', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    status: Optional[int] = field(
        default=None,
        metadata={'chn_name': '状态', 'eng_name': 'status', 'primary_key': False, 'data_type': 'int', 'disp_type': '多选菜单', 'disp_menu': '遥测状态'},
    )

    eng_max: Optional[float] = field(
        default=None,
        metadata={'chn_name': '工程最大值', 'eng_name': 'eng_max', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    eng_min: Optional[float] = field(
        default=None,
        metadata={'chn_name': '工程最小值', 'eng_name': 'eng_min', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    refresh_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调时间0', 'eng_name': 'refresh_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    man_disable: Optional[int] = field(
        default=None,
        metadata={'chn_name': '人工禁止遥调', 'eng_name': 'man_disable', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    man_disable_tm: Optional[int] = field(
        default=None,
        metadata={'chn_name': '人工禁止遥调时刻', 'eng_name': 'man_disable_tm', 'primary_key': False, 'data_type': 'int', 'disp_type': 'DATETIME', 'disp_menu': ''},
    )

    auto_disable: Optional[int] = field(
        default=None,
        metadata={'chn_name': '自动禁止遥调', 'eng_name': 'auto_disable', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    auto_disable_tm: Optional[int] = field(
        default=None,
        metadata={'chn_name': '自动禁止遥调更新时间', 'eng_name': 'auto_disable_tm', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    man_disable_last_tm: Optional[int] = field(
        default=None,
        metadata={'chn_name': '人工禁止遥调持续时间（秒）', 'eng_name': 'man_disable_last_tm', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    refresh_time1: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调时间1', 'eng_name': 'refresh_time1', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    refresh_time2: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调时间2', 'eng_name': 'refresh_time2', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    refresh_time3: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调时间3', 'eng_name': 'refresh_time3', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    cmd_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '本地下发命令时间', 'eng_name': 'cmd_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    cmd_handle_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '本地下发命令处理时间', 'eng_name': 'cmd_handle_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    cmd_check_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '五防定义表ID', 'eng_name': 'cmd_check_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': ''},
    )

__all__ = ['fes_yt_define']

# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_ytyk_message
Chinese name: 遥调遥控下发信息表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_ytyk_message:
    """遥调遥控下发信息表。"""

    __tablename__: ClassVar[str] = 'fes_ytyk_message'
    __table_name_chn__: ClassVar[str] = '遥调遥控下发信息表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('cmd_time', 'is_yk', 'rtu_id', 'pnt_no')

    cmd_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '本地下发命令时间', 'eng_name': 'cmd_time', 'primary_key': True, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    is_yk: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥控/遥调', 'eng_name': 'is_yk', 'primary_key': True, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

    rtu_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属RTU', 'eng_name': 'rtu_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )

    pnt_no: Optional[int] = field(
        default=None,
        metadata={'chn_name': '点号', 'eng_name': 'pnt_no', 'primary_key': True, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': ''},
    )

    name: Optional[str] = field(
        default=None,
        metadata={'chn_name': '遥控/遥调名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    value: Optional[float] = field(
        default=None,
        metadata={'chn_name': '遥调/遥控值', 'eng_name': 'value', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    source: Optional[int] = field(
        default=None,
        metadata={'chn_name': '来源', 'eng_name': 'source', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '遥调遥控来源'},
    )

    logs: Optional[str] = field(
        default=None,
        metadata={'chn_name': '日志', 'eng_name': 'logs', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

__all__ = ['fes_ytyk_message']

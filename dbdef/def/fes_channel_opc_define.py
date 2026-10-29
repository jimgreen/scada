# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_channel_opc_define
Chinese name: OPC通道定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_opc_define:
    """OPC通道定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_opc_define'
    __table_name_chn__: ClassVar[str] = 'OPC通道定义表'
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
        metadata={'chn_name': '所属通道', 'eng_name': 'rtu_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    opc_interval: Optional[int] = field(
        default=None,
        metadata={'chn_name': '采集刷新间隔(MS)', 'eng_name': 'opc_interval', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    opc_user: Optional[str] = field(
        default=None,
        metadata={'chn_name': '登录用户名', 'eng_name': 'opc_user', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    opc_pwd: Optional[str] = field(
        default=None,
        metadata={'chn_name': '登录密码', 'eng_name': 'opc_pwd', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    opc_domain: Optional[str] = field(
        default=None,
        metadata={'chn_name': '登录域', 'eng_name': 'opc_domain', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    opc_name: Optional[str] = field(
        default=None,
        metadata={'chn_name': 'OPC实例名', 'eng_name': 'opc_name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

__all__ = ['fes_channel_opc_define']

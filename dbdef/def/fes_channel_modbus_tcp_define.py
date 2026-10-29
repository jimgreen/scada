# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_channel_modbus_tcp_define
Chinese name: MODBUS-TCP通道参数定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_modbus_tcp_define:
    """MODBUS-TCP通道参数定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_modbus_tcp_define'
    __table_name_chn__: ClassVar[str] = 'MODBUS-TCP通道参数定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('rtu_id',)

    rtu_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属RTU', 'eng_name': 'rtu_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
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

    remote_host: Optional[str] = field(
        default=None,
        metadata={'chn_name': '远端主机', 'eng_name': 'remote_host', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    remote_port: Optional[int] = field(
        default=None,
        metadata={'chn_name': '远端端口', 'eng_name': 'remote_port', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    unit_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '默认从站地址', 'eng_name': 'unit_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    connect_timeout: Optional[float] = field(
        default=None,
        metadata={'chn_name': '连接超时（秒）', 'eng_name': 'connect_timeout', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    response_timeout: Optional[float] = field(
        default=None,
        metadata={'chn_name': '响应超时（秒）', 'eng_name': 'response_timeout', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    retry_count: Optional[int] = field(
        default=None,
        metadata={'chn_name': '重试次数', 'eng_name': 'retry_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    keep_alive: Optional[int] = field(
        default=None,
        metadata={'chn_name': '保持连接', 'eng_name': 'keep_alive', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

__all__ = ['fes_channel_modbus_tcp_define']

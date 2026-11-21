# -*- coding: utf-8 -*-
"""SCADA 数据库表定义（由 dbdef/json 自动生成，请勿手工修改）。

表名：fes_channel_modbus_rtu_define
中文名：MODBUS-RTU通道参数定义表
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_modbus_rtu_define:
    """MODBUS-RTU通道参数定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_modbus_rtu_define'
    __table_name_chn__: ClassVar[str] = 'MODBUS-RTU通道参数定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('channel_id',)

    channel_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属通道', 'eng_name': 'channel_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': '', 'required': True},
    )

    name: Optional[str] = field(
        default=None,
        metadata={'chn_name': '中文名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    serial_port: Optional[str] = field(
        default=None,
        metadata={'chn_name': '串口名称', 'eng_name': 'serial_port', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    baudrate: Optional[int] = field(
        default=9600,
        metadata={'chn_name': '波特率', 'eng_name': 'baudrate', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 9600},
    )

    bytesize: Optional[int] = field(
        default=8,
        metadata={'chn_name': '数据位', 'eng_name': 'bytesize', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 8},
    )

    parity: Optional[str] = field(
        default='E',
        metadata={'chn_name': '校验方式', 'eng_name': 'parity', 'primary_key': False, 'data_type': 'str', 'disp_type': '单选菜单', 'disp_menu': 'MODBUS校验方式', 'default': 'E', 'allowed_values': ['N', 'E', 'O']},
    )

    stopbits: Optional[int] = field(
        default=1,
        metadata={'chn_name': '停止位', 'eng_name': 'stopbits', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 1},
    )

    unit_id: Optional[int] = field(
        default=1,
        metadata={'chn_name': '默认从站地址', 'eng_name': 'unit_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 1},
    )

    response_timeout: Optional[float] = field(
        default=3.0,
        metadata={'chn_name': '响应超时（秒）', 'eng_name': 'response_timeout', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': '', 'default': 3.0},
    )

    retry_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '重试次数', 'eng_name': 'retry_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

__all__ = ['fes_channel_modbus_rtu_define']

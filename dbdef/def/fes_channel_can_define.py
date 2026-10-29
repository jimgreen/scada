# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_channel_can_define
Chinese name: CAN通道参数定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_can_define:
    """CAN通道参数定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_can_define'
    __table_name_chn__: ClassVar[str] = 'CAN通道参数定义表'
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

    interface: Optional[str] = field(
        default=None,
        metadata={'chn_name': 'CAN接口', 'eng_name': 'interface', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    channel: Optional[str] = field(
        default=None,
        metadata={'chn_name': 'CAN通道', 'eng_name': 'channel', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    bitrate: Optional[int] = field(
        default=None,
        metadata={'chn_name': '仲裁段波特率', 'eng_name': 'bitrate', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    data_bitrate: Optional[int] = field(
        default=None,
        metadata={'chn_name': '数据段波特率', 'eng_name': 'data_bitrate', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    fd: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'CAN-FD', 'eng_name': 'fd', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

    receive_own_messages: Optional[int] = field(
        default=None,
        metadata={'chn_name': '接收自身报文', 'eng_name': 'receive_own_messages', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

    receive_timeout: Optional[float] = field(
        default=None,
        metadata={'chn_name': '接收超时（秒）', 'eng_name': 'receive_timeout', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    retry_count: Optional[int] = field(
        default=None,
        metadata={'chn_name': '重试次数', 'eng_name': 'retry_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    is_extended_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '扩展帧默认值', 'eng_name': 'is_extended_id', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

__all__ = ['fes_channel_can_define']

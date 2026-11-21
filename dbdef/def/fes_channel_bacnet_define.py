# -*- coding: utf-8 -*-
"""SCADA 数据库表定义（由 dbdef/json 自动生成，请勿手工修改）。

表名：fes_channel_bacnet_define
中文名：bacnet通道定义表
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_bacnet_define:
    """bacnet通道定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_bacnet_define'
    __table_name_chn__: ClassVar[str] = 'bacnet通道定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('id',)

    id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '标识', 'eng_name': 'id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    name: Optional[str] = field(
        default=None,
        metadata={'chn_name': '中文名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    channel_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属通道', 'eng_name': 'channel_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': '', 'required': True},
    )

    bind_ip: Optional[str] = field(
        default=None,
        metadata={'chn_name': '绑定本端IP', 'eng_name': 'bind_ip', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    bind_port: Optional[int] = field(
        default=None,
        metadata={'chn_name': '绑定本端UDP端口', 'eng_name': 'bind_port', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    dev_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '对端设备ID', 'eng_name': 'dev_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    remote_host: Optional[str] = field(
        default='127.0.0.1',
        metadata={'chn_name': '远端主机', 'eng_name': 'remote_host', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': '127.0.0.1'},
    )

    remote_port: Optional[int] = field(
        default=47808,
        metadata={'chn_name': '远端端口', 'eng_name': 'remote_port', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 47808},
    )

__all__ = ['fes_channel_bacnet_define']

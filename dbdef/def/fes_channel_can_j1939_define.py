# -*- coding: utf-8 -*-
"""Generated dataclass definition from dbdef/json/fes_channel_can_j1939_define.json."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional

@dataclass
class fes_channel_can_j1939_define:
    """J1939报文定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_can_j1939_define'
    __table_name_chn__: ClassVar[str] = 'J1939报文定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('id',)

    id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '标识', 'eng_name': 'id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    channel_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属通道', 'eng_name': 'channel_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': '', 'required': True},
    )


    name: Optional[str] = field(
        default=None,
        metadata={'chn_name': '中文名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    pgn: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'PGN', 'eng_name': 'pgn', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'required': True},
    )

    source_address: Optional[int] = field(
        default=None,
        metadata={'chn_name': '源地址', 'eng_name': 'source_address', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'required': True},
    )

    destination_address: Optional[int] = field(
        default=None,
        metadata={'chn_name': '目标地址', 'eng_name': 'destination_address', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    priority: Optional[int] = field(
        default=6,
        metadata={'chn_name': '优先级', 'eng_name': 'priority', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 6},
    )

    data_page: Optional[int] = field(
        default=0,
        metadata={'chn_name': '数据页', 'eng_name': 'data_page', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    extended_data_page: Optional[int] = field(
        default=0,
        metadata={'chn_name': '扩展数据页', 'eng_name': 'extended_data_page', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    direction: Optional[str] = field(
        default='rx',
        metadata={'chn_name': '方向', 'eng_name': 'direction', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': 'CAN方向', 'allowed_values': ['rx', 'tx', 'both'], 'default': 'rx'},
    )

    message_type: Optional[str] = field(
        default='normal',
        metadata={'chn_name': '报文类型', 'eng_name': 'message_type', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': 'J1939报文类型', 'allowed_values': ['normal', 'dm', 'tp'], 'default': 'normal'},
    )

    dm_number: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'DM编号', 'eng_name': 'dm_number', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    transport_protocol: Optional[str] = field(
        default='none',
        metadata={'chn_name': '传输协议', 'eng_name': 'transport_protocol', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': 'J1939传输协议', 'allowed_values': ['none', 'bam', 'dt', 'cmdt', 'etp'], 'default': 'none'},
    )

    multi_packet: Optional[int] = field(
        default=0,
        metadata={'chn_name': '多包传输', 'eng_name': 'multi_packet', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

    data_length: Optional[int] = field(
        default=8,
        metadata={'chn_name': '数据长度', 'eng_name': 'data_length', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 8},
    )

    data_hex: Optional[str] = field(
        default='',
        metadata={'chn_name': '数据HEX', 'eng_name': 'data_hex', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''},
    )

    period_ms: Optional[int] = field(
        default=0,
        metadata={'chn_name': '周期（毫秒）', 'eng_name': 'period_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    packet_timeout_ms: Optional[int] = field(
        default=1000,
        metadata={'chn_name': '分包超时（毫秒）', 'eng_name': 'packet_timeout_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 1000},
    )

    enabled: Optional[int] = field(
        default=1,
        metadata={'chn_name': '启用', 'eng_name': 'enabled', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 1},
    )

__all__ = ['fes_channel_can_j1939_define']

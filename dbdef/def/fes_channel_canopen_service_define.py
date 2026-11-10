# -*- coding: utf-8 -*-
"""Generated dataclass definition from dbdef/json/fes_channel_canopen_service_define.json."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional

@dataclass
class fes_channel_canopen_service_define:
    """CANopen服务定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_canopen_service_define'
    __table_name_chn__: ClassVar[str] = 'CANopen服务定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('id',)

    id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '标识', 'eng_name': 'id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    channel_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属通道', 'eng_name': 'channel_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': '', 'required': True},
    )

    node_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '节点ID', 'eng_name': 'node_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'required': True},
    )

    service_type: Optional[str] = field(
        default=None,
        metadata={'chn_name': '服务类型', 'eng_name': 'service_type', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': 'CANopen服务', 'required': True, 'allowed_values': ['NMT', 'PDO', 'SDO', 'Heartbeat', 'EMCY']},
    )

    direction: Optional[str] = field(
        default='rx',
        metadata={'chn_name': '方向', 'eng_name': 'direction', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': 'CAN方向', 'allowed_values': ['rx', 'tx', 'both'], 'default': 'rx'},
    )

    cob_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'COB-ID', 'eng_name': 'cob_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    pdo_number: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'PDO编号', 'eng_name': 'pdo_number', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    transmission_type: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'PDO传输类型', 'eng_name': 'transmission_type', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    nmt_command: Optional[str] = field(
        default=None,
        metadata={'chn_name': 'NMT命令', 'eng_name': 'nmt_command', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    object_index: Optional[int] = field(
        default=None,
        metadata={'chn_name': '对象索引', 'eng_name': 'object_index', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    object_subindex: Optional[int] = field(
        default=None,
        metadata={'chn_name': '对象子索引', 'eng_name': 'object_subindex', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    data_type: Optional[str] = field(
        default=None,
        metadata={'chn_name': '数据类型', 'eng_name': 'data_type', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    data_hex: Optional[str] = field(
        default='',
        metadata={'chn_name': '数据HEX', 'eng_name': 'data_hex', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''},
    )

    heartbeat_ms: Optional[int] = field(
        default=0,
        metadata={'chn_name': 'Heartbeat周期（毫秒）', 'eng_name': 'heartbeat_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    emcy_error_code: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'EMCY错误码', 'eng_name': 'emcy_error_code', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    period_ms: Optional[int] = field(
        default=0,
        metadata={'chn_name': '周期（毫秒）', 'eng_name': 'period_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    timeout_ms: Optional[int] = field(
        default=1000,
        metadata={'chn_name': '超时（毫秒）', 'eng_name': 'timeout_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 1000},
    )

    enabled: Optional[int] = field(
        default=1,
        metadata={'chn_name': '启用', 'eng_name': 'enabled', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 1},
    )

__all__ = ['fes_channel_canopen_service_define']

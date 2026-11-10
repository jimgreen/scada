# -*- coding: utf-8 -*-
"""Generated dataclass definition from dbdef/json/fes_channel_can_uds_define.json."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional

@dataclass
class fes_channel_can_uds_define:
    """UDS诊断服务定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_can_uds_define'
    __table_name_chn__: ClassVar[str] = 'UDS诊断服务定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('id',)

    id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '标识', 'eng_name': 'id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    channel_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属通道', 'eng_name': 'channel_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': '', 'required': True},
    )

    code: Optional[str] = field(
        default=None,
        metadata={'chn_name': '英文标识', 'eng_name': 'code', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    name: Optional[str] = field(
        default=None,
        metadata={'chn_name': '中文名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    request_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '请求ID', 'eng_name': 'request_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'required': True},
    )

    response_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '响应ID', 'eng_name': 'response_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'required': True},
    )

    is_extended_id: Optional[int] = field(
        default=0,
        metadata={'chn_name': '扩展帧', 'eng_name': 'is_extended_id', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

    session_type: Optional[str] = field(
        default='default',
        metadata={'chn_name': '诊断会话', 'eng_name': 'session_type', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': 'UDS诊断会话', 'allowed_values': ['default', 'programming', 'extended'], 'default': 'default'},
    )

    service_type: Optional[str] = field(
        default=None,
        metadata={'chn_name': '服务类型', 'eng_name': 'service_type', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': 'UDS服务', 'required': True, 'allowed_values': ['session', 'read_data', 'write_data', 'routine_control', 'read_dtc', 'clear_dtc']},
    )

    service_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '服务ID', 'eng_name': 'service_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'required': True},
    )

    subfunction: Optional[int] = field(
        default=None,
        metadata={'chn_name': '子功能', 'eng_name': 'subfunction', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    did: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'DID', 'eng_name': 'did', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    routine_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'Routine ID', 'eng_name': 'routine_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    dtc_group: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'DTC组', 'eng_name': 'dtc_group', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    request_data_hex: Optional[str] = field(
        default='',
        metadata={'chn_name': '请求数据HEX', 'eng_name': 'request_data_hex', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''},
    )

    response_mask_hex: Optional[str] = field(
        default='',
        metadata={'chn_name': '响应匹配掩码HEX', 'eng_name': 'response_mask_hex', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''},
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

__all__ = ['fes_channel_can_uds_define']

# -*- coding: utf-8 -*-
"""Generated dataclass definition from dbdef/json/fes_channel_can_frame_define.json."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional

@dataclass
class fes_channel_can_frame_define:
    """CAN报文定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_can_frame_define'
    __table_name_chn__: ClassVar[str] = 'CAN报文定义表'
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

    rdfid: Optional[str] = field(
        default=None,
        metadata={'chn_name': 'RDFID', 'eng_name': 'rdfid', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    arbitration_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '仲裁ID', 'eng_name': 'arbitration_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'required': True},
    )

    is_extended_id: Optional[int] = field(
        default=0,
        metadata={'chn_name': '扩展帧', 'eng_name': 'is_extended_id', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

    is_fd: Optional[int] = field(
        default=0,
        metadata={'chn_name': 'CAN-FD', 'eng_name': 'is_fd', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

    bitrate_switch: Optional[int] = field(
        default=0,
        metadata={'chn_name': 'BRS数据相位切换', 'eng_name': 'bitrate_switch', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

    is_remote_frame: Optional[int] = field(
        default=0,
        metadata={'chn_name': '远程帧', 'eng_name': 'is_remote_frame', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

    dlc: Optional[int] = field(
        default=8,
        metadata={'chn_name': 'DLC', 'eng_name': 'dlc', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 8},
    )

    data_hex: Optional[str] = field(
        default='',
        metadata={'chn_name': '数据HEX', 'eng_name': 'data_hex', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''},
    )

    direction: Optional[str] = field(
        default='tx',
        metadata={'chn_name': '方向', 'eng_name': 'direction', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': 'CAN方向', 'allowed_values': ['rx', 'tx', 'both'], 'default': 'tx'},
    )

    schedule_mode: Optional[str] = field(
        default='manual',
        metadata={'chn_name': '调度模式', 'eng_name': 'schedule_mode', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': 'CAN调度模式', 'allowed_values': ['manual', 'periodic', 'replay', 'inject'], 'default': 'manual'},
    )

    period_ms: Optional[int] = field(
        default=0,
        metadata={'chn_name': '周期（毫秒）', 'eng_name': 'period_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    repeat_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '重复次数', 'eng_name': 'repeat_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    sequence_no: Optional[int] = field(
        default=0,
        metadata={'chn_name': '序号', 'eng_name': 'sequence_no', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    enabled: Optional[int] = field(
        default=1,
        metadata={'chn_name': '启用', 'eng_name': 'enabled', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 1},
    )

    replay_group: Optional[str] = field(
        default=None,
        metadata={'chn_name': '回放组', 'eng_name': 'replay_group', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    replay_source: Optional[str] = field(
        default=None,
        metadata={'chn_name': '回放源', 'eng_name': 'replay_source', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    inject_error_type: Optional[str] = field(
        default='none',
        metadata={'chn_name': '异常注入类型', 'eng_name': 'inject_error_type', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': 'CAN异常类型', 'allowed_values': ['none', 'ack', 'crc', 'stuff', 'form', 'bit', 'error_frame', 'bus_off'], 'default': 'none'},
    )

    inject_count: Optional[int] = field(
        default=0,
        metadata={'chn_name': '异常注入次数', 'eng_name': 'inject_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    inject_data_hex: Optional[str] = field(
        default=None,
        metadata={'chn_name': '异常注入数据HEX', 'eng_name': 'inject_data_hex', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

__all__ = ['fes_channel_can_frame_define']

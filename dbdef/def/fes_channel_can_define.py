# -*- coding: utf-8 -*-
"""Generated dataclass definition from dbdef/json/fes_channel_can_define.json."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional

@dataclass
class fes_channel_can_define:
    """CAN通道参数定义表。"""

    __tablename__: ClassVar[str] = 'fes_channel_can_define'
    __table_name_chn__: ClassVar[str] = 'CAN通道参数定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('channel_id',)

    channel_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属通道', 'eng_name': 'channel_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )


    name: Optional[str] = field(
        default=None,
        metadata={'chn_name': '中文名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
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

    sample_point: Optional[float] = field(
        default=87.5,
        metadata={'chn_name': '仲裁段采样点（百分比）', 'eng_name': 'sample_point', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': '', 'default': 87.5},
    )

    data_sample_point: Optional[float] = field(
        default=80.0,
        metadata={'chn_name': '数据段采样点（百分比）', 'eng_name': 'data_sample_point', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': '', 'default': 80.0},
    )

    bitrate_switch: Optional[int] = field(
        default=0,
        metadata={'chn_name': '默认BRS数据相位切换', 'eng_name': 'bitrate_switch', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

    restart_on_error: Optional[int] = field(
        default=0,
        metadata={'chn_name': '错误自动恢复', 'eng_name': 'restart_on_error', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

    bus_off_recovery_ms: Optional[int] = field(
        default=0,
        metadata={'chn_name': 'Bus-off恢复等待（毫秒）', 'eng_name': 'bus_off_recovery_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    receive_error_frames: Optional[int] = field(
        default=0,
        metadata={'chn_name': '接收错误帧', 'eng_name': 'receive_error_frames', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

__all__ = ['fes_channel_can_define']

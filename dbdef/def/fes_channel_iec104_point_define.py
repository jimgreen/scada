# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_channel_iec104_point_define
Chinese name: IEC104四遥点映射表
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_iec104_point_define:
    """IEC104四遥点映射表。"""

    __tablename__: ClassVar[str] = 'fes_channel_iec104_point_define'
    __table_name_chn__: ClassVar[str] = 'IEC104四遥点映射表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('channel_id', 'kind', 'pnt_no')

    channel_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属通道', 'eng_name': 'channel_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )

    kind: Optional[str] = field(
        default=None,
        metadata={'chn_name': '点类型', 'eng_name': 'kind', 'primary_key': True, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    pnt_no: Optional[int] = field(
        default=None,
        metadata={'chn_name': '点号', 'eng_name': 'pnt_no', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    io_address: Optional[int] = field(
        default=None,
        metadata={'chn_name': '信息体地址', 'eng_name': 'io_address', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    type_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '类型标识', 'eng_name': 'type_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    time_tag: Optional[str] = field(default='NONE', metadata={'chn_name': '时间标签', 'eng_name': 'time_tag', 'primary_key': False, 'data_type': 'str', 'disp_type': '单选菜单', 'disp_menu': 'IEC104时间标签', 'allowed_values': ['NONE', 'CP24Time2a', 'CP56Time2a']})

    report_ms: Optional[int] = field(
        default=None,
        metadata={'chn_name': '主动上送周期毫秒', 'eng_name': 'report_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    command_mode: Optional[str] = field(
        default=None,
        metadata={'chn_name': '命令执行方式', 'eng_name': 'command_mode', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    qualifier: Optional[int] = field(
        default=None,
        metadata={'chn_name': '命令限定词', 'eng_name': 'qualifier', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    related_io_address: Optional[int] = field(
        default=None,
        metadata={'chn_name': '关联信息体地址', 'eng_name': 'related_io_address', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    related_io_autoreturn: Optional[int] = field(
        default=None,
        metadata={'chn_name': '关联点自动返回', 'eng_name': 'related_io_autoreturn', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

__all__ = ['fes_channel_iec104_point_define']

# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_channel_bacnet_point_define
Chinese name: BACnet四遥点映射表
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_bacnet_point_define:
    """BACnet四遥点映射表。"""

    __tablename__: ClassVar[str] = 'fes_channel_bacnet_point_define'
    __table_name_chn__: ClassVar[str] = 'BACnet四遥点映射表'
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

    object_type: Optional[str] = field(
        default=None,
        metadata={'chn_name': '对象类型', 'eng_name': 'object_type', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    instance: Optional[int] = field(
        default=None,
        metadata={'chn_name': '对象实例', 'eng_name': 'instance', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    property_identifier: Optional[str] = field(
        default='presentValue',
        metadata={'chn_name': '属性', 'eng_name': 'property_identifier', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': 'presentValue'},
    )

    quality_property: Optional[str] = field(
        default='statusFlags',
        metadata={'chn_name': '质量属性', 'eng_name': 'quality_property', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': 'statusFlags'},
    )

    priority: Optional[int] = field(
        default=None,
        metadata={'chn_name': '写入优先级', 'eng_name': 'priority', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    array_index: Optional[int] = field(
        default=None,
        metadata={'chn_name': '数组下标', 'eng_name': 'array_index', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

__all__ = ['fes_channel_bacnet_point_define']

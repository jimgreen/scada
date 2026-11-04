# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_channel_opc_point_define
Chinese name: OPC UA四遥点映射表
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_channel_opc_point_define:
    """OPC UA四遥点映射表。"""

    __tablename__: ClassVar[str] = 'fes_channel_opc_point_define'
    __table_name_chn__: ClassVar[str] = 'OPC UA四遥点映射表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('rtu_id', 'kind', 'pnt_no')

    rtu_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属RTU', 'eng_name': 'rtu_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )

    kind: Optional[str] = field(
        default=None,
        metadata={'chn_name': '点类型', 'eng_name': 'kind', 'primary_key': True, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    pnt_no: Optional[int] = field(
        default=None,
        metadata={'chn_name': '点号', 'eng_name': 'pnt_no', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    node_id: Optional[str] = field(
        default=None,
        metadata={'chn_name': 'NodeId', 'eng_name': 'node_id', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    data_type: Optional[str] = field(
        default=None,
        metadata={'chn_name': '数据类型', 'eng_name': 'data_type', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    value_attribute: Optional[str] = field(
        default='Value',
        metadata={'chn_name': '值属性', 'eng_name': 'value_attribute', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': 'Value'},
    )

    quality_attribute: Optional[str] = field(
        default='StatusCode',
        metadata={'chn_name': '质量码属性', 'eng_name': 'quality_attribute', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': 'StatusCode'},
    )

    deadband: Optional[float] = field(
        default=None,
        metadata={'chn_name': '死区', 'eng_name': 'deadband', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

__all__ = ['fes_channel_opc_point_define']

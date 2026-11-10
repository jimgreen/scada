# -*- coding: utf-8 -*-
"""Generated dataclass definition for fes_scdfe_t_point_define."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional

@dataclass
class fes_scdfe_t_point_define:
    __tablename__: ClassVar[str] = 'fes_scdfe_t_point_define'
    __table_name_chn__: ClassVar[str] = 'scdfe_t四遥转发点映射表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('channel_id', 'pnt_no', 'kind')

    channel_id: Optional[int] = field(default=None, metadata={'chn_name': '转发通道', 'eng_name': 'channel_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''})

    pnt_no: Optional[int] = field(default=None, metadata={'chn_name': '转发点号', 'eng_name': 'pnt_no', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''})

    name: Optional[str] = field(default='', metadata={'chn_name': '点名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''})

    kind: Optional[str] = field(default=None, metadata={'chn_name': '转发点类型', 'eng_name': 'kind', 'primary_key': True, 'data_type': 'str', 'disp_type': '单选菜单', 'disp_menu': '四遥类型', 'required': True, 'allowed_values': ['yc', 'yx', 'yk', 'yt']})

    source_rtu_id: Optional[int] = field(default=None, metadata={'chn_name': '源RTU', 'eng_name': 'source_rtu_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': '', 'required': True})

    source_pnt_no: Optional[int] = field(default=None, metadata={'chn_name': '源点号', 'eng_name': 'source_pnt_no', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'required': True})

    enabled: Optional[int] = field(default=1, metadata={'chn_name': '启用', 'eng_name': 'enabled', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'})

    value: Optional[float] = field(default=0.0, metadata={'chn_name': '实时值', 'eng_name': 'value', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''})

    status: Optional[int] = field(default=128, metadata={'chn_name': '状态', 'eng_name': 'status', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''})

    refresh_time: Optional[int] = field(default=0, metadata={'chn_name': '刷新时间', 'eng_name': 'refresh_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''})

    forward_time: Optional[int] = field(default=0, metadata={'chn_name': '最后转发时间', 'eng_name': 'forward_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''})

    command_receive_time: Optional[int] = field(default=0, metadata={'chn_name': '指令接收时间', 'eng_name': 'command_receive_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''})

    io_address: Optional[int] = field(default=0, metadata={'chn_name': 'IEC信息体地址', 'eng_name': 'io_address', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''})

    type_id: Optional[int] = field(default=0, metadata={'chn_name': 'IEC类型标识', 'eng_name': 'type_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''})

    time_tag: Optional[str] = field(default='NONE', metadata={'chn_name': 'IEC时间标签', 'eng_name': 'time_tag', 'primary_key': False, 'data_type': 'str', 'disp_type': '单选菜单', 'disp_menu': 'IEC104时间标签', 'allowed_values': ['NONE', 'CP24Time2a', 'CP56Time2a']})

    command_mode: Optional[str] = field(default='DIRECT', metadata={'chn_name': '命令执行方式', 'eng_name': 'command_mode', 'primary_key': False, 'data_type': 'str', 'disp_type': '单选菜单', 'disp_menu': '命令执行方式', 'allowed_values': ['DIRECT', 'SELECT_AND_EXECUTE']})

    register_area: Optional[str] = field(default='holding', metadata={'chn_name': 'Modbus数据区', 'eng_name': 'register_area', 'primary_key': False, 'data_type': 'str', 'disp_type': '单选菜单', 'disp_menu': 'Modbus数据区', 'allowed_values': ['coil', 'discrete', 'holding', 'input']})

    address: Optional[int] = field(default=0, metadata={'chn_name': 'Modbus地址', 'eng_name': 'address', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''})

    address_base: Optional[int] = field(default=0, metadata={'chn_name': 'Modbus地址基准（0/1/10001/30001/40001）', 'eng_name': 'address_base', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'allowed_values': [0, 1, 10001, 30001, 40001], 'default': 0})

    data_type: Optional[str] = field(default='float32', metadata={'chn_name': '数据类型', 'eng_name': 'data_type', 'primary_key': False, 'data_type': 'str', 'disp_type': '单选菜单', 'disp_menu': 'Modbus数据类型', 'allowed_values': ['bool', 'uint16', 'int16', 'uint32', 'int32', 'float32', 'float64', 'uint64', 'int64']})

    byte_order: Optional[str] = field(default='big', metadata={'chn_name': '字节序', 'eng_name': 'byte_order', 'primary_key': False, 'data_type': 'str', 'disp_type': '单选菜单', 'disp_menu': '字节序', 'allowed_values': ['big', 'little']})

    word_order: Optional[str] = field(default='big', metadata={'chn_name': '字序', 'eng_name': 'word_order', 'primary_key': False, 'data_type': 'str', 'disp_type': '单选菜单', 'disp_menu': '字序', 'allowed_values': ['big', 'little']})

    scale: Optional[float] = field(default=1.0, metadata={'chn_name': '比例', 'eng_name': 'scale', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''})

    offset: Optional[float] = field(default=0.0, metadata={'chn_name': '偏移', 'eng_name': 'offset', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''})

__all__ = ['fes_scdfe_t_point_define']

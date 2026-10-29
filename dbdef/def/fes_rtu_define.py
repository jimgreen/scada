# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_rtu_define
Chinese name: RTU定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_rtu_define:
    """RTU定义表。"""

    __tablename__: ClassVar[str] = 'fes_rtu_define'
    __table_name_chn__: ClassVar[str] = 'RTU定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('rtu_id',)

    rtu_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'rtu编号', 'eng_name': 'rtu_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    code: Optional[str] = field(
        default=None,
        metadata={'chn_name': '英文标识', 'eng_name': 'code', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    name: Optional[str] = field(
        default=None,
        metadata={'chn_name': '中文名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    rdfid: Optional[str] = field(
        default=None,
        metadata={'chn_name': 'RDFID', 'eng_name': 'rdfid', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    st_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属厂站', 'eng_name': 'st_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )

    oper_area: Optional[int] = field(
        default=None,
        metadata={'chn_name': '操作区域', 'eng_name': 'oper_area', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    status: Optional[int] = field(
        default=None,
        metadata={'chn_name': '状态', 'eng_name': 'status', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    rtu_104code: Optional[int] = field(
        default=None,
        metadata={'chn_name': '104编码', 'eng_name': 'rtu_104code', 'primary_key': False, 'data_type': 'int', 'disp_type': 'SHORT', 'disp_menu': ''},
    )

    call_all_cycle: Optional[int] = field(
        default=None,
        metadata={'chn_name': '全数据召唤周期（毫秒）', 'eng_name': 'call_all_cycle', 'primary_key': False, 'data_type': 'int', 'disp_type': 'SHORT', 'disp_menu': ''},
    )

    his_save: Optional[int] = field(
        default=None,
        metadata={'chn_name': '保存历史', 'eng_name': 'his_save', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断'},
    )

    refresh_trigger: Optional[int] = field(
        default=None,
        metadata={'chn_name': '数据刷新周期(毫秒)', 'eng_name': 'refresh_trigger', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    refresh_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '数据刷新时刻', 'eng_name': 'refresh_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    channel_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通道标识', 'eng_name': 'channel_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'required': True},
    )

    channel_type: Optional[str] = field(
        default=None,
        metadata={'chn_name': '通道类型', 'eng_name': 'channel_type', 'primary_key': False, 'data_type': 'str', 'disp_type': '单选菜单', 'disp_menu': 'SCADA通道类型', 'required': True, 'allowed_values': ['iec104', 'modbus-rtu', 'modbus-tcp', 'can', 'bacnet', 'opc', 'custom']},
    )

    chan_addr: Optional[str] = field(
        default=None,
        metadata={'chn_name': '通道地址', 'eng_name': 'chan_addr', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    chan_port: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通道端口', 'eng_name': 'chan_port', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    process_enable: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通信线程启用', 'eng_name': 'process_enable', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 1},
    )

    process_restart: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通信线程重启请求', 'eng_name': 'process_restart', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

    process_status: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通信线程状态', 'eng_name': 'process_status', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    process_pid: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通信线程宿主PID', 'eng_name': 'process_pid', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'default': 0},
    )

    process_start_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通信线程启动时间', 'eng_name': 'process_start_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0},
    )

    process_stop_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通信线程停止时间', 'eng_name': 'process_stop_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0},
    )

    process_restart_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通信线程重启时间', 'eng_name': 'process_restart_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0},
    )

    process_heartbeat_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通信线程心跳时间', 'eng_name': 'process_heartbeat_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0},
    )

    process_exit_code: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通信线程退出结果', 'eng_name': 'process_exit_code', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    process_restart_count: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通信线程重启次数', 'eng_name': 'process_restart_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    process_error: Optional[str] = field(
        default=None,
        metadata={'chn_name': '通信线程错误信息', 'eng_name': 'process_error', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''},
    )

__all__ = ['fes_rtu_define']

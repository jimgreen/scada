# -*- coding: utf-8 -*-
"""Generated dataclass definition from dbdef/json/fes_channel_runtime_status.json."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional

@dataclass
class fes_channel_runtime_status:
    """通信通道运行状态快照表。"""

    __tablename__: ClassVar[str] = 'fes_channel_runtime_status'
    __table_name_chn__: ClassVar[str] = '通信通道运行状态快照表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('service_name', 'channel_id')

    service_name: Optional[str] = field(default=None, metadata={'chn_name': '服务实例', 'eng_name': 'service_name', 'primary_key': True, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''})
    channel_id: Optional[int] = field(default=None, metadata={'chn_name': '通道编号', 'eng_name': 'channel_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''})
    protocol: Optional[str] = field(default='', metadata={'chn_name': '规约', 'eng_name': 'protocol', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''})
    channel_name: Optional[str] = field(default='', metadata={'chn_name': '通道名称', 'eng_name': 'channel_name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''})
    state: Optional[str] = field(default='unknown', metadata={'chn_name': '运行状态', 'eng_name': 'state', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '运行状态', 'default': 'unknown', 'allowed_values': ['unknown', 'stopped', 'starting', 'running', 'stopping', 'error', 'degraded']})
    running: Optional[int] = field(default=0, metadata={'chn_name': '运行标志', 'eng_name': 'running', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '二值判断', 'default': 0})
    connection_state: Optional[str] = field(default='unknown', metadata={'chn_name': '连接状态', 'eng_name': 'connection_state', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '连接状态', 'default': 'unknown'})
    process_status: Optional[int] = field(default=0, metadata={'chn_name': '进程状态', 'eng_name': 'process_status', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})
    process_pid: Optional[int] = field(default=0, metadata={'chn_name': '进程号', 'eng_name': 'process_pid', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})
    process_start_time: Optional[int] = field(default=0, metadata={'chn_name': '启动时间', 'eng_name': 'process_start_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})
    process_stop_time: Optional[int] = field(default=0, metadata={'chn_name': '停止时间', 'eng_name': 'process_stop_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})
    process_heartbeat_time: Optional[int] = field(default=0, metadata={'chn_name': '心跳时间', 'eng_name': 'process_heartbeat_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})
    process_exit_code: Optional[int] = field(default=0, metadata={'chn_name': '退出码', 'eng_name': 'process_exit_code', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})
    process_error: Optional[str] = field(default='', metadata={'chn_name': '进程错误', 'eng_name': 'process_error', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''})
    rx_count: Optional[int] = field(default=0, metadata={'chn_name': '接收计数', 'eng_name': 'rx_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})
    tx_count: Optional[int] = field(default=0, metadata={'chn_name': '发送计数', 'eng_name': 'tx_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})
    error_count: Optional[int] = field(default=0, metadata={'chn_name': '错误计数', 'eng_name': 'error_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})
    dropped_count: Optional[int] = field(default=0, metadata={'chn_name': '丢失计数', 'eng_name': 'dropped_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})
    reconnect_count: Optional[int] = field(default=0, metadata={'chn_name': '重连计数', 'eng_name': 'reconnect_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})
    last_rx_time: Optional[int] = field(default=0, metadata={'chn_name': '最近接收时间', 'eng_name': 'last_rx_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})
    last_tx_time: Optional[int] = field(default=0, metadata={'chn_name': '最近发送时间', 'eng_name': 'last_tx_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})
    last_error_time: Optional[int] = field(default=0, metadata={'chn_name': '最后错误时间', 'eng_name': 'last_error_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})
    last_error: Optional[str] = field(default='', metadata={'chn_name': '最后错误', 'eng_name': 'last_error', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''})
    diagnostic_json: Optional[str] = field(default='{}', metadata={'chn_name': '诊断详情 JSON', 'eng_name': 'diagnostic_json', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': '{}'})
    sampled_at: Optional[int] = field(default=0, metadata={'chn_name': '采样时间', 'eng_name': 'sampled_at', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})

__all__ = ['fes_channel_runtime_status']

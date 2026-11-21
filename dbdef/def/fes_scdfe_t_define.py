# -*- coding: utf-8 -*-
"""SCADA 数据库表定义（由 dbdef/json 自动生成，请勿手工修改）。

表名：fes_scdfe_t_define
中文名：scdfe_t四遥转发通道定义表
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_scdfe_t_define:
    """scdfe_t四遥转发通道定义表。"""

    __tablename__: ClassVar[str] = 'fes_scdfe_t_define'
    __table_name_chn__: ClassVar[str] = 'scdfe_t四遥转发通道定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('channel_id',)

    channel_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '通道编号', 'eng_name': 'channel_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    name: Optional[str] = field(
        default=None,
        metadata={'chn_name': '中文名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    protocol: Optional[str] = field(
        default=None,
        metadata={'chn_name': '转发规约', 'eng_name': 'protocol', 'primary_key': False, 'data_type': 'str', 'disp_type': '单选菜单', 'disp_menu': '转发规约', 'allowed_values': ['iec104', 'modbus-tcp']},
    )

    bind_host: Optional[str] = field(
        default='0.0.0.0',
        metadata={'chn_name': '绑定主机', 'eng_name': 'bind_host', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': '0.0.0.0'},
    )

    bind_port: Optional[int] = field(
        default=2404,
        metadata={'chn_name': '绑定端口', 'eng_name': 'bind_port', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 2404},
    )

    common_address: Optional[int] = field(
        default=1,
        metadata={'chn_name': '公共地址', 'eng_name': 'common_address', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 1},
    )

    unit_id: Optional[int] = field(
        default=1,
        metadata={'chn_name': '默认从站地址', 'eng_name': 'unit_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 1},
    )

    scan_interval_ms: Optional[int] = field(
        default=200,
        metadata={'chn_name': '数据扫描周期（毫秒）', 'eng_name': 'scan_interval_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 200},
    )

    report_interval_ms: Optional[int] = field(
        default=1000,
        metadata={'chn_name': '全量上送周期（毫秒）', 'eng_name': 'report_interval_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 1000},
    )

    log_retention_hours: Optional[int] = field(
        default=720,
        metadata={'chn_name': '日志保留小时数', 'eng_name': 'log_retention_hours', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 720},
    )

    k: Optional[int] = field(
        default=12,
        metadata={'chn_name': '发送窗口 k', 'eng_name': 'k', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 12},
    )

    w: Optional[int] = field(
        default=8,
        metadata={'chn_name': '接收确认窗口 w', 'eng_name': 'w', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 8},
    )

    t1: Optional[float] = field(
        default=15.0,
        metadata={'chn_name': 'I 帧确认超时 t1（秒）', 'eng_name': 't1', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': '', 'default': 15.0},
    )

    t2: Optional[float] = field(
        default=10.0,
        metadata={'chn_name': 'S 帧确认周期 t2（秒）', 'eng_name': 't2', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': '', 'default': 10.0},
    )

    t3: Optional[float] = field(
        default=20.0,
        metadata={'chn_name': 'TESTFR 周期 t3（秒）', 'eng_name': 't3', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': '', 'default': 20.0},
    )

    max_asdu_length: Optional[int] = field(
        default=249,
        metadata={'chn_name': 'ASDU 最大长度', 'eng_name': 'max_asdu_length', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 249},
    )

    packet_recording: Optional[int] = field(
        default=0,
        metadata={'chn_name': '保存报文', 'eng_name': 'packet_recording', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

    process_enable: Optional[int] = field(
        default=0,
        metadata={'chn_name': '参与自动运行', 'eng_name': 'process_enable', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0},
    )

    process_status: Optional[int] = field(
        default=0,
        metadata={'chn_name': '进程状态', 'eng_name': 'process_status', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    process_pid: Optional[int] = field(
        default=0,
        metadata={'chn_name': '进程号', 'eng_name': 'process_pid', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    process_start_time: Optional[int] = field(
        default=0,
        metadata={'chn_name': '启动时间', 'eng_name': 'process_start_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0},
    )

    process_stop_time: Optional[int] = field(
        default=0,
        metadata={'chn_name': '停止时间', 'eng_name': 'process_stop_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0},
    )

    process_heartbeat_time: Optional[int] = field(
        default=0,
        metadata={'chn_name': '心跳时间', 'eng_name': 'process_heartbeat_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0},
    )

    process_exit_code: Optional[int] = field(
        default=0,
        metadata={'chn_name': '退出码', 'eng_name': 'process_exit_code', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0},
    )

    process_error: Optional[str] = field(
        default='',
        metadata={'chn_name': '错误信息', 'eng_name': 'process_error', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''},
    )

__all__ = ['fes_scdfe_t_define']

# -*- coding: utf-8 -*-
"""Generated dataclass definition for fes_scdfe_t_define."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional

@dataclass
class fes_scdfe_t_define:
    __tablename__: ClassVar[str] = 'fes_scdfe_t_define'
    __table_name_chn__: ClassVar[str] = 'scdfe_t四遥转发通道定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('channel_id',)

    channel_id: Optional[int] = field(default=None, metadata={'chn_name': '通道编号', 'disp_type': 'INT', 'disp_menu': ''})


    name: Optional[str] = field(default=None, metadata={'chn_name': '中文名称', 'disp_type': 'STRING', 'disp_menu': ''})

    protocol: Optional[str] = field(default=None, metadata={'chn_name': '转发规约', 'disp_type': '单选菜单', 'disp_menu': '转发规约', 'allowed_values': ['iec104', 'modbus-tcp']})

    bind_host: Optional[str] = field(default='0.0.0.0', metadata={'chn_name': '绑定主机', 'disp_type': 'STRING', 'disp_menu': ''})

    bind_port: Optional[int] = field(default=2404, metadata={'chn_name': '绑定端口', 'disp_type': 'INT', 'disp_menu': ''})

    common_address: Optional[int] = field(default=1, metadata={'chn_name': '公共地址', 'disp_type': 'INT', 'disp_menu': ''})

    unit_id: Optional[int] = field(default=1, metadata={'chn_name': '默认从站地址', 'disp_type': 'INT', 'disp_menu': ''})

    scan_interval_ms: Optional[int] = field(default=200, metadata={'chn_name': '数据扫描周期（毫秒）', 'disp_type': 'INT', 'disp_menu': ''})

    report_interval_ms: Optional[int] = field(default=1000, metadata={'chn_name': '全量上送周期（毫秒）', 'disp_type': 'INT', 'disp_menu': ''})

    log_retention_hours: Optional[int] = field(default=720, metadata={'chn_name': '日志保留小时数', 'disp_type': 'INT', 'disp_menu': ''})

    k: Optional[int] = field(default=12, metadata={'chn_name': '发送窗口 k', 'disp_type': 'INT', 'disp_menu': ''})

    w: Optional[int] = field(default=8, metadata={'chn_name': '接收确认窗口 w', 'disp_type': 'INT', 'disp_menu': ''})

    t1: Optional[float] = field(default=15.0, metadata={'chn_name': 'I 帧确认超时 t1（秒）', 'disp_type': 'FLOAT', 'disp_menu': ''})

    t2: Optional[float] = field(default=10.0, metadata={'chn_name': 'S 帧确认周期 t2（秒）', 'disp_type': 'FLOAT', 'disp_menu': ''})

    t3: Optional[float] = field(default=20.0, metadata={'chn_name': 'TESTFR 周期 t3（秒）', 'disp_type': 'FLOAT', 'disp_menu': ''})

    max_asdu_length: Optional[int] = field(default=249, metadata={'chn_name': 'ASDU 最大长度', 'disp_type': 'INT', 'disp_menu': ''})

    packet_recording: Optional[int] = field(default=0, metadata={'chn_name': '保存报文', 'eng_name': 'packet_recording', 'disp_type': '单选菜单', 'disp_menu': '二值判断'})

    process_enable: Optional[int] = field(default=0, metadata={'chn_name': '参与自动运行', 'disp_type': '单选菜单', 'disp_menu': '二值判断'})

    process_status: Optional[int] = field(default=0, metadata={'chn_name': '进程状态', 'disp_type': 'INT', 'disp_menu': ''})

    process_pid: Optional[int] = field(default=0, metadata={'chn_name': '进程号', 'disp_type': 'INT', 'disp_menu': ''})

    process_start_time: Optional[int] = field(default=0, metadata={'chn_name': '启动时间', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''})

    process_stop_time: Optional[int] = field(default=0, metadata={'chn_name': '停止时间', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''})

    process_heartbeat_time: Optional[int] = field(default=0, metadata={'chn_name': '心跳时间', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''})

    process_exit_code: Optional[int] = field(default=0, metadata={'chn_name': '退出码', 'disp_type': 'INT', 'disp_menu': ''})

    process_error: Optional[str] = field(default='', metadata={'chn_name': '错误信息', 'disp_type': 'STRING', 'disp_menu': ''})

__all__ = ['fes_scdfe_t_define']

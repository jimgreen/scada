"""Generated from dbdef/json/fes_scdfe_c_define.json."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_scdfe_c_define:
    __tablename__: ClassVar[str] = 'fes_scdfe_c_define'
    __table_name_chn__: ClassVar[str] = 'scdfe_c主进程控制表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('id',)

    id: Optional[int] = field(default=None, metadata={'chn_name': '固定编号', 'eng_name': 'id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''})

    name: Optional[str] = field(default=None, metadata={'chn_name': '名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''})

    process_enable: Optional[int] = field(default=1, metadata={'chn_name': '主进程启用', 'eng_name': 'process_enable', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 1})

    process_restart: Optional[int] = field(default=0, metadata={'chn_name': '主进程重启请求', 'eng_name': 'process_restart', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0})

    process_status: Optional[int] = field(default=0, metadata={'chn_name': '主进程状态', 'eng_name': 'process_status', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})

    process_pid: Optional[int] = field(default=0, metadata={'chn_name': '主进程号', 'eng_name': 'process_pid', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'default': 0})

    process_start_time: Optional[int] = field(default=0, metadata={'chn_name': '主进程启动时间', 'eng_name': 'process_start_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})

    process_stop_time: Optional[int] = field(default=0, metadata={'chn_name': '主进程停止时间', 'eng_name': 'process_stop_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})

    process_restart_time: Optional[int] = field(default=0, metadata={'chn_name': '主进程重启时间', 'eng_name': 'process_restart_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})

    process_heartbeat_time: Optional[int] = field(default=0, metadata={'chn_name': '主进程心跳时间', 'eng_name': 'process_heartbeat_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})

    process_exit_code: Optional[int] = field(default=0, metadata={'chn_name': '主进程退出码', 'eng_name': 'process_exit_code', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})

    process_restart_count: Optional[int] = field(default=0, metadata={'chn_name': '主进程重启次数', 'eng_name': 'process_restart_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})

    process_error: Optional[str] = field(default='', metadata={'chn_name': '主进程错误信息', 'eng_name': 'process_error', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''})

    log_dir: Optional[str] = field(default='logs', metadata={'chn_name': '日志目录（相对数据库目录）', 'eng_name': 'log_dir', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': 'logs'})

    log_retention_hours: Optional[int] = field(default=720, metadata={'chn_name': '日志保留小时数', 'eng_name': 'log_retention_hours', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 720})

    scan_interval_ms: Optional[int] = field(default=2000, metadata={'chn_name': '主进程扫描周期（毫秒）', 'eng_name': 'scan_interval_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 2000})

    poll_interval_ms: Optional[int] = field(default=1000, metadata={'chn_name': '通讯采集周期（毫秒）', 'eng_name': 'poll_interval_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 1000})

    retry_interval_ms: Optional[int] = field(default=5000, metadata={'chn_name': '失败重试周期（毫秒）', 'eng_name': 'retry_interval_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 5000})

    worker_start_delay_ms: Optional[int] = field(default=250, metadata={'chn_name': '通信线程启动间隔（毫秒）', 'eng_name': 'worker_start_delay_ms', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 250})

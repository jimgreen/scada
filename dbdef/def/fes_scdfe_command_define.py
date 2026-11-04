"""Generated from dbdef/json/fes_scdfe_command_define.json."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional

@dataclass
class fes_scdfe_command_define:
    __tablename__: ClassVar[str] = 'fes_scdfe_command_define'
    __table_name_chn__: ClassVar[str] = 'scdfe_c TCP指令服务定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('id',)

    id: Optional[int] = field(default=1, metadata={'chn_name': '固定编号', 'eng_name': 'id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 1})

    bind_host: Optional[str] = field(default='127.0.0.1', metadata={'chn_name': '监听地址', 'eng_name': 'bind_host', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': '127.0.0.1'})

    bind_port: Optional[int] = field(default=8890, metadata={'chn_name': '监听端口', 'eng_name': 'bind_port', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 8890})

    auth_token: Optional[str] = field(default='', metadata={'chn_name': '认证令牌', 'eng_name': 'auth_token', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''})

    process_enable: Optional[int] = field(default=0, metadata={'chn_name': '进程启用', 'eng_name': 'process_enable', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '二值判断', 'default': 0})

    process_status: Optional[int] = field(default=0, metadata={'chn_name': '进程状态', 'eng_name': 'process_status', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 0})

    process_pid: Optional[int] = field(default=0, metadata={'chn_name': '进程号', 'eng_name': 'process_pid', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'default': 0})

    process_start_time: Optional[int] = field(default=0, metadata={'chn_name': '启动时间', 'eng_name': 'process_start_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})

    process_stop_time: Optional[int] = field(default=0, metadata={'chn_name': '停止时间', 'eng_name': 'process_stop_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})

    process_heartbeat_time: Optional[int] = field(default=0, metadata={'chn_name': '心跳时间', 'eng_name': 'process_heartbeat_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})

    process_exit_code: Optional[int] = field(default=0, metadata={'chn_name': '退出码', 'eng_name': 'process_exit_code', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'default': 0})

    process_error: Optional[str] = field(default='', metadata={'chn_name': '错误信息', 'eng_name': 'process_error', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''})

    log_retention_hours: Optional[int] = field(default=720, metadata={'chn_name': '日志保留小时数', 'eng_name': 'log_retention_hours', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': '', 'default': 720})

__all__ = ['fes_scdfe_command_define']

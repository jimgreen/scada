"""Generated from dbdef/json/fes_process_define.json."""
from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_process_define:
    __tablename__: ClassVar[str] = 'fes_process_define'
    __table_name_chn__: ClassVar[str] = '运行进程监控表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('process_key',)

    process_key: Optional[str] = field(default=None, metadata={'chn_name': '进程标识', 'eng_name': 'process_key', 'primary_key': True, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''})

    kind: Optional[str] = field(default='', metadata={'chn_name': '进程类型', 'eng_name': 'kind', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''})

    target_rtu: Optional[int] = field(default=0, metadata={'chn_name': '调试RTU', 'eng_name': 'target_rtu', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'default': 0})

    process_enable: Optional[int] = field(default=0, metadata={'chn_name': '期望运行', 'eng_name': 'process_enable', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'default': 0})

    include_in_all: Optional[int] = field(default=1, metadata={'chn_name': '参与一键启动', 'eng_name': 'include_in_all', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'default': 1})

    process_status: Optional[int] = field(default=0, metadata={'chn_name': '进程状态', 'eng_name': 'process_status', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'default': 0})

    process_pid: Optional[int] = field(default=0, metadata={'chn_name': '进程号', 'eng_name': 'process_pid', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'default': 0})

    parent_pid: Optional[int] = field(default=0, metadata={'chn_name': '父进程号', 'eng_name': 'parent_pid', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'default': 0})

    process_start_time: Optional[int] = field(default=0, metadata={'chn_name': '启动时间', 'eng_name': 'process_start_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})

    process_stop_time: Optional[int] = field(default=0, metadata={'chn_name': '停止时间', 'eng_name': 'process_stop_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})

    process_heartbeat_time: Optional[int] = field(default=0, metadata={'chn_name': '心跳时间', 'eng_name': 'process_heartbeat_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': '', 'default': 0})

    process_exit_code: Optional[int] = field(default=0, metadata={'chn_name': '退出码', 'eng_name': 'process_exit_code', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': '', 'default': 0})

    process_error: Optional[str] = field(default='', metadata={'chn_name': '错误信息', 'eng_name': 'process_error', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''})

    owner_token: Optional[str] = field(default='', metadata={'chn_name': '运行实例标识', 'eng_name': 'owner_token', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': '', 'default': ''})

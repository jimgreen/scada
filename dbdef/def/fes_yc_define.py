# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_yc_define
Chinese name: 遥测定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_yc_define:
    """遥测定义表。"""

    __tablename__: ClassVar[str] = 'fes_yc_define'
    __table_name_chn__: ClassVar[str] = '遥测定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('rtu_id', 'pnt_no')

    rtu_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '所属RTU', 'eng_name': 'rtu_id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )

    pnt_no: Optional[int] = field(
        default=None,
        metadata={'chn_name': '点号', 'eng_name': 'pnt_no', 'primary_key': True, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    code: Optional[str] = field(
        default=None,
        metadata={'chn_name': '英文标识', 'eng_name': 'code', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    name: Optional[str] = field(
        default=None,
        metadata={'chn_name': '中文标识', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    rdfid: Optional[str] = field(
        default=None,
        metadata={'chn_name': 'RDFID', 'eng_name': 'rdfid', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    raw_base: Optional[float] = field(
        default=None,
        metadata={'chn_name': '生数据基值', 'eng_name': 'raw_base', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    raw_scale: Optional[float] = field(
        default=None,
        metadata={'chn_name': '生数据比例', 'eng_name': 'raw_scale', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    yt_pnt: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调点号', 'eng_name': 'yt_pnt', 'primary_key': False, 'data_type': 'int', 'disp_type': 'SHORT', 'disp_menu': ''},
    )

    yc_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥测ID', 'eng_name': 'yc_id', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    iflock: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否闭锁', 'eng_name': 'iflock', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

    ifalarm: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否报警', 'eng_name': 'ifalarm', 'primary_key': False, 'data_type': 'int', 'disp_type': '单选菜单', 'disp_menu': '遥测越限报警类型'},
    )

    ifreverse: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否取反', 'eng_name': 'ifreverse', 'primary_key': False, 'data_type': 'int', 'disp_type': 'UCHAR', 'disp_menu': ''},
    )

    locked_value: Optional[float] = field(
        default=None,
        metadata={'chn_name': '闭锁值', 'eng_name': 'locked_value', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    value_zone: Optional[float] = field(
        default=None,
        metadata={'chn_name': '数据值刷新死区', 'eng_name': 'value_zone', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    time_zone: Optional[int] = field(
        default=None,
        metadata={'chn_name': '老数据判断（秒）', 'eng_name': 'time_zone', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    raw_value: Optional[float] = field(
        default=None,
        metadata={'chn_name': '生数据值', 'eng_name': 'raw_value', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    raw_status: Optional[int] = field(
        default=None,
        metadata={'chn_name': '生数据状态', 'eng_name': 'raw_status', 'primary_key': False, 'data_type': 'int', 'disp_type': '多选菜单', 'disp_menu': '遥测状态'},
    )

    raw_max: Optional[float] = field(
        default=None,
        metadata={'chn_name': '生数据最大值', 'eng_name': 'raw_max', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    raw_min: Optional[float] = field(
        default=None,
        metadata={'chn_name': '生数据最小值', 'eng_name': 'raw_min', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    raw_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '生数据刷新时间', 'eng_name': 'raw_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    value: Optional[float] = field(
        default=None,
        metadata={'chn_name': '值', 'eng_name': 'value', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    status: Optional[int] = field(
        default=None,
        metadata={'chn_name': '状态', 'eng_name': 'status', 'primary_key': False, 'data_type': 'int', 'disp_type': '多选菜单', 'disp_menu': '遥测状态'},
    )

    ifdata_curb: Optional[int] = field(
        default=None,
        metadata={'chn_name': '是否数据抑制', 'eng_name': 'ifdata_curb', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    eng_max: Optional[float] = field(
        default=None,
        metadata={'chn_name': '数据最大值', 'eng_name': 'eng_max', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    eng_min: Optional[float] = field(
        default=None,
        metadata={'chn_name': '数据最小值', 'eng_name': 'eng_min', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    refresh_time: Optional[int] = field(
        default=None,
        metadata={'chn_name': '刷新时间', 'eng_name': 'refresh_time', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    value_dev: Optional[float] = field(
        default=None,
        metadata={'chn_name': '设备更新后值', 'eng_name': 'value_dev', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    status_dev: Optional[int] = field(
        default=None,
        metadata={'chn_name': '设备更新后状态', 'eng_name': 'status_dev', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    refresh_time_dev: Optional[int] = field(
        default=None,
        metadata={'chn_name': '设备更新时间', 'eng_name': 'refresh_time_dev', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    lock_begintime: Optional[int] = field(
        default=None,
        metadata={'chn_name': '闭锁开始时间', 'eng_name': 'lock_begintime', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    lock_holdtime: Optional[int] = field(
        default=None,
        metadata={'chn_name': '闭锁持续时间', 'eng_name': 'lock_holdtime', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    alarm_upjump: Optional[float] = field(
        default=None,
        metadata={'chn_name': '数据上跳变报警门槛', 'eng_name': 'alarm_upjump', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    alarm_dnjump: Optional[float] = field(
        default=None,
        metadata={'chn_name': '数据下跳变报警门槛', 'eng_name': 'alarm_dnjump', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    run_max1: Optional[float] = field(
        default=None,
        metadata={'chn_name': '运行上限1', 'eng_name': 'run_max1', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    run_max2: Optional[float] = field(
        default=None,
        metadata={'chn_name': '运行上限2', 'eng_name': 'run_max2', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    run_max3: Optional[float] = field(
        default=None,
        metadata={'chn_name': '运行上限3', 'eng_name': 'run_max3', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    run_max4: Optional[float] = field(
        default=None,
        metadata={'chn_name': '运行上限4', 'eng_name': 'run_max4', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    run_max5: Optional[float] = field(
        default=None,
        metadata={'chn_name': '运行上限5', 'eng_name': 'run_max5', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    run_min1: Optional[float] = field(
        default=None,
        metadata={'chn_name': '运行下限1', 'eng_name': 'run_min1', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    run_min2: Optional[float] = field(
        default=None,
        metadata={'chn_name': '运行下限2', 'eng_name': 'run_min2', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    run_min3: Optional[float] = field(
        default=None,
        metadata={'chn_name': '运行下限3', 'eng_name': 'run_min3', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    run_min4: Optional[float] = field(
        default=None,
        metadata={'chn_name': '运行下限4', 'eng_name': 'run_min4', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    run_min5: Optional[float] = field(
        default=None,
        metadata={'chn_name': '运行下限5', 'eng_name': 'run_min5', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    yc_id_1: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥测ID_1', 'eng_name': 'yc_id_1', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    value_dev_1: Optional[float] = field(
        default=None,
        metadata={'chn_name': '设备更新后值_1', 'eng_name': 'value_dev_1', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    status_dev_1: Optional[int] = field(
        default=None,
        metadata={'chn_name': '设备更新后状态_1', 'eng_name': 'status_dev_1', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    refresh_time_dev_1: Optional[int] = field(
        default=None,
        metadata={'chn_name': '设备更新时间_1', 'eng_name': 'refresh_time_dev_1', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    yc_id_2: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥测ID_2', 'eng_name': 'yc_id_2', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    value_dev_2: Optional[float] = field(
        default=None,
        metadata={'chn_name': '设备更新后值_2', 'eng_name': 'value_dev_2', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    status_dev_2: Optional[int] = field(
        default=None,
        metadata={'chn_name': '设备更新后状态_2', 'eng_name': 'status_dev_2', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    refresh_time_dev_2: Optional[int] = field(
        default=None,
        metadata={'chn_name': '设备更新时间_2', 'eng_name': 'refresh_time_dev_2', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    yc_id_3: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥测ID_3', 'eng_name': 'yc_id_3', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    value_dev_3: Optional[float] = field(
        default=None,
        metadata={'chn_name': '设备更新后值_3', 'eng_name': 'value_dev_3', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    status_dev_3: Optional[int] = field(
        default=None,
        metadata={'chn_name': '设备更新后状态_3', 'eng_name': 'status_dev_3', 'primary_key': False, 'data_type': 'int', 'disp_type': 'INT', 'disp_menu': ''},
    )

    refresh_time_dev_3: Optional[int] = field(
        default=None,
        metadata={'chn_name': '设备更新时间_3', 'eng_name': 'refresh_time_dev_3', 'primary_key': False, 'data_type': 'int', 'disp_type': '带毫秒的DATETIME', 'disp_menu': ''},
    )

    yt_rtu: Optional[int] = field(
        default=None,
        metadata={'chn_name': '遥调通道号', 'eng_name': 'yt_rtu', 'primary_key': False, 'data_type': 'int', 'disp_type': 'SHORT', 'disp_menu': ''},
    )

    dev_id: Optional[int] = field(
        default=None,
        metadata={'chn_name': '设备ID', 'eng_name': 'dev_id', 'primary_key': False, 'data_type': 'int', 'disp_type': 'KEYID类型', 'disp_menu': ''},
    )

__all__ = ['fes_yc_define']

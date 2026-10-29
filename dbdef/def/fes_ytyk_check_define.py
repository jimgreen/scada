# -*- coding: utf-8 -*-
"""Generated dataclass definition for the SCADA database table.

Table: fes_ytyk_check_define
Chinese name: 五防定义表

This file is generated from the adjacent JSON table definition.
Do not edit generated fields manually; regenerate from the JSON schema.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Optional


@dataclass
class fes_ytyk_check_define:
    """五防定义表。"""

    __tablename__: ClassVar[str] = 'fes_ytyk_check_define'
    __table_name_chn__: ClassVar[str] = '五防定义表'
    __primary_keys__: ClassVar[tuple[str, ...]] = ('id',)

    id: Optional[int] = field(
        default=None,
        metadata={'chn_name': 'ID', 'eng_name': 'id', 'primary_key': True, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': ''},
    )

    name: Optional[str] = field(
        default=None,
        metadata={'chn_name': '名称', 'eng_name': 'name', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    judge_count: Optional[int] = field(
        default=None,
        metadata={'chn_name': '判断数量', 'eng_name': 'judge_count', 'primary_key': False, 'data_type': 'int', 'disp_type': 'LONG', 'disp_menu': ''},
    )

    judge_app_name1: Optional[str] = field(
        default=None,
        metadata={'chn_name': '应用名1', 'eng_name': 'judge_app_name1', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    judge_keyid1: Optional[int] = field(
        default=None,
        metadata={'chn_name': '关键字ID1', 'eng_name': 'judge_keyid1', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    judge_min_val1: Optional[float] = field(
        default=None,
        metadata={'chn_name': '最小值1', 'eng_name': 'judge_min_val1', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    judge_max_val1: Optional[float] = field(
        default=None,
        metadata={'chn_name': '最大值1', 'eng_name': 'judge_max_val1', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    judge_app_name2: Optional[str] = field(
        default=None,
        metadata={'chn_name': '应用名2', 'eng_name': 'judge_app_name2', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    judge_keyid2: Optional[int] = field(
        default=None,
        metadata={'chn_name': '关键字ID2', 'eng_name': 'judge_keyid2', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    judge_min_val2: Optional[float] = field(
        default=None,
        metadata={'chn_name': '最小值2', 'eng_name': 'judge_min_val2', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    judge_max_val2: Optional[float] = field(
        default=None,
        metadata={'chn_name': '最大值2', 'eng_name': 'judge_max_val2', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    judge_app_name3: Optional[str] = field(
        default=None,
        metadata={'chn_name': '应用名3', 'eng_name': 'judge_app_name3', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    judge_keyid3: Optional[int] = field(
        default=None,
        metadata={'chn_name': '关键字ID3', 'eng_name': 'judge_keyid3', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    judge_min_val3: Optional[float] = field(
        default=None,
        metadata={'chn_name': '最小值3', 'eng_name': 'judge_min_val3', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    judge_max_val3: Optional[float] = field(
        default=None,
        metadata={'chn_name': '最大值3', 'eng_name': 'judge_max_val3', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    judge_app_name4: Optional[str] = field(
        default=None,
        metadata={'chn_name': '应用名4', 'eng_name': 'judge_app_name4', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    judge_keyid4: Optional[int] = field(
        default=None,
        metadata={'chn_name': '关键字ID4', 'eng_name': 'judge_keyid4', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    judge_min_val4: Optional[float] = field(
        default=None,
        metadata={'chn_name': '最小值4', 'eng_name': 'judge_min_val4', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    judge_max_val4: Optional[float] = field(
        default=None,
        metadata={'chn_name': '最大值4', 'eng_name': 'judge_max_val4', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    judge_app_name5: Optional[str] = field(
        default=None,
        metadata={'chn_name': '应用名5', 'eng_name': 'judge_app_name5', 'primary_key': False, 'data_type': 'str', 'disp_type': 'STRING', 'disp_menu': ''},
    )

    judge_keyid5: Optional[int] = field(
        default=None,
        metadata={'chn_name': '关键字ID5', 'eng_name': 'judge_keyid5', 'primary_key': False, 'data_type': 'int', 'disp_type': '带域号的KEYID', 'disp_menu': ''},
    )

    judge_min_val5: Optional[float] = field(
        default=None,
        metadata={'chn_name': '最小值5', 'eng_name': 'judge_min_val5', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

    judge_max_val5: Optional[float] = field(
        default=None,
        metadata={'chn_name': '最大值5', 'eng_name': 'judge_max_val5', 'primary_key': False, 'data_type': 'float', 'disp_type': 'FLOAT', 'disp_menu': ''},
    )

__all__ = ['fes_ytyk_check_define']

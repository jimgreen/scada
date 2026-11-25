"""设备宽表量测列布局、默认未绑定清单及量测状态常量定义。"""

# 旧版 8 列后缀（用于只读识别与存量库无损自动升级）
LEGACY_SUFFIXES = ("", "_qual", "_pre", "_time", "_notuse", "_weight", "_rtu", "_pnt")

# 现行 10 列后缀：新增上下限列 _max / _min
SUFFIXES = ("", "_qual", "_pre", "_time", "_notuse", "_weight", "_rtu", "_pnt", "_max", "_min")

# 遥控/遥调 9 列控调字段组后缀（均带 _cmd_ 前缀，与 YC/YX 完全隔离）
CTRL_SUFFIXES = (
    "",
    "_cmd_time",
    "_cmd_status",
    "_cmd_ack_time",
    "_cmd_notuse",
    "_cmd_rtu",
    "_cmd_pnt",
    "_cmd_max",
    "_cmd_min",
)

# 禁止新指标/控调名结尾使用的保留后缀（不含 _max/_min，避免与 battery_temp_max 等合法物理量冲突）
RESERVED_METRIC_SUFFIXES = (
    "_qual",
    "_pre",
    "_time",
    "_notuse",
    "_weight",
    "_rtu",
    "_pnt",
    "_cmd_time",
    "_cmd_status",
    "_cmd_ack_time",
    "_cmd_notuse",
    "_cmd_rtu",
    "_cmd_pnt",
    "_cmd_max",
    "_cmd_min",
)

# 量测值状态码（写入 <metric>_qual）
QUAL_OK = 0            # 正常
QUAL_UNCONFIGURED = 1  # 无配置（rtu=0/NULL/非法 或 pnt=0/NULL/非法）
QUAL_COMM_ERROR = 2    # 通信异常（无法从 scdfe_c 正常获取有效数据）
QUAL_OVER_MAX = 3      # 越上限（val > max 且 max > min）
QUAL_UNDER_MIN = 4     # 越下限（val < min 且 max > min）

# 控调指令执行状态码（写入 <control>_cmd_status）
CTRL_STATUS_IDLE = 0          # 空闲
CTRL_STATUS_PENDING = 1       # 待回执
CTRL_STATUS_SUCCESS = 2       # 执行成功
CTRL_STATUS_FAILED = 3        # 执行失败
CTRL_STATUS_UNCONFIGURED = 4  # 无配置
CTRL_STATUS_DISABLED = 5      # 控制禁用

import json

QUAL_LABELS = {
    QUAL_OK: "正常",
    QUAL_UNCONFIGURED: "无配置",
    QUAL_COMM_ERROR: "通信异常",
    QUAL_OVER_MAX: "越上限",
    QUAL_UNDER_MIN: "越下限",
}

CTRL_STATUS_LABELS = {
    CTRL_STATUS_IDLE: "空闲",
    CTRL_STATUS_PENDING: "待回执",
    CTRL_STATUS_SUCCESS: "执行成功",
    CTRL_STATUS_FAILED: "执行失败",
    CTRL_STATUS_UNCONFIGURED: "无配置",
    CTRL_STATUS_DISABLED: "控制禁用",
}

# RTDB 嵌入元数据中量测状态列（*_qual）与控调状态列（*_cmd_status）的 N 选 1 菜单显示配置
QUAL_DISP_TYPE = "菜单类型"
QUAL_DISP_MENU = json.dumps(
    {str(k): v for k, v in QUAL_LABELS.items()},
    ensure_ascii=False,
    separators=(",", ":"),
)

CTRL_STATUS_DISP_TYPE = "菜单类型"
CTRL_STATUS_DISP_MENU = json.dumps(
    {str(k): v for k, v in CTRL_STATUS_LABELS.items()},
    ensure_ascii=False,
    separators=(",", ":"),
)

STORAGE_METRICS = frozenset(
    {
        "soc",
        "soh",
        "battery_u",
        "battery_i",
        "battery_temp_min",
        "battery_temp_max",
        "bms_fault",
        "charging",
        "discharging",
        "soc_high",
        "soc_low",
    }
)
EXTENSION_METRICS = {
    "ACWindGen": frozenset({"wind_speed", "rotor_speed"}),
    "ACPVGen": frozenset({"irradiance", "module_temp"}),
    "DCPVGen": frozenset({"irradiance", "module_temp"}),
    "ACDieselGen": frozenset({"coolant_temp", "fuel_level", "oil_pressure", "speed"}),
    "ACStorageGen": STORAGE_METRICS,
    "DCStorageGen": STORAGE_METRICS,
}

# The current IEC 104 catalog has no hydrogen points. These fields are available
# for future bindings. Until then, value/time/RTU/point stay NULL and quality is invalid.
UNBOUND_PROFILES = {
    "HydroNode": (("pressure",), ("run",)),
    "HydroSource": (("pressure", "flow"), ("run", "fault")),
    "HydroLoad": (("pressure", "flow"), ("run",)),
    "HydroValve": (("i_pressure", "j_pressure", "flow"), ("open", "fault")),
    "HydroCompressor": (("i_pressure", "j_pressure", "flow", "p"), ("run", "fault")),
    "HydroStorage": (("pressure", "flow", "soc"), ("run", "fault")),
    "AcE2Hydro": (("p", "flow", "pressure"), ("run", "fault")),
    "Hydro2DcE": (("p", "flow", "pressure"), ("run", "fault")),
}

MEASUREMENT_ORDER = {
    name: i
    for i, name in enumerate(
        (
            "p",
            "q",
            "s",
            "pf",
            "v",
            "u",
            "uab",
            "ubc",
            "uca",
            "i",
            "ia",
            "ib",
            "ic",
            "f",
            "soc",
            "soh",
            "pressure",
            "flow",
            "position",
            "run",
            "fault",
        )
    )
}


def metric_order(item):
    metric, kind = item
    return (kind != "yc", MEASUREMENT_ORDER.get(metric, len(MEASUREMENT_ORDER)), metric)


def measurement_columns(metric, kind):
    """返回单个量测属性对应的 10 个宽表列定义（含上下限列）。"""
    value_type = "INTEGER" if kind == "yx" else "REAL"
    return (
        (metric, value_type, ""),
        (metric + "_qual", "INTEGER", f" DEFAULT {QUAL_UNCONFIGURED}"),
        (metric + "_pre", value_type, ""),
        (metric + "_time", "INTEGER", ""),
        (metric + "_notuse", "INTEGER", " DEFAULT 1"),
        (metric + "_weight", "REAL", " DEFAULT 1"),
        (metric + "_rtu", "INTEGER", ""),
        (metric + "_pnt", "INTEGER", ""),
        (metric + "_max", "REAL", ""),
        (metric + "_min", "REAL", ""),
    )


CONTROL_ORDER = {
    name: i
    for i, name in enumerate(
        (
            "run_stat",
            "status",
            "p_set",
            "q_set",
            "v_set",
            "i_set",
            "p_ac_set",
            "q_ac_set",
            "v_ac_set",
            "p_dc_set",
            "v_dc_set",
            "flow_set",
        )
    )
}

# 常用控调属性中文名及工程单位（前端展示与辅助提示）
CONTROL_META = {
    "run_stat": ("启停控制", ""),
    "status": ("开关分合", ""),
    "p_set": ("有功设定", "kW"),
    "q_set": ("无功设定", "kvar"),
    "v_set": ("电压设定", "V"),
    "i_set": ("电流设定", "A"),
    "p_ac_set": ("交流有功设定", "kW"),
    "q_ac_set": ("交流无功设定", "kvar"),
    "v_ac_set": ("交流电压设定", "V"),
    "p_dc_set": ("直流功率设定", "kW"),
    "v_dc_set": ("直流电压设定", "V"),
    "flow_set": ("流量设定", "Nm³/h"),
}


def control_order(item):
    control, kind = item
    return (kind != "yk", CONTROL_ORDER.get(control, len(CONTROL_ORDER)), control)


def control_columns(control, kind):
    """返回单个遥控/遥调属性对应的 9 个宽表列定义。"""
    value_type = "INTEGER" if kind == "yk" else "REAL"
    return (
        (control, value_type, ""),
        (control + "_cmd_time", "INTEGER", ""),
        (control + "_cmd_status", "INTEGER", f" NOT NULL DEFAULT {CTRL_STATUS_IDLE}"),
        (control + "_cmd_ack_time", "INTEGER", ""),
        (control + "_cmd_notuse", "INTEGER", " NOT NULL DEFAULT 0"),
        (control + "_cmd_rtu", "INTEGER", ""),
        (control + "_cmd_pnt", "INTEGER", ""),
        (control + "_cmd_max", "REAL", ""),
        (control + "_cmd_min", "REAL", ""),
    )

"""设备宽表量测列布局、默认未绑定清单及量测状态常量定义。"""

# 旧版 8 列后缀（用于只读识别与存量库无损自动升级）
LEGACY_SUFFIXES = ("", "_qual", "_pre", "_time", "_notuse", "_weight", "_rtu", "_pnt")

# 现行 10 列后缀：新增上下限列 _max / _min
SUFFIXES = ("", "_qual", "_pre", "_time", "_notuse", "_weight", "_rtu", "_pnt", "_max", "_min")

# 禁止新指标名结尾使用的保留后缀（不含 _max/_min，避免与 battery_temp_max 等合法物理量冲突）
RESERVED_METRIC_SUFFIXES = ("_qual", "_pre", "_time", "_notuse", "_weight", "_rtu", "_pnt")

# 量测值状态码（写入 <metric>_qual）
QUAL_OK = 0            # 正常
QUAL_UNCONFIGURED = 1  # 无配置（rtu=0/NULL/非法 或 pnt=0/NULL/非法）
QUAL_COMM_ERROR = 2    # 通信异常（无法从 scdfe_c 正常获取有效数据）
QUAL_OVER_MAX = 3      # 越上限（val > max 且 max > min）
QUAL_UNDER_MIN = 4     # 越下限（val < min 且 max > min）

import json

QUAL_LABELS = {
    QUAL_OK: "正常",
    QUAL_UNCONFIGURED: "无配置",
    QUAL_COMM_ERROR: "通信异常",
    QUAL_OVER_MAX: "越上限",
    QUAL_UNDER_MIN: "越下限",
}

# RTDB 嵌入元数据中量测状态列（*_qual）的 N 选 1 菜单显示配置
QUAL_DISP_TYPE = "菜单类型"
QUAL_DISP_MENU = json.dumps(
    {str(k): v for k, v in QUAL_LABELS.items()},
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

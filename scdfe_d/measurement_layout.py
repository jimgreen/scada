"""Measurement ownership and default layouts for model device classes."""

SUFFIXES = ("", "_qual", "_pre", "_time", "_notuse", "_weight", "_rtu", "_pnt")

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
    value_type = "INTEGER" if kind == "yx" else "REAL"
    return (
        (metric, value_type, ""),
        (metric + "_qual", "INTEGER", " DEFAULT 128"),
        (metric + "_pre", value_type, ""),
        (metric + "_time", "INTEGER", ""),
        (metric + "_notuse", "INTEGER", " DEFAULT 1"),
        (metric + "_weight", "REAL", " DEFAULT 1"),
        (metric + "_rtu", "INTEGER", ""),
        (metric + "_pnt", "INTEGER", ""),
    )

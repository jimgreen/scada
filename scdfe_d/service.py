"""直接以设备宽表为唯一数据源的实时量测采集与管理服务。

scdfe_d 数据库中不再创建 measurement_value、measurement_refresh、scada_point_mapping、
device_measurement、device 等冗余量测中间表；仅保留以设备类型为名的设备宽表与
RTDB 元数据表（rtdb_schema_*）。源 SCADA 数据库（默认同目录 scdfe_c.sqlite3）始终只读。
"""

from collections import defaultdict, deque
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import threading
import time
from types import SimpleNamespace

LOG = logging.getLogger("scdfe_d.service")
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1


def _strict_int64(value):
    """严格解析 64 位有符号整数（拒绝 bool、小数截断与超 64 位溢出）。"""
    if value is None or isinstance(value, bool):
        raise ValueError("not an int64")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("not an integer float")
        result = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty integer string")
        result = int(text, 10)
    else:
        raise ValueError("unsupported integer type")
    if not (_INT64_MIN <= result <= _INT64_MAX):
        raise OverflowError("int64 out of range")
    return result


def _same_value(a, b):
    """比较两个量测值是否相等（正确处理 NaN == NaN，避免每周期冗余写库）。"""
    if a is b:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
    return a == b

from core.rtdb_schema import RTDB_METADATA_TABLES, sync_embedded_schema
from core.scdfe_control import InstanceLock
from core.scdfe_logger import ScdfeLogger
from core.log_paths import log_entries
from core.log_query import LOG_PATTERNS, search_logs
from core.common import ApiError
from scdfe_d.measurement_layout import (
    CONTROL_META,
    CTRL_STATUS_DISABLED,
    CTRL_STATUS_DISP_MENU,
    CTRL_STATUS_DISP_TYPE,
    CTRL_STATUS_FAILED,
    CTRL_STATUS_IDLE,
    CTRL_STATUS_LABELS,
    CTRL_STATUS_PENDING,
    CTRL_STATUS_SUCCESS,
    CTRL_STATUS_UNCONFIGURED,
    CTRL_SUFFIXES,
    EXTENSION_METRICS,
    LEGACY_SUFFIXES,
    QUAL_COMM_ERROR,
    QUAL_DISP_MENU,
    QUAL_DISP_TYPE,
    QUAL_OK,
    QUAL_OVER_MAX,
    QUAL_UNCONFIGURED,
    QUAL_UNDER_MIN,
    RESERVED_METRIC_SUFFIXES,
    SUFFIXES,
    UNBOUND_PROFILES,
    control_columns,
    control_order,
    measurement_columns,
    metric_order,
)

APPLICATION_ID = int.from_bytes(b"SDFD", "big")
SCHEMA_USER_VERSION = 5
DEFAULT_INTERVAL_SECONDS = 5
MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 60
DEFAULT_MAX_AGE_SECONDS = 120.0
MAX_HISTORY_ITEMS = 100

CATEGORIES = {
    "generator": "发电机",
    "load": "负荷",
    "switch": "开关",
    "line": "线路",
    "bus": "母线",
    "storage": "储能",
    "converter": "变流器",
    "compensator": "无功补偿",
    "other": "其他设备",
}

EXTENSIONS = {
    "ACWindGen": ("ACGenerator", "idx_acgenerator"),
    "ACPVGen": ("ACGenerator", "idx_acgenerator"),
    "ACDieselGen": ("ACGenerator", "idx_acgenerator"),
    "ACStorageGen": ("ACGenerator", "idx_acgenerator"),
    "DCPVGen": ("DCGenerator", "idx_dcgenerator"),
    "DCStorageGen": ("DCGenerator", "idx_dcgenerator"),
}

# 旧版冗余辅助表：新架构中不再创建，也不再依赖，可在清理工具中直接删除。
LEGACY_AUX_TABLES = frozenset(
    {
        "measurement_value",
        "measurement_refresh",
        "scada_point_mapping",
        "device_measurement",
        "device",
        "input_change_log",
        "d_device_members",
        "d_device_tables",
        "d_settings",
    }
)

_TABLE_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9_]{0,63}$")
_METRIC_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def _parse_positive_int(value):
    """解析合法的正整数配置值（RTU/点号）；不合法或 <= 0 返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value < 2**63 else None
    if isinstance(value, float):
        if not math.isfinite(value) or int(value) != value:
            return None
        iv = int(value)
        return iv if 0 < iv < 2**63 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            iv = int(text)
        except ValueError:
            return None
        return iv if 0 < iv < 2**63 else None
    return None


def _is_valid_point_config(rtu, pnt):
    """校验量测点位配置是否有效（rtu 与 pnt 均须为正整数，0 或非法均判定为无配置）。"""
    return _parse_positive_int(rtu) is not None and _parse_positive_int(pnt) is not None


def _parse_limit_number(value):
    """解析上下限数值；NULL、非数字或非有限值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return num if math.isfinite(num) else None


def _check_limit_status(value, max_val, min_val):
    """根据量测上下限判定越限状态；若上限<=下限或任一未有效定义则判定限值无效返回 None。"""
    upper = _parse_limit_number(max_val)
    lower = _parse_limit_number(min_val)
    if upper is None or lower is None or upper <= lower:
        return None
    num = _parse_limit_number(value)
    if num is None:
        return None
    if num > upper:
        return QUAL_OVER_MAX, "over_upper_limit"
    if num < lower:
        return QUAL_UNDER_MIN, "under_lower_limit"
    return None


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def validate_interval(value):
    if (
        type(value) not in (int, float)
        or not MIN_INTERVAL_SECONDS <= value <= MAX_INTERVAL_SECONDS
        or int(value) != value
    ):
        raise ApiError(400, "采集周期必须为 1–60 秒的整数")
    return int(value)


def stored_interval(con_or_store):
    """兼容旧调用签名：优先读取内存周期，未绑定时返回默认周期。"""
    if isinstance(con_or_store, DeviceStore):
        return con_or_store.get_interval()
    return DEFAULT_INTERVAL_SECONDS


def category_for(table, model=None):
    kind = str((model or {}).get("dev_type", "")).lower()
    if "storage" in kind or "Storage" in table:
        return "storage"
    if isinstance(table, str) and table.startswith("Manual_"):
        suffix = table.removeprefix("Manual_")
        if suffix in CATEGORIES:
            return suffix
    for word, category in (
        ("Converter", "converter"),
        ("Generator", "generator"),
        ("Load", "load"),
        ("Break", "switch"),
        ("Branch", "line"),
        ("Node", "bus"),
        ("RealBs", "compensator"),
    ):
        if word in table:
            return category
    return "other"


def _quote_table(name):
    if not isinstance(name, str) or not _TABLE_NAME_RE.fullmatch(name):
        raise ValueError("模型设备表名称不合法")
    return '"' + name + '"'


def _quote_col(name):
    return '"' + name.replace('"', '""') + '"'


def _parse_device_table_layout(columns_info):
    """按列组顺序解析设备宽表（支持 10 列量测、旧版 8 列量测与 9 列控调混合共存）。

    成功返回字典：
        metrics: [(metric, 'yc'|'yx'), ...]
        controls: [(control, 'yk'|'yt'), ...]
        metric_offsets: {metric: col_index}
        control_offsets: {control: col_index}
        has_legacy: bool（是否包含旧版 8 列量测分组）
    结构非法时返回 None。
    """
    if len(columns_info) < 2:
        return None
    if columns_info[0][1] != "idx" or columns_info[1][1] != "name":
        return None
    if not columns_info[0][5]:
        return None
    rest = columns_info[2:]
    metrics = []
    controls = []
    metric_offsets = {}
    control_offsets = {}
    seen_names = set()
    has_legacy = False
    pos = 0
    total = len(rest)
    step_cur = len(SUFFIXES)
    step_legacy = len(LEGACY_SUFFIXES)
    step_ctrl = len(CTRL_SUFFIXES)

    while pos < total:
        base_col = rest[pos]
        attr = base_col[1]
        if (
            not _METRIC_NAME_RE.fullmatch(attr)
            or attr in {"idx", "name"}
            or attr in seen_names
            or attr.endswith(RESERVED_METRIC_SUFFIXES)
        ):
            return None
        if pos + 1 >= total:
            return None
        second_name = rest[pos + 1][1]
        col_type = str(base_col[2] or "").upper()
        if second_name == attr + "_qual":
            if pos + step_cur <= total and [
                r[1] for r in rest[pos : pos + step_cur]
            ] == [attr + s for s in SUFFIXES]:
                kind = "yx" if col_type == "INTEGER" else "yc"
                metrics.append((attr, kind))
                metric_offsets[attr] = 2 + pos
                seen_names.add(attr)
                pos += step_cur
            elif pos + step_legacy <= total and [
                r[1] for r in rest[pos : pos + step_legacy]
            ] == [attr + s for s in LEGACY_SUFFIXES]:
                kind = "yx" if col_type == "INTEGER" else "yc"
                metrics.append((attr, kind))
                metric_offsets[attr] = 2 + pos
                seen_names.add(attr)
                has_legacy = True
                pos += step_legacy
            else:
                return None
        elif second_name == attr + "_cmd_time":
            if pos + step_ctrl <= total and [
                r[1] for r in rest[pos : pos + step_ctrl]
            ] == [attr + s for s in CTRL_SUFFIXES]:
                kind = "yk" if col_type == "INTEGER" else "yt"
                controls.append((attr, kind))
                control_offsets[attr] = 2 + pos
                seen_names.add(attr)
                pos += step_ctrl
            else:
                return None
        else:
            return None

    return dict(
        metrics=metrics,
        controls=controls,
        metric_offsets=metric_offsets,
        control_offsets=control_offsets,
        has_legacy=has_legacy,
    )


def parse_device_table_schema(columns_info):
    """解析设备宽表，返回 (metrics, controls)，结构非法时返回 None。"""
    layout = _parse_device_table_layout(columns_info)
    if layout is None:
        return None
    return layout["metrics"], layout["controls"]


def parse_device_table_columns(columns_info):
    """校验并解析设备宽表量测列定义（兼容现行 10 列、旧版 8 列及含 9 列控调混合表），返回 [(metric, kind), ...]。"""
    layout = _parse_device_table_layout(columns_info)
    if layout is None:
        return None
    return layout["metrics"]


def table_has_limit_columns(columns_info):
    """检查设备宽表是否已采用含 _max/_min 的 10 列标准量测结构（兼容混合控调列）。"""
    layout = _parse_device_table_layout(columns_info)
    return layout is not None and not layout["has_legacy"]


class _DeviceSchemaCatalog:
    """为 RTDB 嵌入元数据提供量测状态列（*_qual）和控调状态列（*_cmd_status）的菜单选项显示提示。"""

    def __init__(self, table_layouts):
        self._layouts = {}
        for item in table_layouts:
            if len(item) == 2:
                self._layouts[item[0]] = (item[1], ())
            else:
                self._layouts[item[0]] = (item[1], item[2])

    def names(self):
        return list(self._layouts.keys())

    def get(self, table_name):
        metrics, controls = self._layouts.get(table_name, ((), ()))
        columns = [
            {
                "eng_name": f"{metric}_qual",
                "data_type": "int",
                "disp_type": QUAL_DISP_TYPE,
                "disp_menu": QUAL_DISP_MENU,
            }
            for metric, _ in metrics
        ]
        columns.extend(
            {
                "eng_name": f"{ctrl}_cmd_status",
                "data_type": "int",
                "disp_type": CTRL_STATUS_DISP_TYPE,
                "disp_menu": CTRL_STATUS_DISP_MENU,
            }
            for ctrl, _ in controls
        )
        return {"head": {}, "columns": columns}


def _sync_device_rtdb_schema(con):
    """同步全部设备宽表的 RTDB 嵌入元数据，确保所有量测与控调状态列配置为菜单类型（N选1）。"""
    discovered = discover_device_table_layouts(con)
    dev_tables = [t for t, *_ in discovered]
    catalog = _DeviceSchemaCatalog(discovered)
    sync_embedded_schema(con, catalog=catalog, include_tables=dev_tables)
    return dev_tables


# RTDB Web 编辑工具写入的伴随表前缀（不影响设备宽表语义，校验与发现时放行）。
RTDB_WEB_TABLE_PREFIX = "rtdb_web_"


def _is_rtdb_companion_table(name):
    return (
        name in RTDB_METADATA_TABLES
        or name in LEGACY_AUX_TABLES
        or name.startswith(RTDB_WEB_TABLE_PREFIX)
    )


def discover_device_table_layouts(con):
    """发现库内全部合法设备宽表及其完整列组布局。

    返回有序列表：[(table, metrics, controls, metric_offsets, control_offsets), ...]。
    """
    all_tables = [
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    result = []
    for table in all_tables:
        if (
            table.startswith("sqlite_")
            or _is_rtdb_companion_table(table)
            or not _TABLE_NAME_RE.fullmatch(table)
        ):
            continue
        info = con.execute(f'PRAGMA table_info("{table}")').fetchall()
        layout = _parse_device_table_layout(info)
        if layout is not None:
            result.append(
                (
                    table,
                    layout["metrics"],
                    layout["controls"],
                    layout["metric_offsets"],
                    layout["control_offsets"],
                )
            )
    return result


def discover_device_tables(con):
    """发现库内全部合法设备宽表，返回有序列表 [(table_name, [(metric, kind), ...]), ...]。"""
    return [(table, metrics) for table, metrics, _, _, _ in discover_device_table_layouts(con)]


def validate_database(path):
    """只读校验 scdfe_d 设备数据库：仅允许合法设备宽表与 RTDB 元数据/伴随表。"""
    try:
        path = Path(path).resolve()
        if not path.is_file():
            raise ValueError("设备数据库文件不存在")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as con:
            con.execute("PRAGMA busy_timeout=10000")
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("数据库完整性检查失败")
            app_id = con.execute("PRAGMA application_id").fetchone()[0]
            if app_id not in (0, APPLICATION_ID):
                raise ValueError("不是 scdfe_d 设备数据库")
            raw_tables = [
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
                if not r[0].startswith("sqlite_")
            ]
            # 拦截 scdfe_c 采集业务库、连接记录库等非设备库
            if {"_scada_schema", "fes_rtu_define"} & set(raw_tables) or {
                "fes_yc_define",
                "fes_yx_define",
            } & set(raw_tables):
                raise ValueError("不是 scdfe_d 设备数据库")

            device_tables = []
            for table in raw_tables:
                if _is_rtdb_companion_table(table):
                    continue
                if not _TABLE_NAME_RE.fullmatch(table):
                    raise ValueError(f"包含非设备表：{table}")
                info = con.execute(f'PRAGMA table_info("{table}")').fetchall()
                metrics = parse_device_table_columns(info)
                if metrics is None:
                    raise ValueError(f"设备表结构不合法：{table}")
                device_tables.append((table, metrics))

            if app_id == 0 and not device_tables:
                raise ValueError("数据库中没有合法设备表")

            device_count = 0
            metric_slots = 0
            bound_count = 0
            for table, metrics in device_tables:
                quoted = _quote_table(table)
                cnt = con.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
                device_count += cnt
                metric_slots += cnt * len(metrics)
                if cnt and metrics:
                    expr = " + ".join(
                        f'(CASE WHEN {_quote_col(m + "_rtu")} > 0 AND {_quote_col(m + "_pnt")} > 0 THEN 1 ELSE 0 END)'
                        for m, _ in metrics
                    )
                    bound_count += con.execute(
                        f"SELECT coalesce(sum({expr}), 0) FROM {quoted}"
                    ).fetchone()[0]

            counts = {
                "device_tables": len(device_tables),
                "device": device_count,
                "device_measurement": bound_count if bound_count else metric_slots,
                "scada_point_mapping": bound_count,
            }
        return dict(valid=True, message="设备数据库校验通过", counts=counts)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        return dict(valid=False, message=f"无效的设备数据库：{exc}")


def drop_legacy_tables(db_path, *, vacuum=True):
    """清理旧版 scdfe_d 库中的冗余辅助表与同步触发器，仅保留设备宽表与 RTDB 元数据表。"""
    path = Path(db_path).resolve()
    removed_tables = []
    removed_triggers = []
    with closing(sqlite3.connect(path, timeout=30)) as con:
        con.execute("PRAGMA foreign_keys=OFF")
        with con:
            con.execute("BEGIN IMMEDIATE")
            triggers = [
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
                if r[0].startswith("d_binding_")
            ]
            for trig in triggers:
                con.execute(f'DROP TRIGGER IF EXISTS "{trig.replace(chr(34), chr(34) * 2)}"')
                removed_triggers.append(trig)
            for category in CATEGORIES:
                con.execute(f'DROP VIEW IF EXISTS "device_{category}"')
            existing = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in (
                "measurement_value",
                "measurement_refresh",
                "scada_point_mapping",
                "device_measurement",
                "d_device_members",
                "d_device_tables",
                "device",
                "input_change_log",
                "d_settings",
            ):
                if table in existing:
                    con.execute(f'DROP TABLE "{table}"')
                    removed_tables.append(table)
            dev_tables = _sync_device_rtdb_schema(con)
            con.execute(f"PRAGMA application_id={APPLICATION_ID}")
            con.execute(f"PRAGMA user_version={SCHEMA_USER_VERSION}")
        if vacuum:
            con.execute("VACUUM")
    return dict(
        removed_tables=removed_tables,
        removed_triggers=len(removed_triggers),
        device_tables=dev_tables,
    )


class DeviceStore:
    def __init__(
        self,
        db_path,
        schema_dir=None,
        *,
        workspace_root=None,
        connection_pool_size=0,
        source_database=None,
        interval_seconds=DEFAULT_INTERVAL_SECONDS,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
    ):
        self.db_path = Path(db_path).resolve()
        self.root = Path(workspace_root or self.db_path.parent).resolve()
        if not self.db_path.is_relative_to(self.root):
            raise ValueError("设备数据库不能超出工作区根目录")
        self.catalog = SimpleNamespace(directory=schema_dir)
        self._mutex = threading.RLock()
        self.connection_pool_size = max(0, int(connection_pool_size))
        self._connection_pool = []
        self._pool_closed = False
        self._config_epoch = 0
        self._interval_seconds = int(validate_interval(interval_seconds))
        self._max_age_seconds = float(max_age_seconds)
        self._source_database = (
            str(source_database).strip() if source_database else None
        )
        # 确定性内存 ID 映射与运行时缓存（不写额外辅助表）
        self._device_ids = {}
        self._id_to_device = {}
        self._next_device_id = 1
        self._meas_ids = {}
        self._id_to_meas = {}
        self._next_meas_id = 1
        self._versions = {}
        self._ctrl_ids = {}
        self._id_to_ctrl = {}
        self._next_ctrl_id = 1
        self._ctrl_versions = {}
        self._runtime_meta = {}  # (table, idx, metric) -> {valid, reason, collected_at}
        self._last_bound_points = {}  # (table, idx, metric) -> (rtu_id, pnt_no)
        self._refresh_seq = 0
        self._refresh_history = deque(maxlen=MAX_HISTORY_ITEMS)
        self._change_history = deque(maxlen=MAX_HISTORY_ITEMS)
        self._control_seq = 0
        self._control_history = deque(maxlen=MAX_HISTORY_ITEMS)
        self._control_baseline = {}  # (table, idx, control) -> (value, cmd_time)
        self._control_baseline_ready = False
        self._pending_controls = {}  # (table, idx, control) -> dict(...)
        self._control_ack_timeout = 15.0
        self._control_poll_interval = 0.25
        self._log_lock = threading.Lock()
        self._control_logger = None
        self._collector_logger = None
        self.log_dir = self.db_path.parent / "logs"
        self._schema_cache = None  # (schema_version, layouts)
        self._overview_cache = None  # (cache_key, overview_dict)

        existing = self.db_path.is_file() and self.db_path.stat().st_size > 0
        if existing:
            result = validate_database(self.db_path)
            if not result["valid"]:
                raise ValueError(result["message"])
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._open_connection()) as init_con:
            mode = str(
                init_con.execute("PRAGMA journal_mode").fetchone()[0] or ""
            ).lower()
            if mode != "wal":
                init_con.execute("PRAGMA journal_mode=WAL")
            if not existing or not self._schema_ready(init_con):
                with init_con:
                    init_con.execute("BEGIN IMMEDIATE")
                    self._upgrade_legacy_device_tables(init_con)
                    _sync_device_rtdb_schema(init_con)
                    init_con.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    init_con.execute(f"PRAGMA user_version={SCHEMA_USER_VERSION}")

    @staticmethod
    def _upgrade_legacy_device_tables(con):
        """将旧版 8 列设备宽表无损升级为含 _max/_min 的 10 列标准分组结构，同时原样保留控调列组。"""
        upgraded = []
        for table, metrics, controls, _, _ in discover_device_table_layouts(con):
            quoted = _quote_table(table)
            info = con.execute(f"PRAGMA table_info({quoted})").fetchall()
            if table_has_limit_columns(info):
                continue
            existing_col_names = {r[1] for r in info}
            keep_cols = ["idx", "name"]
            defs = ["idx INTEGER PRIMARY KEY", "name TEXT NOT NULL"]
            for metric, kind in metrics:
                for col, col_type, default in measurement_columns(metric, kind):
                    defs.append(f'"{col}" {col_type}{default}')
                    if col in existing_col_names:
                        keep_cols.append(col)
            for ctrl, kind in controls:
                for col, col_type, default in control_columns(ctrl, kind):
                    defs.append(f'"{col}" {col_type}{default}')
                    if col in existing_col_names:
                        keep_cols.append(col)
            tmp_name = f"__upgrade_limits_{table}"
            tmp_quoted = f'"{tmp_name}"'
            cols_sql = ", ".join(_quote_col(c) for c in keep_cols)
            con.execute(f"CREATE TABLE {tmp_quoted} ({', '.join(defs)})")
            con.execute(
                f"INSERT INTO {tmp_quoted}({cols_sql}) SELECT {cols_sql} FROM {quoted}"
            )
            con.execute(f"DROP TABLE {quoted}")
            con.execute(f"ALTER TABLE {tmp_quoted} RENAME TO {quoted}")
            upgraded.append(table)
        return upgraded

    @staticmethod
    def _schema_ready(con):
        if con.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            return False
        if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_USER_VERSION:
            return False
        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not (RTDB_METADATA_TABLES <= tables):
            return False
        for table, _ in discover_device_tables(con):
            info = con.execute(f"PRAGMA table_info({_quote_table(table)})").fetchall()
            if not table_has_limit_columns(info):
                return False
        bad_qual_meta = con.execute(
            "SELECT 1 FROM rtdb_schema_columns "
            "WHERE eng_name LIKE '%\\_qual' ESCAPE '\\' "
            "AND (disp_type != ? OR disp_menu != ?) LIMIT 1",
            (QUAL_DISP_TYPE, QUAL_DISP_MENU),
        ).fetchone()
        if bad_qual_meta is not None:
            return False
        bad_ctrl_meta = con.execute(
            "SELECT 1 FROM rtdb_schema_columns "
            "WHERE eng_name LIKE '%\\_cmd\\_status' ESCAPE '\\' "
            "AND (disp_type != ? OR disp_menu != ?) LIMIT 1",
            (CTRL_STATUS_DISP_TYPE, CTRL_STATUS_DISP_MENU),
        ).fetchone()
        if bad_ctrl_meta is not None:
            return False
        return True

    def _open_connection(self):
        con = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA temp_store=MEMORY")
            con.execute("PRAGMA cache_size=-16384")
        except BaseException:
            con.close()
            raise
        return con

    @contextmanager
    def connection(self, *, _writer=None):
        if _writer is not None:
            with _writer:
                yield _writer
            return
        with self._mutex:
            use_pool = self.connection_pool_size > 0 and not self._pool_closed
            con = (
                self._connection_pool.pop()
                if (use_pool and self._connection_pool)
                else None
            )
        if not use_pool:
            with closing(self._open_connection()) as fresh, fresh:
                yield fresh
            return
        if con is None:
            con = self._open_connection()
        healthy = True
        try:
            with con:
                yield con
        except BaseException:
            healthy = False
            raise
        finally:
            reused = False
            if healthy:
                with self._mutex:
                    if (
                        not self._pool_closed
                        and len(self._connection_pool) < self.connection_pool_size
                    ):
                        self._connection_pool.append(con)
                        reused = True
            if not reused:
                con.close()

    def close(self):
        with self._mutex:
            self._pool_closed = True
            while self._connection_pool:
                self._connection_pool.pop().close()

    @staticmethod
    def _table_name(name):
        return _quote_table(name)

    def bump_config_epoch(self):
        with self._mutex:
            self._config_epoch += 1
            self._schema_cache = None

    def get_interval(self):
        with self._mutex:
            return self._interval_seconds

    def set_interval_value(self, value):
        interval = validate_interval(value)
        with self._mutex:
            previous = self._interval_seconds
            self._interval_seconds = interval
            if interval != previous:
                self._record_change(
                    "collector_interval",
                    None,
                    {"interval_seconds": previous},
                    {"interval_seconds": interval},
                )
        return interval

    def default_source_database(self):
        """解析默认源 FES 采集库（优先显式配置，其次同级 ../fes/scdfe_c.sqlite3，再其次同目录 scdfe_c.sqlite3）。"""
        with self._mutex:
            explicit = self._source_database
        if explicit:
            return explicit
        candidates = (
            self.db_path.parent.parent / "fes" / "scdfe_c.sqlite3",
            self.db_path.parent / "scdfe_c.sqlite3",
            self.db_path.parent / "c.sqlite3",
        )
        for raw in candidates:
            path = raw.resolve()
            if path.is_file() and path.is_relative_to(self.root):
                return path.relative_to(self.root).as_posix()
        if self.db_path.parent.name.lower() == "scada":
            fallback = (self.db_path.parent.parent / "fes" / "scdfe_c.sqlite3").resolve()
        else:
            fallback = (self.db_path.parent / "scdfe_c.sqlite3").resolve()
        if fallback.is_relative_to(self.root):
            return fallback.relative_to(self.root).as_posix()
        return "scdfe_c.sqlite3"

    def set_source_database(self, relative_path):
        if relative_path:
            self.source_path(relative_path)
            with self._mutex:
                self._source_database = relative_path
                self._config_epoch += 1

    def _record_change(self, entity, entity_id, before, after):
        with self._mutex:
            change_id = len(self._change_history) + 1
            if self._change_history:
                change_id = self._change_history[0]["change_id"] + 1
            self._change_history.appendleft(
                dict(
                    change_id=change_id,
                    changed_at=utc_now(),
                    entity=entity,
                    entity_id=entity_id,
                    before_json=json.dumps(before, ensure_ascii=False),
                    after_json=json.dumps(after, ensure_ascii=False),
                )
            )

    def _audit(self, _con, entity, key, before, after):
        self._record_change(entity, key, before, after)

    def _get_table_layouts(self, con):
        """按 schema_version 缓存所有设备宽表完整布局 [(table, metrics, controls, metric_offsets, control_offsets), ...]。"""
        schema_ver = con.execute("PRAGMA schema_version").fetchone()[0]
        with self._mutex:
            if self._schema_cache is not None and self._schema_cache[0] == schema_ver:
                return self._schema_cache[1]
        layouts = discover_device_table_layouts(con)
        with self._mutex:
            self._schema_cache = (schema_ver, layouts)
        return layouts

    def _get_table_schemas(self, con):
        """兼容旧调用：返回 [(table, [(metric, kind), ...]), ...]。"""
        return [(tbl, metrics) for tbl, metrics, _, _, _ in self._get_table_layouts(con)]

    def table_metrics(self, con, table):
        for tbl, metrics, _, _, _ in self._get_table_layouts(con):
            if tbl == table:
                return metrics
        return None

    def table_controls(self, con, table):
        for tbl, _, controls, _, _ in self._get_table_layouts(con):
            if tbl == table:
                return controls
        return None

    def table_column_types(self, con, table):
        metrics = self.table_metrics(con, table)
        if metrics is None:
            return {}
        return {
            metric: ("INTEGER" if kind == "yx" else "REAL")
            for metric, kind in metrics
        }

    def device_id_for(self, table, idx, *, con=None):
        key = (table, int(idx))
        with self._mutex:
            did = self._device_ids.get(key)
            if did is not None:
                return did
            empty = not self._device_ids
        if empty and con is not None:
            self._prime_device_ids(con)
        with self._mutex:
            did = self._device_ids.get(key)
            if did is None:
                did = self._next_device_id
                self._next_device_id += 1
                self._device_ids[key] = did
                self._id_to_device[did] = key
            return did

    def resolve_device_id(self, con, device_id):
        did = int(device_id)
        with self._mutex:
            target = self._id_to_device.get(did)
        if target is not None:
            return target
        # 若尚未初始化索引（例如服务重启后直调），扫描一次表建立确定性映射
        self._prime_device_ids(con)
        with self._mutex:
            return self._id_to_device.get(did)

    def measurement_id_for(self, table, idx, metric, *, con=None):
        key = (table, int(idx), str(metric))
        with self._mutex:
            mid = self._meas_ids.get(key)
            if mid is not None:
                return mid
            empty = not self._meas_ids
        if empty and con is not None:
            self._prime_measurement_ids(con)
        with self._mutex:
            mid = self._meas_ids.get(key)
            if mid is None:
                mid = self._next_meas_id
                self._next_meas_id += 1
                self._meas_ids[key] = mid
                self._id_to_meas[mid] = key
            return mid

    def resolve_measurement_id(self, con, measurement_id):
        mid = int(measurement_id)
        with self._mutex:
            target = self._id_to_meas.get(mid)
        if target is not None:
            return target
        self._prime_measurement_ids(con)
        with self._mutex:
            return self._id_to_meas.get(mid)

    def version_for(self, table, idx, metric):
        key = (table, int(idx), str(metric))
        with self._mutex:
            return self._versions.get(key, 1)

    def bump_version(self, table, idx, metric):
        key = (table, int(idx), str(metric))
        with self._mutex:
            nxt = self._versions.get(key, 1) + 1
            self._versions[key] = nxt
            return nxt

    def runtime_meta_for(self, table, idx, metric):
        with self._mutex:
            return self._runtime_meta.get((table, int(idx), str(metric)))

    def set_runtime_meta(self, table, idx, metric, *, valid, reason, collected_at=None):
        with self._mutex:
            self._runtime_meta[(table, int(idx), str(metric))] = {
                "valid": int(bool(valid)),
                "reason": str(reason or ""),
                "collected_at": collected_at,
            }

    def _prime_device_ids(self, con):
        cur = con.cursor()
        cur.row_factory = None
        for table, _ in self._get_table_schemas(con):
            for (idx,) in cur.execute(
                f"SELECT idx FROM {_quote_table(table)} ORDER BY idx"
            ):
                self.device_id_for(table, idx)

    def _prime_measurement_ids(self, con):
        cur = con.cursor()
        cur.row_factory = None
        for table, metrics in self._get_table_schemas(con):
            if not metrics:
                continue
            for (idx,) in cur.execute(
                f"SELECT idx FROM {_quote_table(table)} ORDER BY idx"
            ):
                for metric, _ in metrics:
                    self.measurement_id_for(table, idx, metric)

    @staticmethod
    def _infer_status(value, qual, stamp, notuse, rtu, pnt, cached_meta=None):
        """根据设备宽表字段与内存采集元信息推导 valid 与状态原因码。"""
        if not _is_valid_point_config(rtu, pnt):
            reason = "unmapped" if (rtu is None and pnt is None) else "invalid_config"
            return 0, reason, None
        if cached_meta is not None:
            return (
                cached_meta["valid"],
                cached_meta["reason"],
                cached_meta["collected_at"],
            )
        if notuse:
            return 0, "notuse", None
        if qual is not None:
            q_int = int(qual)
            if q_int == QUAL_UNCONFIGURED:
                return 0, "invalid_config", None
            if q_int == QUAL_COMM_ERROR:
                return 0, "comm_error", None
            if q_int == QUAL_OVER_MAX:
                return 0, "over_upper_limit", None
            if q_int == QUAL_UNDER_MIN:
                return 0, "under_lower_limit", None
            if q_int == QUAL_OK:
                if value is None and (stamp is None or stamp <= 0):
                    return 0, "uncollected", None
                return 1, "", None
            return 0, "bad_quality", None
        if value is None and (stamp is None or stamp <= 0):
            return 0, "uncollected", None
        return 0, "bad_quality", None

    def source_path(self, value):
        if (
            not isinstance(value, str)
            or not value
            or "\\" in value
            or ":" in value
            or "\0" in value
            or PureWindowsPath(value).drive
            or PurePosixPath(value).is_absolute()
            or any(
                p in ("", ".", "..") or p.endswith((" ", ".")) for p in value.split("/")
            )
        ):
            raise ApiError(400, "源数据库必须使用工作区根目录内的相对路径")
        path = (self.root / value).resolve()
        if not path.is_relative_to(self.root) or path == self.db_path:
            raise ApiError(400, "源数据库路径越界或指向设备库自身")
        return path

    @contextmanager
    def source(self, value=None):
        source_name = value or self.default_source_database()
        path = self.source_path(source_name)
        try:
            with closing(
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
            ) as con:
                con.row_factory = sqlite3.Row
                if con.execute("PRAGMA application_id").fetchone()[0] != 0:
                    raise ApiError(400, "请选择 scdfe_c 的采集业务数据库")
                for kind in ("yc", "yx"):
                    columns = {
                        r[1]
                        for r in con.execute(f"PRAGMA table_info(fes_{kind}_define)")
                    }
                    if not {
                        "rtu_id",
                        "pnt_no",
                        "name",
                        "value",
                        "status",
                        "refresh_time",
                    } <= columns:
                        raise ApiError(400, "源库缺少遥测/遥信点表或必要字段")
                con.execute("BEGIN")
                yield con
        except sqlite3.Error as exc:
            raise ApiError(400, "源数据库不存在、不可读取或结构不合法") from exc

    @staticmethod
    def _source_values(connection, kind, keys, *, pk_cache=None):
        if not keys:
            return {}
        table = f"fes_{kind}_define"
        columns = "rtu_id,pnt_no,value,status,refresh_time"
        params = ()
        sql = f"SELECT {columns} FROM {table}"
        has_pk = pk_cache.get(table) if pk_cache is not None else None
        if has_pk is None:
            primary_key = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table})")
                if row[5]
            }
            has_pk = primary_key == {"rtu_id", "pnt_no"}
            if pk_cache is not None:
                pk_cache[table] = has_pk
        if has_pk:
            # 统一按 256 对点分批走 (rtu_id, pnt_no) 联合主键索引，避免大点集退化全表扫描
            cur = connection.cursor()
            cur.row_factory = None
            snapshot = {}
            sorted_keys = sorted(keys)
            qualified = ",".join(f"p.{field}" for field in columns.split(","))
            batch_size = 256
            for offset in range(0, len(sorted_keys), batch_size):
                chunk = sorted_keys[offset : offset + batch_size]
                wanted = ",".join("(?,?)" for _ in chunk)
                chunk_params = tuple(value for pair in chunk for value in pair)
                chunk_sql = (
                    f"WITH wanted(rtu_id,pnt_no) AS (VALUES {wanted}) "
                    f"SELECT {qualified} FROM wanted CROSS JOIN {table} p "
                    "ON p.rtu_id=wanted.rtu_id AND p.pnt_no=wanted.pnt_no"
                )
                for rtu_id, pnt_no, value, status, stamp in cur.execute(chunk_sql, chunk_params):
                    snapshot[(rtu_id, pnt_no)] = (value, status, stamp)
            return snapshot
        cur = connection.cursor()
        cur.row_factory = None
        snapshot = {}
        seen = set()
        for rtu_id, pnt_no, value, status, stamp in cur.execute(sql, params):
            key = (rtu_id, pnt_no)
            if key in seen:
                raise ApiError(400, "源测点主键重复")
            seen.add(key)
            if key in keys:
                snapshot[key] = (value, status, stamp)
        return snapshot

    def _read_cached_source(self, source_name, kinds, source_cache):
        path = self.source_path(source_name)
        try:
            st = path.stat()
        except OSError as exc:
            entry = source_cache.pop(source_name, None)
            if entry is not None:
                entry[0].close()
            raise ApiError(400, "源数据库不存在、不可读取或结构不合法") from exc
        identity = (st.st_dev, st.st_ino)
        entry = source_cache.get(source_name)
        if entry is not None and entry[1] != identity:
            entry[0].close()
            source_cache.pop(source_name, None)
            entry = None
        try:
            if entry is None:
                con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
                try:
                    con.row_factory = sqlite3.Row
                    con.execute("PRAGMA temp_store=MEMORY")
                    if con.execute("PRAGMA application_id").fetchone()[0] != 0:
                        raise ApiError(400, "请选择 scdfe_c 的采集业务数据库")
                    pk_cache = {}
                    for kind in ("yc", "yx"):
                        table = f"fes_{kind}_define"
                        info = con.execute(f"PRAGMA table_info({table})").fetchall()
                        columns = {r[1] for r in info}
                        if not {
                            "rtu_id",
                            "pnt_no",
                            "name",
                            "value",
                            "status",
                            "refresh_time",
                        } <= columns:
                            raise ApiError(400, "源库缺少遥测/遥信点表或必要字段")
                        pk_cache[table] = {r[1] for r in info if r[5]} == {
                            "rtu_id",
                            "pnt_no",
                        }
                    schema_version = con.execute("PRAGMA schema_version").fetchone()[0]
                    entry = [con, identity, schema_version, pk_cache, None, {}]
                    source_cache[source_name] = entry
                except BaseException:
                    con.close()
                    raise
            (
                con,
                _,
                schema_version,
                pk_cache,
                cached_data_version,
                cached_snapshots,
            ) = entry
            con.execute("BEGIN")
            try:
                current_schema = con.execute("PRAGMA schema_version").fetchone()[0]
                if current_schema != schema_version:
                    pk_cache.clear()
                    cached_snapshots.clear()
                    for kind in ("yc", "yx"):
                        table = f"fes_{kind}_define"
                        info = con.execute(f"PRAGMA table_info({table})").fetchall()
                        columns = {r[1] for r in info}
                        if not {
                            "rtu_id",
                            "pnt_no",
                            "name",
                            "value",
                            "status",
                            "refresh_time",
                        } <= columns:
                            raise ApiError(400, "源库缺少遥测/遥信点表或必要字段")
                        pk_cache[table] = {r[1] for r in info if r[5]} == {
                            "rtu_id",
                            "pnt_no",
                        }
                    entry[2] = current_schema
                current_data_version = con.execute("PRAGMA data_version").fetchone()[0]
                if current_data_version != cached_data_version:
                    cached_snapshots.clear()
                    entry[4] = current_data_version
                result = {}
                for kind, keys in kinds.items():
                    cached = cached_snapshots.get(kind)
                    if cached is not None:
                        cached_keys, cached_snap = cached
                        if keys == cached_keys:
                            result[kind] = cached_snap
                            continue
                        if keys <= cached_keys:
                            result[kind] = {
                                k: cached_snap[k] for k in keys if k in cached_snap
                            }
                            continue
                    snap = self._source_values(con, kind, keys, pk_cache=pk_cache)
                    cached_snapshots[kind] = (frozenset(keys), snap)
                    result[kind] = snap
                return result
            finally:
                con.rollback()
        except ApiError:
            cached = source_cache.pop(source_name, None)
            if cached is not None:
                cached[0].close()
            raise
        except sqlite3.Error as exc:
            cached = source_cache.pop(source_name, None)
            if cached is not None:
                cached[0].close()
            raise ApiError(400, "源数据库不存在、不可读取或结构不合法") from exc

    def _scan_bindings(self, con):
        """扫描全部设备宽表的全部量测槽位（含无配置槽位），提取配置、当前值与上下限。"""
        layouts = self._get_table_layouts(con)
        cur = con.cursor()
        cur.row_factory = None
        bindings = []
        requested_sets = defaultdict(set)
        for table, metrics, _, metric_offsets, _ in layouts:
            if not metrics:
                continue
            quoted = _quote_table(table)
            for row in cur.execute(f"SELECT * FROM {quoted}"):
                idx = row[0]
                for metric, kind in metrics:
                    base = metric_offsets[metric]
                    rtu_raw = row[base + 6]
                    pnt_raw = row[base + 7]
                    max_val = row[base + 8]
                    min_val = row[base + 9]
                    rtu_i = _parse_positive_int(rtu_raw)
                    pnt_i = _parse_positive_int(pnt_raw)
                    bindings.append(
                        (
                            table,
                            idx,
                            metric,
                            kind,
                            rtu_raw,
                            pnt_raw,
                            rtu_i,
                            pnt_i,
                            1 if row[base + 4] else 0,
                            row[base],
                            row[base + 1],
                            row[base + 2],
                            row[base + 3],
                            max_val,
                            min_val,
                        )
                    )
                    if rtu_i is not None and pnt_i is not None:
                        requested_sets[kind].add((rtu_i, pnt_i))
        requested = {k: frozenset(pts) for k, pts in requested_sets.items() if pts}
        return bindings, requested

    def collect_once(self, *, _writer=None, _source_cache=None, _plan_cache=None):
        """根据量测配置、源库通信状态及上下限，实时更新设备宽表量测值与状态码（0/1/2/3/4）。"""
        started = utc_now()
        t_start = time.perf_counter()
        source_name = self.default_source_database()

        # 第一阶段：获取各设备表全部量测槽位与当前值（命中 data_version 缓存时零 SQL 扫描）。
        bindings = None
        requested_kinds = None
        plan_key = None
        epoch_before = self._config_epoch
        if _writer is not None and _plan_cache is not None:
            data_ver = _writer.execute("PRAGMA data_version").fetchone()[0]
            plan_key = (data_ver, epoch_before, source_name)
            if _plan_cache.get("key") == plan_key:
                bindings = _plan_cache["bindings"]
                requested_kinds = _plan_cache["requested"]
        if bindings is None:
            with self.connection(_writer=_writer) as read_con:
                read_con.execute("BEGIN")
                try:
                    data_ver = read_con.execute("PRAGMA data_version").fetchone()[0]
                    epoch_before = self._config_epoch
                    bindings, requested_kinds = self._scan_bindings(read_con)
                    plan_key = (data_ver, epoch_before, source_name)
                finally:
                    read_con.rollback()
            if _plan_cache is not None and plan_key is not None:
                _plan_cache["key"] = plan_key
                _plan_cache["bindings"] = bindings
                _plan_cache["requested"] = requested_kinds

        if _source_cache is not None:
            active_sources = {source_name} if requested_kinds else set()
            for stale in [key for key in _source_cache if key not in active_sources]:
                _source_cache.pop(stale)[0].close()

        # 第二阶段：无本地写锁状态下读取外部源库（scdfe_c.sqlite3）快照。
        source_points = {}
        errors = {}
        if requested_kinds:
            try:
                if _source_cache is not None:
                    source_points = self._read_cached_source(
                        source_name, requested_kinds, _source_cache
                    )
                else:
                    with self.source(source_name) as src:
                        source_points = {
                            kind: self._source_values(src, kind, keys)
                            for kind, keys in requested_kinds.items()
                        }
            except (ApiError, OSError, ValueError) as exc:
                errors[source_name] = str(exc)

        now_ms = int(time.time() * 1000)
        future_limit = now_ms + 5000
        max_age_ms = self._max_age_seconds * 1000.0
        collected_at = utc_now()
        source_failed = source_name in errors

        def evaluate(current_bindings):
            configured_cnt = 0
            valid_cnt = 0
            changes_by_row = defaultdict(dict)
            next_bindings = []
            runtime_updates = {}
            isfinite = math.isfinite
            for entry in current_bindings:
                (
                    table,
                    idx,
                    metric,
                    kind,
                    rtu_raw,
                    pnt_raw,
                    rtu_id,
                    pnt_no,
                    notuse,
                    old_val,
                    old_qual,
                    old_pre,
                    old_time,
                    max_val,
                    min_val,
                ) = entry
                cell_key = (table, idx, metric)
                prev_binding = self._last_bound_points.get(cell_key)

                # 1. 量测无配置（rtu=0/NULL/非法 或 pnt=0/NULL/非法） -> 状态 1
                if rtu_id is None or pnt_no is None:
                    if prev_binding is not None:
                        val = None
                        pre = None
                        stamp = None
                    else:
                        val = old_val
                        pre = old_pre
                        stamp = old_time
                    out_qual = QUAL_UNCONFIGURED
                    reason = (
                        "unmapped"
                        if (rtu_raw is None and pnt_raw is None)
                        else "invalid_config"
                    )
                    runtime_updates[cell_key] = {
                        "valid": 0,
                        "reason": reason,
                        "collected_at": collected_at,
                    }
                    if (
                        val != old_val
                        or out_qual != old_qual
                        or pre != old_pre
                        or stamp != old_time
                    ):
                        row_updates = changes_by_row[(table, idx)]
                        if val != old_val:
                            row_updates[metric] = val
                        if out_qual != old_qual:
                            row_updates[metric + "_qual"] = out_qual
                        if pre != old_pre:
                            row_updates[metric + "_pre"] = pre
                        if stamp != old_time:
                            row_updates[metric + "_time"] = stamp
                        next_bindings.append(
                            (
                                table,
                                idx,
                                metric,
                                kind,
                                rtu_raw,
                                pnt_raw,
                                rtu_id,
                                pnt_no,
                                notuse,
                                val,
                                out_qual,
                                pre,
                                stamp,
                                max_val,
                                min_val,
                            )
                        )
                    else:
                        next_bindings.append(entry)
                    continue

                configured_cnt += 1
                rebound = prev_binding is not None and prev_binding != (rtu_id, pnt_no)
                if rebound:
                    val = None
                    pre = None
                    stamp = None
                else:
                    val = old_val
                    pre = old_pre
                    stamp = old_time

                # 2. 量测通信异常（无法从 scdfe_c 正常获取有效数据） -> 状态 2
                reason = ""
                out_qual = QUAL_OK
                if source_failed:
                    reason = "source_error"
                    out_qual = QUAL_COMM_ERROR
                else:
                    pts = source_points.get(kind)
                    point = pts.get((rtu_id, pnt_no)) if pts else None
                    if point is None:
                        reason = "missing_point"
                        out_qual = QUAL_COMM_ERROR
                    else:
                        try:
                            new_raw = float(point[0])
                            new_qual = _strict_int64(point[1])
                            new_stamp = _strict_int64(point[2])
                            if not isfinite(new_raw) or not 0 <= new_qual <= 255:
                                raise ValueError("invalid sample")
                            if kind == "yx":
                                if new_raw not in (0, 1):
                                    raise ValueError("invalid status")
                                new_val = int(new_raw)
                            else:
                                new_val = new_raw
                            if new_qual:
                                reason = "bad_quality"
                                out_qual = QUAL_COMM_ERROR
                            elif new_stamp <= 0:
                                reason = "missing_time"
                                out_qual = QUAL_COMM_ERROR
                            elif new_stamp > future_limit:
                                reason = "future_time"
                                out_qual = QUAL_COMM_ERROR
                            else:
                                # 仅在质量正常且时标有效时更新宽表当前值与前次值，防止异常脏样本污染历史可信值
                                if (
                                    not rebound
                                    and (old_val is not None or old_time is not None)
                                    and (new_stamp != old_time or not _same_value(new_val, old_val))
                                ):
                                    pre = old_val
                                val = new_val
                                stamp = new_stamp
                                if now_ms - stamp > max_age_ms:
                                    reason = "stale"
                                    out_qual = QUAL_COMM_ERROR
                                else:
                                    # 3/4. 通信正常时按上下限判定越限（上限<=下限时判定上下限无效）
                                    limit_hit = _check_limit_status(val, max_val, min_val)
                                    if limit_hit is not None:
                                        out_qual, reason = limit_hit
                                    else:
                                        out_qual = QUAL_OK
                        except (ValueError, TypeError, OverflowError):
                            reason = "invalid_value"
                            out_qual = QUAL_COMM_ERROR

                if not reason and notuse:
                    reason = "notuse"
                valid = 0 if reason else 1
                valid_cnt += valid
                runtime_updates[cell_key] = {
                    "valid": valid,
                    "reason": reason,
                    "collected_at": collected_at,
                }
                if (
                    not _same_value(val, old_val)
                    or out_qual != old_qual
                    or not _same_value(pre, old_pre)
                    or stamp != old_time
                ):
                    row_updates = changes_by_row[(table, idx)]
                    if not _same_value(val, old_val):
                        row_updates[metric] = val
                    if out_qual != old_qual:
                        row_updates[metric + "_qual"] = out_qual
                    if not _same_value(pre, old_pre):
                        row_updates[metric + "_pre"] = pre
                    if stamp != old_time:
                        row_updates[metric + "_time"] = stamp
                    next_bindings.append(
                        (
                            table,
                            idx,
                            metric,
                            kind,
                            rtu_raw,
                            pnt_raw,
                            rtu_id,
                            pnt_no,
                            notuse,
                            val,
                            out_qual,
                            pre,
                            stamp,
                            max_val,
                            min_val,
                        )
                    )
                else:
                    next_bindings.append(entry)
            return configured_cnt, valid_cnt, changes_by_row, next_bindings, runtime_updates

        total_count, valid_count, changes, next_bindings, runtime_updates = evaluate(bindings)
        skipped_stale_write = False

        # 第三阶段：仅当存在设备字段变化时开启写事务，直接按设备表批量 UPDATE。
        if changes:
            try:
                with self.connection(_writer=_writer) as con:
                    con.execute("BEGIN IMMEDIATE")
                    # 若在读源库期间有外部或并发请求修改了设备库，重新扫描设备表后再计算差异写入。
                    if plan_key is not None:
                        cur_ver = con.execute("PRAGMA data_version").fetchone()[0]
                        if (cur_ver, self._config_epoch, source_name) != plan_key:
                            new_bindings, new_requested = self._scan_bindings(con)
                            # 若并发新增/改绑了本轮未读取的源点，不可复用旧源快照误写 missing_point；
                            # 清空计划缓存并跳过本轮缓存回填与状态覆写，留待下一周期使用最新快照写入。
                            unfetched = any(
                                not new_requested[k].issubset(requested_kinds.get(k, set()))
                                for k in new_requested
                            )
                            if unfetched:
                                if _plan_cache is not None:
                                    _plan_cache.clear()
                                changes = {}
                                skipped_stale_write = True
                            else:
                                bindings, requested_kinds = new_bindings, new_requested
                                (
                                    total_count,
                                    valid_count,
                                    changes,
                                    next_bindings,
                                    runtime_updates,
                                ) = evaluate(bindings)
                                plan_key = (cur_ver, self._config_epoch, source_name)
                    if changes:
                        self._write_device_updates(con, changes)
            except BaseException:
                if _plan_cache is not None:
                    _plan_cache.clear()
                raise

        if not skipped_stale_write and _plan_cache is not None and plan_key is not None:
            _plan_cache["key"] = plan_key
            _plan_cache["bindings"] = next_bindings
            _plan_cache["requested"] = requested_kinds

        finished = utc_now()
        duration_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
        with self._mutex:
            # 仅当本轮未触发并发跳写且无配置版本跃迁时才更新内存快照，防止旧快照覆盖并发修改
            if not skipped_stale_write and (
                plan_key is None or self._config_epoch == plan_key[1]
            ):
                self._runtime_meta = runtime_updates
                self._last_bound_points = {
                    (e[0], e[1], e[2]): (e[6], e[7])
                    for e in next_bindings
                    if e[6] is not None and e[7] is not None
                }
            self._refresh_seq += 1
            self._overview_cache = None
            refresh_id = self._refresh_seq
            self._refresh_history.appendleft(
                dict(
                    refresh_id=refresh_id,
                    started_at=started,
                    finished_at=finished,
                    duration_ms=duration_ms,
                    total_count=total_count,
                    valid_count=valid_count,
                    se_valid_count=0,
                    source_database=json.dumps(
                        [source_name] if requested_kinds else [], ensure_ascii=False
                    ),
                    error=json.dumps(errors, ensure_ascii=False) if errors else "",
                )
            )
        return dict(
            refresh_id=refresh_id,
            total_count=total_count,
            valid_count=valid_count,
            updated_rows=len(changes),
            duration_ms=duration_ms,
            errors=errors,
        )

    def _write_device_updates(self, con, changes):
        """按 (设备表, 字段组合) 分组批量 UPDATE 对应设备表的字段。"""
        grouped = defaultdict(list)
        for (table, idx), fields in changes.items():
            cols = tuple(fields.keys())
            grouped[(table, cols)].append((*fields.values(), idx))
        for (table, cols), rows in grouped.items():
            assignment = ",".join(f"{_quote_col(col)}=?" for col in cols)
            con.executemany(
                f"UPDATE {_quote_table(table)} SET {assignment} WHERE idx=?",
                rows,
            )

    def source_points(self, query):
        kind = query.get("kind", "yc")
        if kind not in ("yc", "yx", "yk", "yt"):
            raise ApiError(400, "仅支持遥测 YC、遥信 YX、遥控 YK、遥调 YT")
        where, args = [], []
        if query.get("rtu_id"):
            where.append("rtu_id=?")
            args.append(int(query["rtu_id"]))
        if query.get("q"):
            where.append("instr(lower(name),lower(?))>0")
            args.append(query["q"])
        clause = " WHERE " + " AND ".join(where) if where else ""
        page = max(1, int(query.get("page", 1)))
        with self.source(query.get("source_database") or None) as con:
            table = f"fes_{kind}_define"
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if kind in ("yk", "yt"):
                if not {"rtu_id", "pnt_no", "name", "value", "status", "cmd_time", "cmd_handle_time"} <= cols:
                    raise ApiError(400, f"源库缺少 {table} 表或必要控调字段")
                select_fields = ["rtu_id", "pnt_no", "name", "value", "status", "cmd_time", "cmd_handle_time"]
                if "man_disable" in cols:
                    select_fields.append("man_disable")
                if "auto_disable" in cols:
                    select_fields.append("auto_disable")
                if "eng_max" in cols:
                    select_fields.append("eng_max")
                if "eng_min" in cols:
                    select_fields.append("eng_min")
                select_sql = ",".join(select_fields)
            else:
                select_sql = "rtu_id,pnt_no,name,value,status,refresh_time"
            total = con.execute(
                f"SELECT count(*) FROM {table}" + clause, args
            ).fetchone()[0]
            rows = con.execute(
                f"SELECT {select_sql} FROM {table}"
                + clause
                + " ORDER BY rtu_id,pnt_no LIMIT 50 OFFSET ?",
                args + [(page - 1) * 50],
            ).fetchall()
        return dict(items=[dict(r) for r in rows], total=total, page=page, limit=50)

    def _ensure_device_table(
        self, con, table, initial_metrics=(), *, initial_controls=(), sync_schema=True
    ):
        """确保设备宽表存在并包含指定的量测列组（10 列）及控调列组（9 列）。"""
        quoted = _quote_table(table)
        existing = con.execute(f"PRAGMA table_info({quoted})").fetchall()
        schema_changed = False
        if not existing:
            metrics = sorted(initial_metrics, key=metric_order)
            controls = sorted(initial_controls, key=control_order)
            m_names = {m for m, _ in metrics}
            c_names = {c for c, _ in controls}
            if m_names & c_names:
                raise ApiError(400, f"同一设备表的量测与控调属性名不能冲突：{table}")
            defs = ["idx INTEGER PRIMARY KEY", "name TEXT NOT NULL"]
            for metric, kind in metrics:
                for col, col_type, default in measurement_columns(metric, kind):
                    defs.append(f'"{col}" {col_type}{default}')
            for ctrl, kind in controls:
                for col, col_type, default in control_columns(ctrl, kind):
                    defs.append(f'"{col}" {col_type}{default}')
            con.execute(f"CREATE TABLE {quoted} ({', '.join(defs)})")
            schema_changed = True
        else:
            existing_cols = {r[1]: str(r[2] or "").upper() for r in existing}
            for metric, kind in sorted(initial_metrics, key=metric_order):
                if metric + "_cmd_time" in existing_cols:
                    raise ApiError(400, f"属性名已被控调字段占用：{table}.{metric}")
                if metric in existing_cols:
                    expected_type = "INTEGER" if kind == "yx" else "REAL"
                    if existing_cols[metric] != expected_type:
                        raise ApiError(400, f"同一设备表的量测类型必须一致：{table}.{metric}")
                    continue
                for col, col_type, default in measurement_columns(metric, kind):
                    con.execute(f'ALTER TABLE {quoted} ADD COLUMN "{col}" {col_type}{default}')
                    existing_cols[col] = col_type
                schema_changed = True
            for ctrl, kind in sorted(initial_controls, key=control_order):
                if ctrl + "_qual" in existing_cols:
                    raise ApiError(400, f"属性名已被量测字段占用：{table}.{ctrl}")
                if ctrl in existing_cols:
                    expected_type = "INTEGER" if kind == "yk" else "REAL"
                    if existing_cols[ctrl] != expected_type:
                        raise ApiError(400, f"同一设备表的控调类型必须一致：{table}.{ctrl}")
                    continue
                for col, col_type, default in control_columns(ctrl, kind):
                    con.execute(f'ALTER TABLE {quoted} ADD COLUMN "{col}" {col_type}{default}')
                    existing_cols[col] = col_type
                schema_changed = True
        if schema_changed and sync_schema:
            _sync_device_rtdb_schema(con)
        return schema_changed

    def create_device(self, data):
        raw_name = data.get("name")
        if not isinstance(raw_name, str):
            raise ApiError(400, "请填写设备名称并选择设备类型")
        name, category = raw_name.strip(), data.get("category")
        if not name or len(name) > 200 or category not in CATEGORIES:
            raise ApiError(400, "请填写设备名称并选择设备类型")
        table = "Manual_" + category
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            self._ensure_device_table(con, table)
            quoted = _quote_table(table)
            idx = con.execute(
                f"SELECT coalesce(max(idx), 0) + 1 FROM {quoted}"
            ).fetchone()[0]
            con.execute(f"INSERT INTO {quoted}(idx, name) VALUES(?, ?)", (idx, name))
        device_id = self.device_id_for(table, idx)
        result = dict(
            device_id=device_id,
            model_table=table,
            model_idx=idx,
            name=name,
            category=category,
            active=1,
        )
        self._record_change("device", device_id, {}, result)
        self.bump_config_epoch()
        return result

    def import_model(self, relative_path):
        """从模型库直接导入设备表及设备行，保留已有设备宽表的量测列与绑定。"""
        path = self.source_path(relative_path)
        model = {}
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as src:
                src.execute("PRAGMA temp_store=MEMORY")
                src.execute("BEGIN")
                # 一次性加载全部列元数据，避免按表 N+1 查询 rtdb_schema_columns
                cols_by_table = defaultdict(list)
                for tbl_name, ordinal, eng_name in src.execute(
                    "SELECT table_name, ordinal, eng_name FROM rtdb_schema_columns ORDER BY table_name, ordinal"
                ):
                    cols_by_table[tbl_name].append((ordinal, eng_name))
                for (table,) in src.execute(
                    "SELECT table_name FROM rtdb_schema_tables ORDER BY ordinal"
                ):
                    if table in ("Model", "basevoltage", "HydroMedium"):
                        continue
                    fields = cols_by_table.get(table, ())
                    keys = [r[1] for r in fields]
                    if "idx" not in keys or (
                        "name" not in keys and table not in EXTENSIONS
                    ):
                        continue
                    self._table_name(table)
                    columns = ",".join('"c' + str(int(i)) + '"' for i, _ in fields)
                    model[table] = [
                        dict(zip(keys, row))
                        for row in src.execute(
                            f'SELECT {columns} FROM "rtdb_data_{table}"'
                        )
                    ]
        except (sqlite3.Error, OSError) as exc:
            raise ApiError(400, "源文件不是有效的设备模型库") from exc
        if not model:
            raise ApiError(400, "模型库没有含 idx/name 的设备定义")

        parent_names = {}
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            for table, items in model.items():
                if table in EXTENSIONS:
                    continue
                profile = UNBOUND_PROFILES.get(table)
                initial = []
                if profile:
                    initial.extend((m, "yc") for m in profile[0])
                    initial.extend((m, "yx") for m in profile[1])
                self._ensure_device_table(con, table, initial, sync_schema=False)
                quoted = _quote_table(table)
                rows = []
                for item in items:
                    idx = int(item["idx"])
                    name = "" if item["name"] is None else str(item["name"])
                    rows.append((idx, name))
                    parent_names[(table, idx)] = name
                if rows:
                    con.executemany(
                        f"INSERT INTO {quoted}(idx, name) VALUES(?, ?) "
                        "ON CONFLICT(idx) DO UPDATE SET name=excluded.name WHERE name IS NOT excluded.name",
                        rows,
                    )
            for table, (parent, key) in EXTENSIONS.items():
                items = model.get(table)
                if not items:
                    continue
                self._ensure_device_table(con, table, sync_schema=False)
                quoted = _quote_table(table)
                # 若父表在库中已有旧数据，补充查找父名称
                parent_quoted = _quote_table(parent)
                if con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (parent,),
                ).fetchone():
                    for p_idx, p_name in con.execute(
                        f"SELECT idx, name FROM {parent_quoted}"
                    ):
                        parent_names.setdefault((parent, int(p_idx)), p_name)
                rows = []
                for item in items:
                    owner_name = parent_names.get((parent, int(item[key])))
                    if owner_name is None:
                        raise ApiError(400, f"{table} 中的设备关联不存在")
                    rows.append((int(item["idx"]), owner_name))
                if rows:
                    con.executemany(
                        f"INSERT INTO {quoted}(idx, name) VALUES(?, ?) "
                        "ON CONFLICT(idx) DO UPDATE SET name=excluded.name WHERE name IS NOT excluded.name",
                        rows,
                    )
            _sync_device_rtdb_schema(con)
        self._record_change(
            "model_import",
            None,
            {},
            dict(path=relative_path, tables=len(model)),
        )
        self.bump_config_epoch()
        return dict(
            tables=len(model),
            devices=sum(len(v) for k, v in model.items() if k not in EXTENSIONS),
        )

    def device_tables(self):
        with self.connection() as con:
            schemas = self._get_table_schemas(con)
            return [
                dict(
                    table_name=table,
                    count=con.execute(
                        f"SELECT count(*) FROM {_quote_table(table)}"
                    ).fetchone()[0],
                )
                for table, _ in schemas
            ]

    def device_values(self, device_id):
        with self.connection() as con:
            target = self.resolve_device_id(con, device_id)
            if target is None:
                raise ApiError(404, "设备不存在")
            table, idx = target
            row = con.execute(
                f"SELECT * FROM {_quote_table(table)} WHERE idx=?", (idx,)
            ).fetchone()
            if row is None:
                raise ApiError(404, "设备不存在")
            return dict(row)

    def devices(self, query):
        cat_filter = query.get("category") or ""
        kw = (query.get("q") or "").strip().lower()
        items = []
        with self._mutex:
            meta_snapshot = dict(self._runtime_meta)
        with self.connection() as con:
            con.execute("BEGIN")
            cur = con.cursor()
            cur.row_factory = None
            for table, metrics, _, metric_offsets, _ in self._get_table_layouts(con):
                cat = category_for(table)
                if cat_filter and cat != cat_filter:
                    continue
                quoted = _quote_table(table)
                for row in cur.execute(f"SELECT * FROM {quoted} ORDER BY idx"):
                    idx, name = row[0], row[1]
                    if kw and kw not in str(name or "").lower():
                        continue
                    did = self.device_id_for(table, idx)
                    bound_cnt = 0
                    valid_cnt = 0
                    for metric, _ in metrics:
                        base = metric_offsets[metric]
                        rtu, pnt = row[base + 6], row[base + 7]
                        if _is_valid_point_config(rtu, pnt):
                            bound_cnt += 1
                            meta = meta_snapshot.get((table, int(idx), str(metric)))
                            valid, _, _ = self._infer_status(
                                row[base],
                                row[base + 1],
                                row[base + 3],
                                row[base + 4],
                                rtu,
                                pnt,
                                meta,
                            )
                            valid_cnt += valid
                    items.append(
                        dict(
                            device_id=did,
                            model_table=table,
                            model_idx=idx,
                            name=name,
                            category=cat,
                            active=1,
                            measurement_count=bound_cnt,
                            valid_count=valid_cnt,
                            tables=[dict(table=table, idx=idx)],
                        )
                    )
        return items

    def measurements(self, query):
        filter_device = int(query["device_id"]) if query.get("device_id") else None
        filter_kind = query.get("kind") or ""
        filter_cat = query.get("category") or ""
        filter_table = query.get("table") or ""
        kw = (query.get("q") or "").strip().lower()
        only_invalid = query.get("invalid") == "1"
        page = max(1, int(query.get("page", 1)))
        limit = min(200, max(1, int(query.get("limit", 50))))
        source_db = self.default_source_database()

        start = (page - 1) * limit
        end = start + limit
        page_items = []
        total = 0
        with self._mutex:
            meta_snapshot = dict(self._runtime_meta)
            version_snapshot = dict(self._versions)
        with self.connection() as con:
            con.execute("BEGIN")
            target_dev = (
                self.resolve_device_id(con, filter_device)
                if filter_device is not None
                else None
            )
            if filter_device is not None and target_dev is None:
                return dict(items=[], total=0, page=page, limit=limit)
            cur = con.cursor()
            cur.row_factory = None
            for table, metrics, _, metric_offsets, _ in self._get_table_layouts(con):
                if filter_table and table != filter_table:
                    continue
                if target_dev is not None and table != target_dev[0]:
                    continue
                cat = category_for(table)
                if filter_cat and cat != filter_cat:
                    continue
                if not metrics:
                    continue
                quoted = _quote_table(table)
                if target_dev is not None:
                    rows_iter = cur.execute(
                        f"SELECT * FROM {quoted} WHERE idx=?", (target_dev[1],)
                    )
                else:
                    rows_iter = cur.execute(f"SELECT * FROM {quoted} ORDER BY idx")
                for row in rows_iter:
                    idx, dev_name = row[0], row[1]
                    dev_name_lower = str(dev_name).lower() if kw else ""
                    did = None
                    for metric, kind in metrics:
                        base = metric_offsets[metric]
                        rtu, pnt = row[base + 6], row[base + 7]
                        if _is_valid_point_config(rtu, pnt):
                            if filter_kind and kind != filter_kind:
                                continue
                            if kw and kw not in metric.lower() and kw not in dev_name_lower:
                                continue
                            val = row[base]
                            qual = row[base + 1]
                            stamp = row[base + 3]
                            notuse = 1 if row[base + 4] else 0
                            meta = meta_snapshot.get((table, int(idx), str(metric)))
                            valid, reason, collected_at = self._infer_status(
                                val, qual, stamp, notuse, rtu, pnt, meta
                            )
                            if only_invalid and valid:
                                continue
                            # 仅对落入当前分页窗口的条目构造完整字典，避免全量物化数万条记录。
                            if start <= total < end:
                                if did is None:
                                    did = self.device_id_for(table, idx)
                                pre = row[base + 2]
                                weight = row[base + 5]
                                max_val = row[base + 8]
                                min_val = row[base + 9]
                                mid = self.measurement_id_for(table, idx, metric)
                                page_items.append(
                                    dict(
                                        measurement_id=mid,
                                        device_id=did,
                                        metric=metric,
                                        kind=kind,
                                        name=metric,
                                        unit="",
                                        weight=weight if weight is not None else 1.0,
                                        max_age_seconds=self._max_age_seconds,
                                        notuse=notuse,
                                        se_enabled=0,
                                        quantity_table=table,
                                        version=version_snapshot.get((table, int(idx), str(metric)), 1),
                                        device_name=dev_name,
                                        category=cat,
                                        source_database=source_db,
                                        rtu_id=rtu,
                                        pnt_no=pnt,
                                        max_value=max_val,
                                        min_value=min_val,
                                        model_table=table,
                                        model_idx=idx,
                                        enabled=1,
                                        scale=1.0,
                                        offset=0.0,
                                        value=val,
                                        source_value=val,
                                        previous_value=pre,
                                        quality=qual,
                                        source_time_ms=stamp,
                                        collected_at=collected_at,
                                        valid=valid,
                                        reason=reason,
                                    )
                                )
                            total += 1
        return dict(
            items=page_items,
            total=total,
            page=page,
            limit=limit,
        )

    def save_measurement(self, data, measurement_id=None):
        """直接在对应设备宽表中新增/更新量测字段映射（不再写任何中间映射表）。"""
        metric = data.get("metric", "")
        kind = data.get("kind", "yc")
        if (
            not isinstance(metric, str)
            or not _METRIC_NAME_RE.fullmatch(metric)
            or kind not in ("yc", "yx")
        ):
            raise ApiError(400, "属性名须为小写英文字母/数字/下划线，点类型须为 YC/YX")
        if metric in ("idx", "name") or metric.endswith(RESERVED_METRIC_SUFFIXES):
            raise ApiError(400, "属性名不能使用量测元数据后缀")

        source = data.get("source_database") or self.default_source_database()
        rtu = _parse_positive_int(data.get("rtu_id"))
        point = _parse_positive_int(data.get("pnt_no"))
        if rtu is None or point is None:
            raise ApiError(400, "RTU 编号和点号必须为正整数")
        raw_weight = data.get("weight", 1.0)
        if isinstance(raw_weight, bool):
            raise ApiError(400, "量测权重必须为正数")
        weight = float(raw_weight)
        raw_notuse = data.get("notuse", 0)
        raw_enabled = data.get("enabled", 1)
        if isinstance(raw_notuse, bool) or isinstance(raw_enabled, bool):
            raise ApiError(400, "启用和禁用标记只能为 0 或 1")
        notuse = int(raw_notuse)
        enabled = int(raw_enabled)
        has_max = "max_value" in data
        has_min = "min_value" in data
        raw_max = data.get("max_value")
        raw_min = data.get("min_value")
        max_val = _parse_limit_number(raw_max) if has_max else None
        min_val = _parse_limit_number(raw_min) if has_min else None
        if has_max and raw_max is not None and str(raw_max).strip() != "" and max_val is None:
            raise ApiError(400, "量测上限必须为有限数值或留空")
        if has_min and raw_min is not None and str(raw_min).strip() != "" and min_val is None:
            raise ApiError(400, "量测下限必须为有限数值或留空")
        if not math.isfinite(weight) or weight <= 0:
            raise ApiError(400, "量测权重必须为正数")
        if notuse not in (0, 1) or enabled not in (0, 1):
            raise ApiError(400, "启用和禁用标记只能为 0 或 1")
        if not enabled:
            notuse = 1

        # 校验源测点在源 SCADA 库中确实存在
        with self.source(source) as src_con:
            found = src_con.execute(
                f"SELECT name FROM fes_{kind}_define WHERE rtu_id=? AND pnt_no=?",
                (rtu, point),
            ).fetchall()
            if len(found) != 1:
                raise ApiError(400, "指定的源测点不存在或主键不唯一")

        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            old_target = None
            if measurement_id is not None:
                old_target = self.resolve_measurement_id(con, measurement_id)
                if old_target is None:
                    raise ApiError(404, "属性不存在")
                cur_ver = self.version_for(*old_target)
                if "version" in data:
                    req_ver = data["version"]
                    if type(req_ver) is not int or req_ver < 1:
                        raise ApiError(400, "版本号必须为正整数")
                    if req_ver != cur_ver:
                        raise ApiError(409, "定义已被修改，请刷新后重试")

            if data.get("table") and data.get("idx") is not None:
                table, idx = str(data["table"]), _strict_int64(data["idx"])
                self._table_name(table)
                if self.table_metrics(con, table) is None:
                    raise ApiError(404, "目标设备表不存在")
            elif data.get("device_id") is not None:
                resolved = self.resolve_device_id(con, data["device_id"])
                if resolved is None:
                    raise ApiError(400, "目标设备不存在")
                table, idx = resolved
            elif old_target is not None:
                table, idx, _ = old_target
            else:
                raise ApiError(400, "目标设备不存在")

            quoted = _quote_table(table)
            row = con.execute(f"SELECT 1 FROM {quoted} WHERE idx=?", (idx,)).fetchone()
            if row is None:
                raise ApiError(400, "目标设备不存在")

            # 若编辑时更换了设备或属性名，先清空原属性绑定
            if old_target is not None and old_target != (table, idx, metric):
                old_tbl, old_idx, old_metric = old_target
                self._clear_binding_cell(con, old_tbl, old_idx, old_metric)

            # 若设备表尚无此量测列组，通过 ALTER TABLE 自动追加 10 列
            self._ensure_device_table(con, table, [(metric, kind)])
            # 重新读取当前行的该字段旧值
            cur_vals = con.execute(
                f"SELECT {_quote_col(metric + '_rtu')}, {_quote_col(metric + '_pnt')}, {_quote_col(metric + '_qual')}, "
                f"{_quote_col(metric + '_max')}, {_quote_col(metric + '_min')}, {_quote_col(metric)} FROM {quoted} WHERE idx=?",
                (idx,),
            ).fetchone()
            existing_bound = _is_valid_point_config(cur_vals[0], cur_vals[1])
            final_max = max_val if has_max else _parse_limit_number(cur_vals[3])
            final_min = min_val if has_min else _parse_limit_number(cur_vals[4])
            if (
                (has_max or has_min)
                and final_max is not None
                and final_min is not None
                and float(final_max) <= float(final_min)
            ):
                raise ApiError(400, "量测上限必须大于下限，无法修改")
            point_changed = cur_vals[0] != rtu or cur_vals[1] != point
            recomputed_reason = "definition_changed"
            recomputed_valid = 0
            if point_changed:
                con.execute(
                    f"""UPDATE {quoted} SET
                        {_quote_col(metric)}=NULL,
                        {_quote_col(metric + '_qual')}=?,
                        {_quote_col(metric + '_pre')}=NULL,
                        {_quote_col(metric + '_time')}=NULL,
                        {_quote_col(metric + '_notuse')}=?,
                        {_quote_col(metric + '_weight')}=?,
                        {_quote_col(metric + '_rtu')}=?,
                        {_quote_col(metric + '_pnt')}=?,
                        {_quote_col(metric + '_max')}=?,
                        {_quote_col(metric + '_min')}=?
                    WHERE idx=?""",
                    (
                        QUAL_COMM_ERROR,
                        notuse,
                        weight,
                        rtu,
                        point,
                        final_max,
                        final_min,
                        idx,
                    ),
                )
            else:
                new_qual = cur_vals[2]
                cur_val = cur_vals[5]
                if new_qual is None or new_qual == QUAL_UNCONFIGURED:
                    new_qual = QUAL_COMM_ERROR
                elif new_qual in (QUAL_OK, QUAL_OVER_MAX, QUAL_UNDER_MIN) and cur_val is not None:
                    limit_hit = _check_limit_status(cur_val, final_max, final_min)
                    if limit_hit is not None:
                        new_qual, recomputed_reason = limit_hit
                        recomputed_valid = 0
                    else:
                        new_qual = QUAL_OK
                        recomputed_reason = "notuse" if notuse else ""
                        recomputed_valid = 0 if notuse else 1
                con.execute(
                    f"""UPDATE {quoted} SET
                        {_quote_col(metric + '_qual')}=?,
                        {_quote_col(metric + '_notuse')}=?,
                        {_quote_col(metric + '_weight')}=?,
                        {_quote_col(metric + '_max')}=?,
                        {_quote_col(metric + '_min')}=?
                    WHERE idx=?""",
                    (new_qual, notuse, weight, final_max, final_min, idx),
                )
            mid = self.measurement_id_for(table, idx, metric)
            did = self.device_id_for(table, idx, con=con)
        # 仅在 SQLite 事务提交成功后更新源库路径、版本号、运行时元数据与变更日志
        if data.get("source_database"):
            self.set_source_database(source)
        if old_target is not None and old_target != (table, idx, metric):
            with self._mutex:
                self._runtime_meta.pop(old_target, None)
        if measurement_id is None and not existing_bound:
            ver = self.version_for(table, idx, metric)
        else:
            ver = self.bump_version(table, idx, metric)
        self.set_runtime_meta(
            table,
            idx,
            metric,
            valid=recomputed_valid,
            reason=recomputed_reason,
            collected_at=None if point_changed else utc_now(),
        )
        after = dict(
            measurement_id=mid,
            device_id=did,
            model_table=table,
            model_idx=idx,
            metric=metric,
            kind=kind,
            name=metric,
            unit="",
            weight=weight,
            max_age_seconds=self._max_age_seconds,
            notuse=notuse,
            se_enabled=0,
            source_database=source,
            rtu_id=rtu,
            pnt_no=point,
            max_value=final_max,
            min_value=final_min,
            enabled=0 if notuse else 1,
            scale=1.0,
            offset=0.0,
            version=ver,
        )
        self._record_change("mapping", mid, {}, after)
        self.bump_config_epoch()
        return after

    def _clear_binding_cell(self, con, table, idx, metric):
        quoted = _quote_table(table)
        con.execute(
            f"""UPDATE {quoted} SET
                {_quote_col(metric)}=NULL,
                {_quote_col(metric + '_qual')}=?,
                {_quote_col(metric + '_pre')}=NULL,
                {_quote_col(metric + '_time')}=NULL,
                {_quote_col(metric + '_notuse')}=1,
                {_quote_col(metric + '_weight')}=1,
                {_quote_col(metric + '_rtu')}=NULL,
                {_quote_col(metric + '_pnt')}=NULL,
                {_quote_col(metric + '_max')}=NULL,
                {_quote_col(metric + '_min')}=NULL
            WHERE idx=?""",
            (QUAL_UNCONFIGURED, idx),
        )

    def _rebuild_device_table(self, con, table, keep_metrics, keep_controls):
        """按保留的量测列组与控调列组无损重建设备宽表，并同步 RTDB 元数据。"""
        quoted = _quote_table(table)
        keep_cols = ["idx", "name"]
        defs = ["idx INTEGER PRIMARY KEY", "name TEXT NOT NULL"]
        for m, k in keep_metrics:
            for col, col_type, default in measurement_columns(m, k):
                keep_cols.append(col)
                defs.append(f'"{col}" {col_type}{default}')
        for c, k in keep_controls:
            for col, col_type, default in control_columns(c, k):
                keep_cols.append(col)
                defs.append(f'"{col}" {col_type}{default}')
        tmp_name = f"__rebuild_{table}"
        tmp_quoted = f'"{tmp_name}"'
        cols_sql = ", ".join(_quote_col(c) for c in keep_cols)
        con.execute(f"CREATE TABLE {tmp_quoted} ({', '.join(defs)})")
        con.execute(f"INSERT INTO {tmp_quoted}({cols_sql}) SELECT {cols_sql} FROM {quoted}")
        con.execute(f"DROP TABLE {quoted}")
        con.execute(f"ALTER TABLE {tmp_quoted} RENAME TO {quoted}")
        _sync_device_rtdb_schema(con)

    def _prune_unused_metric_column(self, con, table, metric) -> bool:
        """若该表所有设备均未绑定该指标且不在预设未绑定清单中，移除该 10 列并原样保留所有控调列组。"""
        profile = UNBOUND_PROFILES.get(table)
        if profile and (metric in profile[0] or metric in profile[1]):
            return False
        quoted = _quote_table(table)
        still_used = con.execute(
            f"SELECT 1 FROM {quoted} WHERE {_quote_col(metric + '_rtu')} IS NOT NULL OR {_quote_col(metric + '_pnt')} IS NOT NULL LIMIT 1"
        ).fetchone()
        if still_used:
            return False
        metrics = self.table_metrics(con, table) or []
        controls = self.table_controls(con, table) or []
        remaining = [(m, k) for m, k in metrics if m != metric]
        self._rebuild_device_table(con, table, remaining, controls)
        return True

    def delete_measurement(self, measurement_id, version=None):
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            target = self.resolve_measurement_id(con, measurement_id)
            if target is None:
                raise ApiError(409, "属性不存在或已被修改")
            table, idx, metric = target
            if version is not None:
                if type(version) is not int or version < 1:
                    raise ApiError(400, "版本号必须为正整数")
                if version != self.version_for(table, idx, metric):
                    raise ApiError(409, "属性不存在或已被修改")
            self._clear_binding_cell(con, table, idx, metric)
            pruned = self._prune_unused_metric_column(con, table, metric)
        # 仅在 SQLite 事务成功提交后清理内存缓存并记入变更历史，避免回滚后内存脏状态
        with self._mutex:
            self._runtime_meta.pop((table, int(idx), str(metric)), None)
            if pruned:
                stale_keys = [k for k in self._meas_ids if k[0] == table and k[2] == metric]
                for key in stale_keys:
                    mid = self._meas_ids.pop(key, None)
                    if mid is not None:
                        self._id_to_meas.pop(mid, None)
                    self._versions.pop(key, None)
                    self._runtime_meta.pop(key, None)
            else:
                self.bump_version(table, idx, metric)
        self._record_change(
            "mapping_delete",
            int(measurement_id),
            dict(table=table, idx=idx, metric=metric),
            {},
        )
        self.bump_config_epoch()

    def overview(self):
        source_db = self.default_source_database()
        with self.connection() as con:
            con.execute("BEGIN")
            data_ver = con.execute("PRAGMA data_version").fetchone()[0]
            with self._mutex:
                cache_key = (data_ver, self._config_epoch, self._refresh_seq, source_db, self._interval_seconds)
                cached = self._overview_cache
                if cached is not None and cached[0] == cache_key:
                    return dict(cached[1])
                meta_snapshot = dict(self._runtime_meta)
            categories_map = defaultdict(
                lambda: dict(count=0, bound_count=0, valid_count=0, control_count=0, control_pending=0)
            )
            device_count = 0
            bound_count = 0
            valid_count = 0
            control_count = 0
            control_pending = 0
            cur = con.cursor()
            cur.row_factory = None
            for table, metrics, controls, metric_offsets, control_offsets in self._get_table_layouts(con):
                quoted = _quote_table(table)
                cat = category_for(table)
                cat_stat = categories_map[cat]
                if not metrics and not controls:
                    cnt = cur.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
                    device_count += cnt
                    cat_stat["count"] += cnt
                    continue
                for row in cur.execute(f"SELECT * FROM {quoted}"):
                    idx = row[0]
                    device_count += 1
                    cat_stat["count"] += 1
                    for metric, _ in metrics:
                        base = metric_offsets[metric]
                        rtu, pnt = row[base + 6], row[base + 7]
                        if _is_valid_point_config(rtu, pnt):
                            bound_count += 1
                            cat_stat["bound_count"] += 1
                            meta = meta_snapshot.get((table, int(idx), str(metric)))
                            valid, _, _ = self._infer_status(
                                row[base],
                                row[base + 1],
                                row[base + 3],
                                row[base + 4],
                                rtu,
                                pnt,
                                meta,
                            )
                            valid_count += valid
                            cat_stat["valid_count"] += valid
                    for ctrl, _ in controls:
                        cbase = control_offsets[ctrl]
                        crtu, cpnt = row[cbase + 5], row[cbase + 6]
                        if _is_valid_point_config(crtu, cpnt):
                            control_count += 1
                            cat_stat["control_count"] += 1
                            if row[cbase + 2] == CTRL_STATUS_PENDING:
                                control_pending += 1
                                cat_stat["control_pending"] += 1
        with self._mutex:
            last = dict(self._refresh_history[0]) if self._refresh_history else None
            interval = self._interval_seconds
            result = dict(
                counts=dict(
                    device=device_count,
                    device_measurement=bound_count,
                    scada_point_mapping=bound_count,
                    valid=valid_count,
                    control=control_count,
                    control_pending=control_pending,
                ),
                last_refresh=last,
                recent_controls=[dict(r) for r in list(self._control_history)[:8]],
                categories=[
                    dict(category=cat, **stats)
                    for cat, stats in sorted(categories_map.items())
                ],
                category_labels=CATEGORIES,
                interval_seconds=interval,
                sources=[source_db] if source_db else [],
            )
            self._overview_cache = (cache_key, result)
            return dict(result)

    def history(self):
        with self._mutex:
            return dict(
                refreshes=[dict(r) for r in self._refresh_history],
                changes=[dict(r) for r in self._change_history],
                controls=[dict(r) for r in self._control_history],
            )

    # ------------------------------------------------------------------
    # 运行日志写入与查询（scdfe_d-control / scdfe_d-collector）
    # ------------------------------------------------------------------

    def _get_logger(self, role):
        with self._log_lock:
            if role == "control":
                if self._control_logger is None:
                    self._control_logger = ScdfeLogger(
                        self.log_dir, process_name="scdfe_d-control"
                    )
                return self._control_logger
            if self._collector_logger is None:
                self._collector_logger = ScdfeLogger(
                    self.log_dir, process_name="scdfe_d-collector"
                )
            return self._collector_logger

    def _emit_file_log(self, role, level, message):
        """安全写入按小时轮转运行日志，任何磁盘异常不阻断实时业务。"""
        try:
            logger = self._get_logger(role)
            if level == "WARNING":
                logger.alarm(message)
            elif level == "ERROR":
                logger.error(message)
            elif level == "PROCESS":
                logger.process(message)
            else:
                logger.normal(message)
        except Exception:
            LOG.debug("failed to emit scdfe_d file log (%s): %s", role, message, exc_info=True)

    def list_log_files(self, *, record="runtime"):
        """枚举当前设备库同级 logs 目录下的全部 scdfe_d 日志文件。"""
        if record not in ("runtime", "packet"):
            raise ApiError(400, "记录类型无效")
        root = Path(self.log_dir).resolve()
        if not root.is_dir():
            return []
        pattern = LOG_PATTERNS["d"]
        items = []
        for entry, relative_name in log_entries(root, "scdfe_d-"):
            match = pattern.fullmatch(entry.name)
            if match is None:
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            items.append(
                dict(
                    name=relative_name,
                    process=match.group("process"),
                    hour=match.group("hour"),
                    size=stat.st_size,
                    modified_at=int(stat.st_mtime * 1000),
                )
            )
        items.sort(key=lambda item: (item["modified_at"], item["name"]), reverse=True)
        return items

    def read_logs(self, query):
        """按条件检索 scdfe_d 运行日志（委托 core.log_query.search_logs）。"""
        root = Path(self.log_dir).resolve()
        if not root.is_dir():
            return dict(
                items=[],
                next_cursor=None,
                has_more=False,
                scanned_files=0,
                scanned_bytes=0,
                skipped_lines=0,
                keyword=query.get("q", "").strip(),
                order="hour_file_desc_line_desc",
            )
        try:
            return search_logs(root, dict(query), family="d")
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc

    # ------------------------------------------------------------------
    # 设备遥控（YK）/ 遥调（YT）定义、实时监视与下发至 scdfe_c 闭环
    # ------------------------------------------------------------------

    def control_id_for(self, table, idx, control, *, con=None):
        key = (table, int(idx), str(control))
        with self._mutex:
            cid = self._ctrl_ids.get(key)
            if cid is not None:
                return cid
            empty = not self._ctrl_ids
        if empty and con is not None:
            self._prime_control_ids(con)
        with self._mutex:
            cid = self._ctrl_ids.get(key)
            if cid is None:
                cid = self._next_ctrl_id
                self._next_ctrl_id += 1
                self._ctrl_ids[key] = cid
                self._id_to_ctrl[cid] = key
            return cid

    def resolve_control_id(self, con, control_id):
        cid = int(control_id)
        with self._mutex:
            target = self._id_to_ctrl.get(cid)
        if target is not None:
            return target
        self._prime_control_ids(con)
        with self._mutex:
            return self._id_to_ctrl.get(cid)

    def _prime_control_ids(self, con):
        cur = con.cursor()
        cur.row_factory = None
        for table, _, controls, _, _ in self._get_table_layouts(con):
            if not controls:
                continue
            for (idx,) in cur.execute(
                f"SELECT idx FROM {_quote_table(table)} ORDER BY idx"
            ):
                for ctrl, _ in controls:
                    self.control_id_for(table, idx, ctrl)

    def control_version_for(self, table, idx, control):
        key = (table, int(idx), str(control))
        with self._mutex:
            return self._ctrl_versions.get(key, 1)

    def bump_control_version(self, table, idx, control):
        key = (table, int(idx), str(control))
        with self._mutex:
            nxt = self._ctrl_versions.get(key, 1) + 1
            self._ctrl_versions[key] = nxt
            return nxt

    def _record_control_event(
        self,
        *,
        source,
        table,
        idx,
        device_name,
        control,
        kind,
        rtu_id,
        pnt_no,
        value,
        cmd_time,
        status,
        ack_time=None,
        detail="",
    ):
        with self._mutex:
            self._control_seq += 1
            entry = dict(
                event_id=self._control_seq,
                at=utc_now(),
                source=source,
                model_table=table,
                model_idx=idx,
                device_name=device_name,
                control=control,
                kind=kind,
                rtu_id=rtu_id,
                pnt_no=pnt_no,
                value=value,
                cmd_time_ms=cmd_time,
                cmd_status=status,
                status_label=CTRL_STATUS_LABELS.get(status, str(status)),
                cmd_ack_time_ms=ack_time,
                detail=detail,
            )
            self._control_history.appendleft(entry)
            return entry

    def control_history(self):
        with self._mutex:
            return dict(items=[dict(r) for r in self._control_history])

    def sync_bindings_from_source(
        self,
        *,
        scope="all",
        rtu_id=None,
        dry_run=True,
        source_database=None,
    ):
        """按 <TableName>.<DeviceName>.<attr> 规范从 scdfe_c 自动匹配并同步四遥映射（支持预览与执行）。"""
        if scope not in ("all", "measurements", "controls"):
            raise ApiError(400, "同步范围必须为 all、measurements 或 controls")
        rtu_filter = _parse_positive_int(rtu_id) if rtu_id not in (None, "") else None
        if rtu_id not in (None, "") and rtu_filter is None:
            raise ApiError(400, "RTU 编号必须为正整数")
        source_db = source_database or self.default_source_database()
        kinds = (
            ("yc", "yx")
            if scope == "measurements"
            else (("yk", "yt") if scope == "controls" else ("yc", "yx", "yk", "yt"))
        )

        src_points = []
        with self.source(source_db) as src_con:
            for kind in kinds:
                tbl = f"fes_{kind}_define"
                cols = {r[1] for r in src_con.execute(f"PRAGMA table_info({tbl})")}
                if not {"rtu_id", "pnt_no", "name"} <= cols:
                    continue
                has_eng = kind == "yt" and {"eng_min", "eng_max"} <= cols
                extra = ", eng_min, eng_max" if has_eng else ", NULL AS eng_min, NULL AS eng_max"
                if rtu_filter is not None:
                    rows = src_con.execute(
                        f"SELECT rtu_id, pnt_no, name{extra} FROM {tbl} WHERE rtu_id=? ORDER BY pnt_no",
                        (rtu_filter,),
                    ).fetchall()
                else:
                    rows = src_con.execute(
                        f"SELECT rtu_id, pnt_no, name{extra} FROM {tbl} ORDER BY rtu_id, pnt_no"
                    ).fetchall()
                for r in rows:
                    src_points.append(
                        (kind, int(r[0]), int(r[1]), str(r[2] or ""), r[3], r[4])
                    )

        to_add = []
        to_update = []
        unchanged = 0
        unmatched = 0

        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE" if not dry_run else "BEGIN")
            layouts = self._get_table_layouts(con)
            dev_lookup = {}
            existing_bindings = {}
            table_existing_metrics = {}
            table_existing_controls = {}
            for table, metrics, controls, m_off, c_off in layouts:
                table_existing_metrics[table] = dict(metrics)
                table_existing_controls[table] = dict(controls)
                quoted = _quote_table(table)
                dev_lookup[table] = {}
                cur = con.cursor()
                cur.row_factory = None
                for row in cur.execute(f"SELECT * FROM {quoted}"):
                    idx, dname = int(row[0]), str(row[1] or "")
                    dev_lookup[table][dname] = idx
                    for m, _ in metrics:
                        b = m_off[m]
                        existing_bindings[(table, idx, "m", m)] = (row[b + 6], row[b + 7])
                    for c, _ in controls:
                        b = c_off[c]
                        existing_bindings[(table, idx, "c", c)] = (row[b + 5], row[b + 6])

            add_metrics_by_table = defaultdict(set)
            add_controls_by_table = defaultdict(set)
            plan_ops = []
            seen_target = set()

            for kind, rtu, pnt, full_name, eng_min, eng_max in src_points:
                parts = full_name.split(".")
                if len(parts) < 3:
                    unmatched += 1
                    continue
                table_name = parts[0]
                attr = parts[-1]
                dev_name = ".".join(parts[1:-1])
                if (
                    table_name not in dev_lookup
                    or not _METRIC_NAME_RE.fullmatch(attr)
                    or attr in ("idx", "name")
                    or attr.endswith(RESERVED_METRIC_SUFFIXES)
                ):
                    unmatched += 1
                    continue
                idx = dev_lookup[table_name].get(dev_name)
                if idx is None:
                    unmatched += 1
                    continue
                is_ctrl = kind in ("yk", "yt")
                # 防止同名跨类冲突
                if is_ctrl and attr in table_existing_metrics[table_name]:
                    unmatched += 1
                    continue
                if not is_ctrl and attr in table_existing_controls[table_name]:
                    unmatched += 1
                    continue
                slot_key = (table_name, idx, "c" if is_ctrl else "m", attr)
                if slot_key in seen_target:
                    continue
                seen_target.add(slot_key)
                cur_pair = existing_bindings.get(slot_key)
                cur_bound = cur_pair is not None and _is_valid_point_config(cur_pair[0], cur_pair[1])
                if cur_bound and int(cur_pair[0]) == rtu and int(cur_pair[1]) == pnt:
                    unchanged += 1
                    continue
                lo = _parse_limit_number(eng_min) if kind == "yt" else None
                hi = _parse_limit_number(eng_max) if kind == "yt" else None
                if not (lo is not None and hi is not None and hi > lo):
                    lo, hi = None, None
                op = dict(
                    action="update" if cur_bound else "add",
                    table=table_name,
                    idx=idx,
                    device_name=dev_name,
                    attr=attr,
                    kind=kind,
                    rtu_id=rtu,
                    pnt_no=pnt,
                    min_value=lo,
                    max_value=hi,
                )
                if cur_bound:
                    to_update.append(op)
                else:
                    to_add.append(op)
                plan_ops.append(op)
                if is_ctrl:
                    add_controls_by_table[table_name].add((attr, kind))
                else:
                    add_metrics_by_table[table_name].add((attr, kind))

            if not dry_run and plan_ops:
                all_tables = sorted(set(add_metrics_by_table) | set(add_controls_by_table))
                for tbl in all_tables:
                    self._ensure_device_table(
                        con,
                        tbl,
                        initial_metrics=sorted(add_metrics_by_table.get(tbl, ())),
                        initial_controls=sorted(add_controls_by_table.get(tbl, ())),
                        sync_schema=False,
                    )
                for op in plan_ops:
                    quoted = _quote_table(op["table"])
                    attr = op["attr"]
                    if op["kind"] in ("yk", "yt"):
                        con.execute(
                            f"""UPDATE {quoted} SET
                                {_quote_col(attr + '_cmd_status')}=COALESCE({_quote_col(attr + '_cmd_status')}, ?),
                                {_quote_col(attr + '_cmd_notuse')}=0,
                                {_quote_col(attr + '_cmd_rtu')}=?,
                                {_quote_col(attr + '_cmd_pnt')}=?,
                                {_quote_col(attr + '_cmd_max')}=COALESCE(?, {_quote_col(attr + '_cmd_max')}),
                                {_quote_col(attr + '_cmd_min')}=COALESCE(?, {_quote_col(attr + '_cmd_min')})
                            WHERE idx=?""",
                            (
                                CTRL_STATUS_IDLE,
                                op["rtu_id"],
                                op["pnt_no"],
                                op["max_value"],
                                op["min_value"],
                                op["idx"],
                            ),
                        )
                    else:
                        con.execute(
                            f"""UPDATE {quoted} SET
                                {_quote_col(attr + '_qual')}=?,
                                {_quote_col(attr + '_notuse')}=0,
                                {_quote_col(attr + '_rtu')}=?,
                                {_quote_col(attr + '_pnt')}=?
                            WHERE idx=?""",
                            (QUAL_COMM_ERROR, op["rtu_id"], op["pnt_no"], op["idx"]),
                        )
                _sync_device_rtdb_schema(con)

        if not dry_run and plan_ops:
            self._record_change(
                "sync_bindings",
                None,
                {},
                dict(
                    scope=scope,
                    source_database=source_db,
                    added=len(to_add),
                    updated=len(to_update),
                    unchanged=unchanged,
                ),
            )
            self._emit_file_log(
                "control",
                "INFO",
                f"一键同步绑定完成 范围={scope} 源库={source_db} 新增={len(to_add)} 更新={len(to_update)} 未变={unchanged}",
            )
            self.bump_config_epoch()

        return dict(
            dry_run=bool(dry_run),
            scope=scope,
            source_database=source_db,
            total_source=len(src_points),
            matched=len(to_add) + len(to_update) + unchanged,
            to_add=len(to_add),
            to_update=len(to_update),
            unchanged=unchanged,
            unmatched=unmatched,
            preview=(to_add + to_update)[:30],
        )

    @staticmethod
    def _normalize_control_value(kind, raw_value, max_val=None, min_val=None):
        """校验并规范化遥控/遥调目标值；非法或越限时抛出 ApiError(400)。"""
        if raw_value is None or isinstance(raw_value, bool):
            raise ApiError(400, "控调指令值不能为空或布尔类型")
        if kind == "yk":
            try:
                num = float(raw_value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ApiError(400, "遥控值只能为 0 或 1") from exc
            if not math.isfinite(num) or num not in (0.0, 1.0):
                raise ApiError(400, "遥控值只能为 0（分/停）或 1（合/启）")
            return int(num)
        try:
            num = float(raw_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ApiError(400, "遥调设定值必须为有限数值") from exc
        if not math.isfinite(num):
            raise ApiError(400, "遥调设定值必须为有限数值")
        upper = _parse_limit_number(max_val)
        lower = _parse_limit_number(min_val)
        if upper is not None and lower is not None and upper > lower:
            if num > upper:
                raise ApiError(400, f"遥调值 {num} 超过上限 {upper}")
            if num < lower:
                raise ApiError(400, f"遥调值 {num} 低于下限 {lower}")
        return num

    def controls(self, query):
        """分页查询已绑定的设备遥控/遥调清单。"""
        filter_device = int(query["device_id"]) if query.get("device_id") else None
        filter_kind = query.get("kind") or ""
        if filter_kind and filter_kind not in ("yk", "yt"):
            raise ApiError(400, "控调类型仅支持 yk 或 yt")
        filter_cat = query.get("category") or ""
        filter_table = query.get("table") or ""
        filter_status = (
            int(query["status"])
            if query.get("status") not in (None, "")
            else None
        )
        kw = (query.get("q") or "").strip().lower()
        page = max(1, int(query.get("page", 1)))
        limit = min(500, max(1, int(query.get("limit", 100))))
        source_db = self.default_source_database()

        start = (page - 1) * limit
        end = start + limit
        page_items = []
        total = 0
        yk_total = 0
        yt_total = 0
        pending_total = 0
        disabled_total = 0

        with self._mutex:
            ver_snap = dict(self._ctrl_versions)

        with self.connection() as con:
            con.execute("BEGIN")
            target_dev = (
                self.resolve_device_id(con, filter_device)
                if filter_device is not None
                else None
            )
            if filter_device is not None and target_dev is None:
                return dict(
                    items=[],
                    total=0,
                    page=page,
                    limit=limit,
                    summary=dict(total=0, yk=0, yt=0, pending=0, disabled=0),
                )
            cur = con.cursor()
            cur.row_factory = None
            for table, _, controls, _, control_offsets in self._get_table_layouts(con):
                if filter_table and table != filter_table:
                    continue
                if target_dev is not None and table != target_dev[0]:
                    continue
                cat = category_for(table)
                if filter_cat and cat != filter_cat:
                    continue
                if not controls:
                    continue
                quoted = _quote_table(table)
                if target_dev is not None:
                    rows_iter = cur.execute(
                        f"SELECT * FROM {quoted} WHERE idx=?", (target_dev[1],)
                    )
                else:
                    rows_iter = cur.execute(f"SELECT * FROM {quoted} ORDER BY idx")
                for row in rows_iter:
                    idx, dev_name = row[0], row[1]
                    dev_name_lower = str(dev_name or "").lower() if kw else ""
                    did = None
                    for ctrl, kind in controls:
                        base = control_offsets[ctrl]
                        rtu, pnt = row[base + 5], row[base + 6]
                        if not _is_valid_point_config(rtu, pnt):
                            continue
                        status_val = int(row[base + 2]) if row[base + 2] is not None else CTRL_STATUS_IDLE
                        notuse = 1 if row[base + 4] else 0
                        if notuse:
                            disabled_total += 1
                        if status_val == CTRL_STATUS_PENDING:
                            pending_total += 1
                        if kind == "yk":
                            yk_total += 1
                        else:
                            yt_total += 1
                        if filter_kind and kind != filter_kind:
                            continue
                        if filter_status is not None and status_val != filter_status:
                            continue
                        label, unit = CONTROL_META.get(ctrl, (ctrl, ""))
                        if (
                            kw
                            and kw not in ctrl.lower()
                            and kw not in label.lower()
                            and kw not in dev_name_lower
                            and kw not in table.lower()
                        ):
                            continue
                        if start <= total < end:
                            if did is None:
                                did = self.device_id_for(table, idx)
                            cid = self.control_id_for(table, idx, ctrl)
                            page_items.append(
                                dict(
                                    control_id=cid,
                                    device_id=did,
                                    model_table=table,
                                    model_idx=idx,
                                    device_name=dev_name,
                                    category=cat,
                                    control=ctrl,
                                    label=label,
                                    unit=unit,
                                    kind=kind,
                                    value=row[base],
                                    cmd_time_ms=row[base + 1],
                                    cmd_status=status_val,
                                    status_label=CTRL_STATUS_LABELS.get(status_val, str(status_val)),
                                    cmd_ack_time_ms=row[base + 3],
                                    notuse=notuse,
                                    source_database=source_db,
                                    rtu_id=int(rtu),
                                    pnt_no=int(pnt),
                                    max_value=row[base + 7],
                                    min_value=row[base + 8],
                                    version=ver_snap.get((table, int(idx), str(ctrl)), 1),
                                )
                            )
                        total += 1
        return dict(
            items=page_items,
            total=total,
            page=page,
            limit=limit,
            summary=dict(
                total=yk_total + yt_total,
                yk=yk_total,
                yt=yt_total,
                pending=pending_total,
                disabled=disabled_total,
            ),
        )

    def save_control(self, data, control_id=None):
        """在设备宽表中新增或更新一条遥控（YK）/ 遥调（YT）控调点位映射。"""
        ctrl = data.get("control", "")
        kind = data.get("kind", "yk")
        if (
            not isinstance(ctrl, str)
            or not _METRIC_NAME_RE.fullmatch(ctrl)
            or kind not in ("yk", "yt")
        ):
            raise ApiError(400, "控调属性名须为小写英文字母/数字/下划线，类型须为 YK/YT")
        if ctrl in ("idx", "name") or ctrl.endswith(RESERVED_METRIC_SUFFIXES):
            raise ApiError(400, "控调属性名不能使用保留元数据后缀")

        source = data.get("source_database") or self.default_source_database()
        rtu = _parse_positive_int(data.get("rtu_id"))
        point = _parse_positive_int(data.get("pnt_no"))
        if rtu is None or point is None:
            raise ApiError(400, "RTU 编号和点号必须为正整数")

        raw_notuse = data.get("notuse", 0)
        if isinstance(raw_notuse, bool):
            raise ApiError(400, "禁用标记只能为 0 或 1")
        notuse = int(raw_notuse)
        if notuse not in (0, 1):
            raise ApiError(400, "禁用标记只能为 0 或 1")

        has_max = "max_value" in data
        has_min = "min_value" in data
        raw_max = data.get("max_value")
        raw_min = data.get("min_value")
        max_val = _parse_limit_number(raw_max) if has_max else None
        min_val = _parse_limit_number(raw_min) if has_min else None
        if has_max and raw_max is not None and str(raw_max).strip() != "" and max_val is None:
            raise ApiError(400, "控调上限必须为有限数值或留空")
        if has_min and raw_min is not None and str(raw_min).strip() != "" and min_val is None:
            raise ApiError(400, "控调下限必须为有限数值或留空")
        if max_val is not None and min_val is not None and max_val <= min_val:
            raise ApiError(400, "控调上限必须大于下限")

        # 校验源库 fes_{yk|yt}_define 中确实存在目标点
        with self.source(source) as src_con:
            src_table = f"fes_{kind}_define"
            cols = {r[1] for r in src_con.execute(f"PRAGMA table_info({src_table})")}
            if not {"rtu_id", "pnt_no", "value", "status", "cmd_time", "cmd_handle_time"} <= cols:
                raise ApiError(400, f"源库缺少 {src_table} 表或必要控调字段")
            found = src_con.execute(
                f"SELECT name FROM {src_table} WHERE rtu_id=? AND pnt_no=?",
                (rtu, point),
            ).fetchall()
            if len(found) != 1:
                raise ApiError(400, f"指定的源控调点在 {src_table} 中不存在或主键不唯一")

        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            old_target = None
            if control_id is not None:
                old_target = self.resolve_control_id(con, control_id)
                if old_target is None:
                    raise ApiError(404, "控调映射不存在")
                cur_ver = self.control_version_for(*old_target)
                if "version" in data:
                    req_ver = data["version"]
                    if type(req_ver) is not int or req_ver < 1:
                        raise ApiError(400, "版本号必须为正整数")
                    if req_ver != cur_ver:
                        raise ApiError(409, "控调定义已被修改，请刷新后重试")

            if data.get("table") and data.get("idx") is not None:
                table, idx = str(data["table"]), _strict_int64(data["idx"])
                self._table_name(table)
                if self.table_metrics(con, table) is None:
                    raise ApiError(404, "目标设备表不存在")
            elif data.get("device_id") is not None:
                resolved = self.resolve_device_id(con, data["device_id"])
                if resolved is None:
                    raise ApiError(400, "目标设备不存在")
                table, idx = resolved
            elif old_target is not None:
                table, idx, _ = old_target
            else:
                raise ApiError(400, "目标设备不存在")

            quoted = _quote_table(table)
            dev_row = con.execute(
                f"SELECT name FROM {quoted} WHERE idx=?", (idx,)
            ).fetchone()
            if dev_row is None:
                raise ApiError(400, "目标设备不存在")
            dev_name = dev_row[0]

            if old_target is not None and old_target != (table, idx, ctrl):
                self._clear_control_cell(con, *old_target)

            self._ensure_device_table(con, table, initial_controls=[(ctrl, kind)])
            cur_vals = con.execute(
                f"SELECT {_quote_col(ctrl)}, {_quote_col(ctrl + '_cmd_time')}, "
                f"{_quote_col(ctrl + '_cmd_rtu')}, {_quote_col(ctrl + '_cmd_pnt')}, "
                f"{_quote_col(ctrl + '_cmd_max')}, {_quote_col(ctrl + '_cmd_min')} "
                f"FROM {quoted} WHERE idx=?",
                (idx,),
            ).fetchone()
            existing_bound = _is_valid_point_config(cur_vals[2], cur_vals[3])
            final_max = max_val if has_max else _parse_limit_number(cur_vals[4])
            final_min = min_val if has_min else _parse_limit_number(cur_vals[5])
            if (
                (has_max or has_min)
                and final_max is not None
                and final_min is not None
                and float(final_max) <= float(final_min)
            ):
                raise ApiError(400, "控调上限必须大于下限")
            point_changed = cur_vals[2] != rtu or cur_vals[3] != point
            next_status = CTRL_STATUS_DISABLED if notuse else CTRL_STATUS_IDLE
            if point_changed:
                con.execute(
                    f"""UPDATE {quoted} SET
                        {_quote_col(ctrl)}=NULL,
                        {_quote_col(ctrl + '_cmd_time')}=NULL,
                        {_quote_col(ctrl + '_cmd_status')}=?,
                        {_quote_col(ctrl + '_cmd_ack_time')}=NULL,
                        {_quote_col(ctrl + '_cmd_notuse')}=?,
                        {_quote_col(ctrl + '_cmd_rtu')}=?,
                        {_quote_col(ctrl + '_cmd_pnt')}=?,
                        {_quote_col(ctrl + '_cmd_max')}=?,
                        {_quote_col(ctrl + '_cmd_min')}=?
                    WHERE idx=?""",
                    (next_status, notuse, rtu, point, final_max, final_min, idx),
                )
                baseline_pair = (None, None)
            else:
                con.execute(
                    f"""UPDATE {quoted} SET
                        {_quote_col(ctrl + '_cmd_status')}=CASE
                            WHEN ?=1 THEN ?
                            WHEN {_quote_col(ctrl + '_cmd_status')} IN (?, ?) THEN ?
                            ELSE {_quote_col(ctrl + '_cmd_status')}
                        END,
                        {_quote_col(ctrl + '_cmd_notuse')}=?,
                        {_quote_col(ctrl + '_cmd_max')}=?,
                        {_quote_col(ctrl + '_cmd_min')}=?
                    WHERE idx=?""",
                    (
                        notuse,
                        CTRL_STATUS_DISABLED,
                        CTRL_STATUS_DISABLED,
                        CTRL_STATUS_UNCONFIGURED,
                        CTRL_STATUS_IDLE,
                        notuse,
                        final_max,
                        final_min,
                        idx,
                    ),
                )
                baseline_pair = (cur_vals[0], cur_vals[1])
            cid = self.control_id_for(table, idx, ctrl)
            did = self.device_id_for(table, idx, con=con)

        if data.get("source_database"):
            self.set_source_database(source)
        with self._mutex:
            if old_target is not None and old_target != (table, idx, ctrl):
                self._control_baseline.pop(old_target, None)
                self._pending_controls.pop(old_target, None)
            if self._control_baseline_ready:
                self._control_baseline[(table, idx, ctrl)] = baseline_pair
            if point_changed:
                self._pending_controls.pop((table, idx, ctrl), None)
        ver = (
            self.control_version_for(table, idx, ctrl)
            if (control_id is None and not existing_bound)
            else self.bump_control_version(table, idx, ctrl)
        )
        label, unit = CONTROL_META.get(ctrl, (ctrl, ""))
        after = dict(
            control_id=cid,
            device_id=did,
            model_table=table,
            model_idx=idx,
            device_name=dev_name,
            category=category_for(table),
            control=ctrl,
            label=label,
            unit=unit,
            kind=kind,
            notuse=notuse,
            source_database=source,
            rtu_id=rtu,
            pnt_no=point,
            max_value=final_max,
            min_value=final_min,
            cmd_status=next_status,
            status_label=CTRL_STATUS_LABELS.get(next_status, str(next_status)),
            version=ver,
        )
        self._record_change("control_mapping", cid, {}, after)
        self._emit_file_log(
            "control",
            "INFO",
            f"保存控调映射 设备={table}.{dev_name}(idx={idx}) 属性={ctrl}({kind.upper()}) "
            f"RTU={rtu} 点号={point} 禁用={notuse} 上限={final_max} 下限={final_min}",
        )
        self.bump_config_epoch()
        return after

    def _clear_control_cell(self, con, table, idx, control):
        quoted = _quote_table(table)
        con.execute(
            f"""UPDATE {quoted} SET
                {_quote_col(control)}=NULL,
                {_quote_col(control + '_cmd_time')}=NULL,
                {_quote_col(control + '_cmd_status')}=?,
                {_quote_col(control + '_cmd_ack_time')}=NULL,
                {_quote_col(control + '_cmd_notuse')}=0,
                {_quote_col(control + '_cmd_rtu')}=NULL,
                {_quote_col(control + '_cmd_pnt')}=NULL,
                {_quote_col(control + '_cmd_max')}=NULL,
                {_quote_col(control + '_cmd_min')}=NULL
            WHERE idx=?""",
            (CTRL_STATUS_UNCONFIGURED, idx),
        )

    def _prune_unused_control_column(self, con, table, control) -> bool:
        """若该表所有设备均未绑定该控调属性，移除该 9 列并原样保留所有量测列组。"""
        quoted = _quote_table(table)
        still_used = con.execute(
            f"SELECT 1 FROM {quoted} WHERE {_quote_col(control + '_cmd_rtu')} IS NOT NULL "
            f"OR {_quote_col(control + '_cmd_pnt')} IS NOT NULL LIMIT 1"
        ).fetchone()
        if still_used:
            return False
        metrics = self.table_metrics(con, table) or []
        controls = self.table_controls(con, table) or []
        remaining = [(c, k) for c, k in controls if c != control]
        self._rebuild_device_table(con, table, metrics, remaining)
        return True

    def delete_control(self, control_id, version=None):
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            target = self.resolve_control_id(con, control_id)
            if target is None:
                raise ApiError(409, "控调映射不存在或已被修改")
            table, idx, ctrl = target
            if version is not None:
                if type(version) is not int or version < 1:
                    raise ApiError(400, "版本号必须为正整数")
                if version != self.control_version_for(table, idx, ctrl):
                    raise ApiError(409, "控调映射不存在或已被修改")
            self._clear_control_cell(con, table, idx, ctrl)
            pruned = self._prune_unused_control_column(con, table, ctrl)
        with self._mutex:
            self._control_baseline.pop(target, None)
            self._pending_controls.pop(target, None)
            if pruned:
                stale = [k for k in self._ctrl_ids if k[0] == table and k[2] == ctrl]
                for key in stale:
                    cid = self._ctrl_ids.pop(key, None)
                    if cid is not None:
                        self._id_to_ctrl.pop(cid, None)
                    self._ctrl_versions.pop(key, None)
                    self._control_baseline.pop(key, None)
                    self._pending_controls.pop(key, None)
            else:
                self.bump_control_version(table, idx, ctrl)
        self._record_change(
            "control_mapping_delete",
            int(control_id),
            dict(table=table, idx=idx, control=ctrl),
            {},
        )
        self._emit_file_log(
            "control",
            "INFO",
            f"删除控调映射 表={table} idx={idx} 属性={ctrl} 剪枝列={pruned}",
        )
        self.bump_config_epoch()

    def _dispatch_to_scdfe_c(self, source_name, kind, rtu_id, pnt_no, value):
        """按 scdfe_c 标准下发协议原子写入 fes_{yk|yt}_define（触发 command_version 与 trace）。"""
        if kind not in ("yk", "yt"):
            raise ApiError(400, "控调类型必须为 yk 或 yt")
        source_db = source_name or self.default_source_database()
        path = self.source_path(source_db)
        if not path.is_file():
            raise ApiError(503, f"目标源库不存在：{source_db}")
        table = f"fes_{kind}_define"
        try:
            with closing(sqlite3.connect(path, timeout=10)) as con:
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA busy_timeout=10000")
                con.execute("PRAGMA synchronous=FULL")
                with con:
                    con.execute("BEGIN IMMEDIATE")
                    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                    required = {"rtu_id", "pnt_no", "value", "status", "cmd_time", "cmd_handle_time"}
                    if not required <= cols:
                        raise ApiError(400, f"源库 {source_db} 缺少 {table} 表或必要控调列")
                    sel_cols = ["name", "value", "status", "cmd_time", "cmd_handle_time"]
                    for opt in ("man_disable", "auto_disable", "eng_max", "eng_min", "_version"):
                        if opt in cols:
                            sel_cols.append(opt)
                    row = con.execute(
                        f"SELECT {','.join(sel_cols)} FROM {table} WHERE rtu_id=? AND pnt_no=?",
                        (int(rtu_id), int(pnt_no)),
                    ).fetchone()
                    if row is None:
                        raise ApiError(400, f"源库 {table} 中不存在 RTU={rtu_id} 点号={pnt_no}")
                    row_dict = dict(row)
                    if row_dict.get("man_disable") or row_dict.get("auto_disable"):
                        raise ApiError(409, f"源库测点 RTU={rtu_id} 点号={pnt_no} 已被闭锁禁用")
                    if kind == "yt":
                        eng_max = _parse_limit_number(row_dict.get("eng_max"))
                        eng_min = _parse_limit_number(row_dict.get("eng_min"))
                        if eng_max is not None and eng_min is not None and eng_max > eng_min:
                            if float(value) > eng_max:
                                raise ApiError(400, f"设定值 {value} 超过源库工程上限 {eng_max}")
                            if float(value) < eng_min:
                                raise ApiError(400, f"设定值 {value} 低于源库工程下限 {eng_min}")
                    cur_cmd = int(row_dict.get("cmd_time") or 0)
                    cur_ack = int(row_dict.get("cmd_handle_time") or 0)
                    if cur_cmd > cur_ack:
                        raise ApiError(409, f"源测点 RTU={rtu_id} 点号={pnt_no} 存在未完成指令，请等待回执")
                    dispatch_ms = max(int(time.time() * 1000), cur_cmd + 1, cur_ack + 1)
                    if "_version" in cols:
                        con.execute(
                            f"UPDATE {table} SET value=?, cmd_time=?, status=0, "
                            "_version=COALESCE(_version, 0)+1 WHERE rtu_id=? AND pnt_no=?",
                            (value, dispatch_ms, int(rtu_id), int(pnt_no)),
                        )
                    else:
                        con.execute(
                            f"UPDATE {table} SET value=?, cmd_time=?, status=0 "
                            "WHERE rtu_id=? AND pnt_no=?",
                            (value, dispatch_ms, int(rtu_id), int(pnt_no)),
                        )
                    return dict(
                        dispatch_ms=dispatch_ms,
                        source_database=source_db,
                        point_name=row_dict.get("name") or "",
                    )
        except ApiError:
            raise
        except sqlite3.Error as exc:
            raise ApiError(503, f"写入源库 {source_db} 失败：{exc}") from exc

    def dispatch_control(self, control_id, raw_value, *, source="web"):
        """从 Web 端或接口手动下发遥控/遥调指令：校验 -> 写 scdfe_c -> 回写设备宽表 -> 登记回执跟踪。"""
        source_db = self.default_source_database()
        with self.connection() as con:
            con.execute("BEGIN")
            target = self.resolve_control_id(con, control_id)
            if target is None:
                raise ApiError(404, "控调点位不存在")
            table, idx, ctrl = target
            controls = self.table_controls(con, table)
            if not controls:
                raise ApiError(404, "控调点位不存在")
            kind = next((k for c, k in controls if c == ctrl), None)
            if kind is None:
                raise ApiError(404, "控调点位不存在")
            quoted = _quote_table(table)
            row = con.execute(
                f"SELECT name, {_quote_col(ctrl + '_cmd_notuse')}, {_quote_col(ctrl + '_cmd_rtu')}, "
                f"{_quote_col(ctrl + '_cmd_pnt')}, {_quote_col(ctrl + '_cmd_max')}, {_quote_col(ctrl + '_cmd_min')} "
                f"FROM {quoted} WHERE idx=?",
                (idx,),
            ).fetchone()
            if row is None:
                raise ApiError(404, "目标设备不存在")
            dev_name, notuse, rtu_raw, pnt_raw, max_val, min_val = (
                row[0],
                1 if row[1] else 0,
                row[2],
                row[3],
                row[4],
                row[5],
            )

        rtu_id = _parse_positive_int(rtu_raw)
        pnt_no = _parse_positive_int(pnt_raw)
        if rtu_id is None or pnt_no is None:
            raise ApiError(400, "控调点位尚未配置有效的 RTU 和点号")
        if notuse:
            self._emit_file_log(
                "control",
                "WARNING",
                f"拦截下发(已禁用) 来源={source} 设备={table}.{dev_name} 属性={ctrl}({kind.upper()}) 目标值={raw_value}",
            )
            raise ApiError(409, "该控调点位已被禁用，禁止下发")

        norm_val = self._normalize_control_value(kind, raw_value, max_val, min_val)
        try:
            res = self._dispatch_to_scdfe_c(source_db, kind, rtu_id, pnt_no, norm_val)
        except ApiError as exc:
            now_ms = int(time.time() * 1000)
            self._record_control_event(
                source=source,
                table=table,
                idx=idx,
                device_name=dev_name,
                control=ctrl,
                kind=kind,
                rtu_id=rtu_id,
                pnt_no=pnt_no,
                value=norm_val,
                cmd_time=now_ms,
                status=CTRL_STATUS_FAILED,
                detail=exc.message,
            )
            self._emit_file_log(
                "control",
                "ERROR",
                f"下发失败 来源={source} 设备={table}.{dev_name} 属性={ctrl}({kind.upper()}) "
                f"RTU={rtu_id} 点号={pnt_no} 值={norm_val} 原因={exc.message}",
            )
            raise

        dispatch_ms = res["dispatch_ms"]
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                f"""UPDATE {_quote_table(table)} SET
                    {_quote_col(ctrl)}=?,
                    {_quote_col(ctrl + '_cmd_time')}=?,
                    {_quote_col(ctrl + '_cmd_status')}=?,
                    {_quote_col(ctrl + '_cmd_ack_time')}=NULL
                WHERE idx=?""",
                (norm_val, dispatch_ms, CTRL_STATUS_PENDING, idx),
            )

        with self._mutex:
            self._control_baseline[(table, idx, ctrl)] = (norm_val, dispatch_ms)
            self._control_baseline_ready = True
            self._pending_controls[(table, idx, ctrl)] = dict(
                source_database=source_db,
                source=source,
                kind=kind,
                rtu_id=rtu_id,
                pnt_no=pnt_no,
                value=norm_val,
                dispatch_ms=dispatch_ms,
                cmd_time=dispatch_ms,
                started_mono=time.monotonic(),
                device_name=dev_name,
            )
            self._overview_cache = None

        event = self._record_control_event(
            source=source,
            table=table,
            idx=idx,
            device_name=dev_name,
            control=ctrl,
            kind=kind,
            rtu_id=rtu_id,
            pnt_no=pnt_no,
            value=norm_val,
            cmd_time=dispatch_ms,
            status=CTRL_STATUS_PENDING,
            detail=f"已下发至 {source_db} ({res['point_name']})",
        )
        self._emit_file_log(
            "control",
            "INFO",
            f"下发指令 来源={source} 设备={table}.{dev_name} 属性={ctrl}({kind.upper()}) "
            f"RTU={rtu_id} 点号={pnt_no} 值={norm_val} 时标={dispatch_ms} 源点={res['point_name']}",
        )
        return dict(
            control_id=int(control_id),
            model_table=table,
            model_idx=idx,
            device_name=dev_name,
            control=ctrl,
            kind=kind,
            rtu_id=rtu_id,
            pnt_no=pnt_no,
            value=norm_val,
            cmd_time_ms=dispatch_ms,
            cmd_status=CTRL_STATUS_PENDING,
            status_label=CTRL_STATUS_LABELS[CTRL_STATUS_PENDING],
            source_database=source_db,
            event=event,
        )

    def _scan_control_slots(self, con):
        """扫描全部设备宽表的控调列组，返回完整槽位快照列表。"""
        layouts = self._get_table_layouts(con)
        cur = con.cursor()
        cur.row_factory = None
        slots = []
        for table, _, controls, _, control_offsets in layouts:
            if not controls:
                continue
            quoted = _quote_table(table)
            for row in cur.execute(f"SELECT * FROM {quoted}"):
                idx, dev_name = row[0], row[1]
                for ctrl, kind in controls:
                    base = control_offsets[ctrl]
                    slots.append(
                        (
                            table,
                            idx,
                            dev_name,
                            ctrl,
                            kind,
                            row[base],          # value
                            row[base + 1],      # cmd_time
                            row[base + 2],      # cmd_status
                            row[base + 3],      # cmd_ack_time
                            1 if row[base + 4] else 0,  # notuse
                            row[base + 5],      # rtu_raw
                            row[base + 6],      # pnt_raw
                            row[base + 7],      # max_val
                            row[base + 8],      # min_val
                        )
                    )
        return slots

    def poll_controls_once(self, *, _writer=None, _control_state=None):
        """实时监视设备宽表控调值/时标变化并下发至 scdfe_c，同时同步在途指令回执。"""
        source_db = self.default_source_database()
        dispatched = 0
        rejected = 0
        acked = 0
        timed_out = 0

        # 1. 检测宽表是否有变化（冷启动或 data_version/config_epoch 变化时才扫表，零空闲开销）
        need_scan = False
        cur_ver = None
        epoch = self._config_epoch
        with self._mutex:
            baseline_ready = self._control_baseline_ready
        if not baseline_ready or _control_state is None:
            need_scan = True
        elif _writer is not None:
            cur_ver = _writer.execute("PRAGMA data_version").fetchone()[0]
            if _control_state.get("scan_key") != (cur_ver, epoch):
                need_scan = True
        else:
            need_scan = True

        slots = None
        if need_scan:
            with self.connection(_writer=_writer) as read_con:
                read_con.execute("BEGIN")
                try:
                    cur_ver = read_con.execute("PRAGMA data_version").fetchone()[0]
                    epoch = self._config_epoch
                    slots = self._scan_control_slots(read_con)
                finally:
                    read_con.rollback()

        if slots is not None:
            with self._mutex:
                if not self._control_baseline_ready:
                    # 冷启动建立基线，防止把库内历史指令当成新命令误下发
                    bound_cnt = 0
                    for s in slots:
                        key = (s[0], s[1], s[3])
                        self._control_baseline[key] = (s[5], s[6])
                        if _is_valid_point_config(s[10], s[11]):
                            bound_cnt += 1
                    self._control_baseline_ready = True
                    if bound_cnt:
                        self._emit_file_log(
                            "control",
                            "PROCESS",
                            f"控调实时监视基线已建立，共纳管 {bound_cnt} 个已绑定遥控/遥调点位",
                        )
                    if _control_state is not None:
                        _control_state["scan_key"] = (cur_ver, epoch)
                    slots = []
                else:
                    baseline_copy = dict(self._control_baseline)

            if slots:
                changes_by_row = defaultdict(dict)
                baseline_updates = {}
                pending_additions = {}
                active_keys = set()

                for s in slots:
                    (
                        table,
                        idx,
                        dev_name,
                        ctrl,
                        kind,
                        val,
                        cmd_time,
                        cmd_status,
                        cmd_ack_time,
                        notuse,
                        rtu_raw,
                        pnt_raw,
                        max_val,
                        min_val,
                    ) = s
                    key = (table, idx, ctrl)
                    active_keys.add(key)
                    prev = baseline_copy.get(key)
                    if prev is None:
                        # 运行期新增列：若未写值则仅记录基线；若已写入非空值则视为新指令
                        if val is None:
                            baseline_updates[key] = (val, cmd_time)
                            continue
                        prev_val, prev_time = None, None
                    else:
                        prev_val, prev_time = prev

                    val_changed = not _same_value(val, prev_val)
                    time_changed = cmd_time != prev_time
                    if not val_changed and not time_changed:
                        continue

                    # 外部清空为 NULL 不触发下发
                    if val is None:
                        baseline_updates[key] = (val, cmd_time)
                        continue

                    rtu_id = _parse_positive_int(rtu_raw)
                    pnt_no = _parse_positive_int(pnt_raw)
                    now_ms = int(time.time() * 1000)

                    # 校验配置与禁用闭锁
                    if rtu_id is None or pnt_no is None:
                        rejected += 1
                        row_u = changes_by_row[(table, idx)]
                        row_u[ctrl + "_cmd_status"] = CTRL_STATUS_UNCONFIGURED
                        baseline_updates[key] = (val, cmd_time)
                        self._record_control_event(
                            source="db_watch",
                            table=table,
                            idx=idx,
                            device_name=dev_name,
                            control=ctrl,
                            kind=kind,
                            rtu_id=None,
                            pnt_no=None,
                            value=val,
                            cmd_time=cmd_time or now_ms,
                            status=CTRL_STATUS_UNCONFIGURED,
                            detail="外部写入控调变化但未配置有效 RTU/点号",
                        )
                        self._emit_file_log(
                            "control",
                            "WARNING",
                            f"监视拦截(无配置) 设备={table}.{dev_name} 属性={ctrl}({kind.upper()}) 值={val} 时标={cmd_time}",
                        )
                        continue

                    if notuse:
                        rejected += 1
                        row_u = changes_by_row[(table, idx)]
                        row_u[ctrl + "_cmd_status"] = CTRL_STATUS_DISABLED
                        baseline_updates[key] = (val, cmd_time)
                        self._record_control_event(
                            source="db_watch",
                            table=table,
                            idx=idx,
                            device_name=dev_name,
                            control=ctrl,
                            kind=kind,
                            rtu_id=rtu_id,
                            pnt_no=pnt_no,
                            value=val,
                            cmd_time=cmd_time or now_ms,
                            status=CTRL_STATUS_DISABLED,
                            detail="外部写入控调变化但该点已禁用闭锁",
                        )
                        self._emit_file_log(
                            "control",
                            "WARNING",
                            f"监视拦截(控制禁用) 设备={table}.{dev_name} 属性={ctrl}({kind.upper()}) "
                            f"RTU={rtu_id} 点号={pnt_no} 值={val}",
                        )
                        continue

                    # 值类型与上下限校验 + 下发至 scdfe_c
                    try:
                        norm_val = self._normalize_control_value(kind, val, max_val, min_val)
                        res = self._dispatch_to_scdfe_c(
                            source_db, kind, rtu_id, pnt_no, norm_val
                        )
                    except ApiError as exc:
                        rejected += 1
                        row_u = changes_by_row[(table, idx)]
                        row_u[ctrl + "_cmd_status"] = CTRL_STATUS_FAILED
                        baseline_updates[key] = (val, cmd_time)
                        self._record_control_event(
                            source="db_watch",
                            table=table,
                            idx=idx,
                            device_name=dev_name,
                            control=ctrl,
                            kind=kind,
                            rtu_id=rtu_id,
                            pnt_no=pnt_no,
                            value=val,
                            cmd_time=cmd_time or now_ms,
                            status=CTRL_STATUS_FAILED,
                            detail=exc.message,
                        )
                        self._emit_file_log(
                            "control",
                            "ERROR",
                            f"监视下发失败 设备={table}.{dev_name} 属性={ctrl}({kind.upper()}) "
                            f"RTU={rtu_id} 点号={pnt_no} 值={val} 原因={exc.message}",
                        )
                        continue

                    dispatch_ms = res["dispatch_ms"]
                    final_cmd_time = cmd_time if (time_changed and cmd_time is not None) else dispatch_ms
                    row_u = changes_by_row[(table, idx)]
                    if not _same_value(norm_val, val):
                        row_u[ctrl] = norm_val
                    if final_cmd_time != cmd_time:
                        row_u[ctrl + "_cmd_time"] = final_cmd_time
                    row_u[ctrl + "_cmd_status"] = CTRL_STATUS_PENDING
                    row_u[ctrl + "_cmd_ack_time"] = None
                    baseline_updates[key] = (norm_val, final_cmd_time)
                    pending_additions[key] = dict(
                        source_database=source_db,
                        source="db_watch",
                        kind=kind,
                        rtu_id=rtu_id,
                        pnt_no=pnt_no,
                        value=norm_val,
                        dispatch_ms=dispatch_ms,
                        cmd_time=final_cmd_time,
                        started_mono=time.monotonic(),
                        device_name=dev_name,
                    )
                    dispatched += 1
                    self._record_control_event(
                        source="db_watch",
                        table=table,
                        idx=idx,
                        device_name=dev_name,
                        control=ctrl,
                        kind=kind,
                        rtu_id=rtu_id,
                        pnt_no=pnt_no,
                        value=norm_val,
                        cmd_time=final_cmd_time,
                        status=CTRL_STATUS_PENDING,
                        detail=f"监视到库内变化，已自动转发至 {source_db} ({res['point_name']})",
                    )
                    self._emit_file_log(
                        "control",
                        "INFO",
                        f"监视转发指令 设备={table}.{dev_name} 属性={ctrl}({kind.upper()}) "
                        f"RTU={rtu_id} 点号={pnt_no} 旧值={prev_val} 新值={norm_val} "
                        f"时标={final_cmd_time} 源时标={dispatch_ms}",
                    )

                if changes_by_row:
                    with self.connection(_writer=_writer) as con:
                        con.execute("BEGIN IMMEDIATE")
                        self._write_device_updates(con, changes_by_row)
                        cur_ver = con.execute("PRAGMA data_version").fetchone()[0]
                with self._mutex:
                    # 清理已删除表/列的过期基线
                    for stale in list(self._control_baseline):
                        if stale not in active_keys:
                            self._control_baseline.pop(stale, None)
                    self._control_baseline.update(baseline_updates)
                    self._pending_controls.update(pending_additions)
                    if changes_by_row:
                        self._overview_cache = None
                if _control_state is not None:
                    _control_state["scan_key"] = (cur_ver, epoch)

        # 2. 轮询在途待回执指令的 scdfe_c 执行结果（cmd_handle_time >= dispatch_ms 或超时）
        with self._mutex:
            pending_snapshot = dict(self._pending_controls)

        if pending_snapshot:
            by_source = defaultdict(lambda: defaultdict(list))
            for key, info in pending_snapshot.items():
                by_source[info["source_database"]][info["kind"]].append((key, info))

            ack_updates = defaultdict(dict)
            resolved_keys = []
            now_mono = time.monotonic()

            for src_name, kinds_map in by_source.items():
                src_rows = {}
                try:
                    with self.source(src_name) as src_con:
                        for kind, entries in kinds_map.items():
                            src_table = f"fes_{kind}_define"
                            pts = sorted({(info["rtu_id"], info["pnt_no"]) for _, info in entries})
                            wanted = ",".join("(?,?)" for _ in pts)
                            params = tuple(v for pair in pts for v in pair)
                            sql = (
                                f"WITH wanted(rtu_id,pnt_no) AS (VALUES {wanted}) "
                                f"SELECT p.rtu_id, p.pnt_no, p.status, p.cmd_handle_time "
                                f"FROM wanted JOIN {src_table} p "
                                "ON p.rtu_id=wanted.rtu_id AND p.pnt_no=wanted.pnt_no"
                            )
                            for r in src_con.execute(sql, params):
                                src_rows[(kind, int(r[0]), int(r[1]))] = (r[2], r[3])
                except (ApiError, sqlite3.Error, OSError) as exc:
                    self._emit_file_log(
                        "control",
                        "WARNING",
                        f"查询源库 {src_name} 控调回执失败：{exc}",
                    )
                    continue

                for kind, entries in kinds_map.items():
                    for key, info in entries:
                        table, idx, ctrl = key
                        row_state = src_rows.get((kind, info["rtu_id"], info["pnt_no"]))
                        if row_state is not None:
                            src_status, src_ack_time = row_state
                            ack_ms = int(src_ack_time or 0)
                            if ack_ms >= info["dispatch_ms"]:
                                final_status = (
                                    CTRL_STATUS_SUCCESS
                                    if int(src_status or 0) == 1
                                    else CTRL_STATUS_FAILED
                                )
                                row_u = ack_updates[(table, idx)]
                                row_u[ctrl + "_cmd_status"] = final_status
                                row_u[ctrl + "_cmd_ack_time"] = ack_ms
                                resolved_keys.append(key)
                                acked += 1
                                detail = (
                                    f"scdfe_c 回执成功 (status={src_status})"
                                    if final_status == CTRL_STATUS_SUCCESS
                                    else f"scdfe_c 回执失败 (status={src_status})"
                                )
                                self._record_control_event(
                                    source=info["source"],
                                    table=table,
                                    idx=idx,
                                    device_name=info["device_name"],
                                    control=ctrl,
                                    kind=kind,
                                    rtu_id=info["rtu_id"],
                                    pnt_no=info["pnt_no"],
                                    value=info["value"],
                                    cmd_time=info["cmd_time"],
                                    status=final_status,
                                    ack_time=ack_ms,
                                    detail=detail,
                                )
                                self._emit_file_log(
                                    "control",
                                    "INFO" if final_status == CTRL_STATUS_SUCCESS else "WARNING",
                                    f"收到控调回执 设备={table}.{info['device_name']} 属性={ctrl}({kind.upper()}) "
                                    f"RTU={info['rtu_id']} 点号={info['pnt_no']} 值={info['value']} "
                                    f"结果={CTRL_STATUS_LABELS[final_status]} 回执时标={ack_ms}",
                                )
                                continue
                        if now_mono - info["started_mono"] >= self._control_ack_timeout:
                            timeout_ms = int(time.time() * 1000)
                            row_u = ack_updates[(table, idx)]
                            row_u[ctrl + "_cmd_status"] = CTRL_STATUS_FAILED
                            row_u[ctrl + "_cmd_ack_time"] = timeout_ms
                            resolved_keys.append(key)
                            timed_out += 1
                            self._record_control_event(
                                source=info["source"],
                                table=table,
                                idx=idx,
                                device_name=info["device_name"],
                                control=ctrl,
                                kind=kind,
                                rtu_id=info["rtu_id"],
                                pnt_no=info["pnt_no"],
                                value=info["value"],
                                cmd_time=info["cmd_time"],
                                status=CTRL_STATUS_FAILED,
                                ack_time=timeout_ms,
                                detail=f"等待 scdfe_c 回执超时（>{int(self._control_ack_timeout)}s）",
                            )
                            self._emit_file_log(
                                "control",
                                "WARNING",
                                f"控调回执超时 设备={table}.{info['device_name']} 属性={ctrl}({kind.upper()}) "
                                f"RTU={info['rtu_id']} 点号={info['pnt_no']} 值={info['value']}",
                            )

            if ack_updates:
                with self.connection(_writer=_writer) as con:
                    con.execute("BEGIN IMMEDIATE")
                    self._write_device_updates(con, ack_updates)
                    ack_ver = con.execute("PRAGMA data_version").fetchone()[0]
                if _control_state is not None:
                    _control_state["scan_key"] = (ack_ver, self._config_epoch)
                with self._mutex:
                    for key in resolved_keys:
                        cur_p = self._pending_controls.get(key)
                        if cur_p is not None and cur_p["dispatch_ms"] == pending_snapshot[key]["dispatch_ms"]:
                            self._pending_controls.pop(key, None)
                    self._overview_cache = None

        return dict(
            dispatched=dispatched,
            rejected=rejected,
            acked=acked,
            timed_out=timed_out,
            pending=len(self._pending_controls),
        )


class Collector:
    def __init__(self, store):
        self.store = store
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lease = InstanceLock(store.db_path, "device-collector")
        self.last_error = ""

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def status(self):
        return dict(
            running=self.running,
            external_running=not self.running
            and InstanceLock.held(self.store.db_path, "device-collector"),
            last_error=self.last_error,
        )

    def once(self):
        with self._mutex:
            if self.running or not self._lease.acquire():
                raise ApiError(409, "采集任务已在运行")
            try:
                return self.store.collect_once()
            finally:
                self._lease.release()

    def set_interval(self, value):
        interval = self.store.set_interval_value(value)
        with self._mutex:
            self._wake.set()
            return dict(self.status(), interval_seconds=interval)

    def start(self, interval=None):
        with self._mutex:
            if interval is not None:
                interval = validate_interval(interval)
            if self.running:
                return self.status()
            if not self._lease.acquire():
                raise ApiError(409, "此设备库已有采集进程")
            try:
                if interval is not None:
                    self.store.set_interval_value(interval)
                self._stop.clear()
                self._wake.clear()
                self.last_error = ""

                def run():
                    writer = None
                    source_cache = {}
                    plan_cache = {}
                    control_state = {}
                    ctrl_interval = max(0.05, float(self.store._control_poll_interval))
                    self.store._emit_file_log(
                        "collector",
                        "PROCESS",
                        f"设备采集与控调监视线程已启动，采集周期={self.store.get_interval()}s，控调监视周期={ctrl_interval}s",
                    )
                    try:
                        now = time.monotonic()
                        next_scan = now
                        next_ctrl = now
                        last_scan = None
                        period = None
                        last_err_logged = ""
                        while not self._stop.is_set():
                            self._wake.clear()
                            sample_due = False
                            try:
                                if writer is None:
                                    writer = self.store._open_connection()
                                now = time.monotonic()
                                # 1. 亚秒级控调变化监视与回执闭环（不阻塞量测采集）
                                if now >= next_ctrl:
                                    try:
                                        self.store.poll_controls_once(
                                            _writer=writer,
                                            _control_state=control_state,
                                        )
                                    except Exception as ctrl_exc:
                                        LOG.exception("device control monitor cycle failed")
                                        self.store._emit_file_log(
                                            "control",
                                            "ERROR",
                                            f"控调监视周期异常：{ctrl_exc}",
                                        )
                                        control_state.clear()
                                    next_ctrl = time.monotonic() + ctrl_interval

                                # 2. 按设定周期执行量测采集
                                new_period = self.store.get_interval()
                                now = time.monotonic()
                                if new_period != period and last_scan is not None:
                                    next_scan = max(now, last_scan + new_period)
                                period = new_period
                                if now >= next_scan:
                                    if self._stop.is_set():
                                        break
                                    last_scan = next_scan
                                    sample_due = True
                                    result = self.store.collect_once(
                                        _writer=writer,
                                        _source_cache=source_cache,
                                        _plan_cache=plan_cache,
                                    )
                                    err_text = "; ".join(result["errors"].values())
                                    self.last_error = err_text
                                    if err_text and err_text != last_err_logged:
                                        self.store._emit_file_log(
                                            "collector",
                                            "WARNING",
                                            f"量测采集异常：{err_text}",
                                        )
                                        last_err_logged = err_text
                                    elif not err_text and last_err_logged:
                                        self.store._emit_file_log(
                                            "collector",
                                            "INFO",
                                            f"量测采集已恢复正常，有效量测={result['valid_count']}/{result['total_count']}",
                                        )
                                        last_err_logged = ""
                                    next_scan += period
                                    now = time.monotonic()
                                    if next_scan <= now:
                                        next_scan += period * (
                                            1 + math.floor((now - next_scan) / period)
                                        )
                            except Exception as exc:
                                LOG.exception("device collector cycle failed")
                                self.last_error = str(exc)
                                self.store._emit_file_log(
                                    "collector",
                                    "ERROR",
                                    f"设备采集周期发生异常：{exc}",
                                )
                                plan_cache.clear()
                                control_state.clear()
                                if writer is not None:
                                    try:
                                        writer.close()
                                    except Exception:
                                        pass
                                    writer = None
                                if sample_due:
                                    next_scan = time.monotonic() + (period or DEFAULT_INTERVAL_SECONDS)
                                self._wake.wait(min(1.0, ctrl_interval))
                                continue

                            now = time.monotonic()
                            wait_s = min(
                                max(0.02, next_ctrl - now),
                                max(0.02, next_scan - now),
                                0.5,
                            )
                            self._wake.wait(wait_s)
                    finally:
                        self.store._emit_file_log(
                            "collector",
                            "PROCESS",
                            "设备采集与控调监视线程已停止",
                        )
                        try:
                            for entry in source_cache.values():
                                try:
                                    entry[0].close()
                                except Exception:
                                    pass
                            source_cache.clear()
                            if writer is not None:
                                try:
                                    writer.close()
                                except Exception:
                                    pass
                        finally:
                            self._lease.release()

                self._thread = threading.Thread(
                    target=run, name="scdfe-d-collector", daemon=True
                )
                self._thread.start()
            except BaseException:
                self._lease.release()
                raise
        return self.status()

    def close(self):
        with self._mutex:
            self._stop.set()
            self._wake.set()
            if self._thread:
                self._thread.join(timeout=30)
                if self._thread.is_alive():
                    raise RuntimeError("采集事务尚未结束")

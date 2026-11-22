"""直接以设备宽表为唯一数据源的实时量测采集与管理服务。

scdfe_d 数据库中不再创建 measurement_value、measurement_refresh、scada_point_mapping、
device_measurement、device 等冗余量测中间表；仅保留以设备类型为名的设备宽表与
RTDB 元数据表（rtdb_schema_*）。源 SCADA 数据库（默认同目录 scdfe_c.sqlite3）始终只读。
"""

from collections import defaultdict, deque
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import threading
import time
from types import SimpleNamespace

from core.rtdb_schema import RTDB_METADATA_TABLES, sync_embedded_schema
from core.scdfe_control import InstanceLock
from scdfe_c.web.service import ApiError
from scdfe_d.measurement_layout import (
    EXTENSION_METRICS,
    SUFFIXES,
    UNBOUND_PROFILES,
    measurement_columns,
    metric_order,
)

APPLICATION_ID = int.from_bytes(b"SDFD", "big")
SCHEMA_USER_VERSION = 4
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
_INVALID_QUALITY_REASONS = frozenset(
    {"disabled", "source_error", "missing_point", "invalid_value"}
)


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


def parse_device_table_columns(columns_info):
    """校验并解析设备宽表列定义，返回 [(metric, kind), ...]；结构不合法返回 None。"""
    if len(columns_info) < 2:
        return None
    if columns_info[0][1] != "idx" or columns_info[1][1] != "name":
        return None
    if not columns_info[0][5]:
        return None
    rest = columns_info[2:]
    if len(rest) % len(SUFFIXES) != 0:
        return None
    metrics = []
    step = len(SUFFIXES)
    for offset in range(0, len(rest), step):
        group = rest[offset : offset + step]
        metric = group[0][1]
        if (
            not _METRIC_NAME_RE.fullmatch(metric)
            or metric in {"idx", "name"}
            or metric.endswith(SUFFIXES[1:])
        ):
            return None
        if [row[1] for row in group] != [metric + suffix for suffix in SUFFIXES]:
            return None
        col_type = str(group[0][2] or "").upper()
        kind = "yx" if col_type == "INTEGER" else "yc"
        metrics.append((metric, kind))
    return metrics


def discover_device_tables(con):
    """发现库内全部合法设备宽表，返回有序列表 [(table_name, [(metric, kind), ...]), ...]。"""
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
            or table in RTDB_METADATA_TABLES
            or table in LEGACY_AUX_TABLES
            or not _TABLE_NAME_RE.fullmatch(table)
        ):
            continue
        info = con.execute(f'PRAGMA table_info("{table}")').fetchall()
        metrics = parse_device_table_columns(info)
        if metrics is not None:
            result.append((table, metrics))
    return result


def validate_database(path):
    """只读校验 scdfe_d 设备数据库：仅允许合法设备宽表与 RTDB 元数据表。"""
    try:
        path = Path(path).resolve()
        if not path.is_file():
            raise ValueError("设备数据库文件不存在")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
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
                if table in RTDB_METADATA_TABLES or table in LEGACY_AUX_TABLES:
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
                        f'(CASE WHEN {_quote_col(m + "_rtu")} IS NOT NULL AND {_quote_col(m + "_pnt")} IS NOT NULL THEN 1 ELSE 0 END)'
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
            dev_tables = [t for t, _ in discover_device_tables(con)]
            sync_embedded_schema(con, include_tables=dev_tables)
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
        self._runtime_meta = {}  # (table, idx, metric) -> {valid, reason, collected_at}
        self._last_bound_points = {}  # (table, idx, metric) -> (rtu_id, pnt_no)
        self._refresh_seq = 0
        self._refresh_history = deque(maxlen=MAX_HISTORY_ITEMS)
        self._change_history = deque(maxlen=MAX_HISTORY_ITEMS)
        self._schema_cache = None  # (schema_version, [(table, [(metric, kind), ...])])

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
                    dev_tables = [t for t, _ in discover_device_tables(init_con)]
                    sync_embedded_schema(init_con, include_tables=dev_tables)
                    init_con.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    init_con.execute(f"PRAGMA user_version={SCHEMA_USER_VERSION}")

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
        return RTDB_METADATA_TABLES <= tables

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
        """解析默认源 SCADA 采集库（优先显式配置，其次同目录 scdfe_c.sqlite3）。"""
        with self._mutex:
            explicit = self._source_database
        if explicit:
            return explicit
        for candidate in ("scdfe_c.sqlite3", "c.sqlite3"):
            path = (self.db_path.parent / candidate).resolve()
            if path.is_file() and path.is_relative_to(self.root):
                return path.relative_to(self.root).as_posix()
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

    def _get_table_schemas(self, con):
        """按 schema_version 缓存所有设备宽表结构 [(table, [(metric, kind), ...]), ...]。"""
        schema_ver = con.execute("PRAGMA schema_version").fetchone()[0]
        with self._mutex:
            if self._schema_cache is not None and self._schema_cache[0] == schema_ver:
                return self._schema_cache[1]
        schemas = discover_device_tables(con)
        with self._mutex:
            self._schema_cache = (schema_ver, schemas)
        return schemas

    def table_metrics(self, con, table):
        for tbl, metrics in self._get_table_schemas(con):
            if tbl == table:
                return metrics
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
        """根据设备宽表字段与内存采集元信息推导 valid 与中文状态原因码。"""
        if rtu is None or pnt is None:
            return 0, "unmapped", None
        if cached_meta is not None:
            return (
                cached_meta["valid"],
                cached_meta["reason"],
                cached_meta["collected_at"],
            )
        if notuse:
            return 0, "notuse", None
        if value is None and (stamp is None or stamp <= 0):
            return 0, "uncollected", None
        if qual is not None and (int(qual) & 128) == 0 and int(qual) == 0:
            return 1, "", None
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
            if len(keys) <= 128:
                wanted = ",".join("(?,?)" for _ in keys)
                params = tuple(value for pair in sorted(keys) for value in pair)
                qualified = ",".join(f"p.{field}" for field in columns.split(","))
                sql = (
                    f"WITH wanted(rtu_id,pnt_no) AS (VALUES {wanted}) "
                    f"SELECT {qualified} FROM wanted CROSS JOIN {table} p "
                    "ON p.rtu_id=wanted.rtu_id AND p.pnt_no=wanted.pnt_no"
                )
            else:
                distinct_rtus = sorted({rtu for rtu, _ in keys})
                if len(distinct_rtus) <= 64:
                    placeholders = ",".join("?" for _ in distinct_rtus)
                    sql = f"SELECT {columns} FROM {table} WHERE rtu_id IN ({placeholders})"
                    params = tuple(distinct_rtus)
        cur = connection.cursor()
        cur.row_factory = None
        snapshot = {}
        if has_pk:
            for rtu_id, pnt_no, value, status, stamp in cur.execute(sql, params):
                key = (rtu_id, pnt_no)
                if key in keys:
                    snapshot[key] = (value, status, stamp)
        else:
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
        """直接扫描全部设备宽表，提取所有已绑定 (rtu, pnt) 字段及其当前值。"""
        schemas = self._get_table_schemas(con)
        cur = con.cursor()
        cur.row_factory = None
        bindings = []
        requested_sets = defaultdict(set)
        step = len(SUFFIXES)
        for table, metrics in schemas:
            if not metrics:
                continue
            quoted = _quote_table(table)
            for row in cur.execute(f"SELECT * FROM {quoted}"):
                idx = row[0]
                base = 2
                for metric, kind in metrics:
                    rtu = row[base + 6]
                    pnt = row[base + 7]
                    if rtu is not None and pnt is not None:
                        rtu_i = int(rtu)
                        pnt_i = int(pnt)
                        bindings.append(
                            (
                                table,
                                idx,
                                metric,
                                kind,
                                rtu_i,
                                pnt_i,
                                1 if row[base + 4] else 0,
                                row[base],
                                row[base + 1],
                                row[base + 2],
                                row[base + 3],
                            )
                        )
                        requested_sets[kind].add((rtu_i, pnt_i))
                    base += step
        requested = {k: frozenset(pts) for k, pts in requested_sets.items() if pts}
        return bindings, requested

    def collect_once(self, *, _writer=None, _source_cache=None, _plan_cache=None):
        """直接从设备宽表读取 (rtu, pnt) 映射，只读源库并将变化字段直接 UPDATE 回各设备表。"""
        started = utc_now()
        source_name = self.default_source_database()

        # 第一阶段：获取各设备表绑定与当前值（命中 data_version 缓存时零 SQL 扫描）。
        bindings = None
        requested_kinds = None
        plan_key = None
        if _writer is not None and _plan_cache is not None:
            data_ver = _writer.execute("PRAGMA data_version").fetchone()[0]
            plan_key = (data_ver, self._config_epoch, source_name)
            if _plan_cache.get("key") == plan_key:
                bindings = _plan_cache["bindings"]
                requested_kinds = _plan_cache["requested"]
        if bindings is None:
            with self.connection(_writer=_writer) as read_con:
                bindings, requested_kinds = self._scan_bindings(read_con)
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
                    rtu_id,
                    pnt_no,
                    notuse,
                    old_val,
                    old_qual,
                    old_pre,
                    old_time,
                ) = entry
                cell_key = (table, idx, metric)
                prev_binding = self._last_bound_points.get(cell_key)
                rebound = prev_binding is not None and prev_binding != (rtu_id, pnt_no)
                if rebound:
                    val = None
                    qual = 128
                    pre = None
                    stamp = None
                else:
                    val = old_val
                    qual = old_qual
                    pre = old_pre
                    stamp = old_time
                reason = ""
                if source_failed:
                    reason = "source_error"
                else:
                    pts = source_points.get(kind)
                    point = pts.get((rtu_id, pnt_no)) if pts else None
                    if point is None:
                        reason = "missing_point"
                    else:
                        try:
                            new_raw = float(point[0])
                            new_qual = int(point[1])
                            new_stamp = int(point[2])
                            if not isfinite(new_raw) or not 0 <= new_qual <= 255:
                                raise ValueError("invalid sample")
                            if kind == "yx":
                                if new_raw not in (0, 1):
                                    raise ValueError("invalid status")
                                new_val = int(new_raw)
                            else:
                                new_val = new_raw
                            if (
                                not rebound
                                and (old_val is not None or old_time is not None)
                                and (new_stamp != old_time or new_val != old_val)
                            ):
                                pre = old_val
                            val = new_val
                            qual = new_qual
                            stamp = new_stamp
                            if qual:
                                reason = "bad_quality"
                            elif stamp <= 0:
                                reason = "missing_time"
                            elif stamp > future_limit:
                                reason = "future_time"
                            elif now_ms - stamp > max_age_ms:
                                reason = "stale"
                        except (ValueError, TypeError, OverflowError):
                            reason = "invalid_value"
                if reason in _INVALID_QUALITY_REASONS:
                    qual = (qual or 0) | 128
                if not reason and notuse:
                    reason = "notuse"
                valid = 0 if reason else 1
                valid_cnt += valid
                out_qual = (qual or 0) if valid else (qual or 0) | 128
                runtime_updates[(table, idx, metric)] = {
                    "valid": valid,
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
                            rtu_id,
                            pnt_no,
                            notuse,
                            val,
                            out_qual,
                            pre,
                            stamp,
                        )
                    )
                else:
                    next_bindings.append(entry)
            return valid_cnt, changes_by_row, next_bindings, runtime_updates

        valid_count, changes, next_bindings, runtime_updates = evaluate(bindings)

        # 第三阶段：仅当存在设备字段变化时开启写事务，直接按设备表批量 UPDATE。
        if changes:
            try:
                with self.connection(_writer=_writer) as con:
                    con.execute("BEGIN IMMEDIATE")
                    # 若在读源库期间有外部连接修改了设备库，重新扫描设备表后再计算差异写入。
                    if _writer is not None and plan_key is not None:
                        cur_ver = con.execute("PRAGMA data_version").fetchone()[0]
                        if (cur_ver, self._config_epoch, source_name) != plan_key:
                            bindings, requested_kinds = self._scan_bindings(con)
                            (
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

        if _plan_cache is not None and plan_key is not None:
            _plan_cache["key"] = plan_key
            _plan_cache["bindings"] = next_bindings
            _plan_cache["requested"] = requested_kinds

        finished = utc_now()
        with self._mutex:
            self._runtime_meta = runtime_updates
            self._last_bound_points = {
                (e[0], e[1], e[2]): (e[4], e[5]) for e in next_bindings
            }
            self._refresh_seq += 1
            refresh_id = self._refresh_seq
            self._refresh_history.appendleft(
                dict(
                    refresh_id=refresh_id,
                    started_at=started,
                    finished_at=finished,
                    total_count=len(bindings),
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
            total_count=len(bindings),
            valid_count=valid_count,
            updated_rows=len(changes),
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
        if kind not in ("yc", "yx"):
            raise ApiError(400, "仅支持遥测 YC、遥信 YX")
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
            total = con.execute(
                f"SELECT count(*) FROM fes_{kind}_define" + clause, args
            ).fetchone()[0]
            rows = con.execute(
                f"SELECT rtu_id,pnt_no,name,value,status,refresh_time FROM fes_{kind}_define"
                + clause
                + " ORDER BY rtu_id,pnt_no LIMIT 50 OFFSET ?",
                args + [(page - 1) * 50],
            ).fetchall()
        return dict(items=[dict(r) for r in rows], total=total, page=page, limit=50)

    def _ensure_device_table(self, con, table, initial_metrics=()):
        """确保设备宽表存在并包含指定量测列组。"""
        quoted = _quote_table(table)
        existing = con.execute(f"PRAGMA table_info({quoted})").fetchall()
        schema_changed = False
        if not existing:
            metrics = sorted(initial_metrics, key=metric_order)
            defs = ["idx INTEGER PRIMARY KEY", "name TEXT NOT NULL"]
            for metric, kind in metrics:
                for col, col_type, default in measurement_columns(metric, kind):
                    defs.append(f'"{col}" {col_type}{default}')
            con.execute(f"CREATE TABLE {quoted} ({', '.join(defs)})")
            schema_changed = True
        elif initial_metrics:
            existing_cols = {r[1]: str(r[2] or "").upper() for r in existing}
            for metric, kind in sorted(initial_metrics, key=metric_order):
                if metric in existing_cols:
                    expected_type = "INTEGER" if kind == "yx" else "REAL"
                    if existing_cols[metric] != expected_type:
                        raise ApiError(400, f"同一设备表的量测类型必须一致：{table}.{metric}")
                    continue
                for col, col_type, default in measurement_columns(metric, kind):
                    con.execute(f'ALTER TABLE {quoted} ADD COLUMN "{col}" {col_type}{default}')
                schema_changed = True
        if schema_changed:
            dev_tables = [t for t, _ in discover_device_tables(con)]
            sync_embedded_schema(con, include_tables=dev_tables)
        return schema_changed

    def create_device(self, data):
        name, category = str(data.get("name", "")).strip(), data.get("category")
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
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as src:
                src.execute("BEGIN")
                for (table,) in src.execute(
                    "SELECT table_name FROM rtdb_schema_tables ORDER BY ordinal"
                ):
                    if table in ("Model", "basevoltage", "HydroMedium"):
                        continue
                    fields = src.execute(
                        "SELECT ordinal,eng_name FROM rtdb_schema_columns WHERE table_name=? ORDER BY ordinal",
                        (table,),
                    ).fetchall()
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
                self._ensure_device_table(con, table, initial)
                quoted = _quote_table(table)
                rows = []
                for item in items:
                    idx = int(item["idx"])
                    name = str(item["name"])
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
                self._ensure_device_table(con, table)
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
            dev_tables = [t for t, _ in discover_device_tables(con)]
            sync_embedded_schema(con, include_tables=dev_tables)
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
        with self.connection() as con:
            con.execute("BEGIN")
            cur = con.cursor()
            cur.row_factory = None
            step = len(SUFFIXES)
            for table, metrics in self._get_table_schemas(con):
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
                    base = 2
                    for metric, _ in metrics:
                        rtu, pnt = row[base + 6], row[base + 7]
                        if rtu is not None and pnt is not None:
                            bound_cnt += 1
                            meta = self.runtime_meta_for(table, idx, metric)
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
                        base += step
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

        matched = []
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
            step = len(SUFFIXES)
            for table, metrics in self._get_table_schemas(con):
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
                    did = self.device_id_for(table, idx)
                    base = 2
                    for metric, kind in metrics:
                        rtu, pnt = row[base + 6], row[base + 7]
                        if rtu is not None and pnt is not None:
                            if filter_kind and kind != filter_kind:
                                base += step
                                continue
                            if kw and kw not in metric.lower() and kw not in str(dev_name).lower():
                                base += step
                                continue
                            val = row[base]
                            qual = row[base + 1]
                            pre = row[base + 2]
                            stamp = row[base + 3]
                            notuse = 1 if row[base + 4] else 0
                            weight = row[base + 5]
                            meta = self.runtime_meta_for(table, idx, metric)
                            valid, reason, collected_at = self._infer_status(
                                val, qual, stamp, notuse, rtu, pnt, meta
                            )
                            if only_invalid and valid:
                                base += step
                                continue
                            mid = self.measurement_id_for(table, idx, metric)
                            matched.append(
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
                                    version=self.version_for(table, idx, metric),
                                    device_name=dev_name,
                                    category=cat,
                                    source_database=source_db,
                                    rtu_id=rtu,
                                    pnt_no=pnt,
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
                        base += step
        total = len(matched)
        start = (page - 1) * limit
        return dict(
            items=matched[start : start + limit],
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
        if metric in ("idx", "name") or metric.endswith(SUFFIXES[1:]):
            raise ApiError(400, "属性名不能使用量测元数据后缀")

        source = data.get("source_database") or self.default_source_database()
        rtu = int(data["rtu_id"])
        point = int(data["pnt_no"])
        if rtu <= 0 or point < 0:
            raise ApiError(400, "RTU 编号必须为正整数，点号必须为非负整数")
        weight = float(data.get("weight", 1.0))
        notuse = int(data.get("notuse", 0))
        enabled = int(data.get("enabled", 1))
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
        if data.get("source_database"):
            self.set_source_database(source)

        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            old_target = None
            if measurement_id is not None:
                old_target = self.resolve_measurement_id(con, measurement_id)
                if old_target is None:
                    raise ApiError(404, "属性不存在")
                cur_ver = self.version_for(*old_target)
                if "version" in data and int(data["version"]) != cur_ver:
                    raise ApiError(409, "定义已被修改，请刷新后重试")

            if data.get("table") and data.get("idx") is not None:
                table, idx = str(data["table"]), int(data["idx"])
                self._table_name(table)
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
            row = con.execute(f"SELECT * FROM {quoted} WHERE idx=?", (idx,)).fetchone()
            if row is None:
                raise ApiError(400, "目标设备不存在")

            # 若编辑时更换了设备或属性名，先清空原属性绑定
            if old_target is not None and old_target != (table, idx, metric):
                old_tbl, old_idx, old_metric = old_target
                self._clear_binding_cell(con, old_tbl, old_idx, old_metric)

            # 若设备表尚无此量测列组，通过 ALTER TABLE 自动追加 8 列
            self._ensure_device_table(con, table, [(metric, kind)])
            # 重新读取当前行的该字段旧值
            cur_vals = con.execute(
                f"SELECT {_quote_col(metric + '_rtu')}, {_quote_col(metric + '_pnt')}, {_quote_col(metric + '_qual')} FROM {quoted} WHERE idx=?",
                (idx,),
            ).fetchone()
            point_changed = cur_vals[0] != rtu or cur_vals[1] != point
            if point_changed:
                con.execute(
                    f"""UPDATE {quoted} SET
                        {_quote_col(metric)}=NULL,
                        {_quote_col(metric + '_qual')}=128,
                        {_quote_col(metric + '_pre')}=NULL,
                        {_quote_col(metric + '_time')}=NULL,
                        {_quote_col(metric + '_notuse')}=?,
                        {_quote_col(metric + '_weight')}=?,
                        {_quote_col(metric + '_rtu')}=?,
                        {_quote_col(metric + '_pnt')}=?
                    WHERE idx=?""",
                    (notuse, weight, rtu, point, idx),
                )
            else:
                new_qual = (cur_vals[2] or 0) | 128
                con.execute(
                    f"""UPDATE {quoted} SET
                        {_quote_col(metric + '_qual')}=?,
                        {_quote_col(metric + '_notuse')}=?,
                        {_quote_col(metric + '_weight')}=?
                    WHERE idx=?""",
                    (new_qual, notuse, weight, idx),
                )
            mid = self.measurement_id_for(table, idx, metric)
            did = self.device_id_for(table, idx, con=con)
            if measurement_id is None:
                ver = self.version_for(table, idx, metric)
            else:
                ver = self.bump_version(table, idx, metric)
            self.set_runtime_meta(
                table,
                idx,
                metric,
                valid=0,
                reason="definition_changed",
                collected_at=None,
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
                enabled=1,
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
                {_quote_col(metric + '_qual')}=128,
                {_quote_col(metric + '_pre')}=NULL,
                {_quote_col(metric + '_time')}=NULL,
                {_quote_col(metric + '_notuse')}=1,
                {_quote_col(metric + '_weight')}=1,
                {_quote_col(metric + '_rtu')}=NULL,
                {_quote_col(metric + '_pnt')}=NULL
            WHERE idx=?""",
            (idx,),
        )
        with self._mutex:
            self._runtime_meta.pop((table, int(idx), str(metric)), None)

    def _prune_unused_metric_column(self, con, table, metric):
        """若该表所有设备均未绑定该指标且不在预设未绑定清单中，移除该 8 列并同步 RTDB 元数据。"""
        profile = UNBOUND_PROFILES.get(table)
        if profile and (metric in profile[0] or metric in profile[1]):
            return
        quoted = _quote_table(table)
        still_used = con.execute(
            f"SELECT 1 FROM {quoted} WHERE {_quote_col(metric + '_rtu')} IS NOT NULL OR {_quote_col(metric + '_pnt')} IS NOT NULL LIMIT 1"
        ).fetchone()
        if still_used:
            return
        metrics = self.table_metrics(con, table) or []
        remaining = [(m, k) for m, k in metrics if m != metric]
        keep_cols = ["idx", "name"]
        defs = ["idx INTEGER PRIMARY KEY", "name TEXT NOT NULL"]
        for m, k in remaining:
            for col, col_type, default in measurement_columns(m, k):
                keep_cols.append(col)
                defs.append(f'"{col}" {col_type}{default}')
        tmp_name = f"__rebuild_{table}"
        tmp_quoted = f'"{tmp_name}"'
        cols_sql = ", ".join(_quote_col(c) for c in keep_cols)
        con.execute(f"CREATE TABLE {tmp_quoted} ({', '.join(defs)})")
        con.execute(f"INSERT INTO {tmp_quoted}({cols_sql}) SELECT {cols_sql} FROM {quoted}")
        con.execute(f"DROP TABLE {quoted}")
        con.execute(f"ALTER TABLE {tmp_quoted} RENAME TO {quoted}")
        dev_tables = [t for t, _ in discover_device_tables(con)]
        sync_embedded_schema(con, include_tables=dev_tables)

    def delete_measurement(self, measurement_id, version=None):
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            target = self.resolve_measurement_id(con, measurement_id)
            if target is None:
                raise ApiError(409, "属性不存在或已被修改")
            table, idx, metric = target
            if version is not None and int(version) != self.version_for(table, idx, metric):
                raise ApiError(409, "属性不存在或已被修改")
            self._clear_binding_cell(con, table, idx, metric)
            self._prune_unused_metric_column(con, table, metric)
            self._record_change(
                "mapping_delete",
                int(measurement_id),
                dict(table=table, idx=idx, metric=metric),
                {},
            )
        self.bump_config_epoch()

    def overview(self):
        categories_map = defaultdict(int)
        device_count = 0
        bound_count = 0
        valid_count = 0
        with self.connection() as con:
            con.execute("BEGIN")
            cur = con.cursor()
            cur.row_factory = None
            step = len(SUFFIXES)
            for table, metrics in self._get_table_schemas(con):
                quoted = _quote_table(table)
                cat = category_for(table)
                if not metrics:
                    cnt = cur.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
                    device_count += cnt
                    categories_map[cat] += cnt
                    continue
                for row in cur.execute(f"SELECT * FROM {quoted}"):
                    idx = row[0]
                    device_count += 1
                    categories_map[cat] += 1
                    base = 2
                    for metric, _ in metrics:
                        rtu, pnt = row[base + 6], row[base + 7]
                        if rtu is not None and pnt is not None:
                            bound_count += 1
                            meta = self.runtime_meta_for(table, idx, metric)
                            valid, _, _ = self._infer_status(
                                row[base],
                                row[base + 1],
                                row[base + 3],
                                row[base + 4],
                                rtu,
                                pnt,
                                meta,
                            )
                            valid_cnt_inc = valid
                            valid_count += valid_cnt_inc
                        base += step
        with self._mutex:
            last = dict(self._refresh_history[0]) if self._refresh_history else None
            interval = self._interval_seconds
        source_db = self.default_source_database()
        return dict(
            counts=dict(
                device=device_count,
                device_measurement=bound_count,
                scada_point_mapping=bound_count,
                valid=valid_count,
            ),
            last_refresh=last,
            categories=[
                dict(category=cat, count=cnt)
                for cat, cnt in sorted(categories_map.items())
            ],
            category_labels=CATEGORIES,
            interval_seconds=interval,
            sources=[source_db] if source_db else [],
        )

    def history(self):
        with self._mutex:
            return dict(
                refreshes=[dict(r) for r in self._refresh_history],
                changes=[dict(r) for r in self._change_history],
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
                    try:
                        next_scan = time.monotonic()
                        last_scan = None
                        period = None
                        while not self._stop.is_set():
                            self._wake.clear()
                            sample_due = False
                            try:
                                if writer is None:
                                    writer = self.store._open_connection()
                                new_period = self.store.get_interval()
                                now = time.monotonic()
                                if new_period != period and last_scan is not None:
                                    next_scan = max(now, last_scan + new_period)
                                period = new_period
                                if now < next_scan:
                                    self._wake.wait(min(next_scan - now, 1))
                                    continue
                                if self._stop.is_set():
                                    break
                                last_scan = next_scan
                                sample_due = True
                                result = self.store.collect_once(
                                    _writer=writer,
                                    _source_cache=source_cache,
                                    _plan_cache=plan_cache,
                                )
                                self.last_error = "; ".join(result["errors"].values())
                            except Exception as exc:
                                self.last_error = str(exc)
                                plan_cache.clear()
                                if writer is not None:
                                    writer.close()
                                    writer = None
                                if not sample_due:
                                    self._wake.wait(1)
                                    continue
                            next_scan += period
                            now = time.monotonic()
                            if next_scan <= now:
                                next_scan += period * (
                                    1 + math.floor((now - next_scan) / period)
                                )
                            self._wake.wait(min(next_scan - now, 1))
                    finally:
                        try:
                            for entry in source_cache.values():
                                try:
                                    entry[0].close()
                                except sqlite3.Error:
                                    pass
                            source_cache.clear()
                            if writer is not None:
                                writer.close()
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

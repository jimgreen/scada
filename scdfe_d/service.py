"""Transactional device measurements. Source SCADA databases are always read-only."""

from collections import defaultdict
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import threading
import time
from types import SimpleNamespace

from core.scdfe_control import InstanceLock
from core.rtdb_schema import sync_embedded_schema
from scdfe_c.web.service import ApiError
from scdfe_d.measurement_layout import (
    EXTENSION_METRICS,
    SUFFIXES,
    UNBOUND_PROFILES,
    measurement_columns,
    metric_order,
)

APPLICATION_ID = int.from_bytes(b"SDFD", "big")
DEFAULT_INTERVAL_SECONDS = 5
MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 60
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
BASE_TABLES = {
    "device",
    "device_measurement",
    "scada_point_mapping",
    "measurement_refresh",
    "measurement_value",
    "input_change_log",
}
EXTENSIONS = {
    "ACWindGen": ("ACGenerator", "idx_acgenerator"),
    "ACPVGen": ("ACGenerator", "idx_acgenerator"),
    "ACDieselGen": ("ACGenerator", "idx_acgenerator"),
    "ACStorageGen": ("ACGenerator", "idx_acgenerator"),
    "DCPVGen": ("DCGenerator", "idx_dcgenerator"),
    "DCStorageGen": ("DCGenerator", "idx_dcgenerator"),
}
REQUIRED = {
    "device": {"device_id", "model_table", "model_idx", "name", "model_json", "active"},
    "device_measurement": {
        "measurement_id",
        "device_id",
        "metric",
        "kind",
        "name",
        "unit",
        "weight",
        "max_age_seconds",
        "se_enabled",
        "se_type",
        "origin",
        "catalog_active",
        "version",
        "updated_at",
    },
    "scada_point_mapping": {
        "measurement_id",
        "source_database",
        "rtu_id",
        "pnt_no",
        "enabled",
        "scale",
        "offset",
        "version",
        "updated_at",
    },
    "measurement_refresh": {
        "refresh_id",
        "started_at",
        "finished_at",
        "total_count",
        "valid_count",
        "se_valid_count",
        "source_database",
        "error",
    },
    "measurement_value": {
        "measurement_id",
        "refresh_id",
        "source_value",
        "value",
        "quality",
        "source_time_ms",
        "collected_at",
        "valid",
        "reason",
    },
    "input_change_log": {
        "change_id",
        "changed_at",
        "entity",
        "entity_id",
        "before_json",
        "after_json",
    },
}


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


def stored_interval(con):
    row = con.execute(
        "SELECT value FROM d_settings WHERE key='interval_seconds'"
    ).fetchone()
    if row is None:
        return DEFAULT_INTERVAL_SECONDS
    try:
        seconds = float(row[0])
        if math.isfinite(seconds):
            return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, int(seconds)))
    except (TypeError, ValueError):
        pass
    return DEFAULT_INTERVAL_SECONDS


def category_for(table, model):
    kind = str(model.get("dev_type", "")).lower()
    if "storage" in kind or "Storage" in table:
        return "storage"
    for word, category in [
        ("Converter", "converter"),
        ("Generator", "generator"),
        ("Load", "load"),
        ("Break", "switch"),
        ("Branch", "line"),
        ("Node", "bus"),
        ("RealBs", "compensator"),
    ]:
        if word in table:
            return category
    return "other"


def validate_database(path):
    try:
        path = Path(path).resolve()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("数据库完整性检查失败")
            if con.execute("PRAGMA application_id").fetchone()[0] not in (
                0,
                APPLICATION_ID,
            ):
                raise ValueError("不是 scdfe_d 设备数据库")
            for table, columns in REQUIRED.items():
                found = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
                if not columns <= found:
                    raise ValueError(f"缺少设备库表或字段：{table}")
            if con.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("设备库存在无效的关联关系")
            counts = {
                name: con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in ("device", "device_measurement", "scada_point_mapping")
            }
            if con.execute(
                "SELECT 1 FROM device_measurement WHERE kind NOT IN ('yc','yx') OR weight<=0 OR max_age_seconds<=0 LIMIT 1"
            ).fetchone():
                raise ValueError("设备量测定义不合法")
            for (raw,) in con.execute("SELECT model_json FROM device"):
                if not isinstance(json.loads(raw), dict):
                    raise ValueError("设备模型必须是 JSON 对象")
        return dict(valid=True, message="设备数据库校验通过", counts=counts)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        return dict(valid=False, message=f"无效的设备数据库：{exc}")


class DeviceStore:
    def __init__(self, db_path, schema_dir=None, *, workspace_root=None, connection_pool_size=0):
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
        self._table_types_cache = {}
        existing = self.db_path.is_file() and self.db_path.stat().st_size > 0
        if existing:
            result = validate_database(self.db_path)
            if not result["valid"]:
                raise ValueError(result["message"])
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._open_connection()) as init_con:
            mode = str(init_con.execute("PRAGMA journal_mode").fetchone()[0] or "").lower()
            if mode != "wal":
                init_con.execute("PRAGMA journal_mode=WAL")
            if existing and self._schema_ready(init_con):
                return
        with self.connection() as con:
            con.executescript(
                "BEGIN IMMEDIATE;\n"
                + Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            )
            for table, name, definition in [
                ("device", "category", "TEXT NOT NULL DEFAULT 'other'"),
                ("device_measurement", "notuse", "INTEGER NOT NULL DEFAULT 0"),
                ("device_measurement", "quantity_table", "TEXT NOT NULL DEFAULT ''"),
                ("measurement_value", "previous_value", "REAL"),
            ]:
                if name not in {
                    r[1] for r in con.execute(f"PRAGMA table_info({table})")
                }:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    if name == "category":
                        for row in con.execute(
                            "SELECT device_id,model_table,model_json FROM device"
                        ).fetchall():
                            con.execute(
                                "UPDATE device SET category=? WHERE device_id=?",
                                (
                                    category_for(
                                        row["model_table"],
                                        json.loads(row["model_json"]),
                                    ),
                                    row["device_id"],
                                ),
                            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS d_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS d_device_tables (table_name TEXT PRIMARY KEY)"
            )
            con.execute("""CREATE TABLE IF NOT EXISTS d_device_members (table_name TEXT NOT NULL REFERENCES d_device_tables(table_name),
                idx INTEGER NOT NULL,name TEXT NOT NULL,device_id INTEGER NOT NULL REFERENCES device(device_id),PRIMARY KEY(table_name,idx))""")
            con.execute(
                "INSERT OR IGNORE INTO d_settings VALUES('interval_seconds',?)",
                (str(DEFAULT_INTERVAL_SECONDS),),
            )
            seconds = str(stored_interval(con))
            con.execute(
                "UPDATE d_settings SET value=? WHERE key='interval_seconds' AND value<>?",
                (seconds, seconds),
            )
            # The imported catalog used a file name relative to the device DB.
            # Convert this legacy format once to paths relative to the Web root.
            if not con.execute(
                "SELECT 1 FROM d_settings WHERE key='root_paths'"
            ).fetchone():
                for (value,) in con.execute(
                    "SELECT DISTINCT source_database FROM scada_point_mapping"
                ).fetchall():
                    path = Path(value)
                    path = (
                        path if path.is_absolute() else self.db_path.parent / path
                    ).resolve()
                    if not path.is_relative_to(self.root):
                        raise ValueError("已有映射源数据库超出工作区根目录")
                    con.execute(
                        "UPDATE scada_point_mapping SET source_database=? WHERE source_database=?",
                        (path.relative_to(self.root).as_posix(), value),
                    )
                con.execute("INSERT INTO d_settings VALUES('root_paths','1')")
            con.execute(f"PRAGMA application_id={APPLICATION_ID}")
            con.execute("PRAGMA user_version=3")
            con.execute(
                "CREATE INDEX IF NOT EXISTS d_measurement_device ON device_measurement(device_id,metric)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS d_device_members_device_table ON d_device_members(device_id,table_name)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS d_measurement_value_refresh ON measurement_value(refresh_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS d_measurement_active_device ON device_measurement(catalog_active,device_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS d_measurement_value_valid ON measurement_value(valid)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS d_device_category ON device(category)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS d_mapping_source_db ON scada_point_mapping(source_database)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS d_measurement_qty_device ON device_measurement(quantity_table,device_id)"
            )
            self._tables(con, sync_schema=True)

    @staticmethod
    def _schema_ready(con):
        """检查已有设备库是否已满足最新版本结构与索引，命中时跳过全量重建。"""
        if con.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            return False
        if con.execute("PRAGMA user_version").fetchone()[0] != 3:
            return False
        objects = {
            (row[0], row[1])
            for row in con.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        required_tables = {
            "d_settings",
            "d_device_tables",
            "d_device_members",
            "rtdb_schema_tables",
            "rtdb_schema_columns",
        }
        if not all(("table", name) in objects for name in required_tables):
            return False
        required_indexes = {
            "d_measurement_device",
            "d_device_members_device_table",
            "d_measurement_value_refresh",
            "d_measurement_active_device",
            "d_measurement_value_valid",
            "d_device_category",
            "d_mapping_source_db",
            "d_measurement_qty_device",
        }
        if not all(("index", name) in objects for name in required_indexes):
            return False
        for table, column in (
            ("device", "category"),
            ("device_measurement", "notuse"),
            ("device_measurement", "quantity_table"),
            ("measurement_value", "previous_value"),
        ):
            if column not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
                return False
        settings = dict(
            con.execute(
                "SELECT key, value FROM d_settings WHERE key IN ('root_paths','interval_seconds')"
            ).fetchall()
        )
        if settings.get("root_paths") != "1":
            return False
        interval_raw = settings.get("interval_seconds")
        if interval_raw is None or interval_raw != str(stored_interval(con)):
            return False
        if con.execute(
            "SELECT 1 FROM device_measurement WHERE catalog_active=1 AND quantity_table='' LIMIT 1"
        ).fetchone():
            return False
        registered_tables = {
            row[0] for row in con.execute("SELECT table_name FROM d_device_tables")
        }
        device_tables = {
            row[0] for row in con.execute("SELECT DISTINCT model_table FROM device")
        }
        if not device_tables <= registered_tables:
            return False
        if not all(("table", name) in objects for name in registered_tables):
            return False
        return True

    def bump_config_epoch(self):
        """配置变更后递增内存版本号，使采集器的映射缓存及时失效。"""
        with self._mutex:
            self._config_epoch += 1

    def table_column_types(self, con, table):
        """按 schema_version 缓存设备宽表列类型，避免高频实时轮询重复执行 PRAGMA。"""
        schema_ver = con.execute("PRAGMA schema_version").fetchone()[0]
        cache_key = (table, schema_ver)
        with self._mutex:
            cached = self._table_types_cache.get(cache_key)
        if cached is not None:
            return cached
        table_sql = self._table_name(table)
        types = {row[1]: row[2] for row in con.execute(f"PRAGMA table_info({table_sql})")}
        with self._mutex:
            # 清理同一表旧 schema_version 缓存，防止字典膨胀
            for key in [k for k in self._table_types_cache if k[0] == table]:
                self._table_types_cache.pop(key, None)
            self._table_types_cache[cache_key] = types
        return types

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
            con = self._connection_pool.pop() if (use_pool and self._connection_pool) else None
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
                    if not self._pool_closed and len(self._connection_pool) < self.connection_pool_size:
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
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Z][A-Za-z0-9_]{0,63}", name
        ):
            raise ValueError("模型设备表名称不合法")
        return '"' + name + '"'

    def _tables(
        self,
        con,
        *,
        sync_schema=False,
        target_tables=None,
        target_device_ids=None,
    ):
        """Reconcile device tables to their model membership and owned quantities."""
        full_mode = target_tables is None and target_device_ids is None
        if target_device_ids is not None:
            dev_ids = sorted({int(did) for did in target_device_ids})
            if dev_ids:
                placeholders = ",".join("?" for _ in dev_ids)
                dev_rows = con.execute(
                    f"SELECT device_id,model_table,model_idx,name FROM device WHERE device_id IN ({placeholders})",
                    dev_ids,
                ).fetchall()
            else:
                dev_rows = []
        else:
            dev_rows = con.execute(
                "SELECT device_id,model_table,model_idx,name FROM device"
            ).fetchall()
        table_rows = []
        member_rows = []
        seen_tables = set()
        for d in dev_rows:
            model_table = d["model_table"]
            self._table_name(model_table)
            if model_table not in seen_tables:
                seen_tables.add(model_table)
                table_rows.append((model_table,))
            member_rows.append((model_table, d["model_idx"], d["name"], d["device_id"]))
        if table_rows:
            con.executemany("INSERT OR IGNORE INTO d_device_tables VALUES(?)", table_rows)
        if member_rows:
            con.executemany(
                "INSERT INTO d_device_members VALUES(?,?,?,?) ON CONFLICT(table_name,idx) DO UPDATE SET name=excluded.name,device_id=excluded.device_id",
                member_rows,
            )
        tables = [
            row[0]
            for row in con.execute(
                "SELECT table_name FROM d_device_tables ORDER BY table_name"
            )
        ]
        if full_mode:
            scope_set = set(tables)
        else:
            scope_set = set(target_tables or ()) | seen_tables
            if target_device_ids:
                dev_ids = sorted({int(did) for did in target_device_ids})
                placeholders = ",".join("?" for _ in dev_ids)
                for (tbl,) in con.execute(
                    f"SELECT DISTINCT table_name FROM d_device_members WHERE device_id IN ({placeholders})",
                    dev_ids,
                ):
                    scope_set.add(tbl)
            scope_set &= set(tables)

        metrics_by_table = {table: {} for table in tables}
        locations = {}
        ext_tables = tuple(EXTENSION_METRICS)
        ext_placeholders = ",".join("?" for _ in ext_tables)
        for table, device_id in con.execute(
            f"SELECT table_name,device_id FROM d_device_members WHERE table_name IN ({ext_placeholders})",
            ext_tables,
        ):
            for metric in EXTENSION_METRICS[table]:
                key = (device_id, metric)
                if key in locations:
                    raise ApiError(
                        400, f"同一设备存在重复量测扩展定义：{table}.{metric}"
                    )
                locations[key] = table
        for table, (analog, signals) in UNBOUND_PROFILES.items():
            if table in metrics_by_table:
                metrics_by_table[table].update(dict.fromkeys(analog, "yc"))
                metrics_by_table[table].update(dict.fromkeys(signals, "yx"))

        if full_mode:
            active = con.execute(
                """SELECT m.measurement_id,m.device_id,m.metric,m.kind,m.quantity_table,d.model_table
                FROM device_measurement m JOIN device d USING(device_id) WHERE m.catalog_active=1"""
            ).fetchall()
        elif scope_set:
            t_list = sorted(scope_set)
            t_ph = ",".join("?" for _ in t_list)
            conds = [
                f"m.quantity_table IN ({t_ph})",
                f"d.model_table IN ({t_ph})",
                "m.quantity_table=''",
            ]
            params = t_list + t_list
            if target_device_ids:
                d_list = sorted({int(did) for did in target_device_ids})
                d_ph = ",".join("?" for _ in d_list)
                conds.append(f"m.device_id IN ({d_ph})")
                params.extend(d_list)
            active = con.execute(
                f"""SELECT m.measurement_id,m.device_id,m.metric,m.kind,m.quantity_table,d.model_table
                FROM device_measurement m JOIN device d USING(device_id)
                WHERE m.catalog_active=1 AND ({' OR '.join(conds)})""",
                params,
            ).fetchall()
        else:
            active = []

        qty_updates = []
        for row in active:
            metric, kind = row["metric"], row["kind"]
            if (
                not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", metric)
                or metric in {"idx", "name"}
                or metric.endswith(SUFFIXES[1:])
            ):
                raise ApiError(400, f"量测属性名无效：{metric}")
            table = locations.get((row["device_id"], metric), row["model_table"])
            current_kind = metrics_by_table[table].setdefault(metric, kind)
            if current_kind != kind:
                raise ApiError(400, f"同一设备表的量测类型必须一致：{table}.{metric}")
            if row["quantity_table"] != table:
                qty_updates.append((table, row["measurement_id"]))
                if not full_mode:
                    scope_set.add(table)
        if qty_updates:
            con.executemany(
                "UPDATE device_measurement SET quantity_table=? WHERE measurement_id=?",
                qty_updates,
            )

        scope_tables = [t for t in tables if t in scope_set]
        # 一次性加载当前所有触发器定义，避免逐量测查询 sqlite_master
        existing_triggers = {
            row[0]: row[1]
            for row in con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            )
        }
        rebuilt_tables = set()
        for table in scope_tables:
            name = self._table_name(table)
            metrics = sorted(metrics_by_table[table].items(), key=metric_order)
            columns = [
                column
                for metric, kind in metrics
                for column in measurement_columns(metric, kind)
            ]
            expected = [("idx", "INTEGER", None, 1), ("name", "TEXT", None, 0)]
            expected.extend(
                (column, kind, default.removeprefix(" DEFAULT ") or None, 0)
                for column, kind, default in columns
            )
            actual = [
                (row[1], row[2], row[4], row[5])
                for row in con.execute(f"PRAGMA table_info({name})")
            ]
            if actual != expected:
                if actual:
                    con.execute(f"DROP TABLE {name}")
                definitions = ["idx INTEGER PRIMARY KEY", "name TEXT NOT NULL"]
                definitions.extend(
                    f'"{column}" {kind}{default}' for column, kind, default in columns
                )
                con.execute(f"CREATE TABLE {name} ({', '.join(definitions)})")
                sync_schema = True
                rebuilt_tables.add(table)
                # DROP TABLE 会级联删除该表触发器，需清除对应的内存缓存项
                prefix = f"d_binding_{table}_"
                for trig in [k for k in existing_triggers if k.startswith(prefix)]:
                    existing_triggers.pop(trig, None)
            for metric, _ in metrics:
                self._ensure_binding_trigger(
                    con, table, metric, existing_triggers=existing_triggers
                )
            con.execute(
                f"DELETE FROM {name} WHERE idx NOT IN (SELECT idx FROM d_device_members WHERE table_name=?)",
                (table,),
            )
            con.execute(
                f"INSERT INTO {name}(idx,name) SELECT idx,name FROM d_device_members WHERE table_name=? ON CONFLICT(idx) DO UPDATE SET name=excluded.name WHERE name IS NOT excluded.name",
                (table,),
            )
        if full_mode:
            for category in CATEGORIES:
                con.execute(f"DROP VIEW IF EXISTS device_{category}")
        self._write_tables(
            con,
            target_tables=None if full_mode else scope_tables,
            target_device_ids=None if full_mode else target_device_ids,
            full_tables=rebuilt_tables,
        )
        if sync_schema:
            sync_embedded_schema(con, include_tables=tables)

    def _ensure_binding_trigger(self, con, table, metric, *, existing_triggers=None):
        # Direct edits to an existing mapping are reflected in the collector's definition.
        name = self._table_name(table)
        mid = f"""(SELECT m.measurement_id FROM device_measurement m JOIN d_device_members b USING(device_id)
            WHERE b.table_name='{table}' AND b.idx=NEW.idx AND m.quantity_table=b.table_name
                AND m.metric='{metric}' AND m.catalog_active=1)"""
        trigger_name = f"d_binding_{table}_{metric}"
        trigger = f'"{trigger_name}"'
        definition = f'''CREATE TRIGGER {trigger} AFTER UPDATE OF "{metric}_rtu","{metric}_pnt","{metric}_notuse","{metric}_weight" ON {name}
                    WHEN EXISTS(SELECT 1 FROM device_measurement m JOIN scada_point_mapping s USING(measurement_id)
                      WHERE m.measurement_id={mid} AND (s.rtu_id IS NOT NEW."{metric}_rtu" OR s.pnt_no IS NOT NEW."{metric}_pnt"
                        OR m.notuse IS NOT NEW."{metric}_notuse" OR m.weight IS NOT NEW."{metric}_weight")) BEGIN
                    SELECT CASE WHEN NEW."{metric}_notuse" NOT IN (0,1) OR NEW."{metric}_weight"<=0
                      OR NEW."{metric}_notuse" IS NULL OR NEW."{metric}_weight" IS NULL
                      THEN RAISE(ABORT,'invalid device measurement configuration') END;
                    DELETE FROM measurement_value WHERE measurement_id={mid} AND EXISTS(SELECT 1 FROM scada_point_mapping s
                      WHERE s.measurement_id={mid} AND (s.rtu_id IS NOT NEW."{metric}_rtu" OR s.pnt_no IS NOT NEW."{metric}_pnt"));
                    UPDATE scada_point_mapping SET rtu_id=NEW."{metric}_rtu",pnt_no=NEW."{metric}_pnt",version=version+1,
                      updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE measurement_id={mid};
                    UPDATE device_measurement SET notuse=NEW."{metric}_notuse",weight=NEW."{metric}_weight",version=version+1,
                      updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE measurement_id={mid};
                    UPDATE measurement_value SET valid=0,reason='definition_changed' WHERE measurement_id={mid};
                    END'''
        if existing_triggers is not None:
            current_sql = existing_triggers.get(trigger_name)
        else:
            row = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()
            current_sql = row[0] if row is not None else None
        if current_sql != definition:
            con.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            con.execute(definition)
            if existing_triggers is not None:
                existing_triggers[trigger_name] = definition

    def _write_tables(
        self, con, *, target_tables=None, target_device_ids=None, full_tables=None
    ):
        if target_tables is None:
            tables = [
                row[0] for row in con.execute("SELECT table_name FROM d_device_tables")
            ]
        else:
            tables = list(target_tables)
        if not tables:
            return
        full_set = set(full_tables or ())
        dev_filter = (
            sorted({int(did) for did in target_device_ids})
            if target_device_ids is not None
            else None
        )
        need_all_devs_for_some = dev_filter is None or any(t in full_set for t in tables)

        meas_sql = """SELECT m.device_id,m.quantity_table,m.metric,m.notuse,m.weight,
                s.rtu_id,s.pnt_no,v.value,v.previous_value,v.quality,v.source_time_ms,v.valid
                FROM device_measurement m LEFT JOIN scada_point_mapping s USING(measurement_id)
                LEFT JOIN measurement_value v USING(measurement_id) WHERE m.catalog_active=1"""
        meas_params = []
        if target_tables is not None:
            t_ph = ",".join("?" for _ in tables)
            meas_sql += f" AND m.quantity_table IN ({t_ph})"
            meas_params.extend(tables)
        if not need_all_devs_for_some and dev_filter:
            d_ph = ",".join("?" for _ in dev_filter)
            meas_sql += f" AND m.device_id IN ({d_ph})"
            meas_params.extend(dev_filter)

        cur = con.cursor()
        cur.row_factory = None
        measurements = {}
        for row in cur.execute(meas_sql, meas_params):
            measurements.setdefault((row[0], row[1]), []).append(row)

        for table in tables:
            name = self._table_name(table)
            columns = [r[1] for r in con.execute(f"PRAGMA table_info({name})")][2:]
            if not columns:
                continue
            # Include default values so removed properties never leave stale data.
            base = {
                c: 128
                if c.endswith("_qual")
                else 1
                if c.endswith(("_notuse", "_weight"))
                else None
                for c in columns
            }
            if dev_filter is not None and table not in full_set:
                if not dev_filter:
                    continue
                d_ph = ",".join("?" for _ in dev_filter)
                members_iter = cur.execute(
                    f"SELECT idx,device_id FROM d_device_members WHERE table_name=? AND device_id IN ({d_ph})",
                    [table] + dev_filter,
                )
            else:
                members_iter = cur.execute(
                    "SELECT idx,device_id FROM d_device_members WHERE table_name=?",
                    (table,),
                )
            rows = []
            for idx, device_id in members_iter:
                values = dict(base)
                for (
                    _did,
                    _qtbl,
                    metric,
                    notuse,
                    weight,
                    rtu_id,
                    pnt_no,
                    val,
                    prev_val,
                    qual,
                    src_time,
                    valid,
                ) in measurements.get((device_id, table), ()):
                    quality = (qual or 0) if valid else (qual or 0) | 128
                    group = (
                        val,
                        quality,
                        prev_val,
                        src_time,
                        notuse,
                        weight,
                        rtu_id,
                        pnt_no,
                    )
                    for suffix, value in zip(SUFFIXES, group):
                        col = metric + suffix
                        if col in values:
                            values[col] = value
                rows.append(tuple(values[c] for c in columns) + (idx,))
            if rows:
                con.executemany(
                    f"UPDATE {name} SET "
                    + ",".join('"' + c + '"=?' for c in columns)
                    + " WHERE idx=?",
                    rows,
                )

    def _write_runtime_values(self, con, changes):
        """Publish just the changed runtime fields, without invoking binding triggers."""
        if not changes:
            return
        updates = defaultdict(list)
        device_ids = sorted({device_id for _, device_id in changes})
        if len(device_ids) <= 500:
            placeholders = ",".join("?" for _ in device_ids)
            rows_iter = con.execute(
                f"SELECT table_name,device_id,idx FROM d_device_members WHERE device_id IN ({placeholders})",
                device_ids,
            )
        else:
            rows_iter = con.execute(
                "SELECT table_name,device_id,idx FROM d_device_members"
            )
        for table, device_id, idx in rows_iter:
            fields = changes.get((table, device_id))
            if fields:
                updates[table, tuple(fields)].append((*fields.values(), idx))
        for (table, columns), rows in updates.items():
            # Column names are validated by _tables; quote them also when reading
            # a database that another SQLite client might have changed.
            assignment = ",".join(
                '"' + column.replace('"', '""') + '"=?' for column in columns
            )
            con.executemany(
                f"UPDATE {self._table_name(table)} SET {assignment} WHERE idx=?", rows
            )

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
        # Small, scattered bindings can use the source table's unique point key
        # instead of scanning every point. For denser sets on a few RTUs, filter
        # by the leading primary-key column rtu_id to avoid scanning unrelated RTUs.
        # Keep the full duplicate check for legacy sources without that key.
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

    def import_model(self, relative_path):
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
        with self.connection() as con:
            con.executemany(
                "INSERT OR IGNORE INTO d_device_tables VALUES(?)",
                [(table,) for table in model],
            )
            device_rows = []
            for table, items in model.items():
                if table in EXTENSIONS:
                    continue
                for item in items:
                    device_rows.append(
                        (
                            table,
                            int(item["idx"]),
                            item["name"],
                            json.dumps(item, ensure_ascii=False),
                            category_for(table, item),
                        )
                    )
            if device_rows:
                con.executemany(
                    """INSERT INTO device(model_table,model_idx,name,model_json,category) VALUES(?,?,?,?,?)
                    ON CONFLICT(model_table,model_idx) DO UPDATE SET name=excluded.name,model_json=excluded.model_json,category=excluded.category""",
                    device_rows,
                )
            # Extension tables use their own idx and the owning generator's name.
            # Their property values and mappings reference the same physical device.
            ext_needed_parents = sorted(
                {
                    parent
                    for table, (parent, _key) in EXTENSIONS.items()
                    if model.get(table)
                }
            )
            parent_lookup = {}
            if ext_needed_parents:
                ph = ",".join("?" for _ in ext_needed_parents)
                cur = con.cursor()
                cur.row_factory = None
                for m_table, m_idx, d_id, d_name in cur.execute(
                    f"SELECT model_table,model_idx,device_id,name FROM device WHERE model_table IN ({ph})",
                    ext_needed_parents,
                ):
                    parent_lookup[(m_table, m_idx)] = (d_id, d_name)
            ext_member_rows = []
            for table, (parent, key) in EXTENSIONS.items():
                for item in model.get(table, []):
                    found = parent_lookup.get((parent, item[key]))
                    if not found:
                        raise ApiError(400, f"{table} 中的设备关联不存在")
                    ext_member_rows.append((table, int(item["idx"]), found[1], found[0]))
            if ext_member_rows:
                con.executemany(
                    """INSERT INTO d_device_members VALUES(?,?,?,?) ON CONFLICT(table_name,idx)
                    DO UPDATE SET name=excluded.name,device_id=excluded.device_id""",
                    ext_member_rows,
                )
            self._tables(con)
            con.execute(
                "INSERT INTO d_settings VALUES('model_database',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (relative_path,),
            )
            self._audit(
                con,
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
            return [
                dict(table_name=r[0], count=r[1])
                for r in con.execute(
                    "SELECT t.table_name,count(m.idx) FROM d_device_tables t LEFT JOIN d_device_members m USING(table_name) GROUP BY t.table_name ORDER BY t.table_name"
                )
            ]

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
    def source(self, value):
        path = self.source_path(value)
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
                    if (
                        not {
                            "rtu_id",
                            "pnt_no",
                            "name",
                            "value",
                            "status",
                            "refresh_time",
                        }
                        <= columns
                    ):
                        raise ApiError(400, "源库缺少遥测/遥信点表或必要字段")
                con.execute("BEGIN")
                yield con
        except sqlite3.Error as exc:
            raise ApiError(400, "源数据库不存在、不可读取或结构不合法") from exc

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
        with self.source(query.get("source_database")) as con:
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

    def create_device(self, data):
        name, category = str(data.get("name", "")).strip(), data.get("category")
        if not name or len(name) > 200 or category not in CATEGORIES:
            raise ApiError(400, "请填写设备名称并选择设备类型")
        table = "Manual_" + category
        with self.connection() as con:
            idx = con.execute(
                "SELECT coalesce(max(model_idx),0)+1 FROM device WHERE model_table=?",
                (table,),
            ).fetchone()[0]
            cur = con.execute(
                "INSERT INTO device(model_table,model_idx,name,model_json,category) VALUES(?,?,?,?,?)",
                (table, idx, name, "{}", category),
            )
            device_id = cur.lastrowid
            result = dict(
                con.execute(
                    "SELECT * FROM device WHERE device_id=?", (device_id,)
                ).fetchone()
            )
            self._audit(con, "device", device_id, {}, result)
            self._tables(
                con, target_tables={table}, target_device_ids={device_id}
            )
        self.bump_config_epoch()
        return result

    @staticmethod
    def _audit(con, entity, key, before, after):
        con.execute(
            "INSERT INTO input_change_log(changed_at,entity,entity_id,before_json,after_json) VALUES(?,?,?,?,?)",
            (
                utc_now(),
                entity,
                key,
                json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False),
            ),
        )

    def devices(self, query):
        where, args = [], []
        if query.get("category"):
            where.append("d.category=?")
            args.append(query["category"])
        if query.get("q"):
            where.append("instr(lower(d.name),lower(?))>0")
            args.append(query["q"])
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connection() as con:
            rows = [
                dict(r)
                for r in con.execute(
                    """SELECT d.*,count(m.measurement_id) measurement_count,
                sum(CASE WHEN v.valid=1 THEN 1 ELSE 0 END) valid_count FROM device d
                LEFT JOIN device_measurement m ON m.device_id=d.device_id AND m.catalog_active=1
                LEFT JOIN measurement_value v USING(measurement_id)"""
                    + clause
                    + " GROUP BY d.device_id ORDER BY d.device_id",
                    args,
                )
            ]
            memberships = {}
            for b in con.execute("SELECT * FROM d_device_members"):
                memberships.setdefault(b["device_id"], []).append(
                    dict(table=b["table_name"], idx=b["idx"])
                )
            return [dict(r, tables=memberships.get(r["device_id"], [])) for r in rows]

    def measurements(self, query):
        where, args = ["m.catalog_active=1"], []
        for field in ("device_id", "kind"):
            if query.get(field):
                where.append("m." + field + "=?")
                args.append(query[field])
        need_device = bool(query.get("category"))
        if need_device:
            where.append("d.category=?")
            args.append(query["category"])
        if query.get("table"):
            where.append("m.quantity_table=?")
            args.append(query["table"])
        if query.get("q"):
            where.append(
                "(instr(lower(m.name),lower(?))>0 OR instr(lower(m.metric),lower(?))>0)"
            )
            args += [query["q"]] * 2
        need_value = query.get("invalid") == "1"
        if need_value:
            where.append("coalesce(v.valid,0)=0")
        where_clause = " WHERE " + " AND ".join(where)
        count_from = (
            " FROM device_measurement m"
            + (" JOIN device d USING(device_id)" if need_device else "")
            + " JOIN d_device_members b ON b.device_id=m.device_id AND b.table_name=m.quantity_table"
            + (" LEFT JOIN measurement_value v USING(measurement_id)" if need_value else "")
            + where_clause
        )
        joined = (
            """ FROM device_measurement m JOIN device d USING(device_id)
            JOIN d_device_members b ON b.device_id=m.device_id AND b.table_name=m.quantity_table
            LEFT JOIN scada_point_mapping s USING(measurement_id) LEFT JOIN measurement_value v USING(measurement_id)"""
            + where_clause
        )
        page, limit = (
            max(1, int(query.get("page", 1))),
            min(200, max(1, int(query.get("limit", 50)))),
        )
        with self.connection() as con:
            total = con.execute("SELECT count(*)" + count_from, args).fetchone()[0]
            rows = con.execute(
                """SELECT m.*,d.name device_name,d.category,s.source_database,s.rtu_id,s.pnt_no,
                b.table_name model_table,b.idx model_idx,s.enabled,s.scale,s.offset,v.value,v.source_value,v.previous_value,v.quality,v.source_time_ms,
                v.collected_at,coalesce(v.valid,0) valid,coalesce(v.reason,'uncollected') reason"""
                + joined
                + " ORDER BY m.device_id,m.measurement_id LIMIT ? OFFSET ?",
                args + [limit, (page - 1) * limit],
            ).fetchall()
        return dict(items=[dict(r) for r in rows], total=total, page=page, limit=limit)

    def save_measurement(self, data, measurement_id=None):
        metric, kind = data.get("metric", ""), data.get("kind", "yc")
        if (
            not isinstance(metric, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", metric)
            or kind not in ("yc", "yx")
        ):
            raise ApiError(400, "属性名须为小写英文字母/数字/下划线，点类型须为 YC/YX")
        if metric in ("idx", "name") or metric.endswith(
            ("_qual", "_pre", "_time", "_notuse", "_weight", "_rtu", "_pnt")
        ):
            raise ApiError(400, "属性名不能使用量测元数据后缀")
        source = data.get("source_database")
        rtu, point, device_id = (
            int(data["rtu_id"]),
            int(data["pnt_no"]),
            int(data["device_id"]),
        )
        weight, age, scale, offset = [
            float(data.get(k, v))
            for k, v in [
                ("weight", 1),
                ("max_age_seconds", 120),
                ("scale", 1),
                ("offset", 0),
            ]
        ]
        if (
            not all(math.isfinite(v) for v in (weight, age, scale, offset))
            or weight <= 0
            or age <= 0
        ):
            raise ApiError(400, "权重与超时必须为正数，变换参数必须为有限数值")
        flags = {
            key: int(data.get(key, default))
            for key, default in [("enabled", 1), ("notuse", 0), ("se_enabled", 0)]
        }
        if any(v not in (0, 1) for v in flags.values()):
            raise ApiError(400, "启用和禁用标记只能为 0 或 1")
        if kind == "yx" and (scale != 1 or offset != 0):
            raise ApiError(400, "遥信保持原状态值，不允许比例/偏移变换")
        with self.source(source) as con:
            found = con.execute(
                f"SELECT name FROM fes_{kind}_define WHERE rtu_id=? AND pnt_no=?",
                (rtu, point),
            ).fetchall()
            if len(found) != 1:
                raise ApiError(400, "指定的源测点不存在或主键不唯一")
            name = str(data.get("name") or found[0]["name"]).strip()
        if not name or len(name) > 250 or len(str(data.get("unit", ""))) > 30:
            raise ApiError(400, "属性名称或单位过长")
        now = utc_now()
        with self.connection() as con:
            if not con.execute(
                "SELECT 1 FROM device WHERE device_id=?", (device_id,)
            ).fetchone():
                raise ApiError(400, "目标设备不存在")
            before = {}
            version = 1
            if measurement_id is not None:
                row = con.execute(
                    "SELECT * FROM device_measurement WHERE measurement_id=?",
                    (measurement_id,),
                ).fetchone()
                if not row:
                    raise ApiError(404, "属性不存在")
                before = dict(row)
                if int(data.get("version", 0)) != row["version"]:
                    raise ApiError(409, "定义已被修改，请刷新后重试")
                version = row["version"] + 1
            duplicate = con.execute(
                "SELECT measurement_id FROM device_measurement WHERE device_id=? AND metric=? AND measurement_id!=?",
                (device_id, metric, measurement_id or -1),
            ).fetchone()
            if duplicate:
                raise ApiError(409, "该设备已定义此属性")
            values = (
                device_id,
                metric,
                kind,
                name,
                str(data.get("unit", "")),
                weight,
                age,
                flags["notuse"],
                flags["se_enabled"],
                now,
                version,
            )
            if measurement_id is None:
                measurement_id = con.execute(
                    """INSERT INTO device_measurement(device_id,metric,kind,name,unit,weight,max_age_seconds,notuse,se_enabled,updated_at,version)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                ).lastrowid
            else:
                con.execute(
                    """UPDATE device_measurement SET device_id=?,metric=?,kind=?,name=?,unit=?,weight=?,max_age_seconds=?,notuse=?,se_enabled=?,updated_at=?,version=? WHERE measurement_id=?""",
                    values + (measurement_id,),
                )
            old_mapping = con.execute(
                "SELECT * FROM scada_point_mapping WHERE measurement_id=?",
                (measurement_id,),
            ).fetchone()
            con.execute(
                """INSERT INTO scada_point_mapping(measurement_id,source_database,rtu_id,pnt_no,enabled,scale,offset,version,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(measurement_id) DO UPDATE SET source_database=excluded.source_database,rtu_id=excluded.rtu_id,pnt_no=excluded.pnt_no,enabled=excluded.enabled,
                scale=excluded.scale,offset=excluded.offset,version=excluded.version,updated_at=excluded.updated_at""",
                (
                    measurement_id,
                    source,
                    rtu,
                    point,
                    flags["enabled"],
                    scale,
                    offset,
                    version,
                    now,
                ),
            )
            # Invalidating a changed binding prevents displaying the old source
            # under a new device property before the next collection transaction.
            identity_changed = (
                not old_mapping
                or any(
                    old_mapping[k] != v
                    for k, v in [
                        ("source_database", source),
                        ("rtu_id", rtu),
                        ("pnt_no", point),
                        ("scale", scale),
                        ("offset", offset),
                    ]
                )
                or before.get("kind") != kind
                or before.get("device_id") != device_id
            )
            if identity_changed:
                con.execute(
                    "DELETE FROM measurement_value WHERE measurement_id=?",
                    (measurement_id,),
                )
            elif (
                before.get("notuse") != flags["notuse"]
                or old_mapping["enabled"] != flags["enabled"]
                or before.get("max_age_seconds") != age
            ):
                con.execute(
                    "UPDATE measurement_value SET valid=0,reason='definition_changed' WHERE measurement_id=?",
                    (measurement_id,),
                )
            after = dict(
                con.execute(
                    "SELECT m.*,s.source_database,s.rtu_id,s.pnt_no,s.enabled,s.scale,s.offset FROM device_measurement m JOIN scada_point_mapping s USING(measurement_id) WHERE measurement_id=?",
                    (measurement_id,),
                ).fetchone()
            )
            self._audit(
                con,
                "mapping",
                measurement_id,
                dict(before, mapping=dict(old_mapping) if old_mapping else None),
                after,
            )
            affected_devices = {device_id}
            if before.get("device_id") is not None:
                affected_devices.add(before["device_id"])
            affected_tables = set()
            if before.get("quantity_table"):
                affected_tables.add(before["quantity_table"])
            self._tables(
                con,
                target_tables=affected_tables,
                target_device_ids=affected_devices,
            )
        self.bump_config_epoch()
        return after

    def delete_measurement(self, measurement_id, version):
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM device_measurement WHERE measurement_id=?",
                (measurement_id,),
            ).fetchone()
            if not row or row["version"] != int(version):
                raise ApiError(409, "属性不存在或已被修改")
            self._audit(con, "mapping_delete", measurement_id, dict(row), {})
            for table in (
                "measurement_value",
                "scada_point_mapping",
                "device_measurement",
            ):
                con.execute(
                    f"DELETE FROM {table} WHERE measurement_id=?", (measurement_id,)
                )
            old_table = row["quantity_table"]
            if old_table:
                self._tables(
                    con,
                    target_tables={old_table},
                    target_device_ids={row["device_id"]},
                )
            else:
                self._tables(con)
        self.bump_config_epoch()

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
                        if not {"rtu_id", "pnt_no", "name", "value", "status", "refresh_time"} <= columns:
                            raise ApiError(400, "源库缺少遥测/遥信点表或必要字段")
                        pk_cache[table] = {r[1] for r in info if r[5]} == {"rtu_id", "pnt_no"}
                    schema_version = con.execute("PRAGMA schema_version").fetchone()[0]
                    # entry: [con, identity, schema_version, pk_cache, data_version, snapshots]
                    entry = [con, identity, schema_version, pk_cache, None, {}]
                    source_cache[source_name] = entry
                except BaseException:
                    con.close()
                    raise
            con, _, schema_version, pk_cache, cached_data_version, cached_snapshots = entry
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
                        if not {"rtu_id", "pnt_no", "name", "value", "status", "refresh_time"} <= columns:
                            raise ApiError(400, "源库缺少遥测/遥信点表或必要字段")
                        pk_cache[table] = {r[1] for r in info if r[5]} == {"rtu_id", "pnt_no"}
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

    def collect_once(self, *, _writer=None, _source_cache=None, _plan_cache=None):
        started = utc_now()
        mapping_sql = """SELECT m.measurement_id,m.device_id,m.metric,m.kind,m.quantity_table,
            m.max_age_seconds,m.se_enabled,m.notuse,d.active,
            s.source_database,s.rtu_id,s.pnt_no,s.enabled,s.scale,s.offset,
            v.measurement_id,v.source_value,v.value,v.quality,v.source_time_ms,v.previous_value,v.valid,v.reason
            FROM device_measurement m JOIN device d USING(device_id)
            LEFT JOIN scada_point_mapping s USING(measurement_id)
            LEFT JOIN measurement_value v USING(measurement_id)
            WHERE m.catalog_active=1"""
        # 第一阶段：短读事务获取待采源点列表，立即释放本地库连接，严禁持写锁执行外部源库 I/O。
        requested = None
        plan_key = None
        if _writer is not None and _plan_cache is not None:
            data_ver = _writer.execute("PRAGMA data_version").fetchone()[0]
            plan_key = (data_ver, self._config_epoch)
            if _plan_cache.get("key") == plan_key:
                requested = _plan_cache["requested"]
        if requested is None:
            with self.connection(_writer=_writer) as read_con:
                cur = read_con.cursor()
                cur.row_factory = None
                requested_rows = cur.execute(
                    """SELECT m.kind, s.source_database, s.rtu_id, s.pnt_no
                    FROM device_measurement m JOIN device d USING(device_id)
                    JOIN scada_point_mapping s USING(measurement_id)
                    WHERE m.catalog_active=1 AND d.active=1 AND s.enabled=1 AND s.source_database<>''"""
                ).fetchall()
            tmp_requested = defaultdict(lambda: defaultdict(set))
            for kind, source_db, rtu_id, pnt_no in requested_rows:
                tmp_requested[source_db][kind].add((rtu_id, pnt_no))
            requested = {
                src: {k: frozenset(pts) for k, pts in kind_map.items()}
                for src, kind_map in tmp_requested.items()
            }
            if _plan_cache is not None and plan_key is not None:
                _plan_cache["key"] = plan_key
                _plan_cache["requested"] = requested
        if _source_cache is not None:
            for stale in [key for key in _source_cache if key not in requested]:
                _source_cache.pop(stale)[0].close()
        # 第二阶段：无本地写锁状态下读取各外部源库快照，读完后刷新当前时间基准防止误判 future_time。
        sources, errors = {}, {}
        for source, kinds in sorted(requested.items()):
            try:
                if _source_cache is not None:
                    sources[source] = self._read_cached_source(source, kinds, _source_cache)
                else:
                    with self.source(source) as src:
                        sources[source] = {
                            kind: self._source_values(src, kind, keys)
                            for kind, keys in kinds.items()
                        }
            except (ApiError, OSError, ValueError) as exc:
                errors[source] = str(exc)
        now_ms = int(time.time() * 1000)
        future_limit = now_ms + 5000
        # 第三阶段：进入 BEGIN IMMEDIATE 写事务，重新读取最新映射快照并原子落盘。
        next_mappings = None
        tx_key = None
        has_inactive = None
        try:
            with self.connection(_writer=_writer) as con:
                con.execute("BEGIN IMMEDIATE")
                cur = con.cursor()
                cur.row_factory = None
                mappings = None
                if _writer is not None and _plan_cache is not None:
                    tx_key = (
                        cur.execute("PRAGMA data_version").fetchone()[0],
                        self._config_epoch,
                    )
                    if _plan_cache.get("mappings_key") == tx_key:
                        mappings = _plan_cache["mappings"]
                        has_inactive = _plan_cache.get("has_inactive")
                if mappings is None:
                    mappings = cur.execute(mapping_sql).fetchall()
                refresh = cur.execute(
                    "INSERT INTO measurement_refresh(started_at,source_database,error) VALUES(?,?,?)",
                    (
                        started,
                        json.dumps(list(sources), ensure_ascii=False),
                        json.dumps(errors, ensure_ascii=False) if errors else "",
                    ),
                ).lastrowid
                valid_count = 0
                se_count = 0
                collected_at = utc_now()
                changed_samples = []
                unchanged_count = 0
                changes = defaultdict(dict)
                isfinite = math.isfinite
                invalid_quality_reasons = (
                    "disabled",
                    "source_error",
                    "missing_point",
                    "invalid_value",
                )
                track_next = _writer is not None and _plan_cache is not None
                if track_next:
                    next_mappings = []
                for row in mappings:
                    (
                        mid,
                        device_id,
                        metric,
                        kind,
                        quantity_table,
                        max_age_seconds,
                        se_enabled,
                        notuse,
                        active,
                        source_db,
                        rtu_id,
                        pnt_no,
                        enabled,
                        scale,
                        offset,
                        old_id,
                        old_raw,
                        old_value,
                        old_quality,
                        old_stamp,
                        old_previous,
                        old_valid,
                        old_reason,
                    ) = row
                    raw = old_raw
                    value = old_value
                    quality = old_quality
                    stamp = old_stamp
                    previous = old_previous
                    old_runtime_quality = (
                        (old_quality or 0) if old_valid else (old_quality or 0) | 128
                    )
                    reason = ""
                    src_kinds = sources.get(source_db)
                    points = src_kinds.get(kind) if src_kinds else None
                    point = points.get((rtu_id, pnt_no)) if points else None
                    if not active or not enabled:
                        reason = "disabled"
                    elif source_db in errors:
                        reason = "source_error"
                    elif point is None:
                        reason = "missing_point"
                    else:
                        try:
                            new_raw = float(point[0])
                            new_quality = int(point[1])
                            new_stamp = int(point[2])
                            new_value = new_raw * scale + offset
                            if (
                                not isfinite(new_raw)
                                or not isfinite(new_value)
                                or not 0 <= new_quality <= 255
                            ):
                                raise ValueError("invalid sample")
                            if kind == "yx" and new_raw not in (0, 1):
                                raise ValueError("invalid status")
                            if old_id is not None and (
                                new_stamp != stamp or new_raw != raw
                            ):
                                previous = value
                            raw = new_raw
                            value = new_value
                            quality = new_quality
                            stamp = new_stamp
                            if quality:
                                reason = "bad_quality"
                            elif stamp <= 0:
                                reason = "missing_time"
                            elif stamp > future_limit:
                                reason = "future_time"
                            elif now_ms - stamp > max_age_seconds * 1000:
                                reason = "stale"
                        except (ValueError, TypeError, OverflowError):
                            reason = "invalid_value"
                    if reason in invalid_quality_reasons:
                        quality = (quality or 0) | 128
                    if not reason and notuse:
                        reason = "notuse"
                    valid = 0 if reason else 1
                    valid_count += valid
                    if valid and se_enabled:
                        se_count += 1
                    if (
                        old_id is None
                        or raw != old_raw
                        or value != old_value
                        or quality != old_quality
                        or stamp != old_stamp
                        or valid != old_valid
                        or reason != old_reason
                        or previous != old_previous
                    ):
                        changed_samples.append(
                            (
                                mid,
                                refresh,
                                raw,
                                value,
                                quality,
                                stamp,
                                collected_at,
                                valid,
                                reason,
                                previous,
                            )
                        )
                        if track_next:
                            next_mappings.append(
                                (
                                    mid,
                                    device_id,
                                    metric,
                                    kind,
                                    quantity_table,
                                    max_age_seconds,
                                    se_enabled,
                                    notuse,
                                    active,
                                    source_db,
                                    rtu_id,
                                    pnt_no,
                                    enabled,
                                    scale,
                                    offset,
                                    mid,
                                    raw,
                                    value,
                                    quality,
                                    stamp,
                                    previous,
                                    valid,
                                    reason,
                                )
                            )
                    else:
                        unchanged_count += 1
                        if track_next:
                            next_mappings.append(row)
                    runtime_quality = (quality or 0) if valid else (quality or 0) | 128
                    # A binding trigger can invalidate or remove the normalized row
                    # before the next scan. Refresh all its physical runtime fields.
                    reset_runtime = old_id is None or old_reason == "definition_changed"
                    if (
                        reset_runtime
                        or value != old_value
                        or runtime_quality != old_runtime_quality
                        or previous != old_previous
                        or stamp != old_stamp
                    ):
                        updates = changes[quantity_table, device_id]
                        if reset_runtime or value != old_value:
                            updates[metric] = value
                        if reset_runtime or runtime_quality != old_runtime_quality:
                            updates[metric + "_qual"] = runtime_quality
                        if reset_runtime or previous != old_previous:
                            updates[metric + "_pre"] = previous
                        if reset_runtime or stamp != old_stamp:
                            updates[metric + "_time"] = stamp
                if changed_samples:
                    con.executemany(
                        """INSERT INTO measurement_value(measurement_id,refresh_id,source_value,value,quality,source_time_ms,collected_at,valid,reason,previous_value)
                        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(measurement_id) DO UPDATE SET refresh_id=excluded.refresh_id,source_value=excluded.source_value,
                        value=excluded.value,quality=excluded.quality,source_time_ms=excluded.source_time_ms,collected_at=excluded.collected_at,
                        valid=excluded.valid,reason=excluded.reason,previous_value=excluded.previous_value""",
                        changed_samples,
                    )
                if unchanged_count:
                    if has_inactive is None:
                        has_inactive = (
                            cur.execute(
                                "SELECT 1 FROM device_measurement WHERE catalog_active=0 LIMIT 1"
                            ).fetchone()
                            is not None
                        )
                    if not changed_samples:
                        if not has_inactive:
                            cur.execute(
                                "UPDATE measurement_value SET refresh_id=?,collected_at=?",
                                (refresh, collected_at),
                            )
                        else:
                            cur.execute(
                                """UPDATE measurement_value SET refresh_id=?,collected_at=?
                                WHERE measurement_id IN (SELECT measurement_id FROM device_measurement WHERE catalog_active=1)""",
                                (refresh, collected_at),
                            )
                    else:
                        if not has_inactive:
                            cur.execute(
                                "UPDATE measurement_value SET refresh_id=?,collected_at=? WHERE refresh_id<>?",
                                (refresh, collected_at, refresh),
                            )
                        else:
                            cur.execute(
                                """UPDATE measurement_value SET refresh_id=?,collected_at=?
                                WHERE refresh_id<>? AND measurement_id IN (SELECT measurement_id FROM device_measurement WHERE catalog_active=1)""",
                                (refresh, collected_at, refresh),
                            )
                self._write_runtime_values(con, changes)
                cur.execute(
                    "UPDATE measurement_refresh SET finished_at=?,total_count=?,valid_count=?,se_valid_count=? WHERE refresh_id=?",
                    (utc_now(), len(mappings), valid_count, se_count, refresh),
                )
                if _plan_cache is None or refresh % 50 == 0:
                    cur.execute(
                        """DELETE FROM measurement_refresh
                        WHERE refresh_id<? AND NOT EXISTS (
                            SELECT 1 FROM measurement_value v WHERE v.refresh_id=measurement_refresh.refresh_id
                        )""",
                        (refresh - 1000,),
                    )
        except BaseException:
            if _plan_cache is not None:
                _plan_cache.clear()
            raise
        if _plan_cache is not None and tx_key is not None and next_mappings is not None:
            _plan_cache["mappings_key"] = tx_key
            _plan_cache["mappings"] = next_mappings
            _plan_cache["has_inactive"] = has_inactive
        return dict(
            refresh_id=refresh,
            total_count=len(mappings),
            valid_count=valid_count,
            errors=errors,
        )

    def overview(self):
        with self.connection() as con:
            counts = {
                t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("device", "device_measurement", "scada_point_mapping")
            }
            counts["valid"] = con.execute(
                "SELECT count(*) FROM measurement_value WHERE valid=1"
            ).fetchone()[0]
            last = con.execute(
                "SELECT * FROM measurement_refresh ORDER BY refresh_id DESC LIMIT 1"
            ).fetchone()
            categories = [
                dict(r)
                for r in con.execute(
                    "SELECT category,count(*) count FROM device GROUP BY category"
                )
            ]
            interval = stored_interval(con)
            sources = [
                r[0]
                for r in con.execute(
                    "SELECT DISTINCT source_database FROM scada_point_mapping ORDER BY source_database"
                )
            ]
        return dict(
            counts=counts,
            last_refresh=dict(last) if last else None,
            categories=categories,
            category_labels=CATEGORIES,
            interval_seconds=interval,
            sources=sources,
        )

    def history(self):
        with self.connection() as con:
            return dict(
                refreshes=[
                    dict(r)
                    for r in con.execute(
                        "SELECT * FROM measurement_refresh ORDER BY refresh_id DESC LIMIT 100"
                    )
                ],
                changes=[
                    dict(r)
                    for r in con.execute(
                        "SELECT * FROM input_change_log ORDER BY change_id DESC LIMIT 100"
                    )
                ],
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
        interval = validate_interval(value)
        with self._mutex:
            self._persist_interval(interval)
            self._wake.set()
            return dict(self.status(), interval_seconds=interval)

    def _persist_interval(self, interval):
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            previous = stored_interval(con)
            if interval == previous:
                return
            con.execute(
                "UPDATE d_settings SET value=? WHERE key='interval_seconds'",
                (str(interval),),
            )
            self.store._audit(
                con,
                "collector_interval",
                None,
                {"interval_seconds": previous},
                {"interval_seconds": interval},
            )

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
                    self._persist_interval(interval)
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
                                    # Keep the connection between batches without holding a transaction.
                                    # Closing the last WAL connection per scan can trigger a checkpoint.
                                    writer = self.store._open_connection()
                                new_period = stored_interval(writer)
                                now = time.monotonic()
                                if new_period != period and last_scan is not None:
                                    next_scan = max(now, last_scan + new_period)
                                period = new_period
                                if now < next_scan:
                                    # Local edits wake the thread. An external Web process
                                    # can save through SQLite, so reread at least once a second.
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

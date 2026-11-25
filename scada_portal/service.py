"""SCADA 门户数据聚合、SVG 潮流快照、24 小时日曲线采样与日志检索服务。"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
import xml.etree.ElementTree as ET

from core.log_paths import log_entries, packet_log_directory
from core.scdfe_control import LOG_LEVEL_CODES, LOG_PATTERN, log_directory
from core.web_database import DEFAULT_WORKSPACE_ROOT
from core.log_query import LOG_PATTERNS, search_logs
from core.common import CHANNEL_PARAMETER_TABLES, KIND_TABLES
from scdfe_d.measurement_layout import QUAL_LABELS, QUAL_OK, QUAL_OVER_MAX, QUAL_UNDER_MIN, SUFFIXES
from scdfe_d.service import _check_limit_status, _parse_limit_number

# 秦岭站正式开站投运基准日（2024-02-07 建站开站）
STATION_COMMISSION_DATE = datetime(2024, 2, 7, tzinfo=timezone.utc)

# 元数据与伴随表过滤
_D_SKIP_TABLES = frozenset({
    "rtdb_schema_database",
    "rtdb_schema_tables",
    "rtdb_schema_columns",
    "rtdb_web_commits",
    "rtdb_web_receipt_archive",
})

# 指标中文名与物理单位字典
METRIC_META: dict[str, tuple[str, str]] = {
    "p": ("有功功率", "kW"),
    "q": ("无功功率", "kvar"),
    "s": ("视在功率", "kVA"),
    "pf": ("功率因数", ""),
    "u": ("直流电压", "V"),
    "uab": ("AB 线电压", "V"),
    "ubc": ("BC 线电压", "V"),
    "uca": ("CA 线电压", "V"),
    "i": ("电流", "A"),
    "ia": ("A 相电流", "A"),
    "ib": ("B 相电流", "A"),
    "ic": ("C 相电流", "A"),
    "f": ("频率", "Hz"),
    "i_p": ("首端有功", "kW"),
    "i_q": ("首端无功", "kvar"),
    "i_s": ("首端视在", "kVA"),
    "i_u": ("首端电压", "V"),
    "i_uab": ("首端线电压", "V"),
    "i_i": ("首端电流", "A"),
    "i_ia": ("首端电流", "A"),
    "j_p": ("末端有功", "kW"),
    "j_q": ("末端无功", "kvar"),
    "j_u": ("末端电压", "V"),
    "j_i": ("末端电流", "A"),
    "dc_p": ("直流功率", "kW"),
    "dc_u": ("直流电压", "V"),
    "dc_i": ("直流电流", "A"),
    "ac_p": ("交流有功", "kW"),
    "ac_q": ("交流无功", "kvar"),
    "ac_uab": ("交流电压", "V"),
    "ac_ia": ("交流电流", "A"),
    "soc": ("荷电状态 SOC", "%"),
    "soh": ("健康度 SOH", "%"),
    "pressure": ("氢气压力", "MPa"),
    "flow": ("氢气流量", "Nm³/h"),
    "wind_speed": ("风速", "m/s"),
    "irradiance": ("太阳辐照度", "W/m²"),
    "temperature": ("温度", "°C"),
    "run": ("运行状态", ""),
    "position": ("开关位置", ""),
    "fault": ("故障信号", ""),
    "overload": ("过载告警", ""),
}

# 设备分类定义（顺序决定设备图标签顺序）
DEVICE_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("wind", "风力发电"),
    ("pv", "光伏发电"),
    ("diesel", "柴油发电"),
    ("storage", "电化学储能"),
    ("hydrogen", "氢能系统"),
    ("converter", "变流器"),
    ("load", "交直流负荷"),
    ("line", "交直流线路"),
    ("bus", "母线与节点"),
    ("switch", "断路器开关"),
)

# 模型库（model/devices.sqlite3）扩展表 -> (主表名, 关联外键列名)
_MODEL_EXTENSIONS: dict[str, tuple[str, str]] = {
    "ACWindGen": ("ACGenerator", "idx_acgenerator"),
    "ACPVGen": ("ACGenerator", "idx_acgenerator"),
    "ACDieselGen": ("ACGenerator", "idx_acgenerator"),
    "ACStorageGen": ("ACGenerator", "idx_acgenerator"),
    "DCPVGen": ("DCGenerator", "idx_dcgenerator"),
    "DCStorageGen": ("DCGenerator", "idx_dcgenerator"),
}

# 模型库设备参数中文字典与单位（eng_name -> (中文名, 单位)）
MODEL_PARAM_META: dict[str, tuple[str, str]] = {
    "dev_type": ("设备类型", ""),
    "rated_capacity": ("额定容量", "kW"),
    "rated_voltage": ("额定电压", "V"),
    "vbase": ("基准电压", "V"),
    "control_type": ("控制模式", ""),
    "ac_control_type": ("交流侧控制", ""),
    "dc_control_type": ("直流侧控制", ""),
    "i_control_type": ("I 侧控制", ""),
    "j_control_type": ("J 侧控制", ""),
    "p_set": ("有功设定", "kW"),
    "q_set": ("无功设定", "kvar"),
    "v_set": ("电压设定", "V"),
    "i_set": ("电流设定", "A"),
    "p_ac_set": ("交流有功设定", "kW"),
    "q_ac_set": ("交流无功设定", "kvar"),
    "v_ac_set": ("交流电压设定", "V"),
    "p_dc_set": ("直流功率设定", "kW"),
    "v_dc_set": ("直流电压设定", "V"),
    "p_max": ("有功上限", "kW"),
    "p_min": ("有功下限", "kW"),
    "q_max": ("无功上限", "kvar"),
    "q_min": ("无功下限", "kvar"),
    "v_max": ("电压上限", "pu"),
    "v_min": ("电压下限", "pu"),
    "i_max": ("电流上限", "A"),
    "ac_p_max": ("交流有功上限", "kW"),
    "ac_p_min": ("交流有功下限", "kW"),
    "dc_p_max": ("直流功率上限", "kW"),
    "dc_p_min": ("直流功率下限", "kW"),
    "ac_v_max": ("交流电压上限", "pu"),
    "ac_v_min": ("交流电压下限", "pu"),
    "dc_v_max": ("直流电压上限", "pu"),
    "dc_v_min": ("直流电压下限", "pu"),
    "i_p_max": ("I 侧功率上限", "kW"),
    "i_p_min": ("I 侧功率下限", "kW"),
    "j_p_max": ("J 侧功率上限", "kW"),
    "j_p_min": ("J 侧功率下限", "kW"),
    "i_v_max": ("I 侧电压上限", "pu"),
    "i_v_min": ("I 侧电压下限", "pu"),
    "j_v_max": ("J 侧电压上限", "pu"),
    "j_v_min": ("J 侧电压下限", "pu"),
    "r": ("电阻 R", "Ω"),
    "x": ("电抗 X", "Ω"),
    "b": ("电纳 B", "S"),
    "r1": ("损耗系数 R1", ""),
    "r2": ("损耗系数 R2", ""),
    "pbase": ("基准有功", "kW"),
    "qbase": ("基准无功", "kvar"),
    "status": ("初始状态", ""),
    "run_stat": ("投运状态", ""),
    "node": ("关联节点", ""),
    "i_node": ("首端节点", ""),
    "j_node": ("末端节点", ""),
    "ac_node": ("交流节点", ""),
    "dc_node": ("直流节点", ""),
    "pressure": ("额定压力", "MPa"),
    "pressure_set": ("压力设定", "MPa"),
    "flow_set": ("流量设定", "Nm³/h"),
    "flow_min": ("流量下限", "Nm³/h"),
    "flow_max": ("流量上限", "Nm³/h"),
    "conductance": ("流导系数", ""),
    "ratio": ("压比", ""),
    "e2h_coeff": ("电制氢系数", ""),
    "h2e_coeff": ("氢发电系数", ""),
    # 扩展表：风机
    "wind_turbine_model": ("风机型号", ""),
    "cut_in_wind_speed": ("切入风速", "m/s"),
    "rated_wind_speed": ("额定风速", "m/s"),
    "cut_out_wind_speed": ("切出风速", "m/s"),
    "rotor_diameter": ("叶轮直径", "m"),
    "hub_height": ("轮毂高度", "m"),
    # 扩展表：光伏
    "pv_module_model": ("光伏组件型号", ""),
    "module_efficiency": ("组件效率", ""),
    "array_area": ("阵列面积", "m²"),
    "mppt_count": ("MPPT 路数", ""),
    "reference_irradiance": ("参考辐照度", "W/m²"),
    "reference_temperature": ("参考温度", "°C"),
    "temperature_coefficient": ("温度系数", ""),
    # 扩展表：储能
    "storage_technology": ("储能类型", ""),
    "battery_rack_count": ("电池簇数", ""),
    "energy_capacity": ("额定电量", "kWh"),
    "charge_discharge_efficiency": ("充放电效率", ""),
    "max_charge_power": ("最大充电功率", "kW"),
    "max_discharge_power": ("最大放电功率", "kW"),
    "state_of_charge": ("初始 SOC", "%"),
    "soc_upper_limit": ("SOC 上限", "%"),
    "soc_lower_limit": ("SOC 下限", "%"),
    # 扩展表：柴油发电机
    "diesel_unit_model": ("柴发型号", ""),
    "fuel_grade": ("燃油牌号", ""),
    "specific_fuel_consumption": ("燃油消耗率", "g/kWh"),
    "fuel_tank_capacity": ("油箱容量", "L"),
    "rated_speed": ("额定转速", "rpm"),
    "start_time": ("启动时间", "s"),
}

_MODEL_SKIP_FIELDS = frozenset({
    "idx", "name", "idx_acgenerator", "idx_dcgenerator",
    "idx_ac_load_t1", "idx_h2_unit_t2", "idx_dc_unit_t1", "idx_h2_load_t2",
    "pv0", "pv1", "pv2", "qv0", "qv1", "qv2", "isl", "alpha",
})


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
        return num if math.isfinite(num) else None
    except (TypeError, ValueError):
        return None


def _iso_from_ms(ms: Any) -> str | None:
    if ms is None:
        return None
    try:
        val = float(ms)
        if val <= 0:
            return None
        # 兼容秒与毫秒时间戳
        sec = val / 1000.0 if val > 1e11 else val
        return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _classify_device(table: str, name: str) -> str:
    """将设备表与设备名称归类到面向运行人员的 10 个核心设备大类。"""
    text = str(name or "")
    if table in {"ACWindGen"} or (table == "ACGenerator" and ("风电" in text or "风力" in text)):
        return "wind"
    if table in {"ACPVGen", "DCPVGen"} or (table in {"ACGenerator", "DCGenerator"} and "光伏" in text):
        return "pv"
    if table in {"ACDieselGen"} or (table == "ACGenerator" and "柴油" in text):
        return "diesel"
    if table in {"ACStorageGen", "DCStorageGen"} or (table in {"ACGenerator", "DCGenerator"} and "储能" in text):
        return "storage"
    if table in {"AcE2Hydro", "Hydro2DcE", "HydroCompressor", "HydroLoad", "HydroNode", "HydroSource", "HydroStorage", "HydroValve"}:
        return "hydrogen"
    if "燃料电池" in text or "制氢" in text:
        return "hydrogen"
    if table in {"DCACConverter", "DCDCConverter"}:
        return "converter"
    if table in {"ACLoad", "DCLoad"}:
        return "load"
    if table in {"ACBranch", "ACZeroBranch", "DCBranch"}:
        return "line"
    if table in {"ACBreak", "DCBreak"}:
        return "switch"
    return "bus"


class PortalService:
    """统一读取 scdfe_d / scdfe_c SQLite、SVG 接线图与运行日志，并维护 24 小时日曲线。"""

    def __init__(
        self,
        *,
        workspace_root: Path | str = DEFAULT_WORKSPACE_ROOT,
        runtime_name: str = "realtime",
        scene_name: str = "qinling",
        c_db: Path | str | None = None,
        d_db: Path | str | None = None,
        model_db: Path | str | None = None,
        svg_path: Path | str | None = None,
        log_dir: Path | str | None = None,
        history_db: Path | str | None = None,
        sample_interval_sec: float = 5.0,
        enable_sampler: bool = True,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.runtime_name = runtime_name
        self.scene_name = scene_name
        scene_dir = self.workspace_root / runtime_name / scene_name
        sqldb_dir = scene_dir / "sqldb" / "scada"
        fes_sqldb_dir = scene_dir / "sqldb" / "fes"
        model_sqldb_dir = scene_dir / "sqldb" / "model"

        if c_db:
            self.c_db = Path(c_db).resolve()
        else:
            fes_c_db = fes_sqldb_dir / "scdfe_c.sqlite3"
            legacy_c_db = sqldb_dir / "scdfe_c.sqlite3"
            self.c_db = fes_c_db if (fes_c_db.is_file() or not legacy_c_db.is_file()) else legacy_c_db
        self.d_db = Path(d_db).resolve() if d_db else (sqldb_dir / "scdfe_d.sqlite3")
        self.model_db = Path(model_db).resolve() if model_db else (model_sqldb_dir / "devices.sqlite3")
        self.svg_path = self._resolve_svg(svg_path, scene_dir / "model", scene_name)
        self.log_dir = Path(log_dir).resolve() if log_dir else (self.c_db.parent / "logs")
        self.history_db = (
            Path(history_db).resolve()
            if history_db
            else (sqldb_dir / "scada_portal_history.sqlite3")
        )
        self.sample_interval_sec = max(1.0, float(sample_interval_sec))
        self._stop_event = threading.Event()
        self._sampler_thread: threading.Thread | None = None
        self._history_lock = threading.Lock()
        self._energy_accum_lock = threading.Lock()
        self._last_sample_ts: float | None = None
        # 运行期实时累计（用于微电网总览能量微增量）
        self._session_kwh: dict[str, float] = defaultdict(float)
        self._svg_topo_cache: tuple[int, dict[str, Any]] | None = None
        self._model_params_cache: tuple[int, dict[str, list[dict[str, Any]]]] | None = None

        self._init_history_db()
        if enable_sampler:
            self.start_sampler()

    @staticmethod
    def _resolve_svg(explicit: Path | str | None, model_dir: Path, scene_name: str) -> Path:
        if explicit:
            return Path(explicit).resolve()
        preferred = model_dir / f"{scene_name}.svg"
        if preferred.is_file():
            return preferred
        if model_dir.is_dir():
            for candidate in sorted(model_dir.glob("*.svg")):
                return candidate
        return preferred

    @contextmanager
    def _open_readonly(self, db_path: Path):
        if not db_path.is_file():
            raise FileNotFoundError(f"数据库不存在：{db_path}")
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _open_readwrite(self, db_path: Path):
        if not db_path.is_file():
            raise FileNotFoundError(f"数据库不存在：{db_path}")
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
        finally:
            conn.close()

    def update_device_measurement_limits(
        self,
        table_name: str,
        idx: int,
        metric: str,
        max_value: Any,
        min_value: Any,
    ) -> dict[str, Any]:
        """更新 scdfe_d 设备宽表中指定量测的上下限，若上限<=下限则拒绝修改。"""
        if not isinstance(table_name, str) or not table_name or table_name in _D_SKIP_TABLES or table_name.startswith("rtdb_"):
            raise ValueError("设备表名不合法")
        if not isinstance(metric, str) or not metric:
            raise ValueError("量测属性名不能为空")

        def _parse_opt(raw: Any, label: str) -> float | None:
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return None
            num = _parse_limit_number(raw)
            if num is None:
                raise ValueError(f"{label}必须为有限数值或留空")
            return num

        max_num = _parse_opt(max_value, "量测上限")
        min_num = _parse_opt(min_value, "量测下限")
        if max_num is not None and min_num is not None and max_num <= min_num:
            raise ValueError("量测上限必须大于下限，无法修改")

        with self._open_readwrite(self.d_db) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if not exists:
                raise FileNotFoundError(f"设备表不存在：{table_name}")
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()}
            max_col = f"{metric}_max"
            min_col = f"{metric}_min"
            qual_col = f"{metric}_qual"
            if not {metric, max_col, min_col, qual_col} <= cols:
                raise ValueError(f"设备表 {table_name} 不存在量测 {metric} 或上下限字段")
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f'SELECT "{metric}", "{qual_col}" FROM "{table_name}" WHERE idx=?',
                    (int(idx),),
                ).fetchone()
                if row is None:
                    raise FileNotFoundError(f"设备不存在：{table_name}#{idx}")
                val, old_qual = row[0], row[1]
                new_qual = old_qual
                # 若当前处于正常/越限态（通信正常），即时按新上下限重算状态码
                if old_qual in (QUAL_OK, QUAL_OVER_MAX, QUAL_UNDER_MIN) and val is not None:
                    hit = _check_limit_status(val, max_num, min_num)
                    new_qual = hit[0] if hit is not None else QUAL_OK
                conn.execute(
                    f'UPDATE "{table_name}" SET "{max_col}"=?, "{min_col}"=?, "{qual_col}"=? WHERE idx=?',
                    (max_num, min_num, new_qual, int(idx)),
                )
        return {
            "table_name": table_name,
            "idx": int(idx),
            "metric": metric,
            "max_value": max_num,
            "min_value": min_num,
            "quality": new_qual,
            "quality_label": QUAL_LABELS.get(new_qual, str(new_qual) if new_qual is not None else "—"),
        }

    @contextmanager
    def _open_history(self):
        conn = sqlite3.connect(self.history_db, timeout=15.0)
        try:
            yield conn
        finally:
            conn.close()

    # -------------------- 日曲线历史库与采样器 --------------------
    def _init_history_db(self) -> None:
        self.history_db.parent.mkdir(parents=True, exist_ok=True)
        with self._history_lock, self._open_history() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_daily_samples (
                    ts_ms INTEGER NOT NULL,
                    table_name TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL NOT NULL,
                    PRIMARY KEY (table_name, idx, metric, ts_ms)
                ) WITHOUT ROWID
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_daily_samples_ts ON device_daily_samples(ts_ms)"
            )
            conn.commit()

    def start_sampler(self) -> None:
        if self._sampler_thread is not None and self._sampler_thread.is_alive():
            return
        self._stop_event.clear()
        # 启动时先同步采样一次，保证首次访问立即可见曲线点
        try:
            self.sample_once()
        except Exception:
            pass
        self._sampler_thread = threading.Thread(
            target=self._sampler_loop, name="scada-portal-sampler", daemon=True
        )
        self._sampler_thread.start()

    def close(self) -> None:
        self._stop_event.set()
        if self._sampler_thread is not None and self._sampler_thread.is_alive():
            self._sampler_thread.join(timeout=3.0)

    def _sampler_loop(self) -> None:
        prune_counter = 0
        while not self._stop_event.wait(self.sample_interval_sec):
            try:
                self.sample_once(prune=(prune_counter % 60 == 0))
                prune_counter += 1
            except Exception:
                continue

    def sample_once(self, *, prune: bool = False) -> int:
        """从 scdfe_d.sqlite3 采集一帧全量有效数值量测并写入 24h 日曲线库。"""
        if not self.d_db.is_file():
            return 0
        now_ms = int(time.time() * 1000)
        devices = self._read_all_devices()
        rows_to_insert: list[tuple[int, str, int, str, float]] = []
        for dev in devices:
            tname = dev["table_name"]
            idx = dev["idx"]
            for m in dev["metrics"]:
                if m["rtu_id"] is None or m["value"] is None:
                    continue
                val = _to_float(m["value"])
                if val is None:
                    continue
                # soc 统一按百分比存入日曲线，直观可读
                if m["metric"] == "soc" and 0.0 <= val <= 1.5:
                    val = round(val * 100.0, 3)
                rows_to_insert.append((now_ms, tname, idx, m["metric"], val))

        if not rows_to_insert:
            return 0

        # 顺带累积能量微增量（kWh）
        now_sec = now_ms / 1000.0
        with self._energy_accum_lock:
            if self._last_sample_ts is not None:
                dt_h = max(0.0, min(60.0, now_sec - self._last_sample_ts)) / 3600.0
                if dt_h > 0:
                    agg = self._aggregate_energy(devices)
                    self._session_kwh["wind"] += max(0.0, agg["wind_kw"]) * dt_h
                    self._session_kwh["pv"] += max(0.0, agg["pv_kw"]) * dt_h
                    self._session_kwh["diesel"] += max(0.0, agg["diesel_kw"]) * dt_h
                    self._session_kwh["storage"] += max(0.0, agg["storage_kw"]) * dt_h
                    self._session_kwh["hydrogen"] += max(0.0, agg["hydrogen_gen_kw"]) * dt_h
                    self._session_kwh["load"] += max(0.0, agg["total_load_kw"]) * dt_h
            self._last_sample_ts = now_sec

        cutoff_ms = now_ms - 24 * 3600 * 1000
        with self._history_lock, self._open_history() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO device_daily_samples(ts_ms, table_name, idx, metric, value) "
                "VALUES (?, ?, ?, ?, ?)",
                rows_to_insert,
            )
            if prune:
                conn.execute("DELETE FROM device_daily_samples WHERE ts_ms < ?", (cutoff_ms,))
            conn.commit()
        return len(rows_to_insert)

    # -------------------- 设备全量快照读取 --------------------
    def _read_all_devices(self) -> list[dict[str, Any]]:
        if not self.d_db.is_file():
            return []
        devices: list[dict[str, Any]] = []
        with self._open_readonly(self.d_db) as conn:
            tables = [
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
                if r[0] not in _D_SKIP_TABLES and not r[0].startswith("rtdb_web_")
            ]
            for table in tables:
                cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                col_names = [c[1] for c in cols]
                col_types = {c[1]: (c[2] or "").upper() for c in cols}
                if "idx" not in col_names or "name" not in col_names:
                    continue
                metrics = [cn[:-4] for cn in col_names if cn.endswith("_rtu")]
                if not metrics:
                    continue
                # 构建查询列
                select_cols = ["idx", "name"]
                for m in metrics:
                    for suf in SUFFIXES:
                        cname = f"{m}{suf}"
                        if cname in col_types:
                            select_cols.append(cname)
                quoted = ", ".join(f'"{c}"' for c in select_cols)
                rows = conn.execute(f'SELECT {quoted} FROM "{table}" ORDER BY idx').fetchall()
                for row in rows:
                    row_dict = dict(row)
                    idx = int(row_dict["idx"])
                    dev_name = str(row_dict["name"] or f"{table}-{idx}")
                    m_list: list[dict[str, Any]] = []
                    bound_cnt = 0
                    valid_cnt = 0
                    run_val: int | None = None
                    pos_val: int | None = None
                    for m in metrics:
                        rtu_id = row_dict.get(f"{m}_rtu")
                        pnt_no = row_dict.get(f"{m}_pnt")
                        if not (
                            isinstance(rtu_id, int)
                            and not isinstance(rtu_id, bool)
                            and rtu_id > 0
                            and isinstance(pnt_no, int)
                            and not isinstance(pnt_no, bool)
                            and pnt_no > 0
                        ):
                            continue
                        bound_cnt += 1
                        kind = "yx" if col_types.get(m) == "INTEGER" else "yc"
                        val = row_dict.get(m)
                        qual = row_dict.get(f"{m}_qual")
                        notuse = bool(row_dict.get(f"{m}_notuse"))
                        src_time = row_dict.get(f"{m}_time")
                        weight = row_dict.get(f"{m}_weight")
                        max_val = row_dict.get(f"{m}_max")
                        min_val = row_dict.get(f"{m}_min")
                        is_valid = (not notuse) and (qual == 0) and (val is not None)
                        if is_valid:
                            valid_cnt += 1
                        if m == "run" and val is not None:
                            run_val = int(val)
                        if m == "position" and val is not None:
                            pos_val = int(val)
                        label, unit = METRIC_META.get(m, (m, ""))
                        disp_val = val
                        if m == "soc" and isinstance(val, (int, float)) and 0.0 <= val <= 1.5:
                            disp_val = round(val * 100.0, 2)
                        m_list.append({
                            "metric": m,
                            "label": label,
                            "unit": unit,
                            "kind": kind,
                            "value": val,
                            "display_value": disp_val,
                            "quality": qual,
                            "quality_label": QUAL_LABELS.get(qual, str(qual) if qual is not None else "—"),
                            "max_value": max_val,
                            "min_value": min_val,
                            "weight": weight if weight is not None else 1.0,
                            "valid": is_valid,
                            "disabled": notuse,
                            "source_time": src_time,
                            "rtu_id": rtu_id,
                            "point_no": pnt_no,
                        })
                    category = _classify_device(table, dev_name)
                    devices.append({
                        "dev_id": f"{table}-{idx}",
                        "table_name": table,
                        "idx": idx,
                        "name": dev_name,
                        "category": category,
                        "bound_count": bound_cnt,
                        "valid_count": valid_cnt,
                        "run": run_val,
                        "position": pos_val,
                        "metrics": m_list,
                    })
        return devices

    # -------------------- 1. 主页面实时能源聚合 --------------------
    def _aggregate_energy(self, devices: list[dict[str, Any]]) -> dict[str, Any]:
        wind_kw = 0.0
        pv_kw = 0.0
        diesel_kw = 0.0
        storage_kw = 0.0
        hydrogen_prod_kw = 0.0
        hydrogen_gen_kw = 0.0
        load_kw = 0.0
        storage_socs: list[float] = []
        hydro_socs: list[float] = []
        hydro_pressure: float | None = None
        hydro_flow: float | None = None
        wind_speed: float | None = None
        irradiance: float | None = None

        for dev in devices:
            t = dev["table_name"]
            name = dev["name"]
            m_map = {m["metric"]: m for m in dev["metrics"]}
            p_val = _to_float(m_map["p"]["value"]) if "p" in m_map else None

            if t in {"ACGenerator", "DCGenerator"} and p_val is not None:
                if "风电" in name or "风力" in name:
                    wind_kw += p_val
                elif "光伏" in name:
                    pv_kw += p_val
                elif "柴油" in name:
                    diesel_kw += p_val
                elif "储能" in name:
                    storage_kw += p_val
                elif "燃料电池" in name or "氢" in name:
                    hydrogen_gen_kw += p_val
            elif t in {"ACLoad", "DCLoad"} and p_val is not None:
                if "制氢" in name:
                    hydrogen_prod_kw += p_val
                else:
                    load_kw += p_val

            if t in {"ACStorageGen", "DCStorageGen"} and "soc" in m_map:
                soc_v = _to_float(m_map["soc"]["display_value"])
                if soc_v is not None:
                    storage_socs.append(soc_v)
            if t == "HydroStorage":
                if "soc" in m_map:
                    sv = _to_float(m_map["soc"]["display_value"])
                    if sv is not None:
                        hydro_socs.append(sv)
                if "pressure" in m_map:
                    pv = _to_float(m_map["pressure"]["value"])
                    if pv is not None:
                        hydro_pressure = pv
                if "flow" in m_map:
                    fv = _to_float(m_map["flow"]["value"])
                    if fv is not None:
                        hydro_flow = fv
            if t == "ACWindGen" and "wind_speed" in m_map and wind_speed is None:
                wind_speed = _to_float(m_map["wind_speed"]["value"])
            if t in {"ACPVGen", "DCPVGen"} and "irradiance" in m_map and irradiance is None:
                irradiance = _to_float(m_map["irradiance"]["value"])

        total_load_kw = load_kw + hydrogen_prod_kw
        renewable_kw = max(0.0, wind_kw) + max(0.0, pv_kw) + max(0.0, hydrogen_gen_kw)
        total_gen_kw = renewable_kw + max(0.0, diesel_kw) + max(0.0, storage_kw)
        renewable_ratio_pct = (renewable_kw / total_gen_kw * 100.0) if total_gen_kw > 1e-6 else 0.0

        return {
            "pv_kw": round(pv_kw, 2),
            "wind_kw": round(wind_kw, 2),
            "hydrogen_prod_kw": round(hydrogen_prod_kw, 2),
            "hydrogen_gen_kw": round(hydrogen_gen_kw, 2),
            "diesel_kw": round(diesel_kw, 2),
            "storage_kw": round(storage_kw, 2),
            "storage_soc_pct": round(sum(storage_socs) / len(storage_socs), 1) if storage_socs else 0.0,
            "load_kw": round(load_kw, 2),
            "total_load_kw": round(total_load_kw, 2),
            "renewable_kw": round(renewable_kw, 2),
            "total_gen_kw": round(total_gen_kw, 2),
            "renewable_ratio_pct": round(renewable_ratio_pct, 1),
            "hydro_soc_pct": round(sum(hydro_socs) / len(hydro_socs), 1) if hydro_socs else 0.0,
            "hydro_pressure_mpa": round(hydro_pressure, 2) if hydro_pressure is not None else None,
            "hydro_flow": round(hydro_flow, 2) if hydro_flow is not None else None,
            "wind_speed": round(wind_speed, 2) if wind_speed is not None else None,
            "irradiance": round(irradiance, 2) if irradiance is not None else None,
        }

    def get_overview(self) -> dict[str, Any]:
        devices = self._read_all_devices()
        energy = self._aggregate_energy(devices)
        comm_rtus = self.get_comm_rtus()["items"]
        online_rtus = sum(1 for r in comm_rtus if r.get("connection_state") == "connected")
        total_bound = sum(d["bound_count"] for d in devices)
        total_valid = sum(d["valid_count"] for d in devices)

        now_utc = datetime.now(timezone.utc)
        running_days = max(1, (now_utc - STATION_COMMISSION_DATE).days)

        # 基于站点基准累计数 + 本会话实时积分增量（万 kWh）
        with self._energy_accum_lock:
            inc = dict(self._session_kwh)
        wind_total_wkwh = round(53.95 + inc.get("wind", 0.0) / 10000.0, 4)
        pv_total_wkwh = round(12.50 + inc.get("pv", 0.0) / 10000.0, 4)
        storage_total_wkwh = round(48.34 + inc.get("storage", 0.0) / 10000.0, 4)
        hydro_total_wkwh = round(0.22 + inc.get("hydrogen", 0.0) / 10000.0, 4)
        renewable_total_wkwh = round(wind_total_wkwh + pv_total_wkwh + hydro_total_wkwh, 2)
        diesel_total_wkwh = round(126.10 + inc.get("diesel", 0.0) / 10000.0, 2)
        load_total_wkwh = round(184.80 + inc.get("load", 0.0) / 10000.0, 2)
        # 约 0.25 kg/kWh 柴油节约换算（1 万 kWh ≈ 2.5 吨，基数 128.3 吨）
        diesel_saved_tons = round(128.3 + (inc.get("wind", 0.0) + inc.get("pv", 0.0)) * 0.00025, 2)

        return {
            "timestamp": now_utc.isoformat(timespec="seconds"),
            "station_name": "南极秦岭站综合能量管理系统",
            "scene": f"{self.runtime_name}/{self.scene_name}",
            "kpis": {
                "running_days": running_days,
                "load_total_wkwh": load_total_wkwh,
                "diesel_total_wkwh": diesel_total_wkwh,
                "renewable_total_wkwh": renewable_total_wkwh,
                "diesel_saved_tons": diesel_saved_tons,
                "wind_total_wkwh": round(wind_total_wkwh, 2),
                "pv_total_wkwh": round(pv_total_wkwh, 2),
                "storage_total_wkwh": round(storage_total_wkwh, 2),
                "hydro_total_wkwh": round(hydro_total_wkwh, 2),
                "renewable_share_pct": energy["renewable_ratio_pct"],
            },
            "realtime": energy,
            "health": {
                "device_count": len(devices),
                "bound_measurements": total_bound,
                "valid_measurements": total_valid,
                "rtu_total": len(comm_rtus),
                "rtu_online": online_rtus,
            },
        }

    # -------------------- 2. SVG 潮流图底图与实时映射 --------------------
    def get_svg_bytes(self) -> bytes:
        if not self.svg_path.is_file():
            raise FileNotFoundError(f"未找到潮流图 SVG 文件：{self.svg_path}")
        return self.svg_path.read_bytes()

    def _read_model_params(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
        """读取 model/devices.sqlite3 全部设备静态参数（主表 + 扩展表），按 mtime 缓存。"""
        if not self.model_db.is_file():
            return {}, {}
        mtime = self.model_db.stat().st_mtime_ns
        if self._model_params_cache is not None and self._model_params_cache[0] == mtime:
            return self._model_params_cache[1]

        params_by_dev: dict[str, list[dict[str, Any]]] = defaultdict(list)
        meta_by_dev: dict[str, dict[str, Any]] = {}

        def _fmt_param_val(eng: str, raw: Any, unit: str) -> str | None:
            if raw is None:
                return None
            if isinstance(raw, str):
                text = raw.strip()
                if not text:
                    return None
                return f"{text} {unit}".strip() if unit else text
            if isinstance(raw, bool):
                return "是" if raw else "否"
            if isinstance(raw, (int, float)):
                if not math.isfinite(float(raw)):
                    return None
                if eng == "run_stat":
                    return "投运" if int(raw) else "停运"
                if eng == "status":
                    return "合闸" if int(raw) else "分闸"
                num = float(raw)
                s = str(int(num)) if abs(num - round(num)) < 1e-9 else f"{num:.4g}"
                return f"{s} {unit}".strip() if unit else s
            return str(raw)

        def _append_fields(dev_id: str, fields: list[tuple[str, str]], row: tuple[Any, ...], seen: set[str]) -> None:
            for (eng, chn), raw in zip(fields, row):
                if eng in _MODEL_SKIP_FIELDS or eng in seen:
                    continue
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    continue
                label, unit = MODEL_PARAM_META.get(eng, (chn or eng, ""))
                val_str = _fmt_param_val(eng, raw, unit)
                if val_str is None:
                    continue
                seen.add(eng)
                params_by_dev[dev_id].append({
                    "key": eng,
                    "label": label,
                    "value": val_str,
                })

        try:
            with self._open_readonly(self.model_db) as conn:
                has_schema = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rtdb_schema_tables'"
                ).fetchone()
                if not has_schema:
                    return {}, {}
                tables = [
                    r[0]
                    for r in conn.execute("SELECT table_name FROM rtdb_schema_tables ORDER BY ordinal").fetchall()
                ]
                seen_keys: dict[str, set[str]] = defaultdict(set)
                # 先读主表
                for tbl in tables:
                    if tbl in ("Model", "basevoltage", "HydroMedium") or tbl in _MODEL_EXTENSIONS:
                        continue
                    cols = conn.execute(
                        "SELECT ordinal, eng_name, chn_name FROM rtdb_schema_columns WHERE table_name=? ORDER BY ordinal",
                        (tbl,),
                    ).fetchall()
                    if not cols:
                        continue
                    eng_list = [r[1] for r in cols]
                    if "idx" not in eng_list:
                        continue
                    idx_pos = eng_list.index("idx")
                    name_pos = eng_list.index("name") if "name" in eng_list else -1
                    sel = ", ".join(f'"c{int(r[0])}"' for r in cols)
                    fields = [(r[1], r[2]) for r in cols]
                    for row in conn.execute(f'SELECT {sel} FROM "rtdb_data_{tbl}"').fetchall():
                        try:
                            dev_idx = int(row[idx_pos])
                        except (TypeError, ValueError):
                            continue
                        dev_id = f"{tbl}-{dev_idx}"
                        dev_name = str(row[name_pos]) if name_pos >= 0 and row[name_pos] is not None else dev_id
                        meta_by_dev[dev_id] = {
                            "dev_id": dev_id,
                            "table_name": tbl,
                            "idx": dev_idx,
                            "name": dev_name,
                            "category": _classify_device(tbl, dev_name),
                        }
                        _append_fields(dev_id, fields, tuple(row), seen_keys[dev_id])

                # 再按外键合并扩展表专项参数（风机/光伏/储能/柴发）
                for ext_tbl, (parent_tbl, fk_col) in _MODEL_EXTENSIONS.items():
                    if ext_tbl not in tables:
                        continue
                    cols = conn.execute(
                        "SELECT ordinal, eng_name, chn_name FROM rtdb_schema_columns WHERE table_name=? ORDER BY ordinal",
                        (ext_tbl,),
                    ).fetchall()
                    if not cols:
                        continue
                    eng_list = [r[1] for r in cols]
                    if fk_col not in eng_list:
                        continue
                    fk_pos = eng_list.index(fk_col)
                    sel = ", ".join(f'"c{int(r[0])}"' for r in cols)
                    fields = [(r[1], r[2]) for r in cols]
                    for row in conn.execute(f'SELECT {sel} FROM "rtdb_data_{ext_tbl}"').fetchall():
                        try:
                            parent_idx = int(row[fk_pos])
                        except (TypeError, ValueError):
                            continue
                        parent_dev_id = f"{parent_tbl}-{parent_idx}"
                        _append_fields(parent_dev_id, fields, tuple(row), seen_keys[parent_dev_id])
        except sqlite3.Error:
            return {}, {}

        result = (dict(params_by_dev), meta_by_dev)
        self._model_params_cache = (mtime, result)
        return result

    def get_powerflow(self) -> dict[str, Any]:
        devices = self._read_all_devices()
        model_params_map, model_meta_map = self._read_model_params()
        by_dev_id: dict[str, dict[str, Any]] = {d["dev_id"]: d for d in devices}
        # 储能 SOC 单独存放在 ACStorageGen / DCStorageGen，按设备名关联回 ACGenerator / DCGenerator
        soc_by_name: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for d in devices:
            if d["table_name"] in {"ACStorageGen", "DCStorageGen"}:
                for m in d["metrics"]:
                    if m["metric"] == "soc" and m["display_value"] is not None:
                        soc_by_name[d["name"]] = (d, m)

        def make_slot(
            owner_dev: dict[str, Any],
            metric_item: dict[str, Any],
            value: Any,
            unit: str,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "value": value,
                "unit": unit,
                "valid": metric_item["valid"],
                "table_name": owner_dev["table_name"],
                "idx": owner_dev["idx"],
                "device_name": owner_dev["name"],
                "metric": metric_item["metric"],
                "label": metric_item["label"],
                "kind": metric_item["kind"],
                "rtu_id": metric_item["rtu_id"],
                "point_no": metric_item["point_no"],
                "quality": metric_item["quality"],
                "quality_label": metric_item["quality_label"],
                "disabled": metric_item["disabled"],
                "max_value": metric_item["max_value"],
                "min_value": metric_item["min_value"],
                "weight": metric_item.get("weight", 1.0),
                "source_time": metric_item["source_time"],
            }
            if extra:
                payload.update(extra)
            return payload

        # 组装 SVG 108 个 <g class="mg" dev="..."> 所需的精确测点映射
        svg_slots: dict[str, dict[str, dict[str, Any]]] = {}
        breakers: dict[str, dict[str, Any]] = {}
        dev_summaries: dict[str, dict[str, Any]] = {}

        for dev in devices:
            dev_id = dev["dev_id"]
            t = dev["table_name"]
            m_map = {m["metric"]: m for m in dev["metrics"]}

            def pick(*names: str) -> dict[str, Any] | None:
                for n in names:
                    if n in m_map and m_map[n]["value"] is not None:
                        return m_map[n]
                return None

            slots: dict[str, dict[str, Any]] = {}

            p_m = pick("p", "i_p", "ac_p", "dc_p")
            if p_m is not None:
                slots["activePower"] = make_slot(dev, p_m, round(float(p_m["value"]), 2), "kW")
            q_m = pick("q", "i_q", "ac_q")
            if q_m is not None:
                slots["reactivePower"] = make_slot(dev, q_m, round(float(q_m["value"]), 2), "kvar")
            v_m = pick("uab", "u", "ac_uab", "dc_u", "i_u", "j_u")
            if v_m is not None:
                slots["voltage"] = make_slot(dev, v_m, round(float(v_m["value"]), 1), "V")
            i_m = pick("ia", "i", "i_ia", "i_i", "ac_ia", "dc_i")
            if i_m is not None:
                slots["current"] = make_slot(dev, i_m, round(float(i_m["value"]), 2), "A")
            f_m = pick("f", "ac_f", "i_f")
            if f_m is not None:
                slots["frequency"] = make_slot(dev, f_m, round(float(f_m["value"]), 2), "Hz")
            # SOC / 储氢液位
            if dev["name"] in soc_by_name:
                soc_dev, soc_m = soc_by_name[dev["name"]]
                slots["soc"] = make_slot(soc_dev, soc_m, round(float(soc_m["display_value"]), 1), "%")
            elif "soc" in m_map and m_map["soc"]["display_value"] is not None:
                soc_m = m_map["soc"]
                val_soc = round(float(soc_m["display_value"]), 1)
                slots["soc"] = make_slot(dev, soc_m, val_soc, "%")
                slots["level"] = make_slot(dev, soc_m, val_soc, "%")
            # 氢能压力与流量
            pr_m = pick("pressure")
            if pr_m is not None:
                slots["pressure"] = make_slot(dev, pr_m, round(float(pr_m["value"]), 2), "MPa")
            fl_m = pick("flow")
            if fl_m is not None:
                slots["flow"] = make_slot(dev, fl_m, round(float(fl_m["value"]), 2), "Nm³/h")
            # 断路器状态
            pos_m = pick("position")
            if pos_m is not None:
                closed = int(pos_m["value"]) != 0
                slots["status"] = make_slot(
                    dev,
                    pos_m,
                    "合闸" if closed else "分闸",
                    "",
                    {"raw": 1 if closed else 0},
                )
                if t in {"ACBreak", "DCBreak"}:
                    breakers[dev_id] = {
                        "closed": closed,
                        "current": slots["current"]["value"] if "current" in slots else None,
                    }

            if slots:
                svg_slots[dev_id] = slots

            dev_summaries[dev_id] = {
                "dev_id": dev_id,
                "table_name": t,
                "idx": dev["idx"],
                "name": dev["name"],
                "category": dev["category"],
                "run": dev["run"],
                "position": dev["position"],
                "valid_count": dev["valid_count"],
                "bound_count": dev["bound_count"],
                "metrics": dev["metrics"],
                "model_params": model_params_map.get(dev_id, []),
            }

        # 补充仅存在于模型库、尚未在 scdfe_d 绑定量测的设备（如母线、阀门等），确保悬停仍显示模型参数
        for dev_id, meta in model_meta_map.items():
            if dev_id not in dev_summaries:
                dev_summaries[dev_id] = {
                    "dev_id": dev_id,
                    "table_name": meta["table_name"],
                    "idx": meta["idx"],
                    "name": meta["name"],
                    "category": meta["category"],
                    "run": None,
                    "position": None,
                    "valid_count": 0,
                    "bound_count": 0,
                    "metrics": [],
                    "model_params": model_params_map.get(dev_id, []),
                }

        # ACRealBs / DCRealBs 若自身无电压点，从同压级母线/节点补充代表电压
        ac_bus_v = [
            float(m["value"])
            for d in devices
            if d["table_name"] == "ACNode"
            for m in d["metrics"]
            if m["metric"] == "uab" and m["value"] is not None
        ]
        dc_bus_v = [
            float(m["value"])
            for d in devices
            if d["table_name"] == "DCNode"
            for m in d["metrics"]
            if m["metric"] == "u" and m["value"] is not None and 600 <= float(m["value"]) <= 900
        ]
        if ac_bus_v:
            avg_ac_v = round(sum(ac_bus_v) / len(ac_bus_v), 1)
            for dev_id, d in by_dev_id.items():
                if d["table_name"] == "ACRealBs":
                    svg_slots.setdefault(dev_id, {})["voltage"] = {
                        "value": avg_ac_v, "unit": "V", "valid": True,
                        "table_name": d["table_name"], "idx": d["idx"], "device_name": d["name"],
                        "metric": "uab", "label": "母线电压", "kind": "yc",
                    }
        if dc_bus_v:
            avg_dc_v = round(sum(dc_bus_v) / len(dc_bus_v), 1)
            for dev_id, d in by_dev_id.items():
                if d["table_name"] == "DCRealBs":
                    svg_slots.setdefault(dev_id, {})["voltage"] = {
                        "value": avg_dc_v, "unit": "V", "valid": True,
                        "table_name": d["table_name"], "idx": d["idx"], "device_name": d["name"],
                        "metric": "u", "label": "母线电压", "kind": "yc",
                    }

        # AcE2Hydro / Hydro2DcE 若主表无功率点，从关联负荷/发电设备补充
        for dev in devices:
            if dev["table_name"] == "ACLoad" and "交流电制氢-1" in dev["name"]:
                m_map = {m["metric"]: m for m in dev["metrics"]}
                if "p" in m_map and m_map["p"]["value"] is not None:
                    svg_slots.setdefault("AcE2Hydro-1", {})["activePower"] = make_slot(
                        dev, m_map["p"], round(float(m_map["p"]["value"]), 2), "kW"
                    )
                if "uab" in m_map and m_map["uab"]["value"] is not None:
                    svg_slots.setdefault("AcE2Hydro-1", {})["voltage"] = make_slot(
                        dev, m_map["uab"], round(float(m_map["uab"]["value"]), 1), "V"
                    )
            if dev["table_name"] == "HydroSource" and "交流电制氢-1" in dev["name"]:
                m_map = {m["metric"]: m for m in dev["metrics"]}
                if "flow" in m_map and m_map["flow"]["value"] is not None:
                    svg_slots.setdefault("AcE2Hydro-1", {})["flow"] = make_slot(
                        dev, m_map["flow"], round(float(m_map["flow"]["value"]), 2), "Nm³/h"
                    )
            if dev["table_name"] == "DCGenerator" and "直流燃料电池-1" in dev["name"]:
                m_map = {m["metric"]: m for m in dev["metrics"]}
                if "p" in m_map and m_map["p"]["value"] is not None:
                    svg_slots.setdefault("Hydro2DcE-1", {})["activePower"] = make_slot(
                        dev, m_map["p"], round(float(m_map["p"]["value"]), 2), "kW"
                    )
                if "u" in m_map and m_map["u"]["value"] is not None:
                    svg_slots.setdefault("Hydro2DcE-1", {})["voltage"] = make_slot(
                        dev, m_map["u"], round(float(m_map["u"]["value"]), 1), "V"
                    )
            if dev["table_name"] == "HydroLoad" and "直流燃料电池-1" in dev["name"]:
                m_map = {m["metric"]: m for m in dev["metrics"]}
                if "flow" in m_map and m_map["flow"]["value"] is not None:
                    svg_slots.setdefault("Hydro2DcE-1", {})["flow"] = make_slot(
                        dev, m_map["flow"], round(float(m_map["flow"]["value"]), 2), "Nm³/h"
                    )

        flows = self._compute_powerflow_vectors(devices, breakers)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "svg_slots": svg_slots,
            "breakers": breakers,
            "flows": flows,
            "devices": dev_summaries,
        }

    def _load_svg_topology(self) -> dict[str, Any]:
        """解析并缓存 SVG 中设备节点拓扑与 Segment_Layer 连接边关系。"""
        if not self.svg_path.is_file():
            return {"devices": {}, "edges": []}
        mtime_ns = self.svg_path.stat().st_mtime_ns
        if self._svg_topo_cache is not None and self._svg_topo_cache[0] == mtime_ns:
            return self._svg_topo_cache[1]

        dev_nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []
        try:
            root = ET.parse(self.svg_path).getroot()
            root_g = next((c for c in root if c.attrib.get("id") == "root_g"), root)
            for layer in root_g:
                lid = layer.attrib.get("id", "")
                if lid == "Segment_Layer":
                    for elem in layer:
                        eid = elem.attrib.get("id")
                        src = elem.attrib.get("source-dev-id")
                        dst = elem.attrib.get("target-dev-id")
                        if eid and src and dst and elem.attrib.get("d"):
                            edges.append({
                                "id": eid,
                                "source": src,
                                "target": dst,
                                "cls": elem.attrib.get("class", ""),
                            })
                    continue
                for elem in layer:
                    if not elem.tag.endswith("use"):
                        continue
                    dev_id = elem.attrib.get("dev-id")
                    if not dev_id:
                        continue
                    vt = elem.attrib.get("voltage-type")
                    vt1 = elem.attrib.get("voltage-type-1") or vt
                    vt2 = elem.attrib.get("voltage-type-2") or vt
                    dev_nodes[dev_id] = {
                        "dev_id": dev_id,
                        "table": dev_id.split("-", 1)[0],
                        "vtype": vt or vt1 or "",
                        "node": (vt, elem.attrib.get("node")) if elem.attrib.get("node") else None,
                        "node1": (vt1, elem.attrib.get("node-1")) if elem.attrib.get("node-1") else None,
                        "node2": (vt2, elem.attrib.get("node-2")) if elem.attrib.get("node-2") else None,
                    }
        except Exception:
            return {"devices": {}, "edges": []}

        topo = {"devices": dev_nodes, "edges": edges}
        self._svg_topo_cache = (mtime_ns, topo)
        return topo

    def _compute_powerflow_vectors(
        self,
        devices: list[dict[str, Any]],
        breakers: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """依据实时有功功率与节点拓扑计算每条支路与连接边的有功潮流幅值及方向。"""
        topo = self._load_svg_topology()
        svg_devs = topo["devices"]
        by_dev_id = {d["dev_id"]: d for d in devices}

        # 统计设备向各电气节点 (vtype, node_id) 注入的有功功率（正值 = 设备注入节点）
        node_injections: dict[tuple[str, str], float] = defaultdict(float)
        dev_out_power: dict[str, float] = {}
        branch_flows: dict[str, dict[str, Any]] = {}

        def mval(dev: dict[str, Any] | None, metric: str) -> float | None:
            if not dev:
                return None
            for m in dev["metrics"]:
                if m["metric"] == metric and m["value"] is not None:
                    return _to_float(m["value"])
            return None

        # 氢能关联电功率（制氢负荷 / 燃料电池发电）
        h2_prod_kw = 0.0
        h2_gen_kw = 0.0
        for d in devices:
            if d["table_name"] == "ACLoad" and "制氢" in d["name"]:
                pv = mval(d, "p")
                if pv is not None:
                    h2_prod_kw += pv
            elif d["table_name"] == "DCGenerator" and ("燃料电池" in d["name"] or "氢" in d["name"]):
                pv = mval(d, "p")
                if pv is not None:
                    h2_gen_kw += pv

        for dev in devices:
            dev_id = dev["dev_id"]
            t = dev["table_name"]
            info = svg_devs.get(dev_id)

            if t in {"ACBranch", "DCBranch", "ACZeroBranch"}:
                ip = mval(dev, "i_p")
                jp = mval(dev, "j_p")
                vtype = "dc" if t == "DCBranch" else "ac"
                if ip is not None:
                    branch_flows[dev_id] = {"p_kw": round(ip, 2), "voltage_type": vtype}
                    if info and info["node1"]:
                        node_injections[info["node1"]] += -ip
                if jp is not None and info and info["node2"]:
                    node_injections[info["node2"]] += -jp
                continue

            if t in {"ACGenerator", "DCGenerator"}:
                p = mval(dev, "p")
                if p is not None:
                    dev_out_power[dev_id] = p
                    if info and info["node"]:
                        node_injections[info["node"]] += p
            elif t in {"ACLoad", "DCLoad"}:
                p = mval(dev, "p")
                if p is not None:
                    dev_out_power[dev_id] = -p
                    if info and info["node"]:
                        node_injections[info["node"]] += -p
            elif t == "DCDCConverter":
                ip = mval(dev, "i_p")
                jp = mval(dev, "j_p")
                if info and info["node1"] and ip is not None:
                    node_injections[info["node1"]] += -ip
                if info and info["node2"] and jp is not None:
                    node_injections[info["node2"]] += -jp
            elif t == "DCACConverter":
                acp = mval(dev, "ac_p")
                dcp = mval(dev, "dc_p")
                if info:
                    for term in (info["node1"], info["node2"]):
                        if not term:
                            continue
                        if term[0] == "ac" and acp is not None:
                            node_injections[term] += -acp
                        elif term[0] == "dc" and dcp is not None:
                            node_injections[term] += -dcp

        # 计算 Segment_Layer 每一条连接边 source -> target 的精确功率与方向
        edge_flows: dict[str, dict[str, Any]] = {}
        bus_nodes = {
            dev_id: info["node"]
            for dev_id, info in svg_devs.items()
            if info["table"] in {"ACRealBs", "DCRealBs"} and info["node"]
        }

        for edge in topo["edges"]:
            eid = edge["id"]
            src = edge["source"]
            dst = edge["target"]
            src_tbl = src.split("-", 1)[0]
            dst_tbl = dst.split("-", 1)[0]
            cls = edge.get("cls", "")
            if "dcv" in cls or src_tbl.startswith("DC") or dst_tbl.startswith("DC"):
                vtype = "dc"
            elif "Hydro" in src_tbl or "Hydro" in dst_tbl:
                vtype = "hydro"
            else:
                vtype = "ac"

            # 若涉及断路器且已分闸，潮流归零
            if src in breakers and not breakers[src]["closed"]:
                edge_flows[eid] = {"p_kw": 0.0, "voltage_type": vtype}
                continue
            if dst in breakers and not breakers[dst]["closed"]:
                edge_flows[eid] = {"p_kw": 0.0, "voltage_type": vtype}
                continue

            p_kw: float | None = None
            # 1. 氢能连接边
            if src.startswith("AcE2Hydro") and dst.startswith("HydroStorage"):
                p_kw = h2_prod_kw
                vtype = "hydro"
            elif src.startswith("Hydro2DcE") and dst.startswith("HydroStorage"):
                # 燃料电池发电时从储氢罐流向燃料电池（反向）
                p_kw = -h2_gen_kw
                vtype = "hydro"
            # 2. 发电机直连边（src 为发电机）
            elif src in dev_out_power and src_tbl in {"ACGenerator", "DCGenerator"}:
                p_kw = dev_out_power[src]
            # 3. 负荷直连边（dst 为负荷）
            elif dst in dev_out_power and dst_tbl in {"ACLoad", "DCLoad"}:
                p_kw = -dev_out_power[dst]
            # 4. 断路器连母线（src 为断路器，dst 为母线）
            elif src_tbl in {"ACBreak", "DCBreak"} and dst in bus_nodes:
                b_info = svg_devs.get(src)
                bus_term = bus_nodes[dst]
                if b_info:
                    non_bus = b_info["node1"] if b_info["node2"] == bus_term else b_info["node2"]
                    if non_bus and non_bus in node_injections:
                        p_kw = node_injections[non_bus]
            # 5. 兜底：若目标为断路器，查其非母线侧注入
            elif dst_tbl in {"ACBreak", "DCBreak"} and src in by_dev_id:
                p_kw = dev_out_power.get(src)

            if p_kw is not None:
                edge_flows[eid] = {"p_kw": round(float(p_kw), 2), "voltage_type": vtype}

        return {"branches": branch_flows, "edges": edge_flows}

    # -------------------- 3. 设备图与 24 小时日曲线 --------------------
    def get_devices(self, category: str | None = None, search: str | None = None) -> dict[str, Any]:
        all_devices = [d for d in self._read_all_devices() if d["bound_count"] > 0]
        cat_counts: dict[str, int] = defaultdict(int)
        for d in all_devices:
            cat_counts[d["category"]] += 1

        categories = [
            {"key": key, "label": label, "count": cat_counts.get(key, 0)}
            for key, label in DEVICE_CATEGORIES
            if cat_counts.get(key, 0) > 0
        ]
        items = all_devices
        if category:
            items = [d for d in items if d["category"] == category]
        if search:
            needle = search.strip().lower()
            items = [
                d for d in items
                if needle in d["name"].lower()
                or needle in d["table_name"].lower()
                or needle in d["dev_id"].lower()
            ]
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "categories": categories,
            "items": items,
            "total": len(items),
        }

    def get_device_trend(self, table_name: str, idx: int, *, hours: float = 24.0) -> dict[str, Any]:
        hours = max(0.5, min(24.0, float(hours)))
        now_ms = int(time.time() * 1000)
        since_ms = now_ms - int(hours * 3600 * 1000)
        series_map: dict[str, list[list[float]]] = defaultdict(list)

        with self._history_lock, self._open_history() as conn:
            rows = conn.execute(
                "SELECT metric, ts_ms, value FROM device_daily_samples "
                "WHERE table_name=? AND idx=? AND ts_ms>=? ORDER BY ts_ms ASC",
                (table_name, idx, since_ms),
            ).fetchall()
        for metric, ts_ms, val in rows:
            series_map[metric].append([int(ts_ms), round(float(val), 4)])

        # 若历史点数超出 720 点，均匀降采样保留首尾保证绘制流畅
        max_pts = 720
        series: list[dict[str, Any]] = []
        for metric, pts in sorted(series_map.items()):
            if len(pts) > max_pts:
                step = (len(pts) - 1) / (max_pts - 1)
                pts = [pts[int(round(i * step))] for i in range(max_pts)]
            label, unit = METRIC_META.get(metric, (metric, ""))
            series.append({
                "metric": metric,
                "label": label,
                "unit": unit,
                "points": pts,
                "latest": pts[-1][1] if pts else None,
            })

        return {
            "table_name": table_name,
            "idx": idx,
            "hours": hours,
            "since_ms": since_ms,
            "now_ms": now_ms,
            "series": series,
        }

    # -------------------- 4. 通信图（RTU / 通道 / 四遥） --------------------
    def get_comm_rtus(self) -> dict[str, Any]:
        if not self.c_db.is_file():
            return {"items": [], "total": 0}
        with self._open_readonly(self.c_db) as conn:
            rtus = [
                dict(r)
                for r in conn.execute(
                    "SELECT rtu_id, channel_id, channel_type, name, process_enable, status, refresh_time "
                    "FROM fes_rtu_define ORDER BY rtu_id"
                ).fetchall()
            ]
            runtime_rows = {
                int(r["channel_id"]): dict(r)
                for r in conn.execute(
                    "SELECT * FROM fes_channel_runtime_status WHERE service_name='scdfe_c'"
                ).fetchall()
            }
            # 统计各 RTU 四遥点数
            point_counts: dict[int, dict[str, int]] = defaultdict(lambda: {"yc": 0, "yx": 0, "yk": 0, "yt": 0})
            for kind, tbl in KIND_TABLES.items():
                for rtu_id, cnt in conn.execute(
                    f'SELECT rtu_id, COUNT(*) FROM "{tbl}" GROUP BY rtu_id'
                ).fetchall():
                    point_counts[int(rtu_id)][kind] = int(cnt)

        items: list[dict[str, Any]] = []
        for rtu in rtus:
            rid = int(rtu["rtu_id"])
            cid = int(rtu["channel_id"]) if rtu.get("channel_id") is not None else 0
            rt = runtime_rows.get(cid, {})
            diag = {}
            if rt.get("diagnostic_json"):
                try:
                    diag = json.loads(rt["diagnostic_json"])
                except Exception:
                    diag = {}
            items.append({
                "rtu_id": rid,
                "channel_id": cid,
                "channel_type": rtu.get("channel_type") or "",
                "name": rtu.get("name") or f"RTU-{rid}",
                "process_enable": int(rtu.get("process_enable") or 0),
                "state": rt.get("state") or "stopped",
                "connection_state": rt.get("connection_state") or "disconnected",
                "process_pid": rt.get("process_pid") or 0,
                "heartbeat_time": _iso_from_ms(rt.get("process_heartbeat_time")),
                "refresh_time": _iso_from_ms(rtu.get("refresh_time")),
                "rx_count": int(rt.get("rx_count") or 0),
                "tx_count": int(rt.get("tx_count") or 0),
                "error_count": int(rt.get("error_count") or 0),
                "last_error": rt.get("last_error") or rt.get("process_error") or "",
                "updated_yc": int(diag.get("updated_yc") or 0),
                "updated_yx": int(diag.get("updated_yx") or 0),
                "point_counts": point_counts[rid],
            })
        return {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items, "total": len(items)}

    def get_comm_channels(self) -> dict[str, Any]:
        if not self.c_db.is_file():
            return {"items": [], "total": 0}
        rtu_data = self.get_comm_rtus()["items"]
        rtu_by_ch = {(r["channel_type"], r["channel_id"]): r for r in rtu_data}
        items: list[dict[str, Any]] = []
        with self._open_readonly(self.c_db) as conn:
            existing = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            for ch_type, tbl in CHANNEL_PARAMETER_TABLES.items():
                if tbl not in existing:
                    continue
                for row in conn.execute(f'SELECT * FROM "{tbl}" ORDER BY channel_id').fetchall():
                    d = dict(row)
                    cid = int(d.get("channel_id") or d.get("id") or 0)
                    rtu_info = rtu_by_ch.get((ch_type, cid), {})
                    endpoint = ""
                    if d.get("remote_host"):
                        endpoint = f"{d.get('remote_host')}:{d.get('remote_port') or ''}"
                    elif d.get("serial_port"):
                        endpoint = f"{d.get('serial_port')} @ {d.get('baud_rate') or ''}"
                    elif d.get("opc_name"):
                        endpoint = str(d.get("opc_name"))
                    elif d.get("interface"):
                        endpoint = f"{d.get('interface')}:{d.get('channel') or ''}"
                    items.append({
                        "channel_id": cid,
                        "channel_type": ch_type,
                        "parameter_table": tbl,
                        "name": d.get("name") or rtu_info.get("name") or f"通道 {cid}",
                        "rtu_id": rtu_info.get("rtu_id"),
                        "rtu_name": rtu_info.get("name") or "",
                        "endpoint": endpoint,
                        "state": rtu_info.get("state") or "idle",
                        "connection_state": rtu_info.get("connection_state") or "disconnected",
                        "rx_count": rtu_info.get("rx_count", 0),
                        "tx_count": rtu_info.get("tx_count", 0),
                        "error_count": rtu_info.get("error_count", 0),
                        "last_error": rtu_info.get("last_error", ""),
                        "parameters": {k: v for k, v in d.items() if not k.startswith("_")},
                    })
        return {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items, "total": len(items)}

    def get_comm_points(
        self,
        *,
        kind: str = "yc",
        rtu_id: int | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if kind not in KIND_TABLES:
            raise ValueError(f"不支持的四遥类型：{kind}")
        if not self.c_db.is_file():
            return {"kind": kind, "items": [], "total": 0, "limit": limit, "offset": offset}
        tbl = KIND_TABLES[kind]
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        where: list[str] = []
        params: list[Any] = []
        if rtu_id is not None:
            where.append("rtu_id = ?")
            params.append(int(rtu_id))
        if search and search.strip():
            needle = f"%{search.strip()}%"
            where.append("(name LIKE ? OR CAST(pnt_no AS TEXT) LIKE ?)")
            params.extend([needle, needle])
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        with self._open_readonly(self.c_db) as conn:
            total = int(conn.execute(f'SELECT COUNT(*) FROM "{tbl}"{where_sql}', params).fetchone()[0])
            cols = {c[1] for c in conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()}
            wanted = [c for c in ("rtu_id", "pnt_no", "name", "raw_value", "raw_status", "value", "status", "refresh_time", "cmd_handle_time") if c in cols]
            quoted = ", ".join(f'"{c}"' for c in wanted)
            rows = conn.execute(
                f'SELECT {quoted} FROM "{tbl}"{where_sql} ORDER BY rtu_id, pnt_no LIMIT ? OFFSET ?',
                [*params, limit, offset],
            ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["refresh_time_iso"] = _iso_from_ms(d.get("refresh_time"))
            if "cmd_handle_time" in d:
                d["cmd_handle_time_iso"] = _iso_from_ms(d.get("cmd_handle_time"))
            items.append(d)
        return {
            "kind": kind,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    # -------------------- 5. 运行日志与报文日志 --------------------
    def list_log_files(self, *, record: str = "runtime") -> dict[str, Any]:
        root = packet_log_directory(self.log_dir) if record == "packet" else self.log_dir
        items: list[dict[str, Any]] = []
        if root.is_dir():
            for entry, rel in log_entries(root, "scdfe_c-"):
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                m = LOG_PATTERN.fullmatch(entry.name)
                if not m:
                    continue
                items.append({
                    "filename": rel,
                    "process": m.group("process"),
                    "hour": m.group("hour"),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime * 1000),
                })
        items.sort(key=lambda x: (x["hour"], x["filename"]), reverse=True)
        processes = sorted({x["process"] for x in items})
        return {
            "record": record,
            "directory": str(root),
            "processes": processes,
            "items": items[:200],
            "total": len(items),
        }

    def read_logs(
        self,
        *,
        record: str = "runtime",
        filename: str | None = None,
        process: str | None = None,
        level: str | None = None,
        direction: str | None = None,
        keyword: str | None = None,
        limit: int = 300,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            "record": "packet" if record == "packet" else "runtime",
            "limit": str(max(10, min(1000, int(limit)))),
        }
        if filename:
            query["filename"] = filename
        if process:
            query["process"] = process
        if level:
            query["level"] = level
        if direction:
            query["direction"] = direction
        if keyword:
            query["q"] = keyword

        packet_dir = packet_log_directory(self.log_dir)
        if not self.log_dir.is_dir() and not packet_dir.is_dir():
            return {"items": [], "has_more": False, "scanned_files": 0, "order": "hour_file_desc_line_desc"}
        return search_logs(self.log_dir, query, family="c", packet_directory=packet_dir)

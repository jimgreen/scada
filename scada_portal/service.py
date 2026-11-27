"""SCADA 门户数据聚合、SVG 潮流快照、24 小时日曲线采样与日志检索服务。"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
import xml.etree.ElementTree as ET

LOG = logging.getLogger("scada.portal.service")

from core.log_paths import log_entries, packet_log_directory
from core.scdfe_control import InstanceLock, LOG_LEVEL_CODES, LOG_PATTERN, log_directory
from core.scdfe_logger import ScdfeLogger
from core.web_database import DEFAULT_WORKSPACE_ROOT
from core.log_query import LOG_PATTERNS, search_logs
from core.rtdb_schema import sync_embedded_schema
from core.common import CHANNEL_PARAMETER_TABLES, KIND_TABLES
from scada_portal.history_store import (
    ReadOnlyConnPool,
    ensure_extended_history_schema,
    get_history_stats,
    prune_history_samples,
    upsert_minute_rollup,
)
from scada_portal.report import (
    compute_polar_derived_metrics,
    render_daily_report_csv,
    render_daily_report_html,
    summarize_data_quality,
)
from scada_portal.stream import PortalEventBus
from scdfe_d.measurement_layout import (
    CONTROL_META,
    CTRL_STATUS_FAILED,
    CTRL_STATUS_IDLE,
    CTRL_STATUS_LABELS,
    CTRL_STATUS_PENDING,
    CTRL_SUFFIXES,
    QUAL_LABELS,
    QUAL_OK,
    QUAL_OVER_MAX,
    QUAL_UNDER_MIN,
    SUFFIXES,
)
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

# 模型库（model/devices.db）扩展表 -> (主表名, 关联外键列名)
_MODEL_EXTENSIONS: dict[str, tuple[str, str]] = {
    "ACWindGen": ("ACGenerator", "idx_acgenerator"),
    "ACPVGen": ("ACGenerator", "idx_acgenerator"),
    "ACDieselGen": ("ACGenerator", "idx_acgenerator"),
    "ACStorageGen": ("ACGenerator", "idx_acgenerator"),
    "DCPVGen": ("DCGenerator", "idx_dcgenerator"),
    "DCStorageGen": ("DCGenerator", "idx_dcgenerator"),
}
_EXT_TAB_LABELS: dict[str, str] = {
    "ACWindGen": "风机参数",
    "ACPVGen": "光伏参数",
    "DCPVGen": "光伏参数",
    "ACDieselGen": "柴发参数",
    "ACStorageGen": "储能参数",
    "DCStorageGen": "储能参数",
}
# 电-氢多能耦合设备外键 -> ((外键列, 目标表名, tab_key, tab_label), ...)
_COUPLING_LINKS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "AcE2Hydro": (
        ("idx_ac_load_t1", "ACLoad", "ac", "交流侧"),
        ("idx_h2_unit_t2", "HydroSource", "hydro", "氢能侧"),
    ),
    "Hydro2DcE": (
        ("idx_dc_unit_t1", "DCGenerator", "dc", "直流侧"),
        ("idx_h2_load_t2", "HydroLoad", "hydro", "氢能侧"),
    ),
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
    "ac_i_max": ("交流电流上限", "A"),
    "dc_p_max": ("直流功率上限", "kW"),
    "dc_p_min": ("直流功率下限", "kW"),
    "dc_i_max": ("直流电流上限", "A"),
    "ac_v_max": ("交流电压上限", "pu"),
    "ac_v_min": ("交流电压下限", "pu"),
    "dc_v_max": ("直流电压上限", "pu"),
    "dc_v_min": ("直流电压下限", "pu"),
    "i_p_max": ("I 侧功率上限", "kW"),
    "i_p_min": ("I 侧功率下限", "kW"),
    "i_i_max": ("I 侧电流上限", "A"),
    "j_p_max": ("J 侧功率上限", "kW"),
    "j_p_min": ("J 侧功率下限", "kW"),
    "j_i_max": ("J 侧电流上限", "A"),
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


class _PortalHistorySchemaCatalog:
    """为 scada_portal_history.db 提供 RTDB 嵌入结构中文表名与字段说明。"""

    def names(self) -> list[str]:
        return ["device_daily_samples", "alarm_events"]

    def get(self, table_name: str) -> dict[str, Any]:
        if table_name == "device_daily_samples":
            return {
                "head": {"chn_name": "设备日曲线采样"},
                "columns": [
                    {"eng_name": "ts_ms", "chn_name": "采样时间(ms)"},
                    {"eng_name": "table_name", "chn_name": "设备表名"},
                    {"eng_name": "idx", "chn_name": "设备索引"},
                    {"eng_name": "metric", "chn_name": "量测属性"},
                    {"eng_name": "value", "chn_name": "量测值"},
                ],
            }
        if table_name == "alarm_events":
            return {
                "head": {"chn_name": "告警事件记录"},
                "columns": [
                    {"eng_name": "alarm_key", "chn_name": "告警主键"},
                    {"eng_name": "alarm_type", "chn_name": "告警类型"},
                    {"eng_name": "severity", "chn_name": "严重级别"},
                    {"eng_name": "title", "chn_name": "告警标题"},
                    {"eng_name": "dev_id", "chn_name": "设备标识"},
                    {"eng_name": "table_name", "chn_name": "设备表名"},
                    {"eng_name": "idx", "chn_name": "设备索引"},
                    {"eng_name": "device_name", "chn_name": "设备名称"},
                    {"eng_name": "category", "chn_name": "设备分类"},
                    {"eng_name": "attr", "chn_name": "属性名"},
                    {"eng_name": "attr_label", "chn_name": "属性标签"},
                    {"eng_name": "unit", "chn_name": "单位"},
                    {"eng_name": "value_text", "chn_name": "触发值"},
                    {"eng_name": "detail", "chn_name": "告警详情"},
                    {"eng_name": "rtu_id", "chn_name": "RTU编号"},
                    {"eng_name": "point_no", "chn_name": "点号"},
                    {"eng_name": "fingerprint", "chn_name": "状态指纹"},
                    {"eng_name": "state", "chn_name": "告警状态"},
                    {"eng_name": "first_seen_ms", "chn_name": "首次触发(ms)"},
                    {"eng_name": "last_seen_ms", "chn_name": "最近更新(ms)"},
                    {"eng_name": "recovered_ms", "chn_name": "恢复时间(ms)"},
                    {"eng_name": "acked", "chn_name": "已确认"},
                    {"eng_name": "acked_at", "chn_name": "确认时间"},
                ],
            }
        return {}


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
            c_candidates = (
                fes_sqldb_dir / "scdfe_c.db",
                sqldb_dir / "scdfe_c.db",
                fes_sqldb_dir / "scdfe_c.sqlite3",
                sqldb_dir / "scdfe_c.sqlite3",
            )
            self.c_db = next((p for p in c_candidates if p.is_file()), c_candidates[0])
        self.d_db = Path(d_db).resolve() if d_db else (sqldb_dir / "scdfe_d.db")
        if model_db:
            self.model_db = Path(model_db).resolve()
        else:
            m_db = model_sqldb_dir / "devices.db"
            m_legacy = model_sqldb_dir / "devices.sqlite3"
            self.model_db = m_db if (m_db.is_file() or not m_legacy.is_file()) else m_legacy
        self.svg_path = self._resolve_svg(svg_path, scene_dir / "model", scene_name)
        self.log_dir = Path(log_dir).resolve() if log_dir else (self.c_db.parent / "logs")
        self.d_log_dir = self.d_db.parent / "logs"
        self.history_db = (
            Path(history_db).resolve()
            if history_db
            else (sqldb_dir / "scada_portal_history.db")
        )
        self.sample_interval_sec = max(1.0, float(sample_interval_sec))
        self._stop_event = threading.Event()
        self._sampler_thread: threading.Thread | None = None
        self._history_lock = threading.Lock()
        self._energy_accum_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._alarm_lock = threading.Lock()
        self._acked_alarms: dict[str, dict[str, Any]] = {}
        self._alarm_sync_sig: str = ""
        self._audit_lock = threading.Lock()
        self._audit_loggers: dict[str, ScdfeLogger] = {}
        self._cockpit_cache: dict[float, tuple[float, dict[str, Any]]] = {}
        self._last_sample_ts: float | None = None
        # 运行期实时累计（用于微电网总览能量微增量）
        self._session_kwh: dict[str, float] = defaultdict(float)
        self._svg_topo_cache: tuple[tuple[int, int], dict[str, Any]] | None = None
        self._model_params_cache: tuple[tuple[int, int], tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]] | None = None
        self._pf_meta_cache: tuple[tuple[int, int, int, int, int, int], str, dict[str, Any]] | None = None
        self._pf_live_cache: tuple[float, tuple[int, int, int, int], dict[str, Any]] | None = None
        self._overview_cache: tuple[float, tuple[int, int, int, int], dict[str, Any]] | None = None
        # 线程局部 SQLite 只读连接复用池与 SSE 实时事件总线
        self._ro_pool = ReadOnlyConnPool()
        self.event_bus = PortalEventBus()
        # 高频只读短 TTL 快照缓存，消除同周期多次重复全库扫表
        self._d_schema_cache: tuple[int, list[tuple[str, list[str], dict[str, str], list[str]]]] | None = None
        self._devices_cache: tuple[float, tuple[int, int, int, int], list[dict[str, Any]]] | None = None
        self._comm_cache: tuple[float, tuple[int, int, int, int], dict[str, Any]] | None = None
        self._log_files_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._maint_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._history_stats_cache: tuple[float, dict[str, Any]] | None = None

        self._init_history_db()
        if enable_sampler:
            self.start_sampler()

    @staticmethod
    def _db_file_sig(db_path: Path) -> tuple[int, int, int, int]:
        """获取 SQLite 主文件与 WAL 文件的 (mtime_ns, size) 指纹，用于缓存一致性校验。"""
        try:
            st = db_path.stat()
            main_sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            main_sig = (0, 0)
        wal_path = Path(str(db_path) + "-wal")
        try:
            wst = wal_path.stat()
            wal_sig = (wst.st_mtime_ns, wst.st_size)
        except OSError:
            wal_sig = (0, 0)
        return (main_sig[0], main_sig[1], wal_sig[0], wal_sig[1])

    def _invalidate_devices_cache(self) -> None:
        with self._cache_lock:
            self._devices_cache = None
            self._pf_live_cache = None
            self._overview_cache = None
            self._pf_live_cache = None
            self._overview_cache = None

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
        with self._ro_pool.acquire(db_path, timeout=10.0) as conn:
            yield conn

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
        self._invalidate_devices_cache()
        self._audit_portal_log(
            "scdfe_d-web",
            "INFO",
            f"门户修改量测上下限 设备={table_name}#{idx} 属性={metric} 下限={min_num} 上限={max_num} 状态={new_qual}",
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
    def _open_history(self, *, readonly: bool = False):
        if readonly:
            with self._ro_pool.acquire(self.history_db, timeout=15.0) as conn:
                yield conn
            return
        conn = sqlite3.connect(self.history_db, timeout=15.0)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alarm_events (
                    alarm_key TEXT PRIMARY KEY,
                    alarm_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    dev_id TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    device_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    attr TEXT NOT NULL,
                    attr_label TEXT NOT NULL,
                    unit TEXT NOT NULL DEFAULT '',
                    value_text TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    rtu_id INTEGER,
                    point_no INTEGER,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'active',
                    first_seen_ms INTEGER NOT NULL,
                    last_seen_ms INTEGER NOT NULL,
                    recovered_ms INTEGER,
                    acked INTEGER NOT NULL DEFAULT 0,
                    acked_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alarm_events_state_time ON alarm_events(state, last_seen_ms)"
            )
            # 平滑初始化 1 分钟级降采样表、检修挂牌表、控调审计表与告警操作人字段
            ensure_extended_history_schema(conn)
            # 同步 RTDB 原生内置结构定义，确保 RTDB 打开 sqldb/scada 目录时零告警
            conn.execute("BEGIN IMMEDIATE")
            sync_embedded_schema(
                conn,
                catalog=_PortalHistorySchemaCatalog(),
                include_tables=("device_daily_samples", "alarm_events"),
            )
            conn.commit()
            # 启动时恢复持久化的活动告警确认记录，重启不丢失
            rows = conn.execute(
                "SELECT alarm_key, fingerprint, acked_at, ack_operator, ack_note FROM alarm_events WHERE state='active' AND acked=1"
            ).fetchall()
            with self._alarm_lock:
                for key, fp, acked_at, ack_op, ack_note in rows:
                    self._acked_alarms[str(key)] = {
                        "fp": str(fp or ""),
                        "acked_at": acked_at,
                        "ack_operator": ack_op or "",
                        "ack_note": ack_note or "",
                    }

    def start_sampler(self) -> None:
        if self._sampler_thread is not None and self._sampler_thread.is_alive():
            return
        self._stop_event.clear()
        # 启动时先同步采样一次，保证首次访问立即可见曲线点
        try:
            self.sample_once()
        except Exception:
            LOG.warning("SCADA Portal initial sample_once failed", exc_info=True)
        self._sampler_thread = threading.Thread(
            target=self._sampler_loop, name="scada-portal-sampler", daemon=True
        )
        self._sampler_thread.start()

    def close(self) -> None:
        self._stop_event.set()
        if self._sampler_thread is not None and self._sampler_thread.is_alive():
            self._sampler_thread.join(timeout=3.0)
        self._ro_pool.close_all()

    def _sampler_loop(self) -> None:
        prune_counter = 0
        while not self._stop_event.wait(self.sample_interval_sec):
            prune_counter += 1
            try:
                self.sample_once(prune=(prune_counter % 60 == 0))
            except Exception:
                LOG.warning("SCADA Portal background sample_once failed", exc_info=True)
                continue

    def sample_once(self, *, prune: bool = False) -> int:
        """从 scdfe_d.db 采集一帧全量有效数值量测并写入 24h 日曲线库。"""
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

        with self._history_lock, self._open_history() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO device_daily_samples(ts_ms, table_name, idx, metric, value) "
                "VALUES (?, ?, ?, ?, ?)",
                rows_to_insert,
            )
            upsert_minute_rollup(conn, rows_to_insert)
            if prune:
                prune_history_samples(conn, raw_retention_days=14, rollup_retention_days=365, batch_limit=20000)
            conn.commit()
        self.event_bus.publish("tick", {"samples": len(rows_to_insert)})
        return len(rows_to_insert)

    # -------------------- 设备全量快照读取（带 schema 缓存与短 TTL 快照缓存） --------------------
    def _read_all_devices(self, *, use_cache: bool = True) -> list[dict[str, Any]]:
        if not self.d_db.is_file():
            return []
        now_mono = time.monotonic()
        sig = self._db_file_sig(self.d_db)
        if use_cache:
            with self._cache_lock:
                cached = self._devices_cache
                if cached is not None and (now_mono - cached[0]) < 0.8 and cached[1] == sig:
                    return cached[2]

        devices: list[dict[str, Any]] = []
        with self._open_readonly(self.d_db) as conn:
            conn.execute("BEGIN")
            schema_ver = int(conn.execute("PRAGMA schema_version").fetchone()[0])
            table_specs = None
            with self._cache_lock:
                if self._d_schema_cache is not None and self._d_schema_cache[0] == schema_ver:
                    table_specs = self._d_schema_cache[1]
            if table_specs is None:
                table_specs = []
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
                    metrics = [
                        cn[:-4]
                        for cn in col_names
                        if cn.endswith("_rtu") and not cn.endswith("_cmd_rtu")
                    ]
                    controls = [
                        cn[:-8]
                        for cn in col_names
                        if cn.endswith("_cmd_rtu")
                    ]
                    if not metrics and not controls:
                        continue
                    select_cols = ["idx", "name"]
                    for m in metrics:
                        for suf in SUFFIXES:
                            cname = f"{m}{suf}"
                            if cname in col_types:
                                select_cols.append(cname)
                    for c in controls:
                        for suf in CTRL_SUFFIXES:
                            cname = f"{c}{suf}"
                            if cname in col_types:
                                select_cols.append(cname)
                    table_specs.append((table, metrics, controls, col_types, select_cols))
                with self._cache_lock:
                    self._d_schema_cache = (schema_ver, table_specs)

            for table, metrics, controls, col_types, select_cols in table_specs:
                quoted = ", ".join(f'"{c}"' for c in select_cols)
                rows = conn.execute(f'SELECT {quoted} FROM "{table}" ORDER BY idx').fetchall()
                for row in rows:
                    row_dict = dict(row)
                    idx = int(row_dict["idx"])
                    dev_name = str(row_dict["name"] or f"{table}-{idx}")
                    m_list: list[dict[str, Any]] = []
                    c_list: list[dict[str, Any]] = []
                    bound_cnt = 0
                    valid_cnt = 0
                    alarm_cnt = 0
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
                        if (not notuse) and qual in (2, QUAL_OVER_MAX, QUAL_UNDER_MIN):
                            alarm_cnt += 1
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
                    for c in controls:
                        crtu = row_dict.get(f"{c}_cmd_rtu")
                        cpnt = row_dict.get(f"{c}_cmd_pnt")
                        if not (
                            isinstance(crtu, int)
                            and not isinstance(crtu, bool)
                            and crtu > 0
                            and isinstance(cpnt, int)
                            and not isinstance(cpnt, bool)
                            and cpnt > 0
                        ):
                            continue
                        ckind = "yk" if col_types.get(c) == "INTEGER" else "yt"
                        cval = row_dict.get(c)
                        ctime = row_dict.get(f"{c}_cmd_time")
                        cstatus = row_dict.get(f"{c}_cmd_status")
                        cstatus_i = int(cstatus) if cstatus is not None else CTRL_STATUS_IDLE
                        cack = row_dict.get(f"{c}_cmd_ack_time")
                        cnotuse = bool(row_dict.get(f"{c}_cmd_notuse"))
                        cmax = row_dict.get(f"{c}_cmd_max")
                        cmin = row_dict.get(f"{c}_cmd_min")
                        if cstatus_i == CTRL_STATUS_FAILED:
                            alarm_cnt += 1
                        clabel, cunit = CONTROL_META.get(c, (c, ""))
                        c_list.append({
                            "control": c,
                            "label": clabel,
                            "unit": cunit,
                            "kind": ckind,
                            "value": cval,
                            "cmd_time": ctime,
                            "cmd_status": cstatus_i,
                            "status_label": CTRL_STATUS_LABELS.get(cstatus_i, str(cstatus_i)),
                            "cmd_ack_time": cack,
                            "disabled": cnotuse,
                            "rtu_id": crtu,
                            "point_no": cpnt,
                            "max_value": cmax,
                            "min_value": cmin,
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
                        "control_count": len(c_list),
                        "alarm_count": alarm_cnt,
                        "run": run_val,
                        "position": pos_val,
                        "metrics": m_list,
                        "controls": c_list,
                    })
        with self._cache_lock:
            self._devices_cache = (time.monotonic(), sig, devices)
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
        now_mono = time.monotonic()
        sig = self._db_file_sig(self.d_db) if self.d_db.is_file() else (0, 0, 0, 0)
        with self._cache_lock:
            cached_ov = self._overview_cache
            if cached_ov is not None and (now_mono - cached_ov[0]) < 1.2 and cached_ov[1] == sig:
                return cached_ov[2]
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

        result = {
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
                "collector_running": self.is_collector_running(),
                **self._summarize_alarm_counts(devices),
            },
            "data_quality": summarize_data_quality(devices),
            "event_version": self.event_bus.version,
        }
        with self._cache_lock:
            self._overview_cache = (time.monotonic(), sig, result)
        return result

    def _count_comm_points(self) -> dict[str, int]:
        """复用通信快照统计 scdfe_c 四遥点表（YC/YX/YT/YK）配置总数。"""
        return dict(self._read_comm_snapshot()["point_totals"])

    def get_energy_flow(self) -> dict[str, Any]:
        """聚合交直流微电网与氢能全景能量流实时快照。"""
        devices = self._read_all_devices()
        comm_snap = self._read_comm_snapshot()
        comm_rtus = comm_snap["rtus"]
        comm_channels = comm_snap["channels"]
        point_counts = comm_snap["point_totals"]
        model_params, _ = self._read_model_params()

        # 统计设备量测质量分布与最新源时标
        total_bound = 0
        total_valid = 0
        over_limit_cnt = 0
        under_limit_cnt = 0
        comm_err_cnt = 0
        latest_src_ms: float | None = None

        for dev in devices:
            total_bound += int(dev.get("bound_count") or 0)
            total_valid += int(dev.get("valid_count") or 0)
            for m in dev.get("metrics") or ():
                q = m.get("quality")
                if q == QUAL_OVER_MAX:
                    over_limit_cnt += 1
                elif q == QUAL_UNDER_MIN:
                    under_limit_cnt += 1
                elif q == 2:
                    comm_err_cnt += 1
                st = _to_float(m.get("source_time"))
                if st is not None and st > 0:
                    st_ms = st if st > 1e11 else st * 1000.0
                    if latest_src_ms is None or st_ms > latest_src_ms:
                        latest_src_ms = st_ms

        # 判断哪些风机属于经风机变流器汇入直流 750V 侧的直流风电
        wind_conv_count = sum(
            1 for d in devices if d["table_name"] == "DCACConverter" and "风机" in str(d["name"])
        )

        dc_wind_p = 0.0
        dc_wind_cnt = 0
        dc_wind_on = 0
        ac_wind_p = 0.0
        ac_wind_cnt = 0
        ac_wind_on = 0

        dc_pv_p = 0.0
        dc_pv_cnt = 0
        dc_pv_on = 0
        dc_pv_u: list[float] = []
        ac_pv_p = 0.0
        ac_pv_cnt = 0
        ac_pv_on = 0

        dc_es_p = 0.0
        dc_es_cnt = 0
        dc_es_on = 0
        dc_es_socs: list[float] = []
        ac_es_p = 0.0
        ac_es_cnt = 0
        ac_es_on = 0
        ac_es_socs: list[float] = []

        diesel_p = 0.0
        diesel_cnt = 0
        diesel_on = 0
        diesel_rated = 0.0

        dc_load_p = 0.0
        dc_load_u: list[float] = []
        ac_load_p = 0.0
        ac_load_u: list[float] = []

        elec_p = 0.0
        elec_flow: float | None = None
        elec_pressure: float | None = None
        fc_p = 0.0
        fc_flow: float | None = None
        fc_pressure: float | None = None
        h2_socs: list[float] = []
        h2_pressure: float | None = None
        h2_flow: float | None = None

        dcac_dc_p = 0.0
        dcac_ac_p = 0.0
        dcac_cnt = 0
        dcac_on = 0
        dcac_rated = 0.0
        dcac_dc_u: list[float] = []
        dcac_ac_u: list[float] = []

        wind_speeds: list[float] = []
        irradiances: list[float] = []
        temperatures: list[float] = []

        def _rated_kw(dev_id: str) -> float:
            for item in model_params.get(dev_id, ()):
                if item.get("key") == "rated_capacity":
                    raw = str(item.get("value") or "").split()[0]
                    val = _to_float(raw)
                    if val is not None and val > 0:
                        return val
            return 0.0

        for dev in devices:
            t = dev["table_name"]
            name = str(dev["name"] or "")
            m_map = {m["metric"]: m for m in dev["metrics"]}
            p_val = _to_float(m_map["p"]["value"]) if "p" in m_map else None
            is_on = (dev.get("run") == 1) or (p_val is not None and abs(p_val) > 0.1)

            if t == "ACGenerator":
                if "风电" in name or "风力" in name:
                    # 经风机变流器并入直流母线的风机（如“交流风电-1..10”）归入直流侧风电
                    if wind_conv_count > 0 and "风力发电机" not in name and "风电" in name:
                        dc_wind_cnt += 1
                        if is_on:
                            dc_wind_on += 1
                        if p_val is not None:
                            dc_wind_p += p_val
                    else:
                        ac_wind_cnt += 1
                        if is_on:
                            ac_wind_on += 1
                        if p_val is not None:
                            ac_wind_p += p_val
                elif "光伏" in name:
                    ac_pv_cnt += 1
                    if is_on:
                        ac_pv_on += 1
                    if p_val is not None:
                        ac_pv_p += p_val
                elif "储能" in name:
                    ac_es_cnt += 1
                    if is_on:
                        ac_es_on += 1
                    if p_val is not None:
                        ac_es_p += p_val
                elif "柴油" in name:
                    diesel_cnt += 1
                    if p_val is not None and p_val > 0.5:
                        diesel_on += 1
                    if p_val is not None:
                        diesel_p += p_val
                    diesel_rated += _rated_kw(dev["dev_id"])
            elif t == "DCGenerator":
                if "光伏" in name:
                    dc_pv_cnt += 1
                    if is_on:
                        dc_pv_on += 1
                    if p_val is not None:
                        dc_pv_p += p_val
                    u_v = _to_float(m_map["u"]["value"]) if "u" in m_map else None
                    if u_v is not None and u_v > 0:
                        dc_pv_u.append(u_v)
                elif "储能" in name:
                    dc_es_cnt += 1
                    if is_on:
                        dc_es_on += 1
                    if p_val is not None:
                        dc_es_p += p_val
                elif "燃料电池" in name or "氢" in name:
                    if p_val is not None:
                        fc_p += p_val
            elif t == "DCLoad":
                if p_val is not None:
                    dc_load_p += p_val
                u_v = _to_float(m_map["u"]["value"]) if "u" in m_map else None
                if u_v is not None and u_v > 0:
                    dc_load_u.append(u_v)
            elif t == "ACLoad":
                if "制氢" in name:
                    if p_val is not None:
                        elec_p += p_val
                else:
                    if p_val is not None:
                        ac_load_p += p_val
                    u_v = _to_float(m_map["uab"]["value"]) if "uab" in m_map else None
                    if u_v is not None and u_v > 0:
                        ac_load_u.append(u_v)
            elif t == "DCACConverter":
                if "风机" not in name:
                    dcac_cnt += 1
                    dp = _to_float(m_map["dc_p"]["value"]) if "dc_p" in m_map else None
                    ap = _to_float(m_map["ac_p"]["value"]) if "ac_p" in m_map else None
                    if (dev.get("run") == 1) or (dp is not None and abs(dp) > 0.1):
                        dcac_on += 1
                    if dp is not None:
                        dcac_dc_p += dp
                    if ap is not None:
                        dcac_ac_p += ap
                    du = _to_float(m_map["dc_u"]["value"]) if "dc_u" in m_map else None
                    au = _to_float(m_map["ac_uab"]["value"]) if "ac_uab" in m_map else None
                    if du is not None and du > 0:
                        dcac_dc_u.append(du)
                    if au is not None and au > 0:
                        dcac_ac_u.append(au)
                    dcac_rated += _rated_kw(dev["dev_id"])
            elif t == "DCStorageGen" and "soc" in m_map:
                sv = _to_float(m_map["soc"]["display_value"])
                if sv is not None:
                    dc_es_socs.append(sv)
            elif t == "ACStorageGen" and "soc" in m_map:
                sv = _to_float(m_map["soc"]["display_value"])
                if sv is not None:
                    ac_es_socs.append(sv)
            elif t == "HydroSource":
                if "flow" in m_map:
                    fv = _to_float(m_map["flow"]["value"])
                    if fv is not None:
                        elec_flow = (elec_flow or 0.0) + fv
                if "pressure" in m_map:
                    pv = _to_float(m_map["pressure"]["value"])
                    if pv is not None:
                        elec_pressure = pv if elec_pressure is None else (elec_pressure + pv) / 2.0
            elif t == "HydroLoad":
                if "flow" in m_map:
                    fv = _to_float(m_map["flow"]["value"])
                    if fv is not None:
                        fc_flow = (fc_flow or 0.0) + fv
                if "pressure" in m_map:
                    pv = _to_float(m_map["pressure"]["value"])
                    if pv is not None:
                        fc_pressure = pv if fc_pressure is None else (fc_pressure + pv) / 2.0
            elif t == "HydroStorage":
                if "soc" in m_map:
                    sv = _to_float(m_map["soc"]["display_value"])
                    if sv is not None:
                        h2_socs.append(sv)
                if "pressure" in m_map:
                    pv = _to_float(m_map["pressure"]["value"])
                    if pv is not None:
                        h2_pressure = pv if h2_pressure is None else (h2_pressure + pv) / 2.0
                if "flow" in m_map:
                    fv = _to_float(m_map["flow"]["value"])
                    if fv is not None:
                        h2_flow = (h2_flow or 0.0) + fv
            elif t == "ACWindGen" and "wind_speed" in m_map:
                ws = _to_float(m_map["wind_speed"]["value"])
                if ws is not None:
                    wind_speeds.append(ws)
            elif t in {"ACPVGen", "DCPVGen"} and "irradiance" in m_map:
                irr = _to_float(m_map["irradiance"]["value"])
                if irr is not None:
                    irradiances.append(irr)

            # 仅采信气象/光伏/风电扩展表的环境温度，避免将变流器柜温误作环境气温
            if t in {"ACWindGen", "ACPVGen", "DCPVGen"} and "temperature" in m_map:
                tv = _to_float(m_map["temperature"]["value"])
                if tv is not None:
                    temperatures.append(tv)

        total_ac_load_kw = ac_load_p + elec_p
        total_load_kw = dc_load_p + total_ac_load_kw
        renewable_kw = max(0.0, dc_wind_p) + max(0.0, dc_pv_p) + max(0.0, ac_wind_p) + max(0.0, ac_pv_p) + max(0.0, fc_p)
        storage_kw = dc_es_p + ac_es_p
        total_gen_kw = renewable_kw + max(0.0, dc_es_p) + max(0.0, ac_es_p) + max(0.0, diesel_p)
        green_ratio_pct = (renewable_kw / total_gen_kw * 100.0) if total_gen_kw > 1e-6 else 0.0

        dc_bus_gen_kw = max(0.0, dc_wind_p) + max(0.0, dc_pv_p) + max(0.0, dc_es_p) + max(0.0, fc_p)
        dc_bus_load_kw = dc_load_p + max(0.0, -dc_es_p)
        ac_bus_gen_kw = max(0.0, ac_wind_p) + max(0.0, ac_pv_p) + max(0.0, ac_es_p) + max(0.0, diesel_p)
        ac_bus_load_kw = total_ac_load_kw + max(0.0, -ac_es_p)

        dc_voltage = (
            round(sum(dc_load_u) / len(dc_load_u), 1) if dc_load_u
            else (round(sum(dcac_dc_u) / len(dcac_dc_u), 1) if dcac_dc_u else 750.0)
        )
        ac_voltage = (
            round(sum(ac_load_u) / len(ac_load_u), 1) if ac_load_u
            else (round(sum(dcac_ac_u) / len(dcac_ac_u), 1) if dcac_ac_u else 380.0)
        )

        # DCAC 变流器方向：dc_p > 0 表示从直流母线流出并送往交流母线（DC -> AC）
        dcac_flow_kw = dcac_dc_p if abs(dcac_dc_p) >= 0.05 else -dcac_ac_p
        dcac_dir = "dc_to_ac" if dcac_flow_kw > 0.2 else ("ac_to_dc" if dcac_flow_kw < -0.2 else "idle")

        def _es_state(p_kw: float) -> str:
            if p_kw > 0.2:
                return "discharge"
            if p_kw < -0.2:
                return "charge"
            return "idle"

        online_rtus = sum(1 for r in comm_rtus if r.get("connection_state") == "connected")
        now_utc = datetime.now(timezone.utc)
        now_ms = now_utc.timestamp() * 1000.0
        age_sec = round(max(0.0, (now_ms - latest_src_ms) / 1000.0), 1) if latest_src_ms is not None else None

        return {
            "timestamp": now_utc.isoformat(timespec="seconds"),
            "latest_sample_time": _iso_from_ms(latest_src_ms),
            "sample_age_sec": age_sec,
            "summary": {
                "total_load_kw": round(total_load_kw, 1),
                "renewable_kw": round(renewable_kw, 1),
                "green_ratio_pct": round(green_ratio_pct, 1),
                "storage_kw": round(storage_kw, 1),
                "diesel_kw": round(diesel_p, 1),
                "total_gen_kw": round(total_gen_kw, 1),
                "valid_measurements": total_valid,
                "bound_measurements": total_bound,
                "over_limit_count": over_limit_cnt,
                "under_limit_count": under_limit_cnt,
                "comm_error_count": comm_err_cnt,
                "rtu_online": online_rtus,
                "rtu_total": len(comm_rtus),
                "collector_running": self.is_collector_running(),
                **self._summarize_alarm_counts(devices),
            },
            "dc_bus": {
                "voltage_v": dc_voltage,
                "gen_kw": round(dc_bus_gen_kw, 1),
                "load_kw": round(dc_bus_load_kw, 1),
            },
            "ac_bus": {
                "voltage_v": ac_voltage,
                "gen_kw": round(ac_bus_gen_kw, 1),
                "load_kw": round(ac_bus_load_kw, 1),
            },
            "nodes": {
                "dc_wind": {
                    "count": dc_wind_cnt,
                    "online": dc_wind_on,
                    "p_kw": round(dc_wind_p, 1),
                },
                "dc_pv": {
                    "count": dc_pv_cnt,
                    "online": dc_pv_on,
                    "p_kw": round(dc_pv_p, 1),
                    "voltage_v": round(sum(dc_pv_u) / len(dc_pv_u), 1) if dc_pv_u else None,
                },
                "dc_storage": {
                    "count": dc_es_cnt,
                    "online": dc_es_on,
                    "p_kw": round(dc_es_p, 1),
                    "soc_pct": round(sum(dc_es_socs) / len(dc_es_socs), 1) if dc_es_socs else None,
                    "state": _es_state(dc_es_p),
                },
                "dc_load": {
                    "p_kw": round(dc_load_p, 1),
                    "voltage_v": dc_voltage,
                    "share_pct": round(dc_load_p / total_load_kw * 100.0, 1) if total_load_kw > 1e-6 else 0.0,
                },
                "ac_wind": {
                    "count": ac_wind_cnt,
                    "online": ac_wind_on,
                    "p_kw": round(ac_wind_p, 1),
                },
                "ac_pv": {
                    "count": ac_pv_cnt,
                    "online": ac_pv_on,
                    "p_kw": round(ac_pv_p, 1),
                },
                "ac_storage": {
                    "count": ac_es_cnt,
                    "online": ac_es_on,
                    "p_kw": round(ac_es_p, 1),
                    "soc_pct": round(sum(ac_es_socs) / len(ac_es_socs), 1) if ac_es_socs else None,
                    "state": _es_state(ac_es_p),
                },
                "diesel": {
                    "count": diesel_cnt,
                    "online": diesel_on,
                    "p_kw": round(diesel_p, 1),
                    "rated_kw": round(diesel_rated, 1) if diesel_rated > 0 else None,
                },
                "ac_load": {
                    "p_kw": round(total_ac_load_kw, 1),
                    "regular_kw": round(ac_load_p, 1),
                    "voltage_v": ac_voltage,
                    "share_pct": round(total_ac_load_kw / total_load_kw * 100.0, 1) if total_load_kw > 1e-6 else 0.0,
                },
                "dcac": {
                    "count": dcac_cnt,
                    "online": dcac_on,
                    "p_kw": round(abs(dcac_flow_kw), 1),
                    "signed_dc_p_kw": round(dcac_flow_kw, 1),
                    "direction": dcac_dir,
                    "rated_kw": round(dcac_rated, 1) if dcac_rated > 0 else None,
                },
                "electrolyzer": {
                    "p_kw": round(elec_p, 1),
                    "flow_nm3h": round(elec_flow, 2) if elec_flow is not None else None,
                    "pressure_mpa": round(elec_pressure, 2) if elec_pressure is not None else None,
                },
                "hydro_storage": {
                    "soc_pct": round(sum(h2_socs) / len(h2_socs), 1) if h2_socs else None,
                    "pressure_mpa": round(h2_pressure, 2) if h2_pressure is not None else None,
                    "flow_nm3h": round(h2_flow, 2) if h2_flow is not None else None,
                    "state": (
                        "charging" if (h2_flow is not None and h2_flow < -0.05)
                        else ("discharging" if (h2_flow is not None and h2_flow > 0.05) else "idle")
                    ),
                },
                "fuel_cell": {
                    "p_kw": round(fc_p, 1),
                    "flow_nm3h": round(fc_flow, 2) if fc_flow is not None else None,
                    "pressure_mpa": round(fc_pressure, 2) if fc_pressure is not None else None,
                },
            },
            "environment": {
                "wind_speed_ms": round(sum(wind_speeds) / len(wind_speeds), 2) if wind_speeds else None,
                "irradiance_wm2": round(sum(irradiances) / len(irradiances), 1) if irradiances else None,
                "temperature_c": round(sum(temperatures) / len(temperatures), 1) if temperatures else None,
                "dc_voltage_v": dc_voltage,
                "ac_voltage_v": ac_voltage,
            },
            "sources": {
                "scene": f"{self.runtime_name}/{self.scene_name}",
                "model_db": self.model_db.name if self.model_db.is_file() else "—",
                "d_db": self.d_db.name if self.d_db.is_file() else "—",
                "c_db": self.c_db.name if self.c_db.is_file() else "—",
                "device_count": len(devices),
                "channel_count": len(comm_channels),
                "rtu_online": online_rtus,
                "rtu_total": len(comm_rtus),
                "points": point_counts,
            },
        }

    # -------------------- 2. SVG 潮流图底图与实时映射 --------------------
    def get_svg_bytes(self) -> bytes:
        if not self.svg_path.is_file():
            raise FileNotFoundError(f"未找到潮流图 SVG 文件：{self.svg_path}")
        return self.svg_path.read_bytes()

    def _read_model_params(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
        """读取 model/devices.db 全部设备静态参数（主表 + 扩展表），按 mtime 缓存。"""
        if not self.model_db.is_file():
            return {}, {}
        mtime = self.model_db.stat().st_mtime_ns
        if self._model_params_cache is not None and self._model_params_cache[0] == mtime:
            return self._model_params_cache[1]

        params_by_dev: dict[str, list[dict[str, Any]]] = defaultdict(list)
        base_params_by_dev: dict[str, list[dict[str, Any]]] = defaultdict(list)
        ext_tabs_by_dev: dict[str, list[dict[str, Any]]] = defaultdict(list)
        coupling_refs: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
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

        def _extract_fields(
            fields: list[tuple[str, str]],
            row: tuple[Any, ...],
            seen: set[str],
            *,
            skip_extra: set[str] | None = None,
        ) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for (eng, chn), raw in zip(fields, row):
                if eng in _MODEL_SKIP_FIELDS or (skip_extra and eng in skip_extra) or eng in seen:
                    continue
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    continue
                label, unit = MODEL_PARAM_META.get(eng, (chn or eng, ""))
                val_str = _fmt_param_val(eng, raw, unit)
                if val_str is None:
                    continue
                seen.add(eng)
                out.append({
                    "key": eng,
                    "label": label,
                    "value": val_str,
                })
            return out

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
                    link_defs = _COUPLING_LINKS.get(tbl, ())
                    link_positions = [
                        (eng_list.index(fk_col), child_tbl, tab_key, tab_label)
                        for fk_col, child_tbl, tab_key, tab_label in link_defs
                        if fk_col in eng_list
                    ]
                    link_fk_set = {fk_col for fk_col, _, _, _ in link_defs}
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
                        extracted = _extract_fields(fields, tuple(row), seen_keys[dev_id], skip_extra=link_fk_set)
                        base_params_by_dev[dev_id].extend(extracted)
                        params_by_dev[dev_id].extend(extracted)
                        for pos, child_tbl, tab_key, tab_label in link_positions:
                            try:
                                c_idx = int(row[pos])
                            except (TypeError, ValueError):
                                continue
                            if c_idx > 0:
                                coupling_refs[dev_id].append((f"{child_tbl}-{c_idx}", tab_key, tab_label))

                # 再按外键合并扩展表专项参数（风机/光伏/储能/柴发），同时保留扩展表独立分页
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
                    ext_label = _EXT_TAB_LABELS.get(ext_tbl, ext_tbl)
                    for row in conn.execute(f'SELECT {sel} FROM "rtdb_data_{ext_tbl}"').fetchall():
                        try:
                            parent_idx = int(row[fk_pos])
                        except (TypeError, ValueError):
                            continue
                        parent_dev_id = f"{parent_tbl}-{parent_idx}"
                        ext_items = _extract_fields(fields, tuple(row), seen_keys[parent_dev_id])
                        if ext_items:
                            params_by_dev[parent_dev_id].extend(ext_items)
                            ext_tabs_by_dev[parent_dev_id].append({
                                "key": f"ext_{ext_tbl.lower()}",
                                "label": ext_label,
                                "params": ext_items,
                            })
        except sqlite3.Error:
            return {}, {}

        # 为耦合设备与含扩展表的复合设备构建清晰的参数分页（param_tabs）
        for dev_id, meta in meta_by_dev.items():
            tbl = meta["table_name"]
            base_items = base_params_by_dev.get(dev_id, [])
            ext_tabs = ext_tabs_by_dev.get(dev_id, [])
            links = coupling_refs.get(dev_id, [])
            tabs: list[dict[str, Any]] = []

            if links:
                # 电-氢多能耦合设备：耦合参数 + 两侧关联子设备参数分页
                if base_items:
                    tabs.append({"key": "coupling", "label": "耦合参数", "params": base_items})
                coupled_children: list[dict[str, Any]] = []
                for child_dev_id, tab_key, tab_label in links:
                    child_params = params_by_dev.get(child_dev_id, [])
                    child_meta = meta_by_dev.get(child_dev_id)
                    if child_params:
                        tabs.append({"key": tab_key, "label": tab_label, "params": child_params})
                    coupled_children.append({
                        "dev_id": child_dev_id,
                        "table_name": child_meta["table_name"] if child_meta else child_dev_id.split("-")[0],
                        "idx": child_meta["idx"] if child_meta else 0,
                        "side_label": tab_label,
                    })
                meta["coupled_children"] = coupled_children
            elif tbl == "DCACConverter":
                # 交直流变流器耦合设备：本体参数 | 交流侧 | 直流侧
                core_p: list[dict[str, Any]] = []
                ac_p: list[dict[str, Any]] = []
                dc_p: list[dict[str, Any]] = []
                for item in base_items:
                    k = item["key"].lower()
                    if k.startswith("ac_") or "_ac_" in k:
                        ac_p.append(item)
                    elif k.startswith("dc_") or "_dc_" in k:
                        dc_p.append(item)
                    else:
                        core_p.append(item)
                if core_p:
                    tabs.append({"key": "base", "label": "本体参数", "params": core_p})
                if ac_p:
                    tabs.append({"key": "ac", "label": "交流侧", "params": ac_p})
                if dc_p:
                    tabs.append({"key": "dc", "label": "直流侧", "params": dc_p})
            elif tbl == "DCDCConverter":
                # 直流-直流变流器耦合设备：本体参数 | I 侧 | J 侧
                core_p, i_p, j_p = [], [], []
                for item in base_items:
                    k = item["key"].lower()
                    if k.startswith("i_") or "_i_" in k or k.startswith("high_"):
                        i_p.append(item)
                    elif k.startswith("j_") or "_j_" in k or k.startswith("low_"):
                        j_p.append(item)
                    else:
                        core_p.append(item)
                if i_p or j_p:
                    if core_p:
                        tabs.append({"key": "base", "label": "本体参数", "params": core_p})
                    if i_p:
                        tabs.append({"key": "side_i", "label": "I 侧", "params": i_p})
                    if j_p:
                        tabs.append({"key": "side_j", "label": "J 侧", "params": j_p})
                elif base_items:
                    tabs.append({"key": "params", "label": "设备参数", "params": base_items})
            elif ext_tabs:
                # 主表 + 专项扩展表（如交流柴发 / 风机 / 光伏 / 储能）：电气参数 | 专项参数
                if base_items:
                    tabs.append({"key": "electric", "label": "电气参数", "params": base_items})
                tabs.extend(ext_tabs)
            elif base_items:
                tabs.append({"key": "params", "label": "设备参数", "params": base_items})

            meta["param_tabs"] = tabs

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

            m_meta = model_meta_map.get(dev_id) or {}
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
                "control_count": dev.get("control_count", 0),
                "alarm_count": dev.get("alarm_count", 0),
                "metrics": dev["metrics"],
                "controls": list(dev.get("controls", [])),
                "model_params": model_params_map.get(dev_id, []),
                "model_param_tabs": m_meta.get("param_tabs") or [],
            }

        # 补充仅存在于模型库、尚未在 scdfe_d 绑定量测的设备（如母线、阀门、耦合主体等），确保悬停仍显示模型参数与分页
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
                    "control_count": 0,
                    "alarm_count": 0,
                    "metrics": [],
                    "controls": [],
                    "model_params": model_params_map.get(dev_id, []),
                    "model_param_tabs": meta.get("param_tabs") or [],
                }

        # 为电-氢耦合设备自动聚合关联子设备的就地指令（保留真实 owner_table/owner_idx 供直接下发）
        for dev_id, meta in model_meta_map.items():
            children = meta.get("coupled_children") or ()
            if not children or dev_id not in dev_summaries:
                continue
            parent_summary = dev_summaries[dev_id]
            merged_ctrls = list(parent_summary["controls"])
            seen_ctrl = {(parent_summary["table_name"], parent_summary["idx"], c["control"]) for c in merged_ctrls}
            for child in children:
                c_dev = by_dev_id.get(child["dev_id"])
                if not c_dev:
                    continue
                side = child.get("side_label") or ""
                for c in c_dev.get("controls") or ():
                    ck = (c_dev["table_name"], c_dev["idx"], c["control"])
                    if ck in seen_ctrl:
                        continue
                    seen_ctrl.add(ck)
                    merged_ctrls.append({
                        **c,
                        "owner_table": c_dev["table_name"],
                        "owner_idx": c_dev["idx"],
                        "owner_name": c_dev["name"],
                        "side_label": side,
                    })
            parent_summary["controls"] = merged_ctrls
            parent_summary["control_count"] = len(merged_ctrls)

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

    def get_powerflow_meta(self) -> tuple[str, dict[str, Any]]:
        """返回潮流图静态设备元数据（模型参数、分页定义、基础台账）及强 ETag，支持浏览器 304 缓存。"""
        m_sig = self._db_file_sig(self.model_db) if self.model_db.is_file() else (0, 0, 0, 0)
        d_sig = self._db_file_sig(self.d_db) if self.d_db.is_file() else (0, 0, 0, 0)
        cache_key = (m_sig[0], m_sig[1], d_sig[0], d_sig[1], m_sig[2], m_sig[3])
        with self._cache_lock:
            cached = self._pf_meta_cache
            if cached is not None and cached[0] == cache_key:
                return cached[1], cached[2]

        full = self.get_powerflow()
        devices_meta: dict[str, dict[str, Any]] = {}
        for dev_id, d in full["devices"].items():
            devices_meta[dev_id] = {
                "dev_id": dev_id,
                "table_name": d["table_name"],
                "idx": d["idx"],
                "name": d["name"],
                "category": d["category"],
                "bound_count": d["bound_count"],
                "control_count": d["control_count"],
                "model_params": d.get("model_params") or [],
                "model_param_tabs": d.get("model_param_tabs") or [],
            }
        payload = {
            "timestamp": full["timestamp"],
            "devices": devices_meta,
        }
        digest = hashlib.sha1(
            json.dumps(devices_meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        etag = f'"pf-meta-{digest}"'
        with self._cache_lock:
            self._pf_meta_cache = (cache_key, etag, payload)
        return etag, payload

    def get_powerflow_live(self) -> dict[str, Any]:
        """返回潮流图轻量动态快照（短 TTL 缓存 + 剥离静态模型参数与冗余空值），大幅压缩轮询体积与耗时。"""
        now_mono = time.monotonic()
        sig = self._db_file_sig(self.d_db) if self.d_db.is_file() else (0, 0, 0, 0)
        with self._cache_lock:
            cached = self._pf_live_cache
            if cached is not None and (now_mono - cached[0]) < 1.2 and cached[1] == sig:
                return cached[2]

        full = self.get_powerflow()
        live_devices: dict[str, dict[str, Any]] = {}
        for dev_id, d in full["devices"].items():
            # 仅下发非空实时量测的精简字段（去除重复元信息），有效压缩包体
            slim_metrics = [
                {
                    "metric": m["metric"],
                    "label": m["label"],
                    "kind": m["kind"],
                    "value": m["value"],
                    "display_value": m.get("display_value"),
                    "unit": m.get("unit", ""),
                    "valid": m.get("valid", False),
                }
                for m in (d.get("metrics") or ())
                if m.get("value") is not None
            ]
            item: dict[str, Any] = {
                "run": d.get("run"),
                "position": d.get("position"),
                "valid_count": d.get("valid_count", 0),
                "bound_count": d.get("bound_count", 0),
                "alarm_count": d.get("alarm_count", 0),
            }
            if slim_metrics:
                item["metrics"] = slim_metrics
            if d.get("controls"):
                item["controls"] = d["controls"]
            live_devices[dev_id] = item
        res = {
            "timestamp": full["timestamp"],
            "version": self.event_bus.version,
            "svg_slots": full["svg_slots"],
            "breakers": full["breakers"],
            "flows": full["flows"],
            "devices_live": live_devices,
        }
        with self._cache_lock:
            self._pf_live_cache = (time.monotonic(), sig, res)
        return res

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

    def get_device_trend(
        self,
        table_name: str,
        idx: int,
        *,
        hours: float = 24.0,
        start_ms: int | None = None,
        end_ms: int | None = None,
        metric: str | None = None,
        date_str: str | None = None,
        compare: str | None = None,
    ) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        if date_str and str(date_str).strip():
            raw_date = str(date_str).strip()
            try:
                day_dt = datetime.strptime(raw_date, "%Y-%m-%d").astimezone()
            except ValueError as exc:
                raise ValueError("日期参数格式应为 YYYY-MM-DD") from exc
            since_ms = int(day_dt.timestamp() * 1000)
            until_ms = since_ms + 24 * 3600 * 1000 - 1
            span_hours = 24.0
        elif (start_ms is None) != (end_ms is None):
            raise ValueError("start_ms 与 end_ms 须同时指定")
        elif start_ms is not None and end_ms is not None:
            since_ms = int(start_ms)
            until_ms = int(end_ms)
            if since_ms < 0 or until_ms <= since_ms:
                raise ValueError("曲线时间区间无效，结束时间必须大于开始时间")
            if until_ms - since_ms > 31 * 24 * 3600 * 1000:
                raise ValueError("单次查询时间跨度不能超过 31 天")
            span_hours = round((until_ms - since_ms) / 3600000.0, 4)
        else:
            hours_f = float(hours)
            if not math.isfinite(hours_f) or hours_f <= 0:
                raise ValueError("曲线时间窗口 hours 必须为有限正数")
            span_hours = max(1 / 60.0, min(24.0, hours_f))
            since_ms = now_ms - int(span_hours * 3600 * 1000)
            until_ms = now_ms

        def _query_window(s_ms: int, e_ms: int) -> dict[str, list[list[float]]]:
            out: dict[str, list[list[float]]] = defaultdict(list)
            if not self.history_db.is_file():
                return out
            metric_filter = str(metric).strip() if metric else None
            with self._open_history(readonly=True) as conn:
                if metric_filter:
                    rows = conn.execute(
                        "SELECT metric, ts_ms, value FROM device_daily_samples "
                        "WHERE table_name=? AND idx=? AND metric=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms ASC",
                        (table_name, idx, metric_filter, s_ms, e_ms),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT metric, ts_ms, value FROM device_daily_samples "
                        "WHERE table_name=? AND idx=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms ASC",
                        (table_name, idx, s_ms, e_ms),
                    ).fetchall()
                # 原始明细无数据时自动回退查询 1 分钟级降采样汇总表
                if not rows:
                    try:
                        if metric_filter:
                            rows = conn.execute(
                                "SELECT metric, bucket_ms, avg_val FROM device_minute_rollup "
                                "WHERE table_name=? AND idx=? AND metric=? AND bucket_ms>=? AND bucket_ms<=? ORDER BY bucket_ms ASC",
                                (table_name, idx, metric_filter, s_ms, e_ms),
                            ).fetchall()
                        else:
                            rows = conn.execute(
                                "SELECT metric, bucket_ms, avg_val FROM device_minute_rollup "
                                "WHERE table_name=? AND idx=? AND bucket_ms>=? AND bucket_ms<=? ORDER BY bucket_ms ASC",
                                (table_name, idx, s_ms, e_ms),
                            ).fetchall()
                    except sqlite3.Error:
                        rows = []
            for m_name, ts_ms, val in rows:
                out[m_name].append([int(ts_ms), round(float(val), 4)])
            return out

        series_map = _query_window(since_ms, until_ms)

        # 读取设备当前量测上下限与类型（yc/yx），用于曲线叠加警戒虚线与开关量阶梯绘制
        meta_by_metric: dict[str, dict[str, Any]] = {}
        try:
            for d in self._read_all_devices():
                if d["table_name"] == table_name and int(d["idx"]) == int(idx):
                    for m in d.get("metrics") or ():
                        meta_by_metric[m["metric"]] = {
                            "kind": m.get("kind", "yc"),
                            "min_value": m.get("min_value"),
                            "max_value": m.get("max_value"),
                        }
                    break
        except Exception:
            pass

        max_pts = 720
        series: list[dict[str, Any]] = []
        for m_name, pts in sorted(series_map.items()):
            if len(pts) > max_pts:
                step = (len(pts) - 1) / (max_pts - 1)
                pts = [pts[int(round(i * step))] for i in range(max_pts)]
            label, unit = METRIC_META.get(m_name, (m_name, ""))
            m_info = meta_by_metric.get(m_name) or {}
            series.append({
                "metric": m_name,
                "label": label,
                "unit": unit,
                "kind": m_info.get("kind", "yx" if m_name in {"position", "run_state", "fault", "overload"} else "yc"),
                "min_limit": m_info.get("min_value"),
                "max_limit": m_info.get("max_value"),
                "points": pts,
                "latest": pts[-1][1] if pts else None,
            })

        # 昨日同比叠加（时间轴平移 +24h 对齐至当前窗口）
        compare_mode = str(compare or "").strip().lower()
        if compare_mode == "prev_day":
            shift_ms = 24 * 3600 * 1000
            prev_map = _query_window(since_ms - shift_ms, until_ms - shift_ms)
            for m_name, pts in sorted(prev_map.items()):
                if not pts:
                    continue
                if len(pts) > max_pts:
                    step = (len(pts) - 1) / (max_pts - 1)
                    pts = [pts[int(round(i * step))] for i in range(max_pts)]
                aligned_pts = [[p[0] + shift_ms, p[1]] for p in pts]
                label, unit = METRIC_META.get(m_name, (m_name, ""))
                series.append({
                    "key": f"{m_name}__prev_day",
                    "metric": m_name,
                    "label": f"{label} (昨日)",
                    "unit": unit,
                    "kind": "yc",
                    "dashed": True,
                    "compare": "prev_day",
                    "points": aligned_pts,
                    "latest": aligned_pts[-1][1] if aligned_pts else None,
                })

        return {
            "table_name": table_name,
            "idx": idx,
            "date": date_str or "",
            "compare": compare_mode,
            "hours": span_hours,
            "since_ms": since_ms,
            "end_ms": until_ms,
            "now_ms": now_ms,
            "series": series,
        }

    # -------------------- 4. 通信图（单连接合并读取 + 短 TTL 快照缓存） --------------------
    def _read_comm_snapshot(self) -> dict[str, Any]:
        """单连接一次读取 RTU、通道参数、运行态与四遥点数，并做 1.5s 短 TTL 缓存。"""
        empty = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rtus": [],
            "channels": [],
            "point_totals": {"yc": 0, "yx": 0, "yt": 0, "yk": 0},
        }
        if not self.c_db.is_file():
            return empty
        now_mono = time.monotonic()
        sig = self._db_file_sig(self.c_db)
        with self._cache_lock:
            cached = self._comm_cache
            if cached is not None and (now_mono - cached[0]) < 1.5 and cached[1] == sig:
                return cached[2]

        with self._open_readonly(self.c_db) as conn:
            conn.execute("BEGIN")
            existing = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            rtus = (
                [
                    dict(r)
                    for r in conn.execute(
                        "SELECT rtu_id, channel_id, channel_type, name, process_enable, status, refresh_time "
                        "FROM fes_rtu_define ORDER BY rtu_id"
                    ).fetchall()
                ]
                if "fes_rtu_define" in existing
                else []
            )
            runtime_by_proto: dict[tuple[str, int], dict[str, Any]] = {}
            runtime_by_cid: dict[int, dict[str, Any]] = {}
            if "fes_channel_runtime_status" in existing:
                for r in conn.execute(
                    "SELECT * FROM fes_channel_runtime_status WHERE service_name='scdfe_c'"
                ).fetchall():
                    rd = dict(r)
                    cid_val = int(rd["channel_id"]) if rd.get("channel_id") is not None else 0
                    proto_val = str(rd.get("protocol") or rd.get("channel_type") or "")
                    if proto_val:
                        runtime_by_proto[(proto_val, cid_val)] = rd
                    runtime_by_cid.setdefault(cid_val, rd)

            point_counts: dict[int, dict[str, int]] = defaultdict(lambda: {"yc": 0, "yx": 0, "yk": 0, "yt": 0})
            point_totals = {"yc": 0, "yx": 0, "yk": 0, "yt": 0}
            for kind, tbl in KIND_TABLES.items():
                if tbl not in existing:
                    continue
                for rtu_id, cnt in conn.execute(
                    f'SELECT rtu_id, COUNT(*) FROM "{tbl}" GROUP BY rtu_id'
                ).fetchall():
                    c_int = int(cnt)
                    if rtu_id is not None:
                        point_counts[int(rtu_id)][kind] = c_int
                    point_totals[kind] += c_int

            rtu_items: list[dict[str, Any]] = []
            for rtu in rtus:
                rid = int(rtu["rtu_id"])
                cid = int(rtu["channel_id"]) if rtu.get("channel_id") is not None else 0
                ch_type = str(rtu.get("channel_type") or "")
                rt = runtime_by_proto.get((ch_type, cid)) or runtime_by_cid.get(cid) or {}
                diag = {}
                if rt.get("diagnostic_json"):
                    try:
                        diag = json.loads(rt["diagnostic_json"])
                    except Exception:
                        diag = {}
                rtu_items.append({
                    "rtu_id": rid,
                    "channel_id": cid,
                    "channel_type": ch_type,
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

            rtu_by_ch = {(r["channel_type"], r["channel_id"]): r for r in rtu_items}
            ch_items: list[dict[str, Any]] = []
            for ch_type, tbl in CHANNEL_PARAMETER_TABLES.items():
                if tbl not in existing:
                    continue
                for row in conn.execute(f'SELECT * FROM "{tbl}" ORDER BY channel_id').fetchall():
                    d = dict(row)
                    raw_cid = d.get("channel_id") if d.get("channel_id") is not None else d.get("id")
                    cid = int(raw_cid) if raw_cid is not None else 0
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
                    ch_items.append({
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

        snap = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rtus": rtu_items,
            "channels": ch_items,
            "point_totals": point_totals,
        }
        with self._cache_lock:
            self._comm_cache = (time.monotonic(), sig, snap)
        return snap

    def get_comm_rtus(self) -> dict[str, Any]:
        snap = self._read_comm_snapshot()
        return {"timestamp": snap["timestamp"], "items": snap["rtus"], "total": len(snap["rtus"])}

    def get_comm_channels(self) -> dict[str, Any]:
        snap = self._read_comm_snapshot()
        return {"timestamp": snap["timestamp"], "items": snap["channels"], "total": len(snap["channels"])}

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

    # -------------------- 5. 运行日志与报文日志（支持 scdfe_c / scdfe_d 控调日志） --------------------
    def list_log_files(self, *, record: str = "runtime") -> dict[str, Any]:
        rec_key = record if record in ("runtime", "packet", "control") else "runtime"
        now_mono = time.monotonic()
        with self._cache_lock:
            cached = self._log_files_cache.get(rec_key)
            if cached is not None and (now_mono - cached[0]) < 3.0:
                return cached[1]
        if rec_key == "control":
            root = self.d_log_dir
            prefix = "scdfe_d-"
            pat = LOG_PATTERNS["d"]
        elif rec_key == "packet":
            root = packet_log_directory(self.log_dir)
            prefix = "scdfe_c-"
            pat = LOG_PATTERN
        else:
            root = self.log_dir
            prefix = "scdfe_c-"
            pat = LOG_PATTERN
        items: list[dict[str, Any]] = []
        if root.is_dir():
            for entry, rel in log_entries(root, prefix):
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                m = pat.fullmatch(entry.name)
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
        result = {
            "record": rec_key,
            "directory": str(root),
            "processes": processes,
            "items": items[:200],
            "total": len(items),
        }
        with self._cache_lock:
            self._log_files_cache[rec_key] = (time.monotonic(), result)
        return result

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
        rec_key = record if record in ("runtime", "packet", "control") else "runtime"
        query: dict[str, Any] = {
            "record": "packet" if rec_key == "packet" else "runtime",
            "limit": str(max(10, min(1000, int(limit)))),
        }
        if filename:
            query["filename"] = filename
        if process:
            query["process"] = process
        if level:
            query["level"] = level
        if rec_key == "packet" and direction:
            query["direction"] = direction
        if keyword:
            query["q"] = keyword

        if rec_key == "control":
            if not self.d_log_dir.is_dir():
                return {"items": [], "has_more": False, "scanned_files": 0, "order": "hour_file_desc_line_desc"}
            return search_logs(self.d_log_dir, query, family="d")

        packet_dir = packet_log_directory(self.log_dir)
        if not self.log_dir.is_dir() and not packet_dir.is_dir():
            return {"items": [], "has_more": False, "scanned_files": 0, "order": "hour_file_desc_line_desc"}
        return search_logs(self.log_dir, query, family="c", packet_directory=packet_dir)

    # -------------------- 6. 告警中心持久化、转发状态检测、操作审计与曲线对比 --------------------
    def is_collector_running(self) -> bool:
        """检测 scdfe_d 后台采集与指令监视线程是否正在持有实例锁运行。"""
        try:
            return self.d_db.is_file() and InstanceLock.held(self.d_db, "device-collector")
        except Exception:
            return False

    def _audit_portal_log(self, process_name: str, level: str, message: str) -> None:
        """将门户写操作（指令下发、改限值、告警确认）安全写入按小时轮转运行日志。"""
        try:
            with self._audit_lock:
                logger = self._audit_loggers.get(process_name)
                if logger is None:
                    logger = ScdfeLogger(self.d_log_dir, process_name=process_name)
                    self._audit_loggers[process_name] = logger
            if level == "WARNING":
                logger.alarm(message)
            elif level == "ERROR":
                logger.error(message)
            else:
                logger.normal(message)
        except Exception:
            LOG.debug("portal audit log skipped: %s", message, exc_info=True)

    def _persist_alarm_snapshot(self, alarms: list[dict[str, Any]], active_fps: dict[str, str]) -> None:
        """当活动告警集合或其指纹变化时，批量同步至 alarm_events 表并标记已恢复告警。"""
        sig = "|".join(f"{k}={active_fps[k]}" for k in sorted(active_fps))
        with self._alarm_lock:
            if sig == self._alarm_sync_sig:
                return
            self._alarm_sync_sig = sig
        now_ms = int(time.time() * 1000)
        try:
            with self._history_lock, self._open_history() as conn:
                existing_active = {
                    r[0]
                    for r in conn.execute("SELECT alarm_key FROM alarm_events WHERE state='active'").fetchall()
                }
                recovered_keys = existing_active - set(active_fps)
                if recovered_keys:
                    conn.executemany(
                        "UPDATE alarm_events SET state='recovered', recovered_ms=?, last_seen_ms=? WHERE alarm_key=?",
                        [(now_ms, now_ms, k) for k in recovered_keys],
                    )
                if alarms:
                    rows = [
                        (
                            a["key"],
                            a["type"],
                            a["severity"],
                            a["title"],
                            a["dev_id"],
                            a["table_name"],
                            int(a["idx"]),
                            a["device_name"],
                            a["category"],
                            a["attr"],
                            a["attr_label"],
                            a["unit"],
                            "" if a["value"] is None else str(a["value"]),
                            a["detail"],
                            a["rtu_id"],
                            a["point_no"],
                            active_fps.get(a["key"], ""),
                            now_ms,
                            now_ms,
                            1 if a.get("acked") else 0,
                            a.get("acked_at"),
                        )
                        for a in alarms
                    ]
                    conn.executemany(
                        """
                        INSERT INTO alarm_events(
                            alarm_key, alarm_type, severity, title, dev_id, table_name, idx,
                            device_name, category, attr, attr_label, unit, value_text, detail,
                            rtu_id, point_no, fingerprint, state, first_seen_ms, last_seen_ms,
                            recovered_ms, acked, acked_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,NULL,?,?)
                        ON CONFLICT(alarm_key) DO UPDATE SET
                            value_text=excluded.value_text,
                            detail=excluded.detail,
                            fingerprint=excluded.fingerprint,
                            state='active',
                            last_seen_ms=excluded.last_seen_ms,
                            recovered_ms=NULL,
                            first_seen_ms=CASE WHEN alarm_events.state='recovered' THEN excluded.first_seen_ms ELSE alarm_events.first_seen_ms END,
                            acked=CASE WHEN alarm_events.state='recovered' OR alarm_events.fingerprint!=excluded.fingerprint THEN 0 ELSE alarm_events.acked END,
                            acked_at=CASE WHEN alarm_events.state='recovered' OR alarm_events.fingerprint!=excluded.fingerprint THEN NULL ELSE alarm_events.acked_at END
                        """,
                        rows,
                    )
                conn.commit()
        except Exception:
            LOG.debug("failed to persist alarm_events", exc_info=True)
    def _collect_raw_alarms(self, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        alarms: list[dict[str, Any]] = []
        active_fps: dict[str, str] = {}
        for dev in devices:
            dev_id = dev["dev_id"]
            table = dev["table_name"]
            idx = dev["idx"]
            name = dev["name"]
            cat = dev["category"]
            for m in dev.get("metrics") or ():
                if m.get("disabled"):
                    continue
                q = m.get("quality")
                if q == QUAL_OVER_MAX:
                    atype = "over_max"
                    severity = "high"
                    title = "越上限"
                    detail = f"当前值 {m.get('display_value')} > 上限 {m.get('max_value')}"
                elif q == QUAL_UNDER_MIN:
                    atype = "under_min"
                    severity = "high"
                    title = "越下限"
                    detail = f"当前值 {m.get('display_value')} < 下限 {m.get('min_value')}"
                elif q == 2:
                    atype = "comm_error"
                    severity = "medium"
                    title = "通信异常"
                    detail = f"RTU {m.get('rtu_id')} / #{m.get('point_no')} 质量异常"
                else:
                    continue
                key = f"{table}:{idx}:m:{m['metric']}:{atype}"
                fp = f"{atype}|{m.get('source_time')}|{m.get('max_value')}|{m.get('min_value')}"
                active_fps[key] = fp
                alarms.append({
                    "key": key,
                    "type": atype,
                    "severity": severity,
                    "title": title,
                    "dev_id": dev_id,
                    "table_name": table,
                    "idx": idx,
                    "device_name": name,
                    "category": cat,
                    "attr": m["metric"],
                    "attr_label": m.get("label") or m["metric"],
                    "unit": m.get("unit") or "",
                    "value": m.get("display_value"),
                    "max_value": m.get("max_value"),
                    "min_value": m.get("min_value"),
                    "rtu_id": m.get("rtu_id"),
                    "point_no": m.get("point_no"),
                    "time_ms": m.get("source_time"),
                    "time_iso": _iso_from_ms(m.get("source_time")),
                    "detail": detail,
                    "_fp": fp,
                })
            for c in dev.get("controls") or ():
                if c.get("cmd_status") != CTRL_STATUS_FAILED:
                    continue
                atype = "control_failed"
                key = f"{table}:{idx}:c:{c['control']}:{atype}"
                fp = f"{atype}|{c.get('cmd_time')}|{c.get('cmd_ack_time')}|{c.get('value')}"
                active_fps[key] = fp
                alarms.append({
                    "key": key,
                    "type": atype,
                    "severity": "high",
                    "title": "指令失败",
                    "dev_id": dev_id,
                    "table_name": table,
                    "idx": idx,
                    "device_name": name,
                    "category": cat,
                    "attr": c["control"],
                    "attr_label": c.get("label") or c["control"],
                    "unit": c.get("unit") or "",
                    "value": c.get("value"),
                    "max_value": c.get("max_value"),
                    "min_value": c.get("min_value"),
                    "rtu_id": c.get("rtu_id"),
                    "point_no": c.get("point_no"),
                    "time_ms": c.get("cmd_ack_time") or c.get("cmd_time"),
                    "time_iso": _iso_from_ms(c.get("cmd_ack_time") or c.get("cmd_time")),
                    "detail": f"目标值 {c.get('value')} 执行失败或回执超时",
                    "_fp": fp,
                })

        with self._alarm_lock:
            # 清理已恢复的旧确认记录；若已恢复后再次触发且指纹变化则自动复归未确认
            for k in list(self._acked_alarms):
                if k not in active_fps:
                    self._acked_alarms.pop(k, None)
            for item in alarms:
                rec = self._acked_alarms.get(item["key"])
                is_acked = rec is not None and rec.get("fp") == item["_fp"]
                item["acked"] = is_acked
                item["acked_at"] = rec.get("acked_at") if is_acked else None
                item.pop("_fp", None)
        self._persist_alarm_snapshot(alarms, active_fps)
        return alarms

    def _summarize_alarm_counts(self, devices: list[dict[str, Any]]) -> dict[str, int]:
        alarms = self._collect_raw_alarms(devices)
        unacked = sum(1 for a in alarms if not a["acked"])
        return {"alarm_total": len(alarms), "alarm_unacked": unacked}

    # -------------------- 设备检修挂牌（告警屏蔽） --------------------
    def _load_maintenance_map(self) -> dict[str, dict[str, Any]]:
        now_mono = time.monotonic()
        with self._cache_lock:
            cached = self._maint_cache
            if cached is not None and (now_mono - cached[0]) < 2.0:
                return cached[1]
        out: dict[str, dict[str, Any]] = {}
        if self.history_db.is_file():
            now_ms = int(time.time() * 1000)
            try:
                with self._open_history(readonly=True) as conn:
                    rows = conn.execute(
                        "SELECT dev_id, table_name, idx, device_name, reason, operator, created_ms, expires_ms "
                        "FROM device_maintenance WHERE expires_ms IS NULL OR expires_ms > ?",
                        (now_ms,),
                    ).fetchall()
                    for r in rows:
                        out[str(r["dev_id"])] = dict(r)
            except sqlite3.Error:
                pass
        with self._cache_lock:
            self._maint_cache = (now_mono, out)
        return out

    def list_maintenance(self) -> dict[str, Any]:
        m_map = self._load_maintenance_map()
        items = sorted(m_map.values(), key=lambda x: -(x.get("created_ms") or 0))
        return {"items": items, "total": len(items)}

    def set_maintenance(
        self,
        *,
        table_name: str,
        idx: int,
        device_name: str = "",
        reason: str = "",
        operator: str = "",
        hours: float | None = 8.0,
    ) -> dict[str, Any]:
        if not table_name or not table_name.isidentifier():
            raise ValueError("设备表名不合法")
        dev_id = f"{table_name}-{int(idx)}"
        now_ms = int(time.time() * 1000)
        expires_ms = None
        if hours is not None and float(hours) > 0:
            expires_ms = now_ms + int(min(720.0, float(hours)) * 3600 * 1000)
        name_text = (device_name or dev_id).strip()[:120]
        reason_text = (reason or "检修挂牌").strip()[:240]
        op_text = (operator or "值班员").strip()[:64]
        with self._history_lock, self._open_history() as conn:
            conn.execute(
                """
                INSERT INTO device_maintenance(dev_id, table_name, idx, device_name, reason, operator, created_ms, expires_ms)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dev_id) DO UPDATE SET
                    device_name=excluded.device_name,
                    reason=excluded.reason,
                    operator=excluded.operator,
                    created_ms=excluded.created_ms,
                    expires_ms=excluded.expires_ms
                """,
                (dev_id, table_name, int(idx), name_text, reason_text, op_text, now_ms, expires_ms),
            )
            conn.commit()
        with self._cache_lock:
            self._maint_cache = None
        self.event_bus.publish("maintenance", {"action": "set", "dev_id": dev_id})
        self._audit_portal_log("scdfe_d-web", "INFO", f"门户检修挂牌 设备={dev_id} 操作人={op_text} 原因={reason_text}")
        return {
            "dev_id": dev_id,
            "table_name": table_name,
            "idx": int(idx),
            "device_name": name_text,
            "reason": reason_text,
            "operator": op_text,
            "created_ms": now_ms,
            "expires_ms": expires_ms,
        }

    def clear_maintenance(self, dev_id: str) -> dict[str, Any]:
        key = str(dev_id or "").strip()
        if not key:
            raise ValueError("设备编号不能为空")
        with self._history_lock, self._open_history() as conn:
            cur = conn.execute("DELETE FROM device_maintenance WHERE dev_id=?", (key,))
            conn.commit()
            removed = int(cur.rowcount or 0)
        with self._cache_lock:
            self._maint_cache = None
        self.event_bus.publish("maintenance", {"action": "clear", "dev_id": key})
        self._audit_portal_log("scdfe_d-web", "INFO", f"门户解除检修挂牌 设备={key}")
        return {"dev_id": key, "removed": removed}

    def get_alarms(
        self,
        *,
        alarm_type: str | None = None,
        acked: str | None = None,
        category: str | None = None,
        search: str | None = None,
        hide_maintenance: bool = False,
    ) -> dict[str, Any]:
        devices = self._read_all_devices()
        all_alarms = self._collect_raw_alarms(devices)
        maint_map = self._load_maintenance_map()
        maint_suppressed = 0
        for a in all_alarms:
            m_info = maint_map.get(a.get("dev_id") or "")
            a["maintenance"] = bool(m_info)
            a["maintenance_reason"] = m_info.get("reason", "") if m_info else ""
            if m_info:
                maint_suppressed += 1
        summary = {
            "total": len(all_alarms),
            "unacked": sum(1 for a in all_alarms if not a["acked"] and not a["maintenance"]),
            "over_max": sum(1 for a in all_alarms if a["type"] == "over_max"),
            "under_min": sum(1 for a in all_alarms if a["type"] == "under_min"),
            "comm_error": sum(1 for a in all_alarms if a["type"] == "comm_error"),
            "control_failed": sum(1 for a in all_alarms if a["type"] == "control_failed"),
            "maintenance_suppressed": maint_suppressed,
        }
        items = all_alarms
        if hide_maintenance:
            items = [a for a in items if not a["maintenance"]]
        if alarm_type and alarm_type in {"over_max", "under_min", "comm_error", "control_failed"}:
            items = [a for a in items if a["type"] == alarm_type]
        if acked == "0":
            items = [a for a in items if not a["acked"]]
        elif acked == "1":
            items = [a for a in items if a["acked"]]
        if category:
            items = [a for a in items if a["category"] == category]
        if search and search.strip():
            kw = search.strip().lower()
            items = [
                a for a in items
                if kw in str(a["device_name"]).lower()
                or kw in str(a["attr"]).lower()
                or kw in str(a["attr_label"]).lower()
                or kw in str(a["table_name"]).lower()
            ]
        sev_rank = {"high": 0, "medium": 1, "low": 2}
        items.sort(
            key=lambda a: (
                1 if a["maintenance"] else 0,
                1 if a["acked"] else 0,
                sev_rank.get(a["severity"], 9),
                -(a["time_ms"] or 0),
                a["device_name"],
            )
        )
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "summary": summary,
            "maintenance_count": len(maint_map),
            "items": items[:500],
            "total": len(items),
        }

    def ack_alarms(
        self,
        *,
        keys: list[str] | None = None,
        ack_all: bool = False,
        operator: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        devices = self._read_all_devices()
        # 重新计算当前活动告警指纹
        wanted = set(keys or [])
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        op_text = (operator or "值班员").strip()[:64]
        note_text = (note or "").strip()[:240]
        acked_cnt = 0
        acked_rows: list[tuple[str, str, str, str]] = []
        with self._alarm_lock:
            for dev in devices:
                table = dev["table_name"]
                idx = dev["idx"]
                for m in dev.get("metrics") or ():
                    if m.get("disabled"):
                        continue
                    q = m.get("quality")
                    atype = (
                        "over_max" if q == QUAL_OVER_MAX
                        else ("under_min" if q == QUAL_UNDER_MIN else ("comm_error" if q == 2 else None))
                    )
                    if not atype:
                        continue
                    key = f"{table}:{idx}:m:{m['metric']}:{atype}"
                    if ack_all or key in wanted:
                        fp = f"{atype}|{m.get('source_time')}|{m.get('max_value')}|{m.get('min_value')}"
                        self._acked_alarms[key] = {
                            "fp": fp,
                            "acked_at": now_iso,
                            "ack_operator": op_text,
                            "ack_note": note_text,
                        }
                        acked_rows.append((now_iso, op_text, note_text, key))
                        acked_cnt += 1
                for c in dev.get("controls") or ():
                    if c.get("cmd_status") != CTRL_STATUS_FAILED:
                        continue
                    atype = "control_failed"
                    key = f"{table}:{idx}:c:{c['control']}:{atype}"
                    if ack_all or key in wanted:
                        fp = f"{atype}|{c.get('cmd_time')}|{c.get('cmd_ack_time')}|{c.get('value')}"
                        self._acked_alarms[key] = {
                            "fp": fp,
                            "acked_at": now_iso,
                            "ack_operator": op_text,
                            "ack_note": note_text,
                        }
                        acked_rows.append((now_iso, op_text, note_text, key))
                        acked_cnt += 1
        if acked_rows:
            try:
                with self._history_lock, self._open_history() as conn:
                    conn.executemany(
                        "UPDATE alarm_events SET acked=1, acked_at=?, ack_operator=?, ack_note=? WHERE alarm_key=?",
                        acked_rows,
                    )
                    conn.commit()
            except Exception:
                LOG.debug("failed to persist alarm ack", exc_info=True)
            self._audit_portal_log(
                "scdfe_d-web",
                "INFO",
                f"门户确认告警 数量={acked_cnt} 全部确认={bool(ack_all)} 操作人={op_text} 备注={note_text}",
            )
            self.event_bus.publish("alarm", {"action": "ack", "acked_count": acked_cnt})
        return {"acked_count": acked_cnt, "acked_at": now_iso, "operator": op_text, "note": note_text}

    def get_alarm_history(
        self,
        *,
        alarm_type: str | None = None,
        state: str | None = None,
        search: str | None = None,
        limit: int = 300,
    ) -> dict[str, Any]:
        """从 scada_portal_history.db 检索持久化告警历史（含已恢复与已确认记录）。"""
        # 查询前刷新一次最新活动告警快照，确保刚触发或恢复的状态同步入库
        self._collect_raw_alarms(self._read_all_devices())
        limit = max(10, min(1000, int(limit)))
        where: list[str] = []
        params: list[Any] = []
        if alarm_type and alarm_type in {"over_max", "under_min", "comm_error", "control_failed"}:
            where.append("alarm_type=?")
            params.append(alarm_type)
        if state in {"active", "recovered"}:
            where.append("state=?")
            params.append(state)
        if search and search.strip():
            needle = f"%{search.strip()}%"
            where.append("(device_name LIKE ? OR attr LIKE ? OR attr_label LIKE ? OR table_name LIKE ?)")
            params.extend([needle, needle, needle, needle])
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        items: list[dict[str, Any]] = []
        total = 0
        if self.history_db.is_file():
            with self._open_history(readonly=True) as conn:
                conn.row_factory = sqlite3.Row
                total = int(conn.execute(f"SELECT COUNT(*) FROM alarm_events{where_sql}", params).fetchone()[0])
                rows = conn.execute(
                    f"SELECT * FROM alarm_events{where_sql} ORDER BY last_seen_ms DESC LIMIT ?",
                    [*params, limit],
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    items.append({
                        "key": d["alarm_key"],
                        "type": d["alarm_type"],
                        "severity": d["severity"],
                        "title": d["title"],
                        "dev_id": d["dev_id"],
                        "table_name": d["table_name"],
                        "idx": d["idx"],
                        "device_name": d["device_name"],
                        "category": d["category"],
                        "attr": d["attr"],
                        "attr_label": d["attr_label"],
                        "unit": d["unit"],
                        "value": d["value_text"],
                        "detail": d["detail"],
                        "rtu_id": d["rtu_id"],
                        "point_no": d["point_no"],
                        "state": d["state"],
                        "first_seen_ms": d["first_seen_ms"],
                        "last_seen_ms": d["last_seen_ms"],
                        "recovered_ms": d["recovered_ms"],
                        "acked": bool(d["acked"]),
                        "acked_at": d["acked_at"],
                        "ack_operator": d.get("ack_operator") or "",
                        "ack_note": d.get("ack_note") or "",
                    })
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "items": items,
            "total": total,
        }

    def dispatch_device_control(
        self,
        table_name: str,
        idx: int,
        control: str,
        raw_value: Any,
    ) -> dict[str, Any]:
        """在门户侧校验并写入设备宽表控调字段，触发 scdfe_d 亚秒级监视并下发至 scdfe_c。"""
        if not self.d_db.is_file():
            raise FileNotFoundError(f"未找到设备实时库：{self.d_db}")
        if not table_name or not table_name.isidentifier():
            raise ValueError("设备表名不合法")
        if not control or not control.isidentifier():
            raise ValueError("控调属性名不合法")
        if raw_value is None or isinstance(raw_value, bool):
            raise ValueError("控调目标值不能为空或布尔值")

        with closing(sqlite3.connect(self.d_db, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if not exists:
                raise FileNotFoundError(f"设备表不存在：{table_name}")
            col_info = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            col_types = {c[1]: str(c[2] or "").upper() for c in col_info}
            required_cols = [control + s for s in CTRL_SUFFIXES]
            if any(c not in col_types for c in required_cols):
                raise ValueError(f"设备表 {table_name} 不存在控调字段组：{control}")
            kind = "yk" if col_types[control] == "INTEGER" else "yt"
            quoted_cols = ", ".join(f'"{c}"' for c in ["name", *required_cols])
            row = conn.execute(
                f'SELECT {quoted_cols} FROM "{table_name}" WHERE idx=?',
                (int(idx),),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"设备不存在：{table_name}#{idx}")
            row_dict = dict(row)
            rtu = row_dict.get(f"{control}_cmd_rtu")
            pnt = row_dict.get(f"{control}_cmd_pnt")
            if not (isinstance(rtu, int) and rtu > 0 and isinstance(pnt, int) and pnt > 0):
                raise ValueError("该控调属性尚未绑定有效的 RTU / 点号")
            if row_dict.get(f"{control}_cmd_notuse"):
                raise ValueError("该控调属性已禁用闭锁，禁止下发")

            if kind == "yk":
                try:
                    num = float(raw_value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError("遥控目标值必须为 0 或 1") from exc
                if not math.isfinite(num) or num not in (0.0, 1.0):
                    raise ValueError("遥控目标值必须为 0（分/停）或 1（合/启）")
                norm_val: int | float = int(num)
            else:
                try:
                    num = float(raw_value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError("遥调设定值必须为有限数值") from exc
                if not math.isfinite(num):
                    raise ValueError("遥调设定值必须为有限数值")
                upper = _parse_limit_number(row_dict.get(f"{control}_cmd_max"))
                lower = _parse_limit_number(row_dict.get(f"{control}_cmd_min"))
                if upper is not None and lower is not None and upper > lower:
                    if num > upper:
                        raise ValueError(f"设定值 {num} 超过上限 {upper}")
                    if num < lower:
                        raise ValueError(f"设定值 {num} 低于下限 {lower}")
                norm_val = num

            old_time = int(row_dict.get(f"{control}_cmd_time") or 0)
            cmd_ms = max(int(time.time() * 1000), old_time + 1)
            conn.execute(
                f'UPDATE "{table_name}" SET "{control}"=?, "{control}_cmd_time"=?, '
                f'"{control}_cmd_status"=?, "{control}_cmd_ack_time"=NULL WHERE idx=?',
                (norm_val, cmd_ms, CTRL_STATUS_PENDING, int(idx)),
            )
            conn.commit()

        with self._cache_lock:
            self._devices_cache = None
            self._pf_live_cache = None
            self._overview_cache = None

        collector_ok = self.is_collector_running()
        dev_name = row_dict.get("name") or f"{table_name}-{idx}"
        self._audit_portal_log(
            "scdfe_d-control",
            "INFO" if collector_ok else "WARNING",
            f"门户下发指令 设备={table_name}.{dev_name}(idx={idx}) 属性={control}({kind.upper()}) "
            f"RTU={int(rtu)} 点号={int(pnt)} 值={norm_val} 时标={cmd_ms} 转发在线={collector_ok}",
        )
        label, unit = CONTROL_META.get(control, (control, ""))
        warning_msg = "" if collector_ok else "scdfe_d 后台转发线程未启动，指令已写入宽表待启动后转发"
        try:
            with self._history_lock, self._open_history() as conn:
                conn.execute(
                    """
                    INSERT INTO control_audit(
                        ts_ms, table_name, idx, device_name, control, control_label, kind, target_value, operator, status, detail
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cmd_ms,
                        table_name,
                        int(idx),
                        str(dev_name),
                        control,
                        label,
                        kind.upper(),
                        f"{norm_val}{(' ' + unit) if unit else ''}",
                        "值班员",
                        "pending" if collector_ok else "queued_offline",
                        warning_msg or f"RTU {int(rtu)} · 点 {int(pnt)}",
                    ),
                )
                conn.commit()
        except Exception:
            LOG.debug("failed to persist control audit", exc_info=True)
        self.event_bus.publish(
            "control",
            {
                "dev_id": f"{table_name}-{int(idx)}",
                "device_name": str(dev_name),
                "control": control,
                "label": label,
                "value": norm_val,
                "unit": unit,
                "collector_running": collector_ok,
            },
        )
        return {
            "table_name": table_name,
            "idx": int(idx),
            "device_name": dev_name,
            "control": control,
            "label": label,
            "unit": unit,
            "kind": kind,
            "value": norm_val,
            "cmd_time": cmd_ms,
            "cmd_status": CTRL_STATUS_PENDING,
            "status_label": CTRL_STATUS_LABELS[CTRL_STATUS_PENDING],
            "rtu_id": int(rtu),
            "point_no": int(pnt),
            "collector_running": collector_ok,
            "warning": warning_msg,
        }

    def get_control_audit(self, *, limit: int = 50) -> dict[str, Any]:
        """检索最近控调指令审计回执记录。"""
        limit = max(1, min(200, int(limit)))
        items: list[dict[str, Any]] = []
        if self.history_db.is_file():
            try:
                with self._open_history(readonly=True) as conn:
                    rows = conn.execute(
                        "SELECT id, ts_ms, table_name, idx, device_name, control, control_label, kind, "
                        "target_value, operator, status, detail FROM control_audit ORDER BY ts_ms DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                    items = [dict(r) for r in rows]
            except sqlite3.Error:
                items = []
        return {"items": items, "total": len(items)}

    def get_comm_topology(self) -> dict[str, Any]:
        """构建主站 → 协议通道 → RTU → 关联设备与质量分布的链路拓扑快照。"""
        comm = self._read_comm_snapshot()
        devices = self._read_all_devices()
        rtu_devs: dict[int, list[dict[str, Any]]] = defaultdict(list)
        rtu_valid_pts: dict[int, int] = defaultdict(int)
        rtu_bound_pts: dict[int, int] = defaultdict(int)
        for d in devices:
            seen_rtus: set[int] = set()
            for m in d.get("metrics") or ():
                rid = m.get("rtu_id")
                if rid is None:
                    continue
                rid_i = int(rid)
                seen_rtus.add(rid_i)
                rtu_bound_pts[rid_i] += 1
                if m.get("valid"):
                    rtu_valid_pts[rid_i] += 1
            for rid_i in seen_rtus:
                rtu_devs[rid_i].append({
                    "dev_id": d["dev_id"],
                    "name": d["name"],
                    "category": d["category"],
                    "alarm_count": d.get("alarm_count", 0),
                })

        ch_map = {int(ch["channel_id"]): ch for ch in comm.get("channels") or () if ch.get("channel_id") is not None}
        links: list[dict[str, Any]] = []
        for r in comm.get("rtus") or ():
            rid = int(r["rtu_id"])
            cid = r.get("channel_id")
            ch = ch_map.get(int(cid)) if cid is not None else None
            devs = rtu_devs.get(rid, [])
            bound = rtu_bound_pts.get(rid, 0)
            valid = rtu_valid_pts.get(rid, 0)
            health_pct = round((valid / bound) * 100.0, 1) if bound > 0 else 100.0
            links.append({
                "rtu_id": rid,
                "rtu_name": r.get("name") or f"RTU-{rid}",
                "channel_id": cid,
                "channel_type": (ch or {}).get("channel_type") or r.get("channel_type") or "—",
                "endpoint": (ch or {}).get("endpoint") or "—",
                "connection_state": r.get("connection_state") or "offline",
                "device_count": len(devs),
                "bound_points": bound,
                "valid_points": valid,
                "health_pct": health_pct,
                "fault_devices": [x for x in devs if x["alarm_count"] > 0][:8],
            })
        return {
            "timestamp": comm["timestamp"],
            "station_name": "SCADA 主站",
            "links": links,
            "point_totals": comm.get("point_totals") or {},
        }

    def get_history_summary(self) -> dict[str, Any]:
        if not self.history_db.is_file():
            return {"db_size_mb": 0.0, "minute_rollup_rows": 0, "maintenance_active": 0, "control_audit_rows": 0}
        now_mono = time.monotonic()
        with self._cache_lock:
            cached = self._history_stats_cache
            if cached is not None and (now_mono - cached[0]) < 10.0:
                return cached[1]
        with self._open_history(readonly=True) as conn:
            stats = get_history_stats(conn, self.history_db)
        with self._cache_lock:
            self._history_stats_cache = (now_mono, stats)
        return stats

    def build_daily_report(self, *, date_str: str | None = None, fmt: str = "json") -> tuple[str, bytes | dict[str, Any]]:
        """生成站级运行日报（支持 json / csv / html 可打印版）。"""
        day_label = (date_str or datetime.now().strftime("%Y-%m-%d")).strip()
        cockpit = self.get_polar_cockpit(hours=24.0)
        devices = self._read_all_devices()
        cum = cockpit.get("cumulative") or {}
        res = cockpit.get("resilience") or {}
        derived = cockpit.get("derived") or {}
        dq = cockpit.get("data_quality") or {}
        st = cockpit.get("polar_stats") or {}

        kpis = [
            {"group": "累计电量", "name": "累计供电量", "value": cum.get("total_supply_wkwh", "—"), "unit": "万kWh"},
            {"group": "累计电量", "name": "科考总负荷", "value": cum.get("total_load_wkwh", "—"), "unit": "万kWh"},
            {"group": "节能减碳", "name": "累计节油量", "value": cum.get("diesel_saved_tons", "—"), "unit": "吨"},
            {"group": "节能减碳", "name": "累计减碳量", "value": cum.get("co2_reduced_tons", "—"), "unit": "吨 CO₂"},
            {"group": "实时能效", "name": "绿电占比", "value": st.get("green_ratio_pct", "—"), "unit": "%"},
            {"group": "保供自持", "name": "孤网自持时长", "value": res.get("autonomy_hours", "—"), "unit": "h"},
            {"group": "保供自持", "name": "柴油剩余续航", "value": derived.get("diesel_endurance_hours", "—"), "unit": "h"},
            {"group": "氢能系统", "name": "电-氢-电综合效率", "value": derived.get("h2_roundtrip_eff_pct", "—"), "unit": "%"},
            {"group": "氢能系统", "name": "储氢压力", "value": res.get("hydro_pressure_mpa", "—"), "unit": "MPa"},
            {"group": "新能源", "name": "估算限电/弃电", "value": derived.get("curtailment_kw", "—"), "unit": "kW"},
            {"group": "运行质量", "name": "量测数据健康率", "value": dq.get("health_pct", 100.0), "unit": "%"},
            {"group": "运行质量", "name": "在线 RTU / 告警未确认", "value": f"{st.get('rtu_online', 0)}/{st.get('rtu_total', 0)} · {st.get('alarm_unacked', 0)}", "unit": "台 / 条"},
        ]

        dev_rows = []
        for d in devices:
            m_map = {m["metric"]: m for m in d.get("metrics") or ()}
            p_m = m_map.get("p") or m_map.get("ac_p") or m_map.get("dc_p") or m_map.get("i_p")
            p_val = round(float(p_m["value"]), 2) if (p_m and p_m.get("value") is not None) else "—"
            status_text = "合闸" if d.get("position") == 1 else ("分闸" if d.get("position") == 0 else ("运行" if d.get("run") == 1 else "—"))
            dev_rows.append({
                "name": d["name"],
                "category": d["category"],
                "table_name": d["table_name"],
                "idx": d["idx"],
                "valid_count": d["valid_count"],
                "bound_count": d["bound_count"],
                "p_kw": p_val,
                "status_text": status_text,
                "alarm_count": d.get("alarm_count", 0),
            })

        report_obj = {
            "station_name": cockpit.get("station_name", "南极秦岭站综合能量管理系统"),
            "date": day_label,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "quality_health_pct": dq.get("health_pct", 100.0),
            "kpis": kpis,
            "devices": dev_rows,
        }
        fmt_l = str(fmt or "json").lower()
        if fmt_l == "csv":
            return "text/csv; charset=utf-8", render_daily_report_csv(report_obj)
        if fmt_l == "html":
            return "text/html; charset=utf-8", render_daily_report_html(report_obj)
        return "application/json; charset=utf-8", report_obj

    def compare_trends(
        self,
        targets: list[tuple[str, int, str]],
        *,
        hours: float = 24.0,
    ) -> dict[str, Any]:
        """批量查询最多 8 条跨设备/跨指标历史曲线用于同图叠加对比。"""
        if not targets:
            raise ValueError("请至少指定一条对比曲线")
        if len(targets) > 8:
            raise ValueError("单次最多同时对比 8 条曲线")
        hours_f = float(hours)
        if not math.isfinite(hours_f) or hours_f <= 0:
            raise ValueError("时间窗口 hours 必须为有限正数")
        span_hours = max(1 / 60.0, min(24.0, hours_f))
        now_ms = int(time.time() * 1000)
        since_ms = now_ms - int(span_hours * 3600 * 1000)

        dev_names = {(d["table_name"], d["idx"]): d["name"] for d in self._read_all_devices()}
        series: list[dict[str, Any]] = []
        max_pts = 720
        if self.history_db.is_file():
            with self._open_history(readonly=True) as conn:
                for table_name, idx, metric in targets:
                    rows = conn.execute(
                        "SELECT ts_ms, value FROM device_daily_samples "
                        "WHERE table_name=? AND idx=? AND metric=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms ASC",
                        (table_name, int(idx), metric, since_ms, now_ms),
                    ).fetchall()
                    pts = [[int(ts), round(float(v), 4)] for ts, v in rows]
                    if len(pts) > max_pts:
                        step = (len(pts) - 1) / (max_pts - 1)
                        pts = [pts[int(round(i * step))] for i in range(max_pts)]
                    mlabel, unit = METRIC_META.get(metric, (metric, ""))
                    dname = dev_names.get((table_name, int(idx)), f"{table_name}#{idx}")
                    series.append({
                        "key": f"{table_name}:{idx}:{metric}",
                        "table_name": table_name,
                        "idx": int(idx),
                        "device_name": dname,
                        "metric": metric,
                        "label": f"{dname} · {mlabel}",
                        "unit": unit,
                        "points": pts,
                        "latest": pts[-1][1] if pts else None,
                    })
        return {
            "hours": span_hours,
            "since_ms": since_ms,
            "end_ms": now_ms,
            "now_ms": now_ms,
            "series": series,
        }

    def get_polar_cockpit(self, *, hours: float = 6.0) -> dict[str, Any]:
        """聚合南极秦岭站极地微电网智慧管控大屏（7 大模块 + 源荷与储氢历史曲线）。"""
        hours_f = float(hours)
        if not math.isfinite(hours_f) or hours_f <= 0:
            raise ValueError("时间窗口 hours 必须为有限正数")
        span_hours = round(max(0.5, min(24.0, hours_f)), 2)

        now_mono = time.monotonic()
        with self._cache_lock:
            cached_ck = self._cockpit_cache.get(span_hours)
            if cached_ck is not None and (now_mono - cached_ck[0]) < 4.0:
                return cached_ck[1]

        devices = self._read_all_devices()
        overview = self.get_overview()
        ef = self.get_energy_flow()
        kpis = overview["kpis"]
        rt = overview["realtime"]
        nodes = ef["nodes"]
        env = ef["environment"]
        summary = ef["summary"]

        # 1. 建立设备角色索引，用于历史功率曲线分角色聚合与多单元储能调度追踪
        dev_role: dict[tuple[str, int], str] = {}
        storage_units: dict[tuple[str, int], str] = {}
        for dev in devices:
            t = dev["table_name"]
            idx = int(dev["idx"])
            name = str(dev["name"] or f"{t}#{idx}")
            key = (t, idx)
            if t == "ACGenerator":
                if "风电" in name or "风力" in name:
                    dev_role[key] = "wind"
                elif "光伏" in name:
                    dev_role[key] = "pv"
                elif "柴油" in name:
                    dev_role[key] = "diesel"
                elif "储能" in name:
                    dev_role[key] = "storage"
                    storage_units[key] = name
            elif t == "DCGenerator":
                if "光伏" in name:
                    dev_role[key] = "pv"
                elif "储能" in name:
                    dev_role[key] = "storage"
                    storage_units[key] = name
                elif "燃料电池" in name or "氢" in name:
                    dev_role[key] = "fuel_cell"
            elif t == "ACLoad":
                dev_role[key] = "electrolyzer" if "制氢" in name else "load"
            elif t == "DCLoad":
                dev_role[key] = "load"

        now_ms = int(time.time() * 1000)
        since_ms = now_ms - int(span_hours * 3600 * 1000)
        local_now = datetime.now().astimezone()
        today_start_ms = int(
            local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
        )
        query_since_ms = min(since_ms, today_start_ms)

        # 2. 按 (table_name, idx, 'p', ts_ms) 聚簇主键范围扫描 + SQLite 端降采样，避免全表回表慢查询
        bucket_count = 72
        bucket_ms = max(5000, int((now_ms - since_ms) / bucket_count))
        # bucket_idx -> { (table, idx): avg_val }
        window_buckets: dict[int, dict[tuple[str, int], float]] = defaultdict(dict)
        today_wind_kwh = 0.0
        today_pv_kwh = 0.0

        if self.history_db.is_file() and dev_role:
            step_s = float(self.sample_interval_sec)
            with self._open_history(readonly=True) as conn:
                for dkey, role in dev_role.items():
                    tbl, idx_i = dkey
                    b_rows = conn.execute(
                        "SELECT CAST((ts_ms - ?) / ? AS INTEGER) AS b_idx, AVG(value) "
                        "FROM device_daily_samples "
                        "WHERE table_name=? AND idx=? AND metric='p' AND ts_ms>=? AND ts_ms<=? "
                        "GROUP BY b_idx",
                        (since_ms, bucket_ms, tbl, idx_i, since_ms, now_ms),
                    ).fetchall()
                    for b_idx, avg_v in b_rows:
                        if avg_v is not None:
                            window_buckets[int(b_idx)][dkey] = float(avg_v)

                    if role in {"wind", "pv"}:
                        t_rows = conn.execute(
                            "SELECT CAST(ts_ms / 300000 AS INTEGER) AS slot, "
                            "AVG(CASE WHEN value > 0 THEN value ELSE 0 END), MIN(ts_ms), MAX(ts_ms) "
                            "FROM device_daily_samples "
                            "WHERE table_name=? AND idx=? AND metric='p' AND ts_ms>=? AND ts_ms<=? "
                            "GROUP BY slot",
                            (tbl, idx_i, today_start_ms, now_ms),
                        ).fetchall()
                        dev_kwh = 0.0
                        for _, avg_kw, min_ts, max_ts in t_rows:
                            if avg_kw is None or avg_kw <= 0:
                                continue
                            dur_s = min(300.0, max(step_s, (int(max_ts) - int(min_ts)) / 1000.0 + step_s))
                            dev_kwh += float(avg_kw) * (dur_s / 3600.0)
                        if role == "wind":
                            today_wind_kwh += dev_kwh
                        else:
                            today_pv_kwh += dev_kwh

        if today_wind_kwh <= 0.0 and today_pv_kwh <= 0.0:
            with self._energy_accum_lock:
                today_wind_kwh = float(self._session_kwh.get("wind", 0.0))
                today_pv_kwh = float(self._session_kwh.get("pv", 0.0))

        # 组装时间轴与 5 条源荷功率序列
        power_series: dict[str, list[list[float]]] = {
            "wind": [],
            "pv": [],
            "diesel": [],
            "storage": [],
            "load": [],
        }
        # 挑选最多 4 台有历史数据的储能单元 + 1 条氢能净功率（燃料电池 - 制氢）
        unit_activity: dict[tuple[str, int], float] = defaultdict(float)
        for snap in window_buckets.values():
            for ukey in storage_units:
                if ukey in snap:
                    unit_activity[ukey] = max(unit_activity[ukey], abs(snap[ukey]))
        chosen_units = sorted(
            storage_units.keys(),
            key=lambda k: (-unit_activity.get(k, -1.0), k[0], k[1]),
        )[:4]
        unit_points: dict[tuple[str, int], list[list[float]]] = {k: [] for k in chosen_units}
        hydro_net_points: list[list[float]] = []

        for b_idx in sorted(window_buckets.keys()):
            snap = window_buckets[b_idx]
            ts_mid = min(now_ms, since_ms + b_idx * bucket_ms + bucket_ms // 2)
            sums = {"wind": 0.0, "pv": 0.0, "diesel": 0.0, "storage": 0.0, "load": 0.0, "elec": 0.0, "fc": 0.0}
            for dkey, v in snap.items():
                role = dev_role.get(dkey)
                if role == "wind":
                    sums["wind"] += max(0.0, v)
                elif role == "pv":
                    sums["pv"] += max(0.0, v)
                elif role == "diesel":
                    sums["diesel"] += max(0.0, v)
                elif role == "storage":
                    sums["storage"] += v
                elif role == "load":
                    sums["load"] += max(0.0, v)
                elif role == "electrolyzer":
                    sums["elec"] += max(0.0, v)
                elif role == "fuel_cell":
                    sums["fc"] += max(0.0, v)
            total_l = sums["load"] + sums["elec"]
            power_series["wind"].append([ts_mid, round(sums["wind"], 2)])
            power_series["pv"].append([ts_mid, round(sums["pv"], 2)])
            power_series["diesel"].append([ts_mid, round(sums["diesel"], 2)])
            power_series["storage"].append([ts_mid, round(sums["storage"], 2)])
            power_series["load"].append([ts_mid, round(total_l, 2)])
            for ukey in chosen_units:
                if ukey in snap:
                    unit_points[ukey].append([ts_mid, round(snap[ukey], 2)])
            hydro_net_points.append([ts_mid, round(sums["fc"] - sums["elec"], 2)])

        # 若历史库尚无采样点，回退注入当前实时帧，避免新库首次打开图面空置
        if not power_series["load"]:
            power_series["wind"].append([now_ms, round(max(0.0, float(rt["wind_kw"])), 2)])
            power_series["pv"].append([now_ms, round(max(0.0, float(rt["pv_kw"])), 2)])
            power_series["diesel"].append([now_ms, round(max(0.0, float(rt["diesel_kw"])), 2)])
            power_series["storage"].append([now_ms, round(float(rt["storage_kw"]), 2)])
            power_series["load"].append([now_ms, round(max(0.0, float(rt["total_load_kw"])), 2)])
            hydro_net_points.append(
                [now_ms, round(float(rt["hydrogen_gen_kw"]) - float(rt["hydrogen_prod_kw"]), 2)]
            )

        storage_schedule: list[dict[str, Any]] = []
        for ukey in chosen_units:
            storage_schedule.append({
                "key": f"{ukey[0]}:{ukey[1]}",
                "label": storage_units[ukey],
                "points": unit_points[ukey],
            })
        storage_schedule.append({
            "key": "hydrogen:net",
            "label": "氢能(氢电+/制氢-)",
            "points": hydro_net_points,
        })

        # 3. 极地微电网专属KPI：累计供电/负荷/节油/减碳、电氢孤网自持续航、电源结构占比
        wind_wkwh = float(kpis["wind_total_wkwh"])
        pv_wkwh = float(kpis["pv_total_wkwh"])
        diesel_wkwh = float(kpis["diesel_total_wkwh"])
        hydro_wkwh = float(kpis["hydro_total_wkwh"])
        storage_wkwh = float(kpis["storage_total_wkwh"])
        renew_wkwh = float(kpis["renewable_total_wkwh"])
        total_supply_wkwh = round(renew_wkwh + diesel_wkwh, 2)
        diesel_saved_tons = float(kpis["diesel_saved_tons"])
        co2_reduced_tons = round(diesel_saved_tons * 3.15, 2)
        wp_sum = max(wind_wkwh + pv_wkwh, 0.01)
        wind_saved_tons = round(diesel_saved_tons * (wind_wkwh / wp_sum), 2)
        pv_saved_tons = round(max(0.0, diesel_saved_tons - wind_saved_tons), 2)
        diesel_replace_pct = round(renew_wkwh / max(total_supply_wkwh, 0.01) * 100.0, 1)

        # 估算电储 + 氢储联合孤网自持续航小时数
        es_soc = float(rt.get("storage_soc_pct") or 0.0)
        h2_soc = float(rt.get("hydro_soc_pct") or 0.0)
        es_cap_kwh = max(400.0, len(storage_units) * 120.0)
        h2_cap_kwh = 1500.0
        reserve_kwh = es_cap_kwh * max(0.0, es_soc - 10.0) / 100.0 + h2_cap_kwh * max(0.0, h2_soc - 5.0) / 100.0
        base_load_kw = max(float(rt.get("total_load_kw") or 0.0), 15.0)
        autonomy_hours = round(reserve_kwh / base_load_kw, 1)

        # 电源结构占比（风电 / 光伏 / 柴发 / 电储氢能）
        mix_total = max(wind_wkwh + pv_wkwh + diesel_wkwh + storage_wkwh + hydro_wkwh, 0.01)
        source_mix = [
            {
                "key": "wind",
                "label": "风电",
                "value_wkwh": round(wind_wkwh, 2),
                "pct": round(wind_wkwh / mix_total * 100.0, 1),
            },
            {
                "key": "pv",
                "label": "光伏",
                "value_wkwh": round(pv_wkwh, 2),
                "pct": round(pv_wkwh / mix_total * 100.0, 1),
            },
            {
                "key": "diesel",
                "label": "柴发保供",
                "value_wkwh": round(diesel_wkwh, 2),
                "pct": round(diesel_wkwh / mix_total * 100.0, 1),
            },
            {
                "key": "storage_h2",
                "label": "电储与氢电",
                "value_wkwh": round(storage_wkwh + hydro_wkwh, 2),
                "pct": round((storage_wkwh + hydro_wkwh) / mix_total * 100.0, 1),
            },
        ]

        # 6 大极地实景枢纽节点
        wind_cnt = int(nodes["dc_wind"]["count"]) + int(nodes["ac_wind"]["count"])
        wind_on = int(nodes["dc_wind"]["online"]) + int(nodes["ac_wind"]["online"])
        pv_cnt = int(nodes["dc_pv"]["count"]) + int(nodes["ac_pv"]["count"])
        pv_on = int(nodes["dc_pv"]["online"]) + int(nodes["ac_pv"]["online"])
        es_cnt = int(nodes["dc_storage"]["count"]) + int(nodes["ac_storage"]["count"])
        es_on = int(nodes["dc_storage"]["online"]) + int(nodes["ac_storage"]["online"])
        es_p = float(rt["storage_kw"])
        es_state = "discharge" if es_p > 0.2 else ("charge" if es_p < -0.2 else "idle")
        h2_prod = float(rt["hydrogen_prod_kw"])
        h2_gen = float(rt["hydrogen_gen_kw"])

        result = {
            "timestamp": overview["timestamp"],
            "hours": span_hours,
            "since_ms": since_ms,
            "now_ms": now_ms,
            "station_name": overview["station_name"],
            "running_days": kpis["running_days"],
            "cumulative": {
                "total_supply_wkwh": total_supply_wkwh,
                "total_load_wkwh": round(float(kpis["load_total_wkwh"]), 2),
                "diesel_saved_tons": round(diesel_saved_tons, 2),
                "co2_reduced_tons": co2_reduced_tons,
            },
            "resilience": {
                "autonomy_hours": autonomy_hours,
                "diesel_replace_pct": diesel_replace_pct,
                "wind_saved_tons": wind_saved_tons,
                "pv_saved_tons": pv_saved_tons,
                "hydro_soc_pct": round(h2_soc, 1),
                "hydro_pressure_mpa": rt.get("hydro_pressure_mpa"),
            },
            "source_mix": source_mix,
            "hub": {
                "green_ratio_pct": round(float(summary["green_ratio_pct"]), 1),
                "total_gen_kw": round(float(summary["total_gen_kw"]), 1),
                "total_load_kw": round(float(summary["total_load_kw"]), 1),
                "nodes": {
                    "wind": {
                        "p_kw": round(float(rt["wind_kw"]), 1),
                        "online": wind_on,
                        "count": wind_cnt,
                        "wind_speed_ms": env.get("wind_speed_ms"),
                    },
                    "pv": {
                        "p_kw": round(float(rt["pv_kw"]), 1),
                        "online": pv_on,
                        "count": pv_cnt,
                        "irradiance_wm2": env.get("irradiance_wm2"),
                    },
                    "storage": {
                        "p_kw": round(es_p, 1),
                        "soc_pct": round(es_soc, 1),
                        "state": es_state,
                        "online": es_on,
                        "count": es_cnt,
                    },
                    "load": {
                        "p_kw": round(float(rt["total_load_kw"]), 1),
                        "dc_kw": round(float(nodes["dc_load"]["p_kw"]), 1),
                        "ac_kw": round(float(nodes["ac_load"]["p_kw"]), 1),
                    },
                    "hydrogen": {
                        "prod_kw": round(h2_prod, 1),
                        "gen_kw": round(h2_gen, 1),
                        "soc_pct": round(h2_soc, 1),
                        "pressure_mpa": rt.get("hydro_pressure_mpa"),
                    },
                    "diesel": {
                        "p_kw": round(float(rt["diesel_kw"]), 1),
                        "online": int(nodes["diesel"]["online"]),
                        "count": int(nodes["diesel"]["count"]),
                    },
                },
            },
            "polar_stats": {
                "green_ratio_pct": round(float(summary["green_ratio_pct"]), 1),
                "temperature_c": env.get("temperature_c"),
                "wind_speed_ms": env.get("wind_speed_ms"),
                "irradiance_wm2": env.get("irradiance_wm2"),
                "dc_voltage_v": env.get("dc_voltage_v"),
                "ac_voltage_v": env.get("ac_voltage_v"),
                "rtu_online": summary["rtu_online"],
                "rtu_total": summary["rtu_total"],
                "alarm_unacked": summary["alarm_unacked"],
                "collector_running": summary["collector_running"],
            },
            "renewables": {
                "wind": {
                    "total_wkwh": round(wind_wkwh, 2),
                    "today_kwh": round(today_wind_kwh, 1),
                    "live_kw": round(float(rt["wind_kw"]), 1),
                    "online": wind_on,
                    "count": wind_cnt,
                },
                "pv": {
                    "total_wkwh": round(pv_wkwh, 2),
                    "today_kwh": round(today_pv_kwh, 1),
                    "live_kw": round(float(rt["pv_kw"]), 1),
                    "online": pv_on,
                    "count": pv_cnt,
                },
            },
            "power_curves": [
                {"key": "wind", "label": "风电", "points": power_series["wind"]},
                {"key": "pv", "label": "光伏", "points": power_series["pv"]},
                {"key": "diesel", "label": "柴发", "points": power_series["diesel"]},
                {"key": "storage", "label": "储能", "points": power_series["storage"]},
                {"key": "load", "label": "总负荷", "points": power_series["load"]},
            ],
            "storage_schedule": storage_schedule,
            "derived": compute_polar_derived_metrics(
                rt=rt,
                env=env,
                model_params_map=self._read_model_params()[0],
                devices=devices,
            ),
            "data_quality": overview.get("data_quality") or {},
            "health": overview["health"],
        }
        with self._cache_lock:
            self._cockpit_cache[span_hours] = (time.monotonic(), result)
        return result

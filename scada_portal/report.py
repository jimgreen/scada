"""SCADA 综合门户数据质量聚合、极地特色衍生指标与站级运行日报生成。"""
from __future__ import annotations

import csv
import html
import io
import math
from typing import Any


# 南极秦岭站（罗斯海恩克斯堡岛）典型气象与环境工况预设
POLAR_WEATHER_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "key": "live",
        "label": "现场实测",
        "mode": "measured",
        "temperature_c": -12.0,
        "wind_speed_ms": 8.5,
        "irradiance_wm2": 260.0,
        "pressure_hpa": 985.0,
        "icing_level": 0,
        "snow_condition": "clear",
    },
    {
        "key": "summer_sunny",
        "label": "极昼晴朗",
        "mode": "manual",
        "temperature_c": -4.0,
        "wind_speed_ms": 8.5,
        "irradiance_wm2": 780.0,
        "pressure_hpa": 988.0,
        "icing_level": 0,
        "snow_condition": "clear",
    },
    {
        "key": "katabatic_gale",
        "label": "强下降风",
        "mode": "manual",
        "temperature_c": -22.0,
        "wind_speed_ms": 26.5,
        "irradiance_wm2": 180.0,
        "pressure_hpa": 968.0,
        "icing_level": 1,
        "snow_condition": "blowing_snow",
    },
    {
        "key": "polar_night_cold",
        "label": "极夜极寒",
        "mode": "manual",
        "temperature_c": -38.0,
        "wind_speed_ms": 6.0,
        "irradiance_wm2": 0.0,
        "pressure_hpa": 978.0,
        "icing_level": 2,
        "snow_condition": "overcast",
    },
    {
        "key": "blizzard_icing",
        "label": "暴雪重冰",
        "mode": "manual",
        "temperature_c": -31.0,
        "wind_speed_ms": 21.0,
        "irradiance_wm2": 35.0,
        "pressure_hpa": 962.0,
        "icing_level": 3,
        "snow_condition": "blizzard",
    },
    {
        "key": "calm_overcast",
        "label": "静风阴天",
        "mode": "manual",
        "temperature_c": -14.0,
        "wind_speed_ms": 2.2,
        "irradiance_wm2": 95.0,
        "pressure_hpa": 992.0,
        "icing_level": 0,
        "snow_condition": "overcast",
    },
)

_PRESET_MAP = {p["key"]: p for p in POLAR_WEATHER_PRESETS}
_VALID_SNOW = frozenset({"clear", "overcast", "blowing_snow", "blizzard"})
_ICING_LABELS = {0: "无覆冰", 1: "轻度覆冰", 2: "中度覆冰", 3: "重度覆冰"}
_SNOW_LABELS = {
    "clear": "晴朗",
    "overcast": "阴云",
    "blowing_snow": "吹雪",
    "blizzard": "暴雪积雪",
}


def default_weather_profile() -> dict[str, Any]:
    """返回默认现场实测优先的气象工况配置。"""
    return {
        "mode": "measured",
        "preset": "live",
        "temperature_c": -12.0,
        "wind_speed_ms": None,
        "irradiance_wm2": None,
        "pressure_hpa": 985.0,
        "icing_level": 0,
        "snow_condition": "clear",
        "updated_ms": 0,
        "operator": "",
    }


def validate_weather_payload(payload: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """校验并归一化前端提交的气象与环境工况输入。"""
    base = dict(current or default_weather_profile())
    preset = str(payload.get("preset") if payload.get("preset") is not None else base.get("preset") or "live").strip()
    if preset not in _PRESET_MAP and preset != "custom":
        raise ValueError(f"不支持的气象工况预设：{preset}")

    # 选定内置预设且未显式覆盖模式时，自动同步对应模式与默认物理量
    if preset in _PRESET_MAP and payload.get("preset") is not None:
        pdef = _PRESET_MAP[preset]
        if payload.get("mode") is None:
            base["mode"] = pdef["mode"]
        for k in ("temperature_c", "wind_speed_ms", "irradiance_wm2", "pressure_hpa", "icing_level", "snow_condition"):
            if k not in payload:
                base[k] = pdef[k]

    mode = str(payload.get("mode") if payload.get("mode") is not None else base.get("mode") or "measured").strip().lower()
    if mode not in {"measured", "manual"}:
        raise ValueError("气象数据源模式仅支持 measured（现场实测）或 manual（工况推演）")

    def _opt_num(key: str, lo: float, hi: float, allow_none: bool = False) -> float | None:
        raw = payload[key] if key in payload else base.get(key)
        if raw is None or raw == "":
            if allow_none:
                return None
            return float(default_weather_profile()[key])
        if isinstance(raw, bool):
            raise ValueError(f"{key} 必须为数值")
        try:
            v = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须为有限数值") from exc
        if not math.isfinite(v) or v < lo or v > hi:
            raise ValueError(f"{key} 超出有效范围 [{lo}, {hi}]")
        return round(v, 2)

    temp_c = _opt_num("temperature_c", -65.0, 20.0, allow_none=(mode == "measured"))
    wind_ms = _opt_num("wind_speed_ms", 0.0, 60.0, allow_none=(mode == "measured"))
    irr_wm2 = _opt_num("irradiance_wm2", 0.0, 1400.0, allow_none=(mode == "measured"))
    pres_hpa = _opt_num("pressure_hpa", 900.0, 1050.0, allow_none=False)

    raw_icing = payload.get("icing_level") if "icing_level" in payload else base.get("icing_level", 0)
    if isinstance(raw_icing, bool):
        raise ValueError("icing_level 必须为 0~3 的整数")
    try:
        icing = int(raw_icing)
    except (TypeError, ValueError) as exc:
        raise ValueError("icing_level 必须为 0~3 的整数") from exc
    if icing < 0 or icing > 3:
        raise ValueError("icing_level 超出有效范围 [0, 3]")

    snow = str(payload.get("snow_condition") if "snow_condition" in payload else base.get("snow_condition") or "clear").strip().lower()
    if snow not in _VALID_SNOW:
        raise ValueError(f"不支持的天气/积雪工况：{snow}")

    if mode == "measured" and preset != "live":
        preset = "live"
    elif mode == "manual" and preset == "live":
        preset = "custom"

    return {
        "mode": mode,
        "preset": preset,
        "temperature_c": temp_c,
        "wind_speed_ms": wind_ms,
        "irradiance_wm2": irr_wm2,
        "pressure_hpa": float(pres_hpa if pres_hpa is not None else 985.0),
        "icing_level": icing,
        "snow_condition": snow,
        "operator": str(payload.get("operator") or base.get("operator") or "").strip()[:64],
    }


def merge_effective_weather(measured: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """将现场实测环境值与人工/预设工况配置合并为大屏统一生效的气象视图。"""
    mode = profile.get("mode") or "measured"
    m_temp = measured.get("temperature_c")
    m_wind = measured.get("wind_speed_ms")
    m_irr = measured.get("irradiance_wm2")

    if mode == "manual":
        eff_temp = profile.get("temperature_c") if profile.get("temperature_c") is not None else (m_temp if m_temp is not None else -12.0)
        eff_wind = profile.get("wind_speed_ms") if profile.get("wind_speed_ms") is not None else (m_wind if m_wind is not None else 8.5)
        eff_irr = profile.get("irradiance_wm2") if profile.get("irradiance_wm2") is not None else (m_irr if m_irr is not None else 200.0)
        temp_src = "manual"
        wind_src = "manual"
        irr_src = "manual"
    else:
        eff_temp = m_temp if m_temp is not None else (profile.get("temperature_c") if profile.get("temperature_c") is not None else -12.0)
        eff_wind = m_wind if m_wind is not None else (profile.get("wind_speed_ms") if profile.get("wind_speed_ms") is not None else 0.0)
        eff_irr = m_irr if m_irr is not None else (profile.get("irradiance_wm2") if profile.get("irradiance_wm2") is not None else 0.0)
        temp_src = "measured" if m_temp is not None else "fallback"
        wind_src = "measured" if m_wind is not None else "fallback"
        irr_src = "measured" if m_irr is not None else "fallback"

    pres_hpa = float(profile.get("pressure_hpa") or 985.0)
    icing = int(profile.get("icing_level") or 0)
    snow = str(profile.get("snow_condition") or "clear")
    if snow not in _VALID_SNOW:
        snow = "clear"

    # 计算风寒体感温度（Wind Chill，适用 T <= 10°C 且 v >= 1.34 m/s）
    v_kmh = max(0.0, float(eff_wind)) * 3.6
    t_f = float(eff_temp)
    if t_f <= 10.0 and v_kmh >= 4.8:
        wind_chill_c = 13.12 + 0.6215 * t_f - 11.37 * (v_kmh ** 0.16) + 0.3965 * t_f * (v_kmh ** 0.16)
    else:
        wind_chill_c = t_f

    # 计算极地空气密度 ρ = P / (Rd * T_K)
    air_density = (pres_hpa * 100.0) / (287.05 * max(180.0, t_f + 273.15))

    preset_key = str(profile.get("preset") or ("live" if mode == "measured" else "custom"))
    preset_label = _PRESET_MAP[preset_key]["label"] if preset_key in _PRESET_MAP else "自定义工况"

    return {
        "mode": mode,
        "preset": preset_key,
        "preset_label": preset_label,
        "temperature_c": round(t_f, 1),
        "wind_chill_c": round(wind_chill_c, 1),
        "wind_speed_ms": round(max(0.0, float(eff_wind)), 2),
        "irradiance_wm2": round(max(0.0, float(eff_irr)), 1),
        "pressure_hpa": round(pres_hpa, 1),
        "air_density_kgm3": round(air_density, 3),
        "icing_level": icing,
        "icing_label": _ICING_LABELS.get(icing, "无覆冰"),
        "snow_condition": snow,
        "snow_label": _SNOW_LABELS.get(snow, "晴朗"),
        "sources": {
            "temperature": temp_src,
            "wind_speed": wind_src,
            "irradiance": irr_src,
        },
        "dc_voltage_v": measured.get("dc_voltage_v"),
        "ac_voltage_v": measured.get("ac_voltage_v"),
    }


def compute_polar_weather_coupling(
    *,
    effective: dict[str, Any],
    wind_rated_kw: float,
    pv_rated_kw: float,
    base_load_kw: float,
    base_autonomy_h: float,
) -> dict[str, Any]:
    """计算南极极端气象与风电、光伏、电储能、站区采暖及氢能系统的实时物理耦合影响。"""
    t_c = float(effective.get("temperature_c") if effective.get("temperature_c") is not None else -12.0)
    wc_c = float(effective.get("wind_chill_c") if effective.get("wind_chill_c") is not None else t_c)
    ws = max(0.0, float(effective.get("wind_speed_ms") or 0.0))
    irr = max(0.0, float(effective.get("irradiance_wm2") or 0.0))
    rho = float(effective.get("air_density_kgm3") or 1.29)
    icing = int(effective.get("icing_level") or 0)
    snow = str(effective.get("snow_condition") or "clear")

    w_rated = max(float(wind_rated_kw or 0.0), 100.0)
    p_rated = max(float(pv_rated_kw or 0.0), 100.0)

    # 1. 风电耦合：极地高空气密度增益 × 叶片覆冰气动折减 × 切入/超速切出保护
    rho_factor = min(1.22, max(0.85, rho / 1.225))
    icing_factor = {0: 1.0, 1: 0.88, 2: 0.72, 3: 0.45}.get(icing, 1.0)
    if ws >= 25.0:
        wind_avail_pct = 0.0
        wind_est_kw = 0.0
        wind_state = "超速切出"
        wind_level = "danger"
    elif ws < 3.0:
        wind_avail_pct = 0.0
        wind_est_kw = 0.0
        wind_state = "低风待机"
        wind_level = "muted"
    else:
        curve = min(1.0, ((ws - 3.0) / 9.0) ** 3)
        eff_factor = min(1.0, curve * rho_factor * icing_factor)
        wind_avail_pct = round(eff_factor * 100.0, 1)
        wind_est_kw = round(w_rated * eff_factor, 1)
        if icing >= 2:
            wind_state = "覆冰降容"
            wind_level = "warn"
        elif rho_factor >= 1.08:
            wind_state = "高密增效"
            wind_level = "ok"
        else:
            wind_state = "正常发电"
            wind_level = "ok"

    # 2. 光伏耦合：低温电池效率增益（-0.38%/°C 相对 25°C）× 吹雪/暴雪积雪遮挡折减
    pv_temp_factor = min(1.24, max(0.85, 1.0 + (25.0 - t_c) * 0.0038))
    snow_factor = {"clear": 1.0, "overcast": 0.96, "blowing_snow": 0.75, "blizzard": 0.30}.get(snow, 1.0)
    pv_eff_pct = round(pv_temp_factor * snow_factor * 100.0, 1)
    pv_est_kw = round(p_rated * min(1.15, (irr / 1000.0) * pv_temp_factor * snow_factor), 1)
    if irr < 5.0:
        pv_state = "极夜无光"
        pv_level = "muted"
    elif snow == "blizzard":
        pv_state = "暴雪遮挡"
        pv_level = "warn"
    elif snow == "blowing_snow":
        pv_state = "吹雪衰减"
        pv_level = "warn"
    elif irr >= 600.0:
        pv_state = "极昼高发"
        pv_level = "ok"
    else:
        pv_state = "低温增效" if t_c <= -15.0 else "常规出力"
        pv_level = "ok"

    # 3. 电化学储能低温耦合：容量保持率与电池舱保温加热附加功耗
    if t_c >= -5.0:
        bess_cap_pct = 98.0
    elif t_c >= -20.0:
        bess_cap_pct = 98.0 - (-5.0 - t_c) * 0.8
    elif t_c >= -40.0:
        bess_cap_pct = 86.0 - (-20.0 - t_c) * 1.05
    else:
        bess_cap_pct = max(55.0, 65.0 - (-40.0 - t_c) * 0.5)
    bess_cap_pct = round(bess_cap_pct, 1)
    bess_heat_kw = round(max(0.0, (-5.0 - t_c) * 0.45 + (ws * 0.12 if t_c < -10.0 else 0.0)), 1)
    bess_level = "danger" if bess_cap_pct < 72.0 else ("warn" if bess_cap_pct < 85.0 else "ok")

    # 4. 站区围护风冷散热与采暖负荷增量（相对 -10°C 基准工况）
    cold_excess = max(0.0, -10.0 - wc_c)
    heating_delta_kw = round(cold_excess * 1.45 + (8.0 if snow == "blizzard" else (3.5 if snow == "blowing_snow" else 0.0)), 1)
    h2_trace_heat_kw = round(max(0.0, (-12.0 - t_c) * 0.25), 1)
    coupled_load_kw = round(max(15.0, float(base_load_kw or 0.0)) + heating_delta_kw + bess_heat_kw + h2_trace_heat_kw, 1)

    # 修正极寒下的电氢联合孤网自持时间
    base_l = max(15.0, float(base_load_kw or 15.0))
    autonomy_ratio = (bess_cap_pct / 100.0) * (base_l / max(15.0, coupled_load_kw))
    coupled_autonomy_h = round(max(0.2, float(base_autonomy_h or 0.0) * autonomy_ratio), 1)

    # 5. 综合极地气候风险与调度建议短标签
    alert_level = "normal"
    strategy = "绿电优先消纳"
    if ws >= 25.0:
        alert_level = "extreme"
        strategy = "风机超速切出 · 柴储顶峰保供"
    elif (icing >= 3) or (snow == "blizzard") or (t_c <= -32.0):
        alert_level = "extreme"
        strategy = "极寒重冰保供 · 启动采暖与柴发备载"
    elif (ws >= 17.2) or (t_c <= -24.0) or (icing == 2):
        alert_level = "elevated"
        strategy = "大风寒潮关注 · 预留储能热备"
    elif irr < 5.0 and ws < 3.5:
        alert_level = "elevated"
        strategy = "无风无光低出力 · 氢电柴发协同"
    elif irr >= 650.0 and ws >= 6.0:
        alert_level = "normal"
        strategy = "极昼风光充沛 · 制氢储能消纳"

    return {
        "alert_level": alert_level,
        "strategy": strategy,
        "wind": {
            "air_density_kgm3": round(rho, 3),
            "rho_gain_pct": round((rho_factor - 1.0) * 100.0, 1),
            "icing_derate_pct": round((1.0 - icing_factor) * 100.0, 1),
            "avail_pct": wind_avail_pct,
            "est_kw": wind_est_kw,
            "state": wind_state,
            "level": wind_level,
        },
        "pv": {
            "eff_pct": pv_eff_pct,
            "snow_derate_pct": round((1.0 - snow_factor) * 100.0, 1),
            "est_kw": pv_est_kw,
            "state": pv_state,
            "level": pv_level,
        },
        "bess": {
            "cap_retention_pct": bess_cap_pct,
            "heat_kw": bess_heat_kw,
            "level": bess_level,
        },
        "thermal": {
            "wind_chill_c": round(wc_c, 1),
            "heating_delta_kw": heating_delta_kw,
            "h2_trace_heat_kw": h2_trace_heat_kw,
            "coupled_load_kw": coupled_load_kw,
            "coupled_autonomy_hours": coupled_autonomy_h,
        },
    }


def summarize_data_quality(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总全站设备的数据质量状态，供总览、大屏与顶栏向上传导陈旧/离线风险。"""
    offline_devices: list[dict[str, Any]] = []
    stale_devices: list[dict[str, Any]] = []
    overlimit_devices: list[dict[str, Any]] = []
    disabled_points = 0
    total_bound = 0
    total_valid = 0

    for dev in devices:
        bound = int(dev.get("bound_count") or 0)
        valid = int(dev.get("valid_count") or 0)
        total_bound += bound
        total_valid += valid
        dev_stale = False
        dev_overlimit = False
        for m in dev.get("metrics") or ():
            if m.get("disabled"):
                disabled_points += 1
                continue
            q = m.get("quality")
            if q == 2:
                dev_stale = True
            elif q in (3, 4):
                dev_overlimit = True
        brief = {
            "dev_id": dev.get("dev_id"),
            "name": dev.get("name"),
            "category": dev.get("category"),
            "valid_count": valid,
            "bound_count": bound,
        }
        if bound > 0 and valid == 0:
            offline_devices.append(brief)
        elif dev_stale:
            stale_devices.append(brief)
        if dev_overlimit:
            overlimit_devices.append(brief)

    health_pct = round((total_valid / total_bound) * 100.0, 1) if total_bound > 0 else 100.0
    level = "ok"
    if offline_devices or len(stale_devices) >= 3:
        level = "fault"
    elif stale_devices or overlimit_devices or disabled_points > 0:
        level = "warn"

    return {
        "level": level,
        "health_pct": health_pct,
        "total_bound": total_bound,
        "total_valid": total_valid,
        "disabled_points": disabled_points,
        "offline_count": len(offline_devices),
        "stale_count": len(stale_devices),
        "overlimit_count": len(overlimit_devices),
        "offline_devices": offline_devices[:20],
        "stale_devices": stale_devices[:20],
        "overlimit_devices": overlimit_devices[:20],
    }


def compute_polar_derived_metrics(
    *,
    rt: dict[str, Any],
    env: dict[str, Any],
    model_params_map: dict[str, list[dict[str, Any]]],
    devices: list[dict[str, Any]],
) -> dict[str, Any]:
    """计算极地微电网特色衍生指标：柴油续航、氢能全链路效率、储氢安全区、风光限电估算。"""
    # 1. 统计柴发总油箱容量与平均油耗率（来自模型库）
    total_tank_l = 0.0
    sfc_list: list[float] = []
    wind_rated_kw = 0.0
    pv_rated_kw = 0.0
    for dev in devices:
        dev_id = dev.get("dev_id") or ""
        cat = dev.get("category")
        params = model_params_map.get(dev_id) or []
        for p in params:
            k = p.get("key")
            raw_v = str(p.get("value") or "").split()[0]
            try:
                num = float(raw_v)
            except (TypeError, ValueError):
                continue
            if k == "fuel_tank_capacity" and num > 0:
                total_tank_l += num
            elif k == "specific_fuel_consumption" and num > 0:
                sfc_list.append(num)
            elif k == "rated_capacity" and num > 0:
                if cat == "wind":
                    wind_rated_kw += num
                elif cat == "pv":
                    pv_rated_kw += num

    # 柴油储备续航（若单机油箱偏小则叠加站级主储油罐基准 15,000 L，按 0.84 kg/L 折算）
    reserve_fuel_l = max(total_tank_l, 15000.0)
    avg_sfc_g_kwh = (sum(sfc_list) / len(sfc_list)) if sfc_list else 210.0
    diesel_kw = max(0.0, float(rt.get("diesel_kw") or 0.0))
    if diesel_kw > 0.5:
        fuel_rate_l_per_h = (diesel_kw * avg_sfc_g_kwh) / 840.0
        diesel_endurance_h = round(reserve_fuel_l / max(0.1, fuel_rate_l_per_h), 1)
    else:
        diesel_endurance_h = round(reserve_fuel_l / max(1.0, (max(50.0, float(rt.get("total_load_kw") or 100.0)) * avg_sfc_g_kwh) / 840.0), 1)

    # 2. 氢能电→氢→电往返效率（工程标称值结合实时工况估算）
    h2_prod_kw = max(0.0, float(rt.get("hydrogen_prod_kw") or 0.0))
    h2_gen_kw = max(0.0, float(rt.get("hydrogen_gen_kw") or 0.0))
    el_eff = 68.0 if h2_prod_kw > 1.0 else 65.0
    fc_eff = 55.0 if h2_gen_kw > 1.0 else 52.0
    h2_roundtrip_eff = round((el_eff * fc_eff) / 100.0, 1)

    # 3. 储氢压力安全区判定（常规安全工作区间 1.5 ~ 8.5 MPa）
    pressure = rt.get("hydro_pressure_mpa")
    if pressure is None:
        h2_pressure_zone = "unknown"
    elif float(pressure) < 1.5:
        h2_pressure_zone = "low"
    elif float(pressure) > 8.5:
        h2_pressure_zone = "high"
    else:
        h2_pressure_zone = "safe"

    # 4. 风光理论出力与限电/弃电估算
    wind_kw = max(0.0, float(rt.get("wind_kw") or 0.0))
    pv_kw = max(0.0, float(rt.get("pv_kw") or 0.0))
    wind_speed = env.get("wind_speed_ms")
    irr = env.get("irradiance_wm2")
    avail_wind = wind_kw
    if wind_speed is not None and wind_rated_kw > 0:
        ws = max(0.0, float(wind_speed))
        if ws >= 3.0 and ws <= 25.0:
            ratio = min(1.0, ((ws - 3.0) / 9.0) ** 3)
            avail_wind = max(wind_kw, wind_rated_kw * ratio)
    avail_pv = pv_kw
    if irr is not None and pv_rated_kw > 0:
        avail_pv = max(pv_kw, pv_rated_kw * min(1.0, max(0.0, float(irr)) / 1000.0))
    curtailment_kw = round(max(0.0, (avail_wind - wind_kw) + (avail_pv - pv_kw)), 1)

    # 5. 极地气候状态指示（寒潮/大风保供级别）
    temp_c = env.get("temperature_c")
    weather_alert = "normal"
    if (wind_speed is not None and float(wind_speed) >= 17.2) or (temp_c is not None and float(temp_c) <= -30.0):
        weather_alert = "extreme"
    elif (wind_speed is not None and float(wind_speed) >= 10.8) or (temp_c is not None and float(temp_c) <= -20.0):
        weather_alert = "elevated"

    return {
        "diesel_endurance_hours": diesel_endurance_h,
        "diesel_reserve_liters": round(reserve_fuel_l, 0),
        "h2_roundtrip_eff_pct": h2_roundtrip_eff,
        "h2_pressure_zone": h2_pressure_zone,
        "curtailment_kw": curtailment_kw,
        "weather_alert": weather_alert,
        "wind_rated_kw": round(wind_rated_kw, 1),
        "pv_rated_kw": round(pv_rated_kw, 1),
    }


def render_daily_report_csv(report: dict[str, Any]) -> bytes:
    """将站级运行日报序列化为带 BOM 的 UTF-8 CSV，便于 Excel 直接打开。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["南极秦岭站综合能量管理系统 · 站级运行日报", report.get("date", "")])
    writer.writerow(["生成时间", report.get("generated_at", "")])
    writer.writerow([])
    writer.writerow(["指标类别", "指标名称", "数值", "单位"])
    for row in report.get("kpis") or ():
        writer.writerow([row.get("group", ""), row.get("name", ""), row.get("value", ""), row.get("unit", "")])
    writer.writerow([])
    writer.writerow(["设备名称", "设备类别", "表名#编号", "有效/绑定量测", "实时有功 (kW)", "运行/开关状态", "活跃告警数"])
    for dev in report.get("devices") or ():
        writer.writerow([
            dev.get("name", ""),
            dev.get("category", ""),
            f"{dev.get('table_name', '')}#{dev.get('idx', '')}",
            f"{dev.get('valid_count', 0)}/{dev.get('bound_count', 0)}",
            dev.get("p_kw", ""),
            dev.get("status_text", ""),
            dev.get("alarm_count", 0),
        ])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def render_daily_report_html(report: dict[str, Any]) -> bytes:
    """生成高可读性可打印 HTML 站级运行日报（支持浏览器 Ctrl+P 另存为 PDF）。"""
    esc = html.escape
    kpi_rows = "".join(
        f"<tr><td>{esc(str(r.get('group', '')))}</td><td>{esc(str(r.get('name', '')))}</td>"
        f"<td class='num'><strong>{esc(str(r.get('value', '')))}</strong></td><td>{esc(str(r.get('unit', '')))}</td></tr>"
        for r in (report.get("kpis") or ())
    )
    dev_rows = "".join(
        f"<tr><td>{esc(str(d.get('name', '')))}</td><td>{esc(str(d.get('category', '')))}</td>"
        f"<td class='mono'>{esc(str(d.get('table_name', '')))}#{esc(str(d.get('idx', '')))}</td>"
        f"<td class='num'>{esc(str(d.get('valid_count', 0)))}/{esc(str(d.get('bound_count', 0)))}</td>"
        f"<td class='num'>{esc(str(d.get('p_kw', '—')))}</td>"
        f"<td>{esc(str(d.get('status_text', '—')))}</td>"
        f"<td class='num'>{esc(str(d.get('alarm_count', 0)))}</td></tr>"
        for d in (report.get("devices") or ())
    )
    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>运行日报 - {esc(str(report.get('date', '')))}</title>
<style>
  body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; color: #0f172a; margin: 24px; font-size: 13px; }}
  h1 {{ margin: 0 0 6px; font-size: 20px; }}
  .meta {{ color: #475569; margin-bottom: 18px; font-size: 12px; }}
  h2 {{ margin: 18px 0 8px; font-size: 15px; border-left: 3px solid #0284c7; padding-left: 8px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 6px 8px; text-align: left; }}
  th {{ background: #f1f5f9; font-weight: 600; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; font-family: Consolas, monospace; }}
  .mono {{ font-family: Consolas, monospace; font-size: 12px; color: #334155; }}
  @media print {{ .no-print {{ display: none; }} body {{ margin: 10mm; }} }}
</style>
</head>
<body>
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <div>
      <h1>{esc(str(report.get('station_name', '南极秦岭站综合能量管理系统')))} · 站级运行日报</h1>
      <div class="meta">统计日期：{esc(str(report.get('date', '')))} &nbsp;|&nbsp; 生成时间：{esc(str(report.get('generated_at', '')))} &nbsp;|&nbsp; 数据健康率：{esc(str(report.get('quality_health_pct', 100)))}%</div>
    </div>
    <button class="no-print" onclick="window.print()" style="padding:6px 14px;cursor:pointer;">打印 / 另存 PDF</button>
  </div>
  <h2>一、站级运行核心指标</h2>
  <table><thead><tr><th>类别</th><th>指标</th><th class="num">数值</th><th>单位</th></tr></thead><tbody>{kpi_rows}</tbody></table>
  <h2>二、关键设备运行台账（共 {len(report.get('devices') or ())} 台）</h2>
  <table><thead><tr><th>设备名称</th><th>类别</th><th>模型主键</th><th class="num">量测有效</th><th class="num">实时有功 (kW)</th><th>状态</th><th class="num">告警</th></tr></thead><tbody>{dev_rows}</tbody></table>
</body>
</html>"""
    return doc.encode("utf-8")

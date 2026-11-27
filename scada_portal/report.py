"""SCADA 综合门户数据质量聚合、极地特色衍生指标与站级运行日报生成。"""
from __future__ import annotations

import csv
import html
import io
from typing import Any


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

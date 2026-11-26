"use strict";

(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // 默认未显式选过主题时启用科幻·电光青，与南极秦岭站底图统一
  try {
    if (!localStorage.getItem("scada_ui_theme")) {
      document.documentElement.dataset.theme = "sci-fi-cyan";
      localStorage.setItem("scada_ui_theme", "sci-fi-cyan");
    }
  } catch (_) {}

  const SERIES_COLORS_DARK = ["#38BDF8", "#34D399", "#FBBF24", "#FB7185", "#A78BFA", "#2DD4BF", "#F97316"];
  const SERIES_COLORS_LIGHT = ["#2563EB", "#0D9488", "#D97706", "#DC2626", "#7C3AED", "#0284C7", "#EA580C"];

  const state = {
    view: "home",
    refreshMs: 5000,
    timer: null,
    refreshing: false,
    // 潮流图状态
    svgLoaded: false,
    svgWidth: 2518,
    svgHeight: 2143,
    pfScale: 0.4,
    pfX: 0,
    pfY: 0,
    pfData: null,
    pfSelectedDev: null,
    pfHoverDev: null,
    pfHoverMeas: null, // { devId, mt, slot }
    pfMpGran: "hour", // "minute" | "hour" | "day"
    pfMpOffset: 0, // 0=当前，-1=上一个单位
    pfMpWindow: null, // { startMs, endMs, label, gran }
    pfMpReqSeq: 0,
    pfMpTrendCache: new Map(), // key -> { ts, points }
    pfMpPoints: [],
    pfMpHoverX: null,
    pfPopHideTimer: null,
    pfShowArrows: true,
    pfArrowPaths: [], // [{ key, kind, voltageType, pathEl, length, pKw, dir, speed, scale, arrows: [{el, offset}] }]
    pfAnimRaf: null,
    pfLastFrameTs: 0,
    // 设备图状态
    devCategories: [],
    devCategory: "",
    devFilter: "all", // "all" | "alarm" | "running"
    devDetailTab: "monitor", // "monitor" | "control"
    devItems: [],
    devSelected: null, // { table_name, idx, dev_id }
    trendData: null,
    trendHidden: new Set(),
    trendHoverX: null,
    compareTargets: [], // [{ key, table_name, idx, metric, label }]
    compareMode: false,
    ctrlDrafts: new Map(), // `${table}:${idx}:${control}` -> rawString
    // 告警中心状态
    alarmScope: "active", // "active" | "history"
    alarmType: "",
    alarmGroupRtu: true,
    alarmExpandedRtus: new Set(),
    alarmItems: [],
    collectorRunning: null,
    // 通信图状态
    commTab: "overview",
    commPointKind: "yc",
    commPointOffset: 0,
    commPointLimit: 50,
    commPointTotal: 0,
    commRtuOptionsLoaded: false,
    // 日志状态
    logRecord: "runtime",
    logFilename: "",
    logLines: [],
    // 极地管控大屏状态
    cockpitHours: 6,
    cockpitData: null,
    cockpitHiddenPower: new Set(),
    cockpitHiddenSched: new Set(),
    cockpitPowerHoverX: null,
    cockpitSchedHoverX: null,
  };

  function esc(val) {
    return String(val ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtNum(val, digits = 2) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return "--";
    return Number(val).toFixed(digits);
  }

  function fmtTime(isoOrMs) {
    if (!isoOrMs) return "--";
    const d = typeof isoOrMs === "number" ? new Date(isoOrMs > 1e11 ? isoOrMs : isoOrMs * 1000) : new Date(isoOrMs);
    if (Number.isNaN(d.getTime())) return String(isoOrMs);
    return d.toLocaleString("zh-CN", { hour12: false });
  }

  function isDarkTheme() {
    const t = document.documentElement.dataset.theme || "sci-fi-cyan";
    return t !== "minimalist-light" && t !== "bento-grid" && t !== "editorial";
  }

  function debounce(fn, delayMs = 250) {
    let timer = null;
    return (...args) => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        fn(...args);
      }, delayMs);
    };
  }

  async function fetchJson(url, { method = "GET", body, signal, timeoutMs = 12000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error("请求超时")), timeoutMs);
    const onAbort = () => controller.abort(signal.reason);
    if (signal) {
      if (signal.aborted) {
        clearTimeout(timer);
        throw new DOMException("Aborted", "AbortError");
      }
      signal.addEventListener("abort", onAbort, { once: true });
    }
    try {
      const headers = { Accept: "application/json" };
      const init = { method, headers, signal: controller.signal };
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }
      const resp = await fetch(url, init);
      const text = await resp.text();
      let data = {};
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (_) {
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          throw new Error("响应不是有效的 JSON");
        }
      }
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
      return data;
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
    }
  }

  function api(url, opts) {
    return fetchJson(url, opts);
  }

  function apiPut(url, body, opts) {
    return fetchJson(url, { ...(opts || {}), method: "PUT", body });
  }

  function apiPost(url, body, opts) {
    return fetchJson(url, { ...(opts || {}), method: "POST", body });
  }

  // 动态同步顶栏跳转 scdfe_d（同主机 8980 端口）
  const jumpDLink = $("#portal-jump-d");
  if (jumpDLink) {
    jumpDLink.href = `${location.protocol}//${location.hostname}:8980/`;
  }

  function updateAlarmBadge(unacked) {
    const badge = $("#nav-alarm-badge");
    if (!badge) return;
    const count = Number(unacked || 0);
    badge.hidden = count <= 0;
    badge.textContent = count > 99 ? "99+" : String(count);
  }

  function ctrlStatusBadge(c) {
    const st = Number(c.cmd_status ?? 0);
    if (c.disabled || st === 5) return '<span class="badge muted">控制禁用</span>';
    if (st === 1) return '<span class="badge warn">待回执</span>';
    if (st === 2) return '<span class="badge ok">执行成功</span>';
    if (st === 3) return '<span class="badge danger">执行失败</span>';
    return '<span class="badge muted">空闲</span>';
  }

  function updateCollectorBadge(running) {
    if (running === undefined || running === null) return;
    state.collectorRunning = Boolean(running);
    const el = $("#dev-collector-badge");
    if (!el) return;
    el.textContent = state.collectorRunning ? "转发在线" : "转发离线";
    el.className = "badge " + (state.collectorRunning ? "ok" : "danger");
  }

  async function dispatchPortalControl(dev, ctrl, rawValue) {
    if (!dev || !ctrl) return;
    const isYk = ctrl.kind === "yk";
    let parsedValue;
    if (isYk) {
      parsedValue = Number(rawValue);
    } else {
      const txt = String(rawValue ?? "").trim();
      if (!txt) {
        setConnection(false, "请输入有效的遥调设定值");
        return;
      }
      parsedValue = Number(txt);
      if (!Number.isFinite(parsedValue)) {
        setConnection(false, "遥调设定值必须为有限数值");
        return;
      }
      if (ctrl.max_value != null && ctrl.min_value != null && Number(ctrl.max_value) > Number(ctrl.min_value)) {
        if (parsedValue > Number(ctrl.max_value) || parsedValue < Number(ctrl.min_value)) {
          setConnection(false, `设定值 ${parsedValue} 超出限值 [${ctrl.min_value}, ${ctrl.max_value}]`);
          return;
        }
      }
    }
    const actionDesc = isYk
      ? (parsedValue === 1 ? "合 / 启 (1)" : "分 / 停 (0)")
      : `${parsedValue}${ctrl.unit ? " " + ctrl.unit : ""}`;
    const warnLine = state.collectorRunning === false
      ? "\n⚠ 提示：scdfe_d 后台转发线程当前未运行，指令将暂存宽表待启动后转发。"
      : "";
    const msg = `目标设备：${dev.name}（${dev.table_name} #${dev.idx}）\n指令属性：${ctrl.label || ctrl.control} [${ctrl.control}] · RTU ${ctrl.rtu_id} / #${ctrl.point_no}\n下发目标值：${actionDesc}${warnLine}\n确认立即写入并下发指令？`;
    const ok = typeof window.scadaConfirm === "function"
      ? await window.scadaConfirm(msg, {
          title: isYk ? "确认下发遥控指令" : "确认下发遥调指令",
          confirmText: "确认下发",
          danger: true,
        })
      : false;
    if (!ok) return;
    const key = `${dev.table_name}:${dev.idx}:${ctrl.control}`;
    const res = await apiPost(
      `/api/devices/${encodeURIComponent(dev.table_name)}/${dev.idx}/control`,
      { control: ctrl.control, value: parsedValue }
    );
    state.ctrlDrafts.delete(key);
    updateCollectorBadge(res.collector_running);
    if (res.warning) {
      setConnection(false, res.warning);
    } else {
      setConnection(true, `已下发 ${dev.name} · ${ctrl.label || ctrl.control} = ${actionDesc}`);
    }
    if (state.view === "devices") await loadDevices();
    else if (state.view === "powerflow") await loadPowerflow();
  }

  function qualityBadge(m) {
    const q = m.quality == null ? null : Number(m.quality);
    if (q === 0 && !m.disabled) return '<span class="badge ok">正常</span>';
    if (q === 1) return '<span class="badge muted">无配置</span>';
    if (q === 2) return '<span class="badge danger">通信异常</span>';
    if (q === 3) return '<span class="badge over">越上限</span>';
    if (q === 4) return '<span class="badge under">越下限</span>';
    if (m.disabled) return '<span class="badge muted">禁用</span>';
    return `<span class="badge danger">${esc(m.quality_label || "异常")}</span>`;
  }

  function fmtLimitCompact(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return "—";
    return String(Number(Number(val).toPrecision(6)));
  }

  let limitTarget = null;

  function validatePortalLimitDialog() {
    const minEl = $("#limit-min");
    const maxEl = $("#limit-max");
    const errEl = $("#limit-error");
    const saveBtn = $("#limit-save");
    if (!minEl || !maxEl || !errEl || !saveBtn) return { ok: false, minVal: null, maxVal: null };
    const rawMin = minEl.value.trim();
    const rawMax = maxEl.value.trim();
    const minVal = rawMin === "" ? null : Number(rawMin);
    const maxVal = rawMax === "" ? null : Number(rawMax);
    let msg = "";
    if ((minVal !== null && !Number.isFinite(minVal)) || (maxVal !== null && !Number.isFinite(maxVal))) {
      msg = "量测上下限必须为有限数值或留空";
    } else if (minVal !== null && maxVal !== null && maxVal <= minVal) {
      msg = "量测上限必须大于下限，无法修改";
    }
    const invalid = Boolean(msg);
    minEl.classList.toggle("limit-invalid", invalid);
    maxEl.classList.toggle("limit-invalid", invalid);
    errEl.textContent = msg;
    errEl.hidden = !invalid;
    saveBtn.disabled = invalid;
    return { ok: !invalid, minVal, maxVal };
  }

  function openLimitDialog(dev, metricItem) {
    const dlg = $("#limit-dialog");
    if (!dlg) return;
    limitTarget = { dev, metric: metricItem };
    $("#limit-dialog-title").textContent = `${metricItem.label} · 上下限`;
    $("#limit-dialog-meta").textContent = `${dev.name} (${dev.table_name}#${dev.idx} · ${metricItem.metric})`;
    $("#limit-min").value = metricItem.min_value == null ? "" : String(metricItem.min_value);
    $("#limit-max").value = metricItem.max_value == null ? "" : String(metricItem.max_value);
    validatePortalLimitDialog();
    dlg.showModal();
  }

  function bindLimitDialog() {
    const dlg = $("#limit-dialog");
    const form = $("#limit-form");
    if (!dlg || !form || form.dataset.bound) return;
    form.dataset.bound = "1";
    $("#limit-min")?.addEventListener("input", validatePortalLimitDialog);
    $("#limit-max")?.addEventListener("input", validatePortalLimitDialog);
    $("#limit-close")?.addEventListener("click", () => dlg.close());
    $("#limit-cancel")?.addEventListener("click", () => dlg.close());
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!limitTarget) return;
      const check = validatePortalLimitDialog();
      if (!check.ok) return;
      const saveBtn = $("#limit-save");
      const errEl = $("#limit-error");
      saveBtn.disabled = true;
      try {
        const { dev, metric } = limitTarget;
        await apiPut(
          `/api/devices/${encodeURIComponent(dev.table_name)}/${dev.idx}/limits`,
          { metric: metric.metric, max_value: check.maxVal, min_value: check.minVal }
        );
        dlg.close();
        await loadDevices();
      } catch (err) {
        errEl.textContent = err.message || "保存失败";
        errEl.hidden = false;
      } finally {
        validatePortalLimitDialog();
      }
    });
  }

  function setConnection(ok, text) {
    const pill = $("#conn-pill");
    const label = $("#conn-text");
    if (!pill || !label) return;
    pill.className = "conn-pill " + (ok ? "ok" : "error");
    label.textContent = text;
  }

  function updateClock() {
    const el = $("#live-clock");
    if (!el) return;
    const now = new Date();
    el.textContent = now.toLocaleString("zh-CN", { hour12: false });
  }
  updateClock();
  setInterval(updateClock, 1000);

  // ==================== 视图切换与路由 ====================
  function switchView(view, updateHash = true) {
    const valid = ["home", "cockpit", "energyflow", "powerflow", "devices", "alarms", "comm", "logs"];
    if (!valid.includes(view)) view = "home";
    state.view = view;
    document.body.classList.toggle("view-home", view === "home");
    if (updateHash && location.hash !== "#" + view) {
      history.replaceState(null, "", "#" + view);
    }
    $$("#portal-nav .nav-item").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === view);
    });
    valid.forEach((v) => {
      const sec = $("#view-" + v);
      if (sec) sec.hidden = v !== view;
    });
    refreshCurrentView();
    if (view === "home") {
      requestAnimationFrame(() => updateHeroFlowLines(lastRealtimeEnergy));
    }
    if (view === "cockpit") {
      requestAnimationFrame(() => drawCockpitCharts());
    }
    if (view === "powerflow" && state.svgLoaded) {
      requestAnimationFrame(() => fitSvgView());
      startPowerflowAnimation();
    } else if (view !== "powerflow") {
      stopPowerflowAnimation();
      hidePfPopovers?.();
    }
    if (view === "devices") {
      requestAnimationFrame(() => drawTrendChart());
    }
  }

  $$("#portal-nav .nav-item").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });

  window.addEventListener("hashchange", () => {
    const h = location.hash.replace(/^#/, "");
    if (h && h !== state.view) switchView(h, false);
  });

  // ==================== 1. 系统首页（纯净底图 + 中央全息面板 + DOM精准能量流） ====================
  let lastRealtimeEnergy = null;

  function renderRenewPie(pct) {
    const slice = $("#renew-pie-slice");
    if (!slice) return;
    const clamped = Math.max(0, Math.min(99.9, Number(pct || 0)));
    if (clamped <= 0.1) {
      slice.setAttribute("d", "");
      return;
    }
    const cx = 61;
    const cy = 58;
    const r = 44;
    const startAngle = -Math.PI / 2;
    const endAngle = startAngle + (clamped / 100) * Math.PI * 2;
    const x1 = cx + r * Math.cos(startAngle);
    const y1 = cy + r * Math.sin(startAngle);
    const x2 = cx + r * Math.cos(endAngle);
    const y2 = cy + r * Math.sin(endAngle);
    const largeArc = clamped > 50 ? 1 : 0;
    slice.setAttribute(
      "d",
      `M ${cx} ${cy} L ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 ${largeArc} 1 ${x2.toFixed(2)} ${y2.toFixed(2)} Z`
    );
  }

  function updateHeroFlowLines(rt) {
    const stage = $("#hero-stage");
    const svg = $("#hero-flow-svg");
    const tracksG = $("#hero-flow-tracks");
    const pulsesG = $("#hero-flow-pulses");
    const hud = $("#hero-center-hud");
    if (!stage || !svg || !tracksG || !pulsesG || !hud) return;

    const sRect = stage.getBoundingClientRect();
    if (sRect.width < 40 || sRect.height < 40) return;
    svg.setAttribute("viewBox", `0 0 ${Math.round(sRect.width)} ${Math.round(sRect.height)}`);

    const relRect = (el) => {
      const r = el.getBoundingClientRect();
      return {
        left: r.left - sRect.left,
        right: r.right - sRect.left,
        top: r.top - sRect.top,
        bottom: r.bottom - sRect.top,
        cy: (r.top + r.bottom) / 2 - sRect.top,
      };
    };

    const h = relRect(hud);
    const leftNodes = [
      { id: "node-pv", kw: rt?.pv_kw ?? 0, reverse: false },
      { id: "node-wind", kw: rt?.wind_kw ?? 0, reverse: false },
      { id: "node-h2prod", kw: rt?.hydrogen_prod_kw ?? 0, reverse: true },
      { id: "node-h2gen", kw: rt?.hydrogen_gen_kw ?? 0, reverse: false },
    ];
    const rightNodes = [
      { id: "node-diesel", kw: rt?.diesel_kw ?? 0, reverse: true },
      { id: "node-storage", kw: rt?.storage_kw ?? 0, reverse: (rt?.storage_kw ?? 0) > 0 },
      { id: "node-load", kw: rt?.load_kw ?? 0, reverse: false },
    ];

    const segs = [];
    // 左侧 4 张卡 -> 左侧汇流点 -> 中央 HUD 左边缘中点
    const leftEls = leftNodes.map((n) => ({ ...n, rect: $("#" + n.id) ? relRect($("#" + n.id)) : null })).filter((n) => n.rect);
    if (leftEls.length) {
      const maxLeftR = Math.max(...leftEls.map((n) => n.rect.right));
      const busX = maxLeftR + (h.left - maxLeftR) * 0.56;
      const busY = h.cy;
      leftEls.forEach((n) => {
        const sx = n.rect.right;
        const sy = n.rect.cy;
        const c1x = sx + (busX - sx) * 0.55;
        const c2x = sx + (busX - sx) * 0.55;
        segs.push({
          d: `M ${sx.toFixed(1)} ${sy.toFixed(1)} C ${c1x.toFixed(1)} ${sy.toFixed(1)}, ${c2x.toFixed(1)} ${busY.toFixed(1)}, ${busX.toFixed(1)} ${busY.toFixed(1)}`,
          kw: n.kw,
          reverse: n.reverse,
          trunk: false,
        });
      });
      const leftNet = (rt?.pv_kw || 0) + (rt?.wind_kw || 0) + (rt?.hydrogen_gen_kw || 0) - (rt?.hydrogen_prod_kw || 0);
      segs.push({
        d: `M ${busX.toFixed(1)} ${busY.toFixed(1)} L ${h.left.toFixed(1)} ${busY.toFixed(1)}`,
        kw: leftNet,
        reverse: leftNet < 0,
        trunk: true,
      });
    }

    // 中央 HUD 右边缘中点 -> 右侧汇流点 -> 右侧 3 张卡左边缘中点
    const rightEls = rightNodes.map((n) => ({ ...n, rect: $("#" + n.id) ? relRect($("#" + n.id)) : null })).filter((n) => n.rect);
    if (rightEls.length) {
      const minRightL = Math.min(...rightEls.map((n) => n.rect.left));
      const rBusX = h.right + (minRightL - h.right) * 0.58;
      const rBusY = rightEls.length >= 2 ? rightEls[1].rect.cy : h.cy;
      const midC1x = h.right + (rBusX - h.right) * 0.55;
      const midC2x = h.right + (rBusX - h.right) * 0.45;
      segs.push({
        d: `M ${h.right.toFixed(1)} ${h.cy.toFixed(1)} C ${midC1x.toFixed(1)} ${h.cy.toFixed(1)}, ${midC2x.toFixed(1)} ${rBusY.toFixed(1)}, ${rBusX.toFixed(1)} ${rBusY.toFixed(1)}`,
        kw: (rt?.load_kw || 0) + Math.abs(rt?.diesel_kw || 0) + Math.abs(rt?.storage_kw || 0),
        reverse: false,
        trunk: true,
      });
      rightEls.forEach((n) => {
        const tx = n.rect.left;
        const ty = n.rect.cy;
        const c1x = rBusX + (tx - rBusX) * 0.45;
        const c2x = rBusX + (tx - rBusX) * 0.55;
        segs.push({
          d: `M ${rBusX.toFixed(1)} ${rBusY.toFixed(1)} C ${c1x.toFixed(1)} ${rBusY.toFixed(1)}, ${c2x.toFixed(1)} ${ty.toFixed(1)}, ${tx.toFixed(1)} ${ty.toFixed(1)}`,
          kw: n.kw,
          reverse: n.reverse,
          trunk: false,
        });
      });
    }

    const trackHtml = [];
    const pulseHtml = [];
    segs.forEach((seg) => {
      const abs = Math.abs(Number(seg.kw || 0));
      const idle = abs < 0.05;
      trackHtml.push(`<path class="flow-track${idle ? " idle" : ""}" d="${seg.d}" />`);
      const cls = ["flow-path"];
      if (seg.trunk) cls.push("trunk");
      if (idle) cls.push("idle");
      else if (seg.reverse) cls.push("reverse");
      const dur = idle ? "" : ` style="animation-duration:${Math.max(0.45, Math.min(2.0, 2.0 - Math.min(abs, 250) / 160)).toFixed(2)}s"`;
      pulseHtml.push(`<path class="${cls.join(" ")}" d="${seg.d}"${dur} />`);
    });
    tracksG.innerHTML = trackHtml.join("");
    pulsesG.innerHTML = pulseHtml.join("");
  }

  async function loadOverview() {
    const data = await api("/api/overview");
    if (data.station_name && $("#station-title")) $("#station-title").textContent = data.station_name;
    if (data.scene && $("#station-scene")) $("#station-scene").textContent = data.scene;

    const k = data.kpis || {};
    const rt = data.realtime || {};
    const h = data.health || {};
    lastRealtimeEnergy = rt;
    updateAlarmBadge(h.alarm_unacked);
    updateCollectorBadge(h.collector_running);

    $("#kpi-days").textContent = k.running_days ?? "--";
    $("#kpi-load-total").textContent = fmtNum(k.load_total_wkwh, 1);
    $("#kpi-diesel-total").textContent = fmtNum(k.diesel_total_wkwh, 1);
    $("#kpi-renew-total").textContent = fmtNum(k.renewable_total_wkwh, 1);
    $("#kpi-diesel-saved").textContent = fmtNum(k.diesel_saved_tons, 1);

    $("#kpi-wind-total").textContent = fmtNum(k.wind_total_wkwh, 2);
    $("#kpi-pv-total").textContent = fmtNum(k.pv_total_wkwh, 2);
    $("#kpi-storage-total").textContent = fmtNum(k.storage_total_wkwh, 2);
    $("#kpi-hydro-total").textContent = fmtNum(k.hydro_total_wkwh, 2);

    const sharePct = Number(k.renewable_share_pct ?? 0);
    $("#kpi-renew-share").textContent = fmtNum(sharePct, 1);
    if ($("#hero-renew-kw")) {
      $("#hero-renew-kw").textContent = `${fmtNum(rt.renewable_kw, 1)} / ${fmtNum(rt.total_gen_kw, 1)} kW`;
    }
    renderRenewPie(sharePct);

    $("#rt-pv").textContent = fmtNum(rt.pv_kw, 1);
    $("#rt-wind").textContent = fmtNum(rt.wind_kw, 1);
    $("#rt-h2prod").textContent = fmtNum(rt.hydrogen_prod_kw, 1);
    $("#rt-h2gen").textContent = fmtNum(rt.hydrogen_gen_kw, 1);
    $("#rt-diesel").textContent = fmtNum(rt.diesel_kw, 1);
    $("#rt-storage").textContent = fmtNum(rt.storage_kw, 1);
    $("#rt-storage-soc").textContent = `kW / ${fmtNum(rt.storage_soc_pct, 0)}%`;
    $("#rt-load").textContent = fmtNum(rt.load_kw, 1);

    $("#env-wind").textContent = fmtNum(rt.wind_speed, 1);
    $("#env-irr").textContent = fmtNum(rt.irradiance, 1);
    $("#env-h2").textContent =
      rt.hydro_pressure_mpa !== null && rt.hydro_pressure_mpa !== undefined
        ? `${fmtNum(rt.hydro_pressure_mpa, 2)} MPa / ${fmtNum(rt.hydro_soc_pct, 0)}%`
        : `${fmtNum(rt.hydro_soc_pct, 0)}%`;

    // 侧边栏健康速览
    $("#health-rtus").textContent = `${h.rtu_online ?? 0} / ${h.rtu_total ?? 0}`;
    $("#health-points").textContent = `${h.valid_measurements ?? 0} / ${h.bound_measurements ?? 0}`;
    $("#health-gen").textContent = `${fmtNum(rt.total_gen_kw, 1)} kW`;
    $("#health-load").textContent = `${fmtNum(rt.total_load_kw, 1)} kW`;

    if (state.view === "home") {
      requestAnimationFrame(() => updateHeroFlowLines(rt));
    }
  }

  $$(".energy-node[data-jump-cat]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.devCategory = btn.dataset.jumpCat;
      state.devSelected = null;
      switchView("devices");
    });
  });

  // ==================== 2. 潮流图（qinling.svg 实时刷新） ====================
  function applySvgTransform() {
    const stage = $("#pf-stage");
    if (!stage) return;
    stage.style.transform = `translate(${state.pfX}px, ${state.pfY}px) scale(${state.pfScale})`;
    const label = $("#pf-zoom-label");
    if (label) label.textContent = Math.round(state.pfScale * 100) + "%";
  }

  function fitSvgView() {
    const wrap = $("#pf-canvas-wrap");
    if (!wrap || !state.svgLoaded) return;
    const rect = wrap.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 20) return;
    const scale = Math.min((rect.width - 24) / state.svgWidth, (rect.height - 24) / state.svgHeight);
    state.pfScale = Math.max(0.15, Math.min(2.5, scale));
    state.pfX = (rect.width - state.svgWidth * state.pfScale) / 2;
    state.pfY = (rect.height - state.svgHeight * state.pfScale) / 2;
    applySvgTransform();
  }

  function applySvgThemeColors() {
    const svg = $("#pf-stage svg");
    if (!svg) return;
    const dark = isDarkTheme();
    const bgRect = svg.querySelector("#Background_Layer rect");
    if (bgRect) {
      bgRect.setAttribute("fill", dark ? "#071122" : "#f1f5f9");
    }
    svg.querySelectorAll("#Text_Layer text").forEach((t) => {
      t.setAttribute("fill", dark ? "#cfe6ff" : "#334155");
      t.setAttribute("stroke", dark ? "rgba(6, 16, 34, 0.9)" : "rgba(255, 255, 255, 0.85)");
    });
    svg.querySelectorAll("#Measurement_Layer g.mg rect").forEach((r) => {
      r.setAttribute("fill", dark ? "rgba(8, 22, 44, 0.72)" : "rgba(255, 255, 255, 0.78)");
      r.setAttribute("stroke", dark ? "rgba(0, 229, 255, 0.25)" : "rgba(100, 116, 139, 0.25)");
      r.setAttribute("stroke-width", "1");
    });
    svg.querySelectorAll("#Measurement_Layer text").forEach((t) => {
      t.setAttribute("fill", dark ? "#93c5fd" : "#334155");
    });
    svg.querySelectorAll("#Measurement_Layer tspan.mv").forEach((t) => {
      t.setAttribute("fill", dark ? "#34f5a8" : "#0f766e");
      t.setAttribute("font-weight", "700");
    });
  }

  let svgLoadPromise = null;
  async function ensureSvgLoaded() {
    if (state.svgLoaded) return;
    if (svgLoadPromise) return svgLoadPromise;
    svgLoadPromise = (async () => {
      const resp = await fetch("/api/svg");
      if (!resp.ok) throw new Error("无法加载潮流图 SVG");
      const svgText = await resp.text();
      const stage = $("#pf-stage");
      stage.innerHTML = svgText;
      const svg = stage.querySelector("svg");
      if (!svg) throw new Error("SVG 格式无效");
      const vb = (svg.getAttribute("viewBox") || "0,0,2518,2143").split(/[\s,]+/).map(Number);
      if (vb.length === 4 && vb[2] > 0 && vb[3] > 0) {
        state.svgWidth = vb[2];
        state.svgHeight = vb[3];
      }
      svg.setAttribute("width", String(state.svgWidth));
      svg.setAttribute("height", String(state.svgHeight));
      // 移除 SVG 内原生 <title>，防止浏览器弹出白底系统提示框遮挡自定义悬浮框
      svg.querySelectorAll("title").forEach((el) => el.remove());
      state.svgLoaded = true;
      initPowerflowArrowPaths(svg);
      applySvgThemeColors();
      fitSvgView();
      return svg;
    })();
    try {
      const svg = await svgLoadPromise;
      if (!svg || svg.dataset.eventsBound === "1") return;
      svg.dataset.eventsBound = "1";

    svg.addEventListener("mousemove", (event) => {
      if ($("#pf-canvas-wrap")?.classList.contains("dragging")) return;
      const measText = event.target.closest("#Measurement_Layer g.mg[dev] text");
      if (measText) {
        const group = measText.closest("g.mg[dev]");
        const devId = group?.getAttribute("dev");
        const mv = measText.querySelector("tspan.mv[mt]");
        const mt = mv?.getAttribute("mt");
        if (devId && mt) {
          cancelHidePfPopovers();
          if (!state.pfHoverMeas || state.pfHoverMeas.devId !== devId || state.pfHoverMeas.mt !== mt) {
            showMeasPopover(devId, mt, event.clientX, event.clientY);
          }
          return;
        }
      }
      const devNode = event.target.closest("use[dev-id], text[dev-id]");
      if (devNode) {
        const devId = devNode.getAttribute("dev-id");
        if (devId) {
          cancelHidePfPopovers();
          if (state.pfHoverDev !== devId || $("#pf-dev-popover")?.hidden) {
            showDevicePopover(devId, event.clientX, event.clientY, false);
          }
          return;
        }
      }
      if (state.pfHoverDev || state.pfHoverMeas) {
        scheduleHidePfPopovers();
      }
    });

    svg.addEventListener("mouseleave", () => {
      scheduleHidePfPopovers();
    });

    svg.addEventListener("click", (event) => {
      const devNode = event.target.closest("use[dev-id], text[dev-id]");
      if (!devNode) return;
      const devId = devNode.getAttribute("dev-id");
      const dev = devId ? state.pfData?.devices?.[devId] : null;
      if (dev) {
        hidePfPopovers();
        state.devCategory = dev.category;
        state.devSelected = { table_name: dev.table_name, idx: dev.idx, dev_id: dev.dev_id };
        switchView("devices");
      }
    });
    } catch (err) {
      svgLoadPromise = null;
      throw err;
    }
  }

  // -------------------- SVG 潮流动态流动箭头引擎 --------------------
  const SVG_NS = "http://www.w3.org/2000/svg";

  function transformSimplePathD(d, tx, ty, sx, sy, vx, vy) {
    // 将 <symbol> 内 M/L 折线坐标映射到 SVG 主画布坐标，保证 getPointAtLength 与切线角绝对精确
    const tokens = String(d || "").match(/[MLml]|-?\d*\.?\d+(?:e[-+]?\d+)?/g);
    if (!tokens) return "";
    const out = [];
    let i = 0;
    let cmd = "M";
    while (i < tokens.length) {
      if (/^[MLml]$/.test(tokens[i])) {
        cmd = tokens[i].toUpperCase();
        out.push(cmd);
        i += 1;
        continue;
      }
      const x = Number(tokens[i]);
      const y = Number(tokens[i + 1]);
      if (Number.isNaN(x) || Number.isNaN(y)) break;
      const gx = tx + (x - vx) * sx;
      const gy = ty + (y - vy) * sy;
      out.push(`${gx.toFixed(2)} ${gy.toFixed(2)}`);
      i += 2;
    }
    return out.join(" ");
  }

  function arrowColorFor(voltageType) {
    const dark = isDarkTheme();
    if (voltageType === "dc") return dark ? "#38bdf8" : "#2563eb";
    if (voltageType === "hydro") return dark ? "#c084fc" : "#7c3aed";
    return dark ? "#34f5a8" : "#0d9488";
  }

  function arrowStrokeFor() {
    return isDarkTheme() ? "#04101f" : "#ffffff";
  }

  function initPowerflowArrowPaths(svg) {
    const rootG = svg.querySelector("#root_g") || svg;
    const oldArrows = rootG.querySelector("#PowerFlow_Arrows_Layer");
    if (oldArrows) oldArrows.remove();
    const oldGeom = rootG.querySelector("#PowerFlow_Geom_Hidden");
    if (oldGeom) oldGeom.remove();

    const geomLayer = document.createElementNS(SVG_NS, "g");
    geomLayer.setAttribute("id", "PowerFlow_Geom_Hidden");
    geomLayer.setAttribute("visibility", "hidden");
    geomLayer.setAttribute("pointer-events", "none");
    rootG.appendChild(geomLayer);

    const arrowLayer = document.createElementNS(SVG_NS, "g");
    arrowLayer.setAttribute("id", "PowerFlow_Arrows_Layer");
    arrowLayer.setAttribute("pointer-events", "none");
    const measLayer = rootG.querySelector("#Measurement_Layer") || rootG.querySelector("#Text_Layer");
    if (measLayer) rootG.insertBefore(arrowLayer, measLayer);
    else rootG.appendChild(arrowLayer);

    const entries = [];

    // 1. 解析 Segment_Layer 全部连接导线
    svg.querySelectorAll("#Segment_Layer path[id][d]").forEach((pathEl) => {
      const len = typeof pathEl.getTotalLength === "function" ? pathEl.getTotalLength() : 0;
      if (len < 16) return;
      entries.push({
        key: pathEl.getAttribute("id"),
        kind: "edge",
        voltageType: (pathEl.getAttribute("class") || "").includes("dcv") ? "dc" : "ac",
        pathEl,
        length: len,
        pKw: 0,
        dir: 1,
        speed: 0,
        scale: 1,
        arrows: [],
      });
    });

    // 2. 解析 ACBranch / DCBranch / ACZeroBranch 支路几何路径
    svg.querySelectorAll("#ACBranch_Layer use[dev-id], #DCBranch_Layer use[dev-id], #ACZeroBranch_Layer use[dev-id]").forEach((useEl) => {
      const devId = useEl.getAttribute("dev-id");
      const href = (useEl.getAttribute("href") || useEl.getAttribute("xlink:href") || "").replace(/^#/, "");
      if (!devId || !href) return;
      const sym = svg.querySelector(`#${CSS.escape(href)}`);
      const symPath = sym?.querySelector("path[d]");
      if (!sym || !symPath) return;
      const vb = (sym.getAttribute("viewBox") || "-75 -18 150 36").split(/[\s,]+/).map(Number);
      const x = Number(useEl.getAttribute("x") || 0);
      const y = Number(useEl.getAttribute("y") || 0);
      const w = Number(useEl.getAttribute("width") || vb[2] || 150);
      const h = Number(useEl.getAttribute("height") || vb[3] || 36);
      const dGlobal = transformSimplePathD(
        symPath.getAttribute("d"),
        x,
        y,
        vb[2] ? w / vb[2] : 1,
        vb[3] ? h / vb[3] : 1,
        vb[0] || 0,
        vb[1] || 0
      );
      if (!dGlobal) return;
      const pEl = document.createElementNS(SVG_NS, "path");
      pEl.setAttribute("d", dGlobal);
      geomLayer.appendChild(pEl);
      const len = typeof pEl.getTotalLength === "function" ? pEl.getTotalLength() : 0;
      if (len < 16) return;
      entries.push({
        key: devId,
        kind: "branch",
        voltageType: devId.startsWith("DC") ? "dc" : "ac",
        pathEl: pEl,
        length: len,
        pKw: 0,
        dir: 1,
        speed: 0,
        scale: 1,
        arrows: [],
      });
    });

    state.pfArrowPaths = entries;
  }

  function updatePowerflowArrowsData(flows) {
    const svg = $("#pf-stage svg");
    const arrowLayer = svg?.querySelector("#PowerFlow_Arrows_Layer");
    if (!arrowLayer || !flows) return;

    const edges = flows.edges || {};
    const branches = flows.branches || {};
    const strokeCol = arrowStrokeFor();
    let activeCount = 0;

    state.pfArrowPaths.forEach((item) => {
      const rec = item.kind === "edge" ? edges[item.key] : branches[item.key];
      const pKw = rec && typeof rec.p_kw === "number" ? rec.p_kw : 0;
      const absP = Math.abs(pKw);
      if (rec?.voltage_type) item.voltageType = rec.voltage_type;
      item.pKw = pKw;

      if (absP < 0.05) {
        item.speed = 0;
        item.arrows.forEach((a) => {
          a.el.setAttribute("visibility", "hidden");
        });
        return;
      }

      activeCount += 1;
      item.dir = pKw >= 0 ? 1 : -1;
      // 功率大小对数映射箭头缩放（0.72x ~ 1.55x）与速度（28 ~ 146 px/s）
      const norm = Math.min(1, Math.log10(1 + absP) / 2.38);
      item.scale = 0.72 + 0.83 * norm;
      item.speed = 28 + 118 * norm;

      // 依导线长度与箭头尺寸自动确定循环箭头数量（1 ~ 6 枚）
      const spacing = Math.max(58, 96 - norm * 24);
      const desired = Math.max(1, Math.min(6, Math.round(item.length / spacing)));
      const fillCol = arrowColorFor(item.voltageType);

      while (item.arrows.length < desired) {
        const poly = document.createElementNS(SVG_NS, "polygon");
        poly.setAttribute("class", "pf-arrow-node");
        poly.setAttribute("points", "9.5,0 -6.2,-5.4 -2.6,0 -6.2,5.4");
        poly.setAttribute("stroke-width", "1.15");
        poly.setAttribute("stroke-linejoin", "round");
        arrowLayer.appendChild(poly);
        item.arrows.push({ el: poly, offset: (item.arrows.length / desired) * item.length });
      }
      while (item.arrows.length > desired) {
        const removed = item.arrows.pop();
        removed.el.remove();
      }
      item.arrows.forEach((a, idx) => {
        a.el.setAttribute("visibility", "visible");
        a.el.setAttribute("fill", fillCol);
        a.el.setAttribute("stroke", strokeCol);
        if (a.offset >= item.length || a.offset < 0) {
          a.offset = (idx / desired) * item.length;
        }
      });
    });

    if (activeCount > 0) startPowerflowAnimation();
    else stopPowerflowAnimation();
  }

  function stepPowerflowAnimation(ts) {
    if (state.view !== "powerflow" || !state.pfShowArrows || document.hidden) {
      state.pfAnimRaf = null;
      state.pfLastFrameTs = 0;
      return;
    }
    const dt = state.pfLastFrameTs ? Math.min(0.08, (ts - state.pfLastFrameTs) / 1000) : 0.016;
    state.pfLastFrameTs = ts;

    let anyMoving = false;
    for (const item of state.pfArrowPaths) {
      if (item.speed <= 0 || !item.arrows.length) continue;
      anyMoving = true;
      const len = item.length;
      const step = item.speed * dt * item.dir;
      const eps = Math.min(2.0, len * 0.05);
      for (const a of item.arrows) {
        a.offset = (a.offset + step) % len;
        if (a.offset < 0) a.offset += len;
        const pos = a.offset;
        const p0 = item.pathEl.getPointAtLength(Math.max(0, pos - eps));
        const p1 = item.pathEl.getPointAtLength(Math.min(len, pos + eps));
        const pt = item.pathEl.getPointAtLength(pos);
        let dx = p1.x - p0.x;
        let dy = p1.y - p0.y;
        if (item.dir < 0) {
          dx = -dx;
          dy = -dy;
        }
        const angle = (Math.atan2(dy, dx) * 180) / Math.PI;
        a.el.setAttribute(
          "transform",
          `translate(${pt.x.toFixed(1)} ${pt.y.toFixed(1)}) rotate(${angle.toFixed(1)}) scale(${item.scale.toFixed(2)})`
        );
      }
    }

    if (anyMoving) {
      state.pfAnimRaf = requestAnimationFrame(stepPowerflowAnimation);
    } else {
      state.pfAnimRaf = null;
      state.pfLastFrameTs = 0;
    }
  }

  function startPowerflowAnimation() {
    if (state.pfAnimRaf || state.view !== "powerflow" || !state.pfShowArrows) return;
    state.pfLastFrameTs = 0;
    state.pfAnimRaf = requestAnimationFrame(stepPowerflowAnimation);
  }

  function stopPowerflowAnimation() {
    if (state.pfAnimRaf) {
      cancelAnimationFrame(state.pfAnimRaf);
      state.pfAnimRaf = null;
      state.pfLastFrameTs = 0;
    }
  }

  function updateSvgLiveValues(pf) {
    const svg = $("#pf-stage svg");
    if (!svg || !pf) return;
    const slots = pf.svg_slots || {};
    const breakers = pf.breakers || {};
    let updatedSlots = 0;

    svg.querySelectorAll("#Measurement_Layer g.mg[dev]").forEach((group) => {
      const devId = group.getAttribute("dev");
      const devSlots = slots[devId];
      group.querySelectorAll("text").forEach((textEl) => {
        const mv = textEl.querySelector("tspan.mv[mt]");
        if (!mv) return;
        const mt = mv.getAttribute("mt");
        const entry = devSlots && devSlots[mt];
        if (!entry) {
          mv.textContent = "--";
          return;
        }
        updatedSlots += 1;
        mv.textContent = typeof entry.value === "number" ? String(entry.value) : String(entry.value ?? "--");
        const tspans = textEl.querySelectorAll("tspan");
        if (tspans.length >= 3 && entry.unit !== undefined) {
          tspans[tspans.length - 1].textContent = entry.unit;
        }
        if (mt === "status") {
          mv.setAttribute("fill", entry.raw ? "#00e6a8" : "#ff4d6d");
        }
      });
    });

    let closedCnt = 0;
    let totalBreakers = 0;
    Object.entries(breakers).forEach(([devId, info]) => {
      totalBreakers += 1;
      if (info.closed) closedCnt += 1;
      const useEl = svg.querySelector(`use[dev-id="${CSS.escape(devId)}"]`);
      if (!useEl) return;
      const href = useEl.getAttribute("href") || useEl.getAttribute("xlink:href") || "";
      if (href.includes("_state_")) {
        const nextHref = href.replace(/_state_[01]/, info.closed ? "_state_1" : "_state_0");
        if (nextHref !== href) useEl.setAttribute("href", nextHref);
      }
      useEl.style.color = info.closed ? "" : "#ff4d6d";
    });

    if (pf.flows) {
      updatePowerflowArrowsData(pf.flows);
    }

    const stat = $("#pf-stat");
    if (stat) {
      stat.textContent = `已刷新 ${updatedSlots} 项量测 · 断路器合闸 ${closedCnt}/${totalBreakers}`;
      stat.className = "badge ok";
    }
    if (state.pfHoverDev && !$("#pf-dev-popover")?.hidden) {
      renderDevicePopoverContent(state.pfHoverDev, false);
    }
    if (state.pfHoverMeas && !$("#pf-meas-popover")?.hidden) {
      const { devId, mt } = state.pfHoverMeas;
      renderMeasPopoverContent(devId, mt, false);
    }
  }

  function cancelHidePfPopovers() {
    if (state.pfPopHideTimer) {
      clearTimeout(state.pfPopHideTimer);
      state.pfPopHideTimer = null;
    }
  }

  function hidePfPopovers() {
    cancelHidePfPopovers();
    const devPop = $("#pf-dev-popover");
    const measPop = $("#pf-meas-popover");
    if (devPop) devPop.hidden = true;
    if (measPop) measPop.hidden = true;
    state.pfHoverDev = null;
    state.pfHoverMeas = null;
  }

  function scheduleHidePfPopovers(delay = 180) {
    cancelHidePfPopovers();
    state.pfPopHideTimer = setTimeout(() => {
      hidePfPopovers();
    }, delay);
  }

  function positionPopover(popEl, clientX, clientY) {
    const wrap = $("#pf-canvas-wrap");
    if (!wrap || !popEl) return;
    const rect = wrap.getBoundingClientRect();
    const popW = popEl.offsetWidth || 300;
    const popH = popEl.offsetHeight || 200;
    const relX = clientX - rect.left;
    const relY = clientY - rect.top;
    let left = relX + 16;
    let top = relY + 14;
    if (left + popW > rect.width - 10) left = relX - popW - 16;
    if (top + popH > rect.height - 10) top = relY - popH - 14;
    left = Math.max(8, Math.min(rect.width - popW - 8, left));
    top = Math.max(8, Math.min(rect.height - popH - 8, top));
    popEl.style.left = `${Math.round(left)}px`;
    popEl.style.top = `${Math.round(top)}px`;
  }

  function buildKvTableHtml(pairs) {
    if (!pairs || !pairs.length) return "";
    const rows = [];
    for (let i = 0; i < pairs.length; i += 2) {
      const a = pairs[i];
      const b = pairs[i + 1];
      rows.push(
        `<tr>` +
        `<th title="${esc(a.label)}">${esc(a.label)}</th><td title="${esc(a.value)}">${esc(a.value)}</td>` +
        (b
          ? `<th title="${esc(b.label)}">${esc(b.label)}</th><td title="${esc(b.value)}">${esc(b.value)}</td>`
          : `<th></th><td></td>`) +
        `</tr>`
      );
    }
    return `<table class="pf-kv-table"><tbody>${rows.join("")}</tbody></table>`;
  }

  function renderDevicePopoverContent(devId, highlight = false) {
    const dev = state.pfData?.devices?.[devId];
    const svg = $("#pf-stage svg");
    if (highlight && svg) {
      svg.querySelectorAll(".pf-highlight").forEach((el) => el.classList.remove("pf-highlight"));
      const node = svg.querySelector(`use[dev-id="${CSS.escape(devId)}"]`);
      if (node) node.classList.add("pf-highlight");
    }
    const jumpBtn = $("#pf-jump-device");
    if (!dev) {
      $("#pf-dev-pop-name").textContent = devId;
      $("#pf-dev-pop-meta").textContent = "暂无模型参数";
      $("#pf-dev-pop-metrics").innerHTML = '<div class="empty-hint">模型库无此设备参数</div>';
      if (jumpBtn) jumpBtn.disabled = true;
      return;
    }
    const params = dev.model_params || [];
    $("#pf-dev-pop-name").textContent = dev.name;
    $("#pf-dev-pop-meta").textContent = `${dev.table_name} #${dev.idx} · 参数 ${params.length} 项`;
    if (jumpBtn) {
      jumpBtn.disabled = !dev.bound_count;
      jumpBtn.onclick = () => {
        hidePfPopovers();
        state.devCategory = dev.category;
        state.devSelected = { table_name: dev.table_name, idx: dev.idx, dev_id: dev.dev_id };
        switchView("devices");
      };
    }
    $("#pf-dev-pop-metrics").innerHTML = params.length
      ? buildKvTableHtml(params)
      : '<div class="empty-hint">暂无设备模型参数</div>';

    // 潮流图设备弹窗就地控调区（存在 YK/YT 时展示）
    const ctrlHost = $("#pf-dev-pop-controls");
    if (ctrlHost) {
      const ctrls = dev.controls || [];
      if (!ctrls.length) {
        ctrlHost.hidden = true;
        ctrlHost.innerHTML = "";
      } else {
        ctrlHost.hidden = false;
        ctrlHost.innerHTML =
          '<div class="pf-pop-controls-title">就地指令（YK / YT）</div>' +
          ctrls.map((c, ci) => {
            const disabled = Boolean(c.disabled);
            const key = `${dev.table_name}:${dev.idx}:${c.control}`;
            const draft = state.ctrlDrafts.has(key) ? state.ctrlDrafts.get(key) : (c.value == null ? "" : String(c.value));
            const ops = c.kind === "yk"
              ? `<span class="ctrl-btn-group"><button type="button" class="btn secondary" data-pf-yk="${ci}" data-val="1"${disabled ? " disabled" : ""}>合/启</button><button type="button" class="btn secondary" data-pf-yk="${ci}" data-val="0"${disabled ? " disabled" : ""}>分/停</button></span>`
              : `<span class="ctrl-btn-group"><input class="ctrl-inline-input pf-pop-ctrl-yt" type="number" step="any" data-pf-yt-input="${ci}" value="${esc(draft)}" placeholder="${esc(c.unit || "设定值")}"${disabled ? " disabled" : ""}><button type="button" class="btn primary" data-pf-yt-send="${ci}"${disabled ? " disabled" : ""}>下发</button></span>`;
            return `<div class="pf-pop-ctrl-row"><span class="pf-pop-ctrl-name" title="${esc(c.control)}">${esc(c.label || c.control)}</span>${ctrlStatusBadge(c)}${ops}</div>`;
          }).join("");
        ctrlHost.querySelectorAll("[data-pf-yt-input]").forEach((inp) => {
          inp.addEventListener("input", () => {
            const c = ctrls[Number(inp.dataset.pfYtInput)];
            if (c) state.ctrlDrafts.set(`${dev.table_name}:${dev.idx}:${c.control}`, inp.value);
          });
          inp.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              const c = ctrls[Number(inp.dataset.pfYtInput)];
              dispatchPortalControl(dev, c, inp.value).catch((err) => setConnection(false, err.message));
            }
          });
        });
        ctrlHost.querySelectorAll("[data-pf-yk]").forEach((btn) => {
          btn.addEventListener("click", () => {
            const c = ctrls[Number(btn.dataset.pfYk)];
            dispatchPortalControl(dev, c, Number(btn.dataset.val)).catch((err) => setConnection(false, err.message));
          });
        });
        ctrlHost.querySelectorAll("[data-pf-yt-send]").forEach((btn) => {
          btn.addEventListener("click", () => {
            const ci = Number(btn.dataset.pfYtSend);
            const c = ctrls[ci];
            const inp = ctrlHost.querySelector(`[data-pf-yt-input="${ci}"]`);
            dispatchPortalControl(dev, c, inp ? inp.value : c?.value).catch((err) => setConnection(false, err.message));
          });
        });
      }
    }
  }

  function showDevicePopover(devId, clientX, clientY, highlight = false) {
    const devPop = $("#pf-dev-popover");
    const measPop = $("#pf-meas-popover");
    if (!devPop) return;
    if (measPop) measPop.hidden = true;
    state.pfHoverMeas = null;
    state.pfHoverDev = devId;
    renderDevicePopoverContent(devId, highlight);
    devPop.hidden = false;
    positionPopover(devPop, clientX, clientY);
  }

  function setupMeasPopoverCanvas() {
    const canvas = $("#pf-meas-canvas");
    const wrap = $("#pf-mp-canvas-wrap");
    if (!canvas || !wrap) return null;
    const w = wrap.clientWidth || Math.round(wrap.getBoundingClientRect().width);
    const h = wrap.clientHeight || Math.round(wrap.getBoundingClientRect().height);
    if (w < 40 || h < 40) return null;
    const dpr = Math.max(window.devicePixelRatio || 1, 2);
    const targetW = Math.round(w * dpr);
    const targetH = Math.round(h * dpr);
    if (canvas.width !== targetW || canvas.height !== targetH) {
      canvas.width = targetW;
      canvas.height = targetH;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function clearMeasPopoverChart(message = "") {
    state.pfMpPoints = [];
    state.pfMpHoverX = null;
    const env = setupMeasPopoverCanvas();
    if (!env) return;
    if (message) {
      const pal = chartPalette();
      env.ctx.fillStyle = pal.axis;
      env.ctx.font = '12px "Microsoft YaHei", sans-serif';
      env.ctx.textAlign = "center";
      env.ctx.textBaseline = "middle";
      env.ctx.fillText(message, env.w / 2, env.h / 2);
    }
  }

  function fmtChartAxisVal(v) {
    const abs = Math.abs(v);
    if (abs >= 10000) return v.toFixed(0);
    if (abs >= 100) return v.toFixed(1);
    if (abs >= 1) return v.toFixed(2);
    return v.toFixed(3);
  }

  function computeMeasWindow(gran, offset) {
    const now = new Date();
    const pad2 = (n) => String(n).padStart(2, "0");
    let start;
    let end;
    let label;
    if (gran === "minute") {
      start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours(), now.getMinutes() + offset, 0, 0);
      end = new Date(start.getTime() + 60 * 1000);
      label = `${pad2(start.getMonth() + 1)}/${pad2(start.getDate())} ${pad2(start.getHours())}:${pad2(start.getMinutes())}`;
    } else if (gran === "day") {
      start = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset, 0, 0, 0, 0);
      end = new Date(start.getTime() + 24 * 3600 * 1000);
      label = `${start.getFullYear()}-${pad2(start.getMonth() + 1)}-${pad2(start.getDate())}`;
    } else {
      start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours() + offset, 0, 0, 0);
      end = new Date(start.getTime() + 3600 * 1000);
      label = `${pad2(start.getMonth() + 1)}/${pad2(start.getDate())} ${pad2(start.getHours())}:00–${pad2(end.getHours())}:00`;
    }
    return { startMs: start.getTime(), endMs: end.getTime(), label, gran };
  }

  function updateMeasNavUI(win) {
    const lbl = $("#pf-mp-window-label");
    if (lbl && win) lbl.textContent = win.label;
    const nextBtn = $("#pf-mp-next");
    if (nextBtn) nextBtn.disabled = state.pfMpOffset >= 0;
    const nowBtn = $("#pf-mp-now");
    if (nowBtn) nowBtn.disabled = state.pfMpOffset === 0;
  }

  function drawMeasPopoverChart(points, slot, win = state.pfMpWindow) {
    state.pfMpPoints = points || [];
    const statEl = $("#pf-mp-chart-stat");
    const env = setupMeasPopoverCanvas();
    if (!env) return;
    const { ctx, w, h } = env;
    const pal = chartPalette();
    const dark = isDarkTheme();

    if (!points || !points.length) {
      if (statEl) statEl.textContent = "暂无采样";
      ctx.fillStyle = pal.axis;
      ctx.font = '12px "Microsoft YaHei", sans-serif';
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("该时段暂无采样数据", w / 2, h / 2);
      return;
    }

    const pad = { left: 50, right: 14, top: 14, bottom: 24 };
    const plotW = Math.max(20, w - pad.left - pad.right);
    const plotH = Math.max(20, h - pad.top - pad.bottom);

    let minT = win ? win.startMs : points[0][0];
    let maxT = win ? win.endMs : points[points.length - 1][0];
    if (maxT <= minT) maxT = minT + 60000;

    let minV = Infinity;
    let maxV = -Infinity;
    for (const [, v] of points) {
      if (v < minV) minV = v;
      if (v > maxV) maxV = v;
    }
    if (statEl) {
      statEl.textContent = `${points.length} 点 · ${fmtChartAxisVal(minV)} ~ ${fmtChartAxisVal(maxV)}`;
    }

    const hasLimits = slot && slot.max_value != null && slot.min_value != null && Number(slot.max_value) > Number(slot.min_value);
    const isSocPct = slot?.metric === "soc" && maxV > 1.5;
    const limMax = hasLimits ? (isSocPct && Number(slot.max_value) <= 1.5 ? Number(slot.max_value) * 100 : Number(slot.max_value)) : null;
    const limMin = hasLimits ? (isSocPct && Number(slot.min_value) <= 1.5 ? Number(slot.min_value) * 100 : Number(slot.min_value)) : null;

    if (Math.abs(maxV - minV) < 1e-6) {
      const span = Math.max(0.5, Math.abs(maxV) * 0.08);
      minV -= span;
      maxV += span;
    } else {
      const m = (maxV - minV) * 0.14;
      minV -= m;
      maxV += m;
    }

    const xFor = (t) => pad.left + ((t - minT) / (maxT - minT)) * plotW;
    const yFor = (v) => pad.top + (1 - (v - minV) / (maxV - minV)) * plotH;

    // 1. 精细 4 档水平网格与 Y 轴刻度（0.5px 像素对齐）
    ctx.lineWidth = 1;
    ctx.font = '10.5px "JetBrains Mono", Consolas, monospace';
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const ySteps = 3;
    for (let i = 0; i <= ySteps; i += 1) {
      const ratio = i / ySteps;
      const y = Math.round(pad.top + ratio * plotH) + 0.5;
      const v = maxV - ratio * (maxV - minV);
      ctx.strokeStyle = i === ySteps ? (dark ? "rgba(148,163,184,0.28)" : "rgba(100,116,139,0.32)") : pal.grid;
      ctx.setLineDash(i === ySteps ? [] : [3, 3]);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + plotW, y);
      ctx.stroke();
      ctx.fillStyle = pal.axis;
      ctx.fillText(fmtChartAxisVal(v), pad.left - 7, y);
    }

    // 2. 4 档 X 轴时间刻度与极淡垂直网格
    ctx.setLineDash([]);
    ctx.fillStyle = pal.axis;
    ctx.textBaseline = "top";
    const gran = win?.gran || state.pfMpGran;
    const fmtTick = (ms, isLast) => {
      if (gran === "day" && isLast) return "24:00";
      const d = new Date(ms);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return gran === "minute" ? `${mm}:${ss}` : `${hh}:${mm}`;
    };
    const xSteps = 3;
    for (let i = 0; i <= xSteps; i += 1) {
      const ratio = i / xSteps;
      const t = minT + ratio * (maxT - minT);
      const x = Math.round(pad.left + ratio * plotW) + 0.5;
      if (i > 0 && i < xSteps) {
        ctx.strokeStyle = dark ? "rgba(148,163,184,0.08)" : "rgba(100,116,139,0.10)";
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();
      }
      ctx.textAlign = i === 0 ? "left" : i === xSteps ? "right" : "center";
      ctx.fillText(fmtTick(t, i === xSteps), x, pad.top + plotH + 6);
    }

    // 3. 有效上下限参考虚线（落在当前视野内时绘出并标注）
    const drawLimitLine = (val, color, tag) => {
      if (val === null || val < minV || val > maxV) return;
      const y = Math.round(yFor(val)) + 0.5;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + plotW, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.font = '9.5px "JetBrains Mono", Consolas, monospace';
      ctx.textAlign = "right";
      ctx.textBaseline = "bottom";
      ctx.fillText(`${tag} ${fmtChartAxisVal(val)}`, pad.left + plotW - 2, y - 2);
      ctx.restore();
    };
    drawLimitLine(limMax, dark ? "rgba(251,146,60,0.8)" : "rgba(194,65,12,0.8)", "H");
    drawLimitLine(limMin, dark ? "rgba(165,180,252,0.8)" : "rgba(67,56,202,0.8)", "L");

    // 4. 垂直渐变面积填充 + 抗锯齿发光曲线 + 末端高亮实时点
    const lineColor = dark ? "#38BDF8" : "#2563EB";
    const glowColor = dark ? "rgba(56, 189, 248, 0.45)" : "rgba(37, 99, 235, 0.22)";
    const baseLineY = pad.top + plotH;

    if (points.length >= 2) {
      const grad = ctx.createLinearGradient(0, pad.top, 0, baseLineY);
      grad.addColorStop(0, dark ? "rgba(56, 189, 248, 0.28)" : "rgba(37, 99, 235, 0.22)");
      grad.addColorStop(1, dark ? "rgba(56, 189, 248, 0.01)" : "rgba(37, 99, 235, 0.01)");
      ctx.beginPath();
      points.forEach(([t, v], i) => {
        const x = xFor(t);
        const y = yFor(v);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.lineTo(xFor(points[points.length - 1][0]), baseLineY);
      ctx.lineTo(xFor(points[0][0]), baseLineY);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.save();
      ctx.strokeStyle = lineColor;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.shadowColor = glowColor;
      ctx.shadowBlur = 6;
      ctx.beginPath();
      points.forEach(([t, v], i) => {
        const x = xFor(t);
        const y = yFor(v);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.restore();
    }

    // 末端实时点（无悬停时高亮末端最新值）
    const lastPt = points[points.length - 1];
    const lastX = xFor(lastPt[0]);
    const lastY = yFor(lastPt[1]);
    ctx.save();
    ctx.fillStyle = dark ? "rgba(56,189,248,0.28)" : "rgba(37,99,235,0.22)";
    ctx.beginPath();
    ctx.arc(lastX, lastY, 5.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = dark ? "#e6f7ff" : "#ffffff";
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(lastX, lastY, 2.8, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.restore();

    // 5. 悬停十字准星与精确读数气泡
    if (
      state.pfMpHoverX !== null
      && state.pfMpHoverX >= pad.left
      && state.pfMpHoverX <= pad.left + plotW
    ) {
      const hoverT = minT + ((state.pfMpHoverX - pad.left) / plotW) * (maxT - minT);
      let best = points[0];
      let bestDist = Infinity;
      for (const pt of points) {
        const dist = Math.abs(pt[0] - hoverT);
        if (dist < bestDist) {
          bestDist = dist;
          best = pt;
        }
      }
      const px = Math.round(xFor(best[0])) + 0.5;
      const py = yFor(best[1]);

      ctx.save();
      ctx.strokeStyle = pal.crosshair;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(px, pad.top);
      ctx.lineTo(px, baseLineY);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = lineColor;
      ctx.strokeStyle = dark ? "#081121" : "#ffffff";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(px, py, 3.8, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();

      const d = new Date(best[0]);
      const tStr = d.toLocaleTimeString("zh-CN", { hour12: false });
      const vStr = `${fmtChartAxisVal(best[1])}${slot?.unit ? " " + slot.unit : ""}`;
      const labelText = `${tStr}  ${vStr}`;
      ctx.font = '10.5px "JetBrains Mono", Consolas, monospace';
      const tw = ctx.measureText(labelText).width;
      const boxW = tw + 14;
      const boxH = 20;
      let bx = px + 8;
      if (bx + boxW > w - 6) bx = px - boxW - 8;
      bx = Math.max(pad.left + 2, bx);
      const by = Math.max(4, Math.min(baseLineY - boxH - 4, py - boxH / 2));

      ctx.fillStyle = pal.tooltipBg;
      ctx.strokeStyle = pal.tooltipBorder;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(bx, by, boxW, boxH, 4);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = pal.tooltipText;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(labelText, bx + 7, by + boxH / 2 + 0.5);
      ctx.restore();
    }
  }

  async function loadMeasPopoverTrend(slot, gran = state.pfMpGran, offset = state.pfMpOffset) {
    const win = computeMeasWindow(gran, offset);
    state.pfMpWindow = win;
    updateMeasNavUI(win);
    const statEl = $("#pf-mp-chart-stat");
    if (!slot || !slot.table_name || slot.idx == null || !slot.metric) {
      drawMeasPopoverChart([], slot, win);
      return;
    }
    const cacheKey = `${slot.table_name}:${slot.idx}:${slot.metric}:${win.startMs}:${win.endMs}`;
    const cached = state.pfMpTrendCache.get(cacheKey);
    const ttl = offset === 0 ? 4000 : 30000;
    if (cached && Date.now() - cached.ts < ttl) {
      drawMeasPopoverChart(cached.points, slot, win);
      return;
    }
    const seq = ++state.pfMpReqSeq;
    clearMeasPopoverChart("加载曲线…");
    if (statEl) statEl.textContent = "加载曲线…";
    try {
      const data = await api(
        `/api/devices/${encodeURIComponent(slot.table_name)}/${slot.idx}/trend?start_ms=${win.startMs}&end_ms=${win.endMs}&metric=${encodeURIComponent(slot.metric)}`
      );
      if (seq !== state.pfMpReqSeq) return;
      const series = (data.series || []).find((s) => s.metric === slot.metric);
      const pts = series ? series.points || [] : [];
      if (state.pfMpTrendCache.size >= 120) {
        const oldestKey = state.pfMpTrendCache.keys().next().value;
        if (oldestKey !== undefined) state.pfMpTrendCache.delete(oldestKey);
      }
      state.pfMpTrendCache.set(cacheKey, { ts: Date.now(), points: pts });
      drawMeasPopoverChart(pts, slot, win);
    } catch (_) {
      if (seq === state.pfMpReqSeq) drawMeasPopoverChart([], slot, win);
    }
  }

  function renderMeasPopoverContent(devId, mt, reloadChart = true) {
    const slot = state.pfData?.svg_slots?.[devId]?.[mt];
    const dev = state.pfData?.devices?.[devId];
    const title = slot?.label || mt;
    const devName = slot?.device_name || dev?.name || devId;
    $("#pf-mp-title").textContent = `${devName} · ${title}`;
    const fieldRef = slot?.table_name && slot?.idx != null && slot?.metric
      ? `${slot.table_name}#${slot.idx}.${slot.metric}`
      : `${devId}.${mt}`;
    $("#pf-mp-sub").textContent = fieldRef;
    const valStr = slot
      ? `${typeof slot.value === "number" ? fmtNum(slot.value, 2) : (slot.value ?? "--")} ${slot.unit || ""}`.trim()
      : "--";
    $("#pf-mp-value").textContent = valStr;
    $("#pf-mp-badge").innerHTML = slot ? qualityBadge(slot) : '<span class="badge muted">无配置</span>';

    const rtuPnt = slot?.rtu_id != null && slot?.point_no != null
      ? `RTU ${slot.rtu_id} / #${slot.point_no}`
      : "未配置";
    const statusText = slot?.quality != null
      ? `${slot.quality} · ${slot.quality_label || ""}`.trim()
      : (slot?.valid ? "0 · 正常" : "—");
    const minStr = slot?.min_value != null ? fmtLimitCompact(slot.min_value) : "—";
    const maxStr = slot?.max_value != null ? fmtLimitCompact(slot.max_value) : "—";
    const weightStr = slot?.weight != null ? fmtLimitCompact(slot.weight) : "—";
    const timeStr = slot?.source_time ? fmtTime(slot.source_time).slice(5) : "—";

    $("#pf-mp-meta-grid").innerHTML = buildKvTableHtml([
      { label: "RTU / 点号", value: rtuPnt },
      { label: "量测状态", value: statusText },
      { label: "下限 (min)", value: minStr },
      { label: "上限 (max)", value: maxStr },
      { label: "权重", value: weightStr },
      { label: "更新时间", value: timeStr },
    ]);

    state.pfHoverMeas = { devId, mt, slot };
    if (reloadChart) {
      loadMeasPopoverTrend(slot, state.pfMpGran, state.pfMpOffset);
    }
  }

  function showMeasPopover(devId, mt, clientX, clientY) {
    const devPop = $("#pf-dev-popover");
    const measPop = $("#pf-meas-popover");
    if (!measPop) return;
    if (devPop) devPop.hidden = true;
    state.pfHoverDev = null;
    state.pfMpOffset = 0;
    // 先填充文本并取消 hidden 完成布局，再按真实高清像素尺寸绘曲线，彻底避免 0x0 回退拉伸模糊
    renderMeasPopoverContent(devId, mt, false);
    measPop.hidden = false;
    positionPopover(measPop, clientX, clientY);
    loadMeasPopoverTrend(state.pfHoverMeas?.slot, state.pfMpGran, 0);
  }

  function focusSvgDevice(devId) {
    const svg = $("#pf-stage svg");
    const wrap = $("#pf-canvas-wrap");
    if (!svg || !wrap) return;
    const el = svg.querySelector(`use[dev-id="${CSS.escape(devId)}"]`);
    if (!el) return;
    const x = Number(el.getAttribute("x") || 0) + Number(el.getAttribute("width") || 0) / 2;
    const y = Number(el.getAttribute("y") || 0) + Number(el.getAttribute("height") || 0) / 2;
    const rect = wrap.getBoundingClientRect();
    state.pfScale = Math.max(state.pfScale, 0.9);
    state.pfX = rect.width / 2 - x * state.pfScale;
    state.pfY = rect.height / 2 - y * state.pfScale;
    applySvgTransform();
    showDevicePopover(devId, rect.left + rect.width / 2 + 24, rect.top + rect.height / 2 - 20, true);
  }

  async function loadPowerflow() {
    await ensureSvgLoaded();
    const pf = await api("/api/powerflow");
    state.pfData = pf;
    updateSvgLiveValues(pf);
  }

  // 潮流图平移与缩放交互
  (() => {
    const wrap = $("#pf-canvas-wrap");
    if (!wrap) return;
    let dragging = false;
    let startX = 0;
    let startY = 0;
    let origX = 0;
    let origY = 0;

    wrap.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || e.target.closest("use[dev-id], g.mg[dev], .pf-popover")) return;
      hidePfPopovers();
      dragging = true;
      startX = e.clientX;
      startY = e.clientY;
      origX = state.pfX;
      origY = state.pfY;
      wrap.classList.add("dragging");
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      state.pfX = origX + (e.clientX - startX);
      state.pfY = origY + (e.clientY - startY);
      applySvgTransform();
    });
    window.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      wrap.classList.remove("dragging");
    });
    wrap.addEventListener("wheel", (e) => {
      if (e.target.closest(".pf-popover")) return;
      e.preventDefault();
      hidePfPopovers();
      const rect = wrap.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const factor = e.deltaY < 0 ? 1.15 : 0.87;
      const next = Math.max(0.15, Math.min(3.5, state.pfScale * factor));
      state.pfX = mx - ((mx - state.pfX) * next) / state.pfScale;
      state.pfY = my - ((my - state.pfY) * next) / state.pfScale;
      state.pfScale = next;
      applySvgTransform();
    }, { passive: false });

    // 悬浮框自身鼠标进出与 当分/当时/当日 分页标签及 ◀/当前/▶ 步进切换
    ["#pf-dev-popover", "#pf-meas-popover"].forEach((sel) => {
      const el = $(sel);
      el?.addEventListener("mouseenter", cancelHidePfPopovers);
      el?.addEventListener("mouseleave", () => scheduleHidePfPopovers());
    });
    $$("#pf-mp-tabs .pf-mp-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        const gran = btn.dataset.mpGran || "hour";
        state.pfMpGran = gran;
        state.pfMpOffset = 0;
        $$("#pf-mp-tabs .pf-mp-tab").forEach((b) => {
          const active = b === btn;
          b.classList.toggle("active", active);
          b.setAttribute("aria-selected", active ? "true" : "false");
        });
        if (state.pfHoverMeas?.slot) {
          loadMeasPopoverTrend(state.pfHoverMeas.slot, gran, 0);
        }
      });
    });
    const shiftMeasWindow = (delta) => {
      const next = delta === 0 ? 0 : Math.min(0, state.pfMpOffset + delta);
      if (next === state.pfMpOffset && delta !== 0) return;
      state.pfMpOffset = next;
      if (state.pfHoverMeas?.slot) {
        loadMeasPopoverTrend(state.pfHoverMeas.slot, state.pfMpGran, state.pfMpOffset);
      }
    };
    $("#pf-mp-prev")?.addEventListener("click", () => shiftMeasWindow(-1));
    $("#pf-mp-now")?.addEventListener("click", () => shiftMeasWindow(0));
    $("#pf-mp-next")?.addEventListener("click", () => shiftMeasWindow(1));
    $("#pf-meas-canvas")?.addEventListener("mousemove", (e) => {
      if (!state.pfMpPoints.length || !state.pfHoverMeas?.slot) return;
      const rect = e.currentTarget.getBoundingClientRect();
      state.pfMpHoverX = e.clientX - rect.left;
      drawMeasPopoverChart(state.pfMpPoints, state.pfHoverMeas.slot);
    });
    $("#pf-meas-canvas")?.addEventListener("mouseleave", () => {
      if (state.pfMpHoverX === null) return;
      state.pfMpHoverX = null;
      if (state.pfMpPoints.length && state.pfHoverMeas?.slot) {
        drawMeasPopoverChart(state.pfMpPoints, state.pfHoverMeas.slot);
      }
    });

    $("#pf-zoom-in")?.addEventListener("click", () => {
      state.pfScale = Math.min(3.5, state.pfScale * 1.2);
      applySvgTransform();
    });
    $("#pf-zoom-out")?.addEventListener("click", () => {
      state.pfScale = Math.max(0.15, state.pfScale / 1.2);
      applySvgTransform();
    });
    $("#pf-zoom-fit")?.addEventListener("click", fitSvgView);
    $("#pf-show-arrows")?.addEventListener("change", (e) => {
      state.pfShowArrows = Boolean(e.target.checked);
      $("#pf-stage").classList.toggle("hide-arrows", !state.pfShowArrows);
      if (state.pfShowArrows) startPowerflowAnimation();
      else stopPowerflowAnimation();
    });
    $("#pf-show-meas")?.addEventListener("change", (e) => {
      $("#pf-stage").classList.toggle("hide-meas", !e.target.checked);
    });
    $("#pf-show-labels")?.addEventListener("change", (e) => {
      $("#pf-stage").classList.toggle("hide-labels", !e.target.checked);
    });
    let lastPfQuery = "";
    let lastPfMatchIdx = -1;
    $("#pf-search")?.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const q = e.target.value.trim().toLowerCase();
      if (!q || !state.pfData?.devices) return;
      const matches = Object.values(state.pfData.devices).filter(
        (d) => d.name.toLowerCase().includes(q) || d.dev_id.toLowerCase().includes(q)
      );
      const statEl = $("#pf-stat");
      if (!matches.length) {
        lastPfQuery = q;
        lastPfMatchIdx = -1;
        if (statEl) statEl.textContent = `未找到匹配设备：${e.target.value.trim()}`;
        return;
      }
      if (q === lastPfQuery) {
        lastPfMatchIdx = (lastPfMatchIdx + 1) % matches.length;
      } else {
        lastPfQuery = q;
        lastPfMatchIdx = 0;
      }
      const target = matches[lastPfMatchIdx];
      focusSvgDevice(target.dev_id);
      if (statEl && matches.length > 1) {
        statEl.textContent = `定位 ${lastPfMatchIdx + 1}/${matches.length}：${target.name}`;
      }
    });
  })();

  // ==================== 3. 设备图（分类标签 + 实时值 + 日曲线） ====================
  function primaryMetricSummary(dev) {
    const order = ["p", "i_p", "ac_p", "dc_p", "soc", "uab", "u", "pressure", "ia", "i"];
    for (const key of order) {
      const m = (dev.metrics || []).find((x) => x.metric === key && x.display_value !== null);
      if (m) return `${fmtNum(m.display_value, 1)} ${m.unit || ""}`.trim();
    }
    return `${dev.valid_count}/${dev.bound_count}`;
  }

  let devReqSeq = 0;
  let trendReqSeq = 0;

  async function loadDevices() {
    const seq = ++devReqSeq;
    const search = ($("#dev-search")?.value || "").trim();
    const qs = new URLSearchParams();
    if (state.devCategory) qs.set("category", state.devCategory);
    if (search) qs.set("search", search);
    const data = await api("/api/devices?" + qs.toString());
    if (seq !== devReqSeq) return;
    state.devCategories = data.categories || [];
    if (!state.devCategory && state.devCategories.length) {
      state.devCategory = state.devCategories[0].key;
      return loadDevices();
    }
    state.devItems = data.items || [];
    const filteredDevs = state.devItems.filter((d) => {
      if (state.devFilter === "alarm") return Number(d.alarm_count || 0) > 0;
      if (state.devFilter === "running") {
        if (d.position !== null) return Boolean(d.position);
        return d.run !== 0;
      }
      return true;
    });

    // 渲染设备类别标签
    const tabsEl = $("#dev-cat-tabs");
    tabsEl.innerHTML = state.devCategories
      .map(
        (c) =>
          `<button type="button" class="tab-btn ${c.key === state.devCategory ? "active" : ""}" data-cat="${esc(c.key)}">${esc(c.label)}<small>${c.count}</small></button>`
      )
      .join("");
    tabsEl.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.devCategory = btn.dataset.cat;
        state.devSelected = null;
        loadDevices();
      });
    });

    const curCat = state.devCategories.find((c) => c.key === state.devCategory);
    $("#dev-list-title").textContent = curCat ? curCat.label : "设备列表";
    $("#dev-list-count").textContent = String(filteredDevs.length);

    // 选中默认或保持原选中设备
    let activeDev = state.devSelected
      ? filteredDevs.find((d) => d.dev_id === state.devSelected.dev_id)
      : null;
    if (!activeDev && filteredDevs.length) {
      activeDev = filteredDevs[0];
      state.devSelected = { table_name: activeDev.table_name, idx: activeDev.idx, dev_id: activeDev.dev_id };
      state.trendHidden.clear();
    }

    const listEl = $("#dev-list");
    if (!filteredDevs.length) {
      listEl.innerHTML = '<div class="empty-hint">无匹配设备</div>';
      renderDeviceDetail(null);
      return;
    }

    listEl.innerHTML = filteredDevs
      .map((d) => {
        const selected = activeDev && d.dev_id === activeDev.dev_id;
        const okBadge =
          d.position !== null
            ? d.position ? '<span class="badge ok">合闸</span>' : '<span class="badge danger">分闸</span>'
            : d.run === 0
              ? '<span class="badge warn">停运</span>'
              : '<span class="badge ok">运行</span>';
        const alarmDot = Number(d.alarm_count || 0) > 0
          ? `<span class="dev-alarm-dot" title="当前 ${d.alarm_count} 项告警"></span>`
          : "";
        return (
          `<button type="button" class="dev-item ${selected ? "selected" : ""}" data-dev-id="${esc(d.dev_id)}">` +
          `<div class="dev-item-top"><strong title="${esc(d.name)}">${alarmDot}${esc(d.name)}</strong>${okBadge}</div>` +
          `<div class="dev-item-sub"><span>${esc(d.table_name)} #${d.idx}</span><span class="num">${esc(primaryMetricSummary(d))}</span></div>` +
          `</button>`
        );
      })
      .join("");

    listEl.querySelectorAll(".dev-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        const dev = filteredDevs.find((x) => x.dev_id === btn.dataset.devId);
        if (!dev) return;
        state.devSelected = { table_name: dev.table_name, idx: dev.idx, dev_id: dev.dev_id };
        state.trendHidden.clear();
        listEl.querySelectorAll(".dev-item").forEach((el) => el.classList.toggle("selected", el === btn));
        renderDeviceDetail(dev);
        if (state.compareMode && state.compareTargets.length) loadCompareTrends();
        else loadDeviceTrend(dev);
      });
    });

    renderDeviceDetail(activeDev);
    if (state.compareMode && state.compareTargets.length) await loadCompareTrends();
    else if (activeDev) await loadDeviceTrend(activeDev);
  }

  function pickPrimaryMetric(dev) {
    const order = ["p", "i_p", "ac_p", "dc_p", "soc", "uab", "u", "pressure", "flow", "ia", "i"];
    for (const key of order) {
      const m = (dev?.metrics || []).find((x) => x.metric === key && x.kind === "yc");
      if (m) return m;
    }
    return (dev?.metrics || []).find((x) => x.kind === "yc") || null;
  }

  function updateCompareBarUI() {
    const bar = $("#compare-pill-bar");
    const badge = $("#compare-count-badge");
    const toggleBtn = $("#compare-toggle-btn");
    const titleEl = $("#trend-title-text");
    if (!bar) return;
    const cnt = state.compareTargets.length;
    bar.hidden = cnt === 0;
    if (badge) badge.textContent = `对比 ${cnt}`;
    if (toggleBtn) {
      toggleBtn.textContent = state.compareMode ? "返回单设备" : "同图对比";
      toggleBtn.classList.toggle("primary", state.compareMode);
    }
    if (titleEl) {
      titleEl.textContent = state.compareMode ? `多设备曲线对比（${cnt} 项）` : "设备日曲线";
    }
  }

  async function loadCompareTrends() {
    if (!state.compareTargets.length) {
      state.compareMode = false;
      updateCompareBarUI();
      return;
    }
    const seq = ++trendReqSeq;
    const hours = Number($("#trend-hours")?.value || 24);
    const targetsParam = state.compareTargets.map((t) => t.key).join(",");
    const data = await api(`/api/trends/compare?targets=${encodeURIComponent(targetsParam)}&hours=${hours}`);
    if (seq !== trendReqSeq) return;
    state.trendData = data;
    updateCompareBarUI();
    renderTrendChips();
    drawTrendChart();
  }

  function exportTrendCsv() {
    const series = (state.trendData?.series || []).filter((s) => !state.trendHidden.has(s.key || s.metric));
    if (!series.length) {
      setConnection(false, "当前无可导出的曲线数据");
      return;
    }
    const tsSet = new Set();
    const maps = series.map((s) => {
      const m = new Map();
      (s.points || []).forEach(([t, v]) => {
        tsSet.add(t);
        m.set(t, v);
      });
      return m;
    });
    const sortedTs = Array.from(tsSet).sort((a, b) => a - b);
    const header = ["时间", ...series.map((s) => `${s.label}${s.unit ? " (" + s.unit + ")" : ""}`)];
    const csvRows = [header.map((h) => `"${String(h).replace(/"/g, '""')}"`).join(",")];
    for (const ts of sortedTs) {
      const row = [fmtTime(ts), ...maps.map((m) => (m.has(ts) ? String(m.get(ts)) : ""))];
      csvRows.push(row.join(","));
    }
    const blob = new Blob(["\uFEFF" + csvRows.join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `scada-trend-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function applyDevDetailTab(tab) {
    const ctrlBtn = $("#dev-tab-control-btn");
    const hasCtrl = Boolean(ctrlBtn && !ctrlBtn.hidden);
    const nextTab = tab === "control" && hasCtrl ? "control" : "monitor";
    state.devDetailTab = nextTab;
    $$("#dev-detail-tabs [data-dev-tab]").forEach((btn) => {
      const active = btn.dataset.devTab === nextTab;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", String(active));
    });
    const monPane = $("#dev-tab-monitor");
    const ctrlPane = $("#dev-tab-control");
    if (monPane) monPane.hidden = nextTab !== "monitor";
    if (ctrlPane) ctrlPane.hidden = nextTab !== "control";
    if (nextTab === "monitor") {
      requestAnimationFrame(() => drawTrendChart());
    }
  }

  function renderDeviceControls(dev) {
    const tbody = $("#dev-ctrl-body");
    const hint = $("#dev-ctrl-hint");
    const liveStrip = $("#dev-ctrl-live-strip");
    const ctrlTabBtn = $("#dev-tab-control-btn");
    const ctrlTabCnt = $("#dev-tab-control-cnt");
    if (!tbody) return;
    const ctrls = dev?.controls || [];
    if (ctrlTabBtn) ctrlTabBtn.hidden = !ctrls.length;
    if (ctrlTabCnt) ctrlTabCnt.textContent = String(ctrls.length);
    if (!dev || !ctrls.length) {
      tbody.innerHTML = "";
      if (liveStrip) liveStrip.innerHTML = "";
      applyDevDetailTab("monitor");
      return;
    }
    if (liveStrip) {
      liveStrip.innerHTML = (dev.metrics || []).slice(0, 6).map((m) => {
        const valStr = m.kind === "yx"
          ? (m.metric === "position" ? (m.value ? "合闸" : "分闸") : (m.value ? "运行" : "停运"))
          : `${fmtNum(m.display_value, 2)}${m.unit ? " " + m.unit : ""}`;
        return `<span class="dev-ctrl-live-item"><span>${esc(m.label)}</span><strong>${esc(valStr)}</strong></span>`;
      }).join("");
    }
    if (hint) hint.textContent = `${ctrls.length} 项指令 · RTU ${ctrls[0].rtu_id}`;
    tbody.innerHTML = ctrls.map((c, ci) => {
      const disabled = Boolean(c.disabled);
      const key = `${dev.table_name}:${dev.idx}:${c.control}`;
      const draft = state.ctrlDrafts.has(key) ? state.ctrlDrafts.get(key) : (c.value == null ? "" : String(c.value));
      const valCell = c.kind === "yt"
        ? `<td class="ctrl-cell-input"><input class="ctrl-inline-input" type="number" step="any" data-dev-yt-input="${ci}" value="${esc(draft)}" placeholder="${esc(c.unit ? "设定(" + c.unit + ")" : "输入设定值")}"${disabled ? " disabled" : ""}></td>`
        : `<td class="num">${c.value == null ? "—" : (Number(c.value) === 1 ? "1 · 合/启" : "0 · 分/停")}</td>`;
      const limStr = (c.min_value == null && c.max_value == null) ? "—" : `${fmtLimitCompact(c.min_value)} ~ ${fmtLimitCompact(c.max_value)}`;
      const opCell = c.kind === "yk"
        ? `<span class="ctrl-btn-group"><button type="button" class="btn secondary" data-dev-yk="${ci}" data-val="1"${disabled ? " disabled" : ""}>合/启 (1)</button><button type="button" class="btn secondary" data-dev-yk="${ci}" data-val="0"${disabled ? " disabled" : ""}>分/停 (0)</button></span>`
        : `<span class="ctrl-btn-group"><button type="button" class="btn primary" data-dev-yt-send="${ci}"${disabled ? " disabled" : ""}>下发</button></span>`;
      return `<tr>` +
        `<td><strong>${esc(c.label || c.control)}</strong> <small class="muted">${esc(c.control)}${c.unit ? " · " + esc(c.unit) : ""}</small></td>` +
        `<td><span class="badge">${esc(c.kind.toUpperCase())}</span></td>` +
        valCell +
        `<td class="num">${esc(limStr)}</td>` +
        `<td>${ctrlStatusBadge(c)}</td>` +
        `<td>${opCell}</td>` +
        `</tr>`;
    }).join("");

    tbody.querySelectorAll("[data-dev-yt-input]").forEach((inp) => {
      inp.addEventListener("input", () => {
        const c = ctrls[Number(inp.dataset.devYtInput)];
        if (!c) return;
        state.ctrlDrafts.set(`${dev.table_name}:${dev.idx}:${c.control}`, inp.value);
        const raw = inp.value.trim();
        const v = raw === "" ? null : Number(raw);
        const bad = raw !== "" && (!Number.isFinite(v) || (
          c.max_value != null && c.min_value != null && Number(c.max_value) > Number(c.min_value)
          && (v > Number(c.max_value) || v < Number(c.min_value))
        ));
        inp.classList.toggle("limit-invalid", bad);
      });
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          const c = ctrls[Number(inp.dataset.devYtInput)];
          dispatchPortalControl(dev, c, inp.value).catch((err) => setConnection(false, err.message));
        }
      });
    });
    tbody.querySelectorAll("[data-dev-yk]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const c = ctrls[Number(btn.dataset.devYk)];
        dispatchPortalControl(dev, c, Number(btn.dataset.val)).catch((err) => setConnection(false, err.message));
      });
    });
    tbody.querySelectorAll("[data-dev-yt-send]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const ci = Number(btn.dataset.devYtSend);
        const c = ctrls[ci];
        const inp = tbody.querySelector(`[data-dev-yt-input="${ci}"]`);
        dispatchPortalControl(dev, c, inp ? inp.value : c?.value).catch((err) => setConnection(false, err.message));
      });
    });
    applyDevDetailTab(state.devDetailTab);
  }

  function renderDeviceDetail(dev) {
    const grid = $("#dev-metric-grid");
    const locBtn = $("#dev-locate-svg");
    const cmpBtn = $("#dev-add-compare");
    renderDeviceControls(dev);
    if (!dev) {
      $("#dev-detail-name").textContent = "请选择左侧设备";
      $("#dev-detail-meta").textContent = "";
      grid.innerHTML = "";
      locBtn.disabled = true;
      if (cmpBtn) cmpBtn.disabled = true;
      state.trendData = null;
      drawTrendChart();
      return;
    }
    $("#dev-detail-name").textContent = dev.name;
    $("#dev-detail-meta").textContent = `${dev.table_name} · 编号 #${dev.idx} · 有效量测 ${dev.valid_count}/${dev.bound_count}` +
      (dev.control_count ? ` · 指令 ${dev.control_count}` : "");
    locBtn.disabled = false;
    locBtn.onclick = async () => {
      switchView("powerflow");
      await loadPowerflow();
      focusSvgDevice(dev.dev_id);
    };
    const primary = pickPrimaryMetric(dev);
    if (cmpBtn) {
      cmpBtn.disabled = !primary;
      cmpBtn.onclick = () => {
        if (!primary) return;
        const key = `${dev.table_name}:${dev.idx}:${primary.metric}`;
        if (!state.compareTargets.some((t) => t.key === key)) {
          if (state.compareTargets.length >= 8) {
            setConnection(false, "最多同时对比 8 条曲线");
            return;
          }
          state.compareTargets.push({
            key,
            table_name: dev.table_name,
            idx: dev.idx,
            metric: primary.metric,
            label: `${dev.name} · ${primary.label}`,
          });
        }
        state.compareMode = true;
        updateCompareBarUI();
        loadCompareTrends().catch((err) => setConnection(false, err.message));
      };
    }
    grid.innerHTML = (dev.metrics || [])
      .map((m, idx) => {
        const valStr = m.kind === "yx"
          ? (m.metric === "position" ? (m.value ? "合闸" : "分闸") : (m.value ? "运行" : "停运"))
          : fmtNum(m.display_value, 2);
        const qBadge = qualityBadge(m);
        const hasLimits = m.min_value != null || m.max_value != null;
        const limitBtn = m.kind === "yc"
          ? `<button type="button" class="m-limit-btn" data-limit-idx="${idx}" title="点击修改上下限">${hasLimits ? `${esc(fmtLimitCompact(m.min_value))} ~ ${esc(fmtLimitCompact(m.max_value))}` : "设限"}</button>`
          : `<span>${esc(m.metric)}</span>`;
        return (
          `<div class="metric-box">` +
          `<div class="m-top">` +
          `<div class="m-main"><span class="m-name">${esc(m.label)}</span><span class="metric-val">${esc(valStr)}<small>${esc(m.unit || "")}</small></span></div>` +
          `${qBadge}` +
          `</div>` +
          `<div class="m-foot"><span>RTU ${m.rtu_id ?? "-"} / #${m.point_no ?? "-"}</span>${limitBtn}</div>` +
          `</div>`
        );
      })
      .join("");
    bindLimitDialog();
    grid.querySelectorAll(".m-limit-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = (dev.metrics || [])[Number(btn.dataset.limitIdx)];
        if (m) openLimitDialog(dev, m);
      });
    });
  }

  async function loadDeviceTrend(dev) {
    const seq = ++trendReqSeq;
    const hours = Number($("#trend-hours")?.value || 24);
    const data = await api(`/api/devices/${encodeURIComponent(dev.table_name)}/${dev.idx}/trend?hours=${hours}`);
    if (seq !== trendReqSeq) return;
    state.trendData = data;
    renderTrendChips();
    drawTrendChart();
  }

  function chartPalette() {
    const dark = isDarkTheme();
    return {
      dark,
      colors: dark ? SERIES_COLORS_DARK : SERIES_COLORS_LIGHT,
      grid: dark ? "rgba(148, 163, 184, 0.14)" : "rgba(100, 116, 139, 0.18)",
      axis: dark ? "#94A3B8" : "#64748B",
      crosshair: dark ? "rgba(0, 229, 255, 0.45)" : "rgba(37, 99, 235, 0.45)",
      tooltipBg: dark ? "rgba(8, 17, 33, 0.92)" : "rgba(255, 255, 255, 0.95)",
      tooltipBorder: dark ? "rgba(0, 229, 255, 0.45)" : "rgba(148, 163, 184, 0.55)",
      tooltipText: dark ? "#E6F7FF" : "#0F172A",
    };
  }

  function renderTrendChips() {
    const el = $("#trend-series-chips");
    if (!el) return;
    const series = state.trendData?.series || [];
    const palette = chartPalette();
    el.innerHTML = series
      .map((s, idx) => {
        const skey = s.key || s.metric;
        const color = palette.colors[idx % palette.colors.length];
        const hidden = state.trendHidden.has(skey);
        return (
          `<button type="button" class="series-chip ${hidden ? "muted" : ""}" data-metric="${esc(skey)}">` +
          `<span class="chip-dot" style="background:${color}"></span>` +
          `<span>${esc(s.label)}${s.unit ? " (" + esc(s.unit) + ")" : ""}</span>` +
          `</button>`
        );
      })
      .join("");
    el.querySelectorAll(".series-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = btn.dataset.metric;
        if (state.trendHidden.has(m)) state.trendHidden.delete(m);
        else state.trendHidden.add(m);
        renderTrendChips();
        drawTrendChart();
      });
    });
  }

  function drawTrendChart() {
    const canvas = $("#trend-canvas");
    const wrap = $("#trend-canvas-wrap");
    if (!canvas || !wrap) return;
    const rect = wrap.getBoundingClientRect();
    if (rect.width < 40 || rect.height < 40) return;

    const dpr = window.devicePixelRatio || 1;
    const targetW = Math.round(rect.width * dpr);
    const targetH = Math.round(rect.height * dpr);
    if (canvas.width !== targetW || canvas.height !== targetH) {
      canvas.width = targetW;
      canvas.height = targetH;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);

    const pal = chartPalette();
    const allSeries = state.trendData?.series || [];
    const visible = allSeries
      .map((s, idx) => ({ ...s, color: pal.colors[idx % pal.colors.length] }))
      .filter((s) => !state.trendHidden.has(s.key || s.metric) && s.points && s.points.length);

    if (!visible.length) {
      ctx.fillStyle = pal.axis;
      ctx.font = "13px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("暂无可绘制的日曲线采样数据", rect.width / 2, rect.height / 2);
      return;
    }

    const pad = { top: 16, right: 20, bottom: 28, left: 60 };
    const plotW = Math.max(10, rect.width - pad.left - pad.right);
    const plotH = Math.max(10, rect.height - pad.top - pad.bottom);

    let minT = Infinity;
    let maxT = -Infinity;
    let minV = Infinity;
    let maxV = -Infinity;
    visible.forEach((s) => {
      s.points.forEach(([t, v]) => {
        if (t < minT) minT = t;
        if (t > maxT) maxT = t;
        if (v < minV) minV = v;
        if (v > maxV) maxV = v;
      });
    });
    if (maxT <= minT) {
      minT -= 60000;
      maxT += 60000;
    }
    if (maxV <= minV) {
      const delta = Math.max(1, Math.abs(minV) * 0.1);
      minV -= delta;
      maxV += delta;
    } else {
      const margin = (maxV - minV) * 0.08;
      minV -= margin;
      maxV += margin;
    }

    const xFor = (t) => pad.left + ((t - minT) / (maxT - minT)) * plotW;
    const yFor = (v) => pad.top + (1 - (v - minV) / (maxV - minV)) * plotH;

    // 水平网格与 Y 轴刻度
    ctx.strokeStyle = pal.grid;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.fillStyle = pal.axis;
    ctx.font = '11px "JetBrains Mono", Consolas, monospace';
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const yTicks = 5;
    for (let i = 0; i <= yTicks; i += 1) {
      const ratio = i / yTicks;
      const y = pad.top + ratio * plotH;
      const val = maxV - ratio * (maxV - minV);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + plotW, y);
      ctx.stroke();
      ctx.fillText(val.toFixed(Math.abs(val) >= 100 ? 1 : 2), pad.left - 8, y);
    }

    // X 轴时间刻度
    ctx.setLineDash([]);
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const xTicks = Math.max(2, Math.min(6, Math.floor(plotW / 110)));
    for (let i = 0; i <= xTicks; i += 1) {
      const t = minT + (i / xTicks) * (maxT - minT);
      const x = xFor(t);
      const d = new Date(t);
      const txt = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
      ctx.fillText(txt, x, pad.top + plotH + 8);
    }

    // 曲线绘制
    visible.forEach((s) => {
      ctx.strokeStyle = s.color;
      ctx.fillStyle = s.color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      s.points.forEach(([t, v], idx) => {
        const x = xFor(t);
        const y = yFor(v);
        if (idx === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      if (s.points.length <= 2) {
        s.points.forEach(([t, v]) => {
          ctx.beginPath();
          ctx.arc(xFor(t), yFor(v), 3.5, 0, Math.PI * 2);
          ctx.fill();
        });
      }
    });

    // 悬停十字准星与主题胶囊提示
    if (state.trendHoverX !== null && state.trendHoverX >= pad.left && state.trendHoverX <= pad.left + plotW) {
      const hoverT = minT + ((state.trendHoverX - pad.left) / plotW) * (maxT - minT);
      ctx.strokeStyle = pal.crosshair;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(state.trendHoverX, pad.top);
      ctx.lineTo(state.trendHoverX, pad.top + plotH);
      ctx.stroke();
      ctx.setLineDash([]);

      const rows = [];
      let snapT = hoverT;
      visible.forEach((s) => {
        let best = s.points[0];
        let bestDist = Infinity;
        for (const pt of s.points) {
          const dist = Math.abs(pt[0] - hoverT);
          if (dist < bestDist) {
            bestDist = dist;
            best = pt;
          }
        }
        snapT = best[0];
        const px = xFor(best[0]);
        const py = yFor(best[1]);
        ctx.fillStyle = s.color;
        ctx.beginPath();
        ctx.arc(px, py, 4, 0, Math.PI * 2);
        ctx.fill();
        rows.push({ color: s.color, text: `${s.label}: ${best[1].toFixed(2)} ${s.unit || ""}`.trim() });
      });

      const d = new Date(snapT);
      const header = d.toLocaleTimeString("zh-CN", { hour12: false });
      ctx.font = '11px "JetBrains Mono", Consolas, monospace';
      const maxTextW = Math.max(ctx.measureText(header).width, ...rows.map((r) => ctx.measureText(r.text).width + 14));
      const boxW = maxTextW + 22;
      const boxH = 22 + rows.length * 17 + 6;
      const bx = state.trendHoverX + 12 + boxW > rect.width ? state.trendHoverX - 12 - boxW : state.trendHoverX + 12;
      const by = pad.top + 6;

      ctx.fillStyle = pal.tooltipBg;
      ctx.strokeStyle = pal.tooltipBorder;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(bx, by, boxW, boxH, 6);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = pal.tooltipText;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(header, bx + 10, by + 6);
      rows.forEach((r, idx) => {
        const ry = by + 23 + idx * 17;
        ctx.fillStyle = r.color;
        ctx.beginPath();
        ctx.arc(bx + 13, ry + 5, 3.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = pal.tooltipText;
        ctx.fillText(r.text, bx + 22, ry);
      });
    }
  }

  let trendHoverRaf = 0;
  $("#trend-canvas")?.addEventListener("mousemove", (e) => {
    const rect = e.target.getBoundingClientRect();
    state.trendHoverX = e.clientX - rect.left;
    if (!trendHoverRaf) {
      trendHoverRaf = requestAnimationFrame(() => {
        trendHoverRaf = 0;
        drawTrendChart();
      });
    }
  });
  $("#trend-canvas")?.addEventListener("mouseleave", () => {
    state.trendHoverX = null;
    drawTrendChart();
  });
  $("#trend-hours")?.addEventListener("change", () => {
    if (state.compareMode && state.compareTargets.length) loadCompareTrends().catch(() => {});
    else if (state.devSelected) loadDeviceTrend(state.devSelected).catch(() => {});
  });
  $("#dev-search")?.addEventListener("input", debounce(() => loadDevices().catch(() => {}), 250));

  // ==================== 4. 通信图（RTU / 通道 / 四遥） ====================
  function connBadge(connState) {
    if (connState === "connected") return '<span class="badge ok">已连接</span>';
    if (connState === "retrying") return '<span class="badge warn">重连中</span>';
    return '<span class="badge danger">未连接</span>';
  }

  async function loadComm() {
    const rtusRes = await api("/api/comm/rtus");
    const rtus = rtusRes.items || [];
    const online = rtus.filter((r) => r.connection_state === "connected").length;
    const totalRx = rtus.reduce((s, r) => s + (r.rx_count || 0), 0);
    const totalErr = rtus.reduce((s, r) => s + (r.error_count || 0), 0);

    $("#comm-summary").innerHTML =
      `<div class="comm-stat-item"><span class="cs-lbl">RTU</span><strong>${rtus.length}</strong></div>` +
      `<div class="comm-stat-item"><span class="cs-lbl">在线</span><strong>${online}/${rtus.length}</strong></div>` +
      `<div class="comm-stat-item"><span class="cs-lbl">接收帧</span><strong>${totalRx.toLocaleString()}</strong></div>` +
      `<div class="comm-stat-item"><span class="cs-lbl">错误</span><strong>${totalErr.toLocaleString()}</strong></div>`;

    if (!state.commRtuOptionsLoaded) {
      const sel = $("#comm-rtu-filter");
      sel.innerHTML = '<option value="">全部 RTU</option>' +
        rtus.map((r) => `<option value="${r.rtu_id}">RTU ${r.rtu_id} · ${esc(r.name)}</option>`).join("");
      state.commRtuOptionsLoaded = true;
    }

    if (state.commTab === "overview") {
      const chRes = await api("/api/comm/channels");
      const channels = chRes.items || [];
      if ($("#comm-rtus-count")) $("#comm-rtus-count").textContent = String(rtus.length);
      if ($("#comm-channels-count")) $("#comm-channels-count").textContent = String(channels.length);

      $("#comm-rtus-body").innerHTML = rtus.length
        ? rtus.map((r) => {
            const pc = r.point_counts || {};
            return (
              `<tr>` +
              `<td class="num">${r.rtu_id}</td>` +
              `<td>${esc(r.name)}</td>` +
              `<td>#${r.channel_id} / ${esc(r.channel_type)}</td>` +
              `<td>${r.process_enable ? '<span class="badge ok">启用</span>' : '<span class="badge">停用</span>'}</td>` +
              `<td>${connBadge(r.connection_state)}</td>` +
              `<td class="num">${pc.yc || 0} / ${pc.yx || 0} / ${pc.yk || 0} / ${pc.yt || 0}</td>` +
              `<td class="num">${r.rx_count} / ${r.tx_count} / ${r.error_count}</td>` +
              `<td class="num">${esc(fmtTime(r.heartbeat_time))}</td>` +
              `<td title="${esc(r.last_error)}">${esc(r.last_error || "正常")}</td>` +
              `</tr>`
            );
          }).join("")
        : '<tr><td colspan="9" class="empty-hint">暂无 RTU 记录</td></tr>';

      $("#comm-channels-body").innerHTML = channels.length
        ? channels.map((c) => {
            const p = c.parameters || {};
            const keyParams = [
              p.rtu_addr != null ? `站址=${p.rtu_addr}` : "",
              p.k != null ? `k=${p.k}/w=${p.w}` : "",
              p.t1 != null ? `t1=${p.t1}s` : "",
              p.call_alldata_time != null ? `总召=${p.call_alldata_time}s` : "",
              p.unit_id != null ? `从站=${p.unit_id}` : "",
            ].filter(Boolean).join(" · ");
            return (
              `<tr>` +
              `<td class="num">${c.channel_id}</td>` +
              `<td>${esc(c.channel_type)}</td>` +
              `<td>${esc(c.name)}</td>` +
              `<td>${c.rtu_id != null ? `RTU ${c.rtu_id}` : "--"}</td>` +
              `<td class="num">${esc(c.endpoint || "--")}</td>` +
              `<td>${connBadge(c.connection_state)}</td>` +
              `<td class="num">${c.rx_count}</td>` +
              `<td class="num">${c.error_count}</td>` +
              `<td title="${esc(keyParams)}">${esc(keyParams || "--")}</td>` +
              `</tr>`
            );
          }).join("")
        : '<tr><td colspan="9" class="empty-hint">暂无通信通道</td></tr>';
    } else {
      const qs = new URLSearchParams({
        kind: state.commPointKind || "yc",
        limit: String(state.commPointLimit),
        offset: String(state.commPointOffset),
      });
      const rid = $("#comm-rtu-filter").value;
      const kw = $("#comm-point-search").value.trim();
      if (rid) qs.set("rtu_id", rid);
      if (kw) qs.set("search", kw);
      const ptRes = await api("/api/comm/points?" + qs.toString());
      state.commPointTotal = ptRes.total || 0;
      const items = ptRes.items || [];
      $("#comm-points-body").innerHTML = items.length
        ? items.map((p) => {
            const st = p.status;
            const badge = st === 0 ? '<span class="badge ok">正常 (0)</span>'
              : st === null || st === undefined ? '<span class="badge">未采集</span>'
              : `<span class="badge danger">异常 (${st})</span>`;
            return (
              `<tr>` +
              `<td class="num">${p.rtu_id}</td>` +
              `<td class="num">${p.pnt_no}</td>` +
              `<td title="${esc(p.name)}">${esc(p.name || "")}</td>` +
              `<td class="num">${p.raw_value != null ? fmtNum(p.raw_value, 3) : "--"}</td>` +
              `<td class="num">${p.value != null ? fmtNum(p.value, 3) : "--"}</td>` +
              `<td>${badge}</td>` +
              `<td class="num">${esc(fmtTime(p.refresh_time_iso || p.refresh_time))}</td>` +
              `</tr>`
            );
          }).join("")
        : '<tr><td colspan="7" class="empty-hint">无匹配四遥测点</td></tr>';

      const curPage = Math.floor(state.commPointOffset / state.commPointLimit) + 1;
      const totalPages = Math.max(1, Math.ceil(state.commPointTotal / state.commPointLimit));
      $("#comm-points-info").textContent = `共 ${state.commPointTotal} 条`;
      $("#comm-page-label").textContent = `${curPage} / ${totalPages}`;
      $("#comm-prev").disabled = state.commPointOffset <= 0;
      $("#comm-next").disabled = state.commPointOffset + state.commPointLimit >= state.commPointTotal;
    }
  }

  $$("#comm-tabs .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.commTab = btn.dataset.commTab;
      $$("#comm-tabs .tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      $("#comm-overview-panel").hidden = state.commTab !== "overview";
      $("#comm-points-panel").hidden = state.commTab !== "points";
      $("#comm-point-filters").hidden = state.commTab !== "points";
      loadComm();
    });
  });
  $("#comm-rtu-filter")?.addEventListener("change", () => { state.commPointOffset = 0; loadComm(); });
  $$("#comm-kind-tabs .sub-tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const kind = btn.dataset.kind;
      if (!kind || kind === state.commPointKind) return;
      state.commPointKind = kind;
      state.commPointOffset = 0;
      $$("#comm-kind-tabs .sub-tab-btn").forEach((b) => {
        const active = b === btn;
        b.classList.toggle("active", active);
        b.setAttribute("aria-selected", String(active));
      });
      loadComm();
    });
  });
  $("#comm-point-search")?.addEventListener("input", debounce(() => {
    state.commPointOffset = 0;
    loadComm().catch(() => {});
  }, 250));
  $("#comm-prev")?.addEventListener("click", () => {
    state.commPointOffset = Math.max(0, state.commPointOffset - state.commPointLimit);
    loadComm().catch(() => {});
  });
  $("#comm-next")?.addEventListener("click", () => {
    if (state.commPointOffset + state.commPointLimit < state.commPointTotal) {
      state.commPointOffset += state.commPointLimit;
      loadComm().catch(() => {});
    }
  });

  // ==================== 5. 运行日志 ====================
  let logReqSeq = 0;
  let lastLogProcSig = "";
  let lastLogStreamSig = "";

  async function loadLogs() {
    const seq = ++logReqSeq;
    const filesRes = await api(`/api/logs/files?record=${encodeURIComponent(state.logRecord)}`);
    if (seq !== logReqSeq) return;
    const files = filesRes.items || [];
    $("#log-file-count").textContent = String(filesRes.total || 0);

    const procSel = $("#log-process");
    const procList = filesRes.processes || [];
    const nextProcSig = procList.join("|");
    if (nextProcSig !== lastLogProcSig && document.activeElement !== procSel) {
      const prevProc = procSel.value;
      procSel.innerHTML = '<option value="">全部进程</option>' +
        procList.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("");
      if ([...procSel.options].some((o) => o.value === prevProc)) procSel.value = prevProc;
      lastLogProcSig = nextProcSig;
    }

    const fileList = $("#log-file-list");
    const allBtn = `<button type="button" class="log-file-btn ${!state.logFilename ? "selected" : ""}" data-file=""><strong>全部最新日志</strong><small>合并检索最近文件</small></button>`;
    fileList.innerHTML = allBtn + files.slice(0, 60).map((f) =>
      `<button type="button" class="log-file-btn ${state.logFilename === f.filename ? "selected" : ""}" data-file="${esc(f.filename)}">` +
      `<strong title="${esc(f.filename)}">${esc(f.process)}</strong>` +
      `<small>${esc(f.hour)} · ${(f.size / 1024).toFixed(1)} KB</small>` +
      `</button>`
    ).join("");
    fileList.querySelectorAll(".log-file-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.logFilename = btn.dataset.file;
        loadLogs().catch(() => {});
      });
    });

    const qs = new URLSearchParams({ record: state.logRecord, limit: "300" });
    if (state.logFilename) qs.set("filename", state.logFilename);
    if (procSel.value) qs.set("process", procSel.value);
    if ($("#log-level").value) qs.set("level", $("#log-level").value);
    if (state.logRecord === "packet" && $("#log-direction").value) qs.set("direction", $("#log-direction").value);
    const kw = $("#log-keyword").value.trim();
    if (kw) qs.set("keyword", kw);

    const res = await api("/api/logs/read?" + qs.toString());
    if (seq !== logReqSeq) return;
    const lines = (res.items || []).slice().reverse(); // 按时间正序阅读，最新在底部
    state.logLines = lines;
    $("#log-active-file").textContent = state.logFilename || "全部最新日志";
    $("#log-line-count").textContent = `${lines.length} 条`;

    const streamSig = `${lines.length}|${lines[0]?.text || ""}|${lines[lines.length - 1]?.text || ""}`;
    const stream = $("#log-stream");
    if (streamSig !== lastLogStreamSig) {
      lastLogStreamSig = streamSig;
      stream.innerHTML = lines.length
        ? lines.map((l) => `<div class="log-line ${esc(l.level || "")}">${esc(l.text)}</div>`).join("")
        : '<div class="empty-hint">当前筛选条件下暂无日志</div>';
      if ($("#log-follow").checked) {
        stream.scrollTop = stream.scrollHeight;
      }
    }
  }

  $$("#log-record-tabs .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.logRecord = btn.dataset.logRecord;
      state.logFilename = "";
      lastLogStreamSig = "";
      $$("#log-record-tabs .tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      $("#log-direction").disabled = state.logRecord !== "packet";
      if (state.logRecord !== "packet") $("#log-direction").value = "";
      loadLogs().catch(() => {});
    });
  });
  ["#log-process", "#log-level", "#log-direction"].forEach((id) => {
    $(id)?.addEventListener("change", () => loadLogs().catch(() => {}));
  });
  $("#log-keyword")?.addEventListener("input", debounce(() => loadLogs().catch(() => {}), 250));
  $("#log-export")?.addEventListener("click", () => {
    const text = state.logLines.map((l) => l.text).join("\n");
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `scada-${state.logRecord}-logs.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  // ==================== 1.5 全景流向（交直流微电网与氢能实时能流） ====================
  function setFlowLineState(id, mode) {
    const el = $("#" + id);
    if (!el) return;
    el.classList.remove("flow-fwd", "flow-rev", "flow-idle");
    el.classList.add(mode === "fwd" ? "flow-fwd" : mode === "rev" ? "flow-rev" : "flow-idle");
  }

  function esStateLabel(st) {
    if (st === "discharge") return "放电";
    if (st === "charge") return "充电";
    return "待机";
  }

  async function loadEnergyFlow() {
    const data = await api("/api/energy-flow");
    const s = data.summary || {};
    const dc = data.dc_bus || {};
    const ac = data.ac_bus || {};
    const n = data.nodes || {};
    const env = data.environment || {};
    const src = data.sources || {};

    // 顶部 6 项核心指标
    $("#ef-kpi-load").textContent = fmtNum(s.total_load_kw, 1);
    $("#ef-kpi-renew").textContent = fmtNum(s.renewable_kw, 1);
    $("#ef-kpi-green").textContent = fmtNum(s.green_ratio_pct, 1);
    $("#ef-kpi-storage").textContent = fmtNum(s.storage_kw, 1);
    $("#ef-kpi-diesel").textContent = fmtNum(s.diesel_kw, 1);
    $("#ef-kpi-valid").textContent = String(s.valid_measurements ?? 0);
    $("#ef-kpi-valid-sub").textContent = `/ ${s.bound_measurements ?? 0}`;

    // 交直流平衡栏与母线电压
    const dcV = Math.round(dc.voltage_v || 750);
    const acV = Math.round(ac.voltage_v || 380);
    if ($("#ef-dc-volt")) $("#ef-dc-volt").textContent = `DC ${dcV}V`;
    if ($("#ef-dc-balance")) $("#ef-dc-balance").textContent = `发 ${fmtNum(dc.gen_kw, 1)} / 荷 ${fmtNum(dc.load_kw, 1)} kW`;
    if ($("#ef-ac-volt")) $("#ef-ac-volt").textContent = `AC ${acV}V`;
    if ($("#ef-ac-balance")) $("#ef-ac-balance").textContent = `发 ${fmtNum(ac.gen_kw, 1)} / 荷 ${fmtNum(ac.load_kw, 1)} kW`;
    $("#ef-svg-dc-cap").textContent = `DC ${dcV}V`;
    $("#ef-svg-ac-cap").textContent = `AC ${acV}V`;

    // 左侧直流节点
    const dcWind = n.dc_wind || {};
    $("#ef-dc-wind-cnt").textContent = `· ${dcWind.count ?? 0}台`;
    $("#ef-dc-wind-kw").textContent = `${fmtNum(dcWind.p_kw, 1)} kW`;
    $("#ef-dc-wind-sub").textContent = `在线 ${dcWind.online ?? 0}/${dcWind.count ?? 0}`;
    setFlowLineState("ef-line-dc-wind", (dcWind.p_kw || 0) > 0.2 ? "fwd" : "idle");

    const dcPv = n.dc_pv || {};
    $("#ef-dc-pv-cnt").textContent = `· ${dcPv.count ?? 0}台`;
    $("#ef-dc-pv-kw").textContent = `${fmtNum(dcPv.p_kw, 1)} kW`;
    $("#ef-dc-pv-sub").textContent = dcPv.voltage_v != null ? `${fmtNum(dcPv.voltage_v, 1)} V` : `在线 ${dcPv.online ?? 0}/${dcPv.count ?? 0}`;
    setFlowLineState("ef-line-dc-pv", (dcPv.p_kw || 0) > 0.2 ? "fwd" : "idle");

    const dcEs = n.dc_storage || {};
    $("#ef-dc-es-soc").textContent = `· SOC ${dcEs.soc_pct != null ? fmtNum(dcEs.soc_pct, 1) : "--"}%`;
    $("#ef-dc-es-kw").textContent = `${fmtNum(dcEs.p_kw, 1)} kW`;
    $("#ef-dc-es-sub").textContent = `${esStateLabel(dcEs.state)} · ${dcEs.count ?? 0}组`;
    setFlowLineState("ef-line-dc-storage", dcEs.state === "discharge" ? "fwd" : dcEs.state === "charge" ? "rev" : "idle");

    const dcLoad = n.dc_load || {};
    $("#ef-dc-load-v").textContent = `· ${Math.round(dcLoad.voltage_v || dcV)}V`;
    $("#ef-dc-load-kw").textContent = `${fmtNum(dcLoad.p_kw, 1)} kW`;
    $("#ef-dc-load-sub").textContent = `占总负荷 ${fmtNum(dcLoad.share_pct, 1)}%`;
    setFlowLineState("ef-line-dc-load", (dcLoad.p_kw || 0) > 0.2 ? "fwd" : "idle");

    // 中间枢纽：DC/AC、电制氢、储氢罐、燃料电池
    const dcac = n.dcac || {};
    $("#ef-dcac-cap").textContent = dcac.rated_kw ? `· ${dcac.count || 0}台 ${fmtNum(dcac.rated_kw, 0)} kW` : `· ${dcac.count || 0}台`;
    $("#ef-dcac-kw").textContent = `${fmtNum(dcac.p_kw, 1)} kW`;
    if (dcac.direction === "dc_to_ac") {
      $("#ef-dcac-arrow").textContent = "→";
      $("#ef-dcac-dir").textContent = "DC → AC";
      setFlowLineState("ef-line-dcac-l", "fwd");
      setFlowLineState("ef-line-dcac-r", "fwd");
    } else if (dcac.direction === "ac_to_dc") {
      $("#ef-dcac-arrow").textContent = "←";
      $("#ef-dcac-dir").textContent = "AC → DC";
      setFlowLineState("ef-line-dcac-l", "rev");
      setFlowLineState("ef-line-dcac-r", "rev");
    } else {
      $("#ef-dcac-arrow").textContent = "⇄";
      $("#ef-dcac-dir").textContent = "待机";
      setFlowLineState("ef-line-dcac-l", "idle");
      setFlowLineState("ef-line-dcac-r", "idle");
    }

    const elec = n.electrolyzer || {};
    $("#ef-elec-kw").textContent = `${fmtNum(elec.p_kw, 1)} kW`;
    $("#ef-elec-flow").textContent = `${elec.flow_nm3h != null ? fmtNum(elec.flow_nm3h, 2) : "--"} Nm³/h`;
    setFlowLineState("ef-line-elec-ac", (elec.p_kw || 0) > 0.2 ? "fwd" : "idle");
    setFlowLineState("ef-line-elec-h2", (elec.flow_nm3h || 0) > 0.05 ? "fwd" : "idle");

    const h2 = n.hydro_storage || {};
    const h2Soc = h2.soc_pct != null ? Math.max(0, Math.min(100, Number(h2.soc_pct))) : 0;
    const h2Circ = 2 * Math.PI * 32;
    $("#ef-h2-arc")?.setAttribute("stroke-dasharray", `${(h2Circ * h2Soc / 100).toFixed(2)} ${h2Circ.toFixed(2)}`);
    $("#ef-h2-soc").textContent = h2.soc_pct != null ? `${fmtNum(h2.soc_pct, 1)}%` : "--%";
    $("#ef-h2-pres").textContent = h2.pressure_mpa != null ? `${fmtNum(h2.pressure_mpa, 2)} MPa` : "-- MPa";
    $("#ef-h2-state").textContent = h2.state === "charging"
      ? `储氢 ${fmtNum(Math.abs(h2.flow_nm3h || 0), 1)} Nm³/h`
      : h2.state === "discharging"
        ? `供氢 ${fmtNum(Math.abs(h2.flow_nm3h || 0), 1)} Nm³/h`
        : "静置";

    const fc = n.fuel_cell || {};
    $("#ef-fc-kw").textContent = `${fmtNum(fc.p_kw, 1)} kW`;
    $("#ef-fc-flow").textContent = `${fc.flow_nm3h != null ? fmtNum(fc.flow_nm3h, 2) : "0.00"} Nm³/h`;
    setFlowLineState("ef-line-h2-fc", (fc.flow_nm3h || 0) > 0.05 || (fc.p_kw || 0) > 0.2 ? "fwd" : "idle");
    setFlowLineState("ef-line-fc-dc", (fc.p_kw || 0) > 0.2 ? "fwd" : "idle");

    // 右侧交流节点
    const acWind = n.ac_wind || {};
    $("#ef-ac-wind-cnt").textContent = `· ${acWind.count ?? 0}台`;
    $("#ef-ac-wind-kw").textContent = `${fmtNum(acWind.p_kw, 1)} kW`;
    $("#ef-ac-wind-sub").textContent = `在线 ${acWind.online ?? 0}/${acWind.count ?? 0}`;
    setFlowLineState("ef-line-ac-wind", (acWind.p_kw || 0) > 0.2 ? "fwd" : "idle");

    const acPv = n.ac_pv || {};
    $("#ef-ac-pv-cnt").textContent = `· ${acPv.count ?? 0}台`;
    $("#ef-ac-pv-kw").textContent = `${fmtNum(acPv.p_kw, 1)} kW`;
    $("#ef-ac-pv-sub").textContent = `在线 ${acPv.online ?? 0}/${acPv.count ?? 0}`;
    setFlowLineState("ef-line-ac-pv", (acPv.p_kw || 0) > 0.2 ? "fwd" : "idle");

    const acEs = n.ac_storage || {};
    $("#ef-ac-es-soc").textContent = `· SOC ${acEs.soc_pct != null ? fmtNum(acEs.soc_pct, 1) : "--"}%`;
    $("#ef-ac-es-kw").textContent = `${fmtNum(acEs.p_kw, 1)} kW`;
    $("#ef-ac-es-sub").textContent = `${esStateLabel(acEs.state)} · ${acEs.count ?? 0}组`;
    setFlowLineState("ef-line-ac-storage", acEs.state === "discharge" ? "fwd" : acEs.state === "charge" ? "rev" : "idle");

    const diesel = n.diesel || {};
    $("#ef-diesel-cnt").textContent = `· ${diesel.count ?? 0}台`;
    $("#ef-diesel-kw").textContent = `${fmtNum(diesel.p_kw, 1)} kW`;
    $("#ef-diesel-sub").textContent = `发电 ${diesel.online ?? 0}/${diesel.count ?? 0} 台`;
    setFlowLineState("ef-line-diesel", (diesel.p_kw || 0) > 0.2 ? "fwd" : "idle");

    const acLoad = n.ac_load || {};
    $("#ef-ac-load-v").textContent = `· ${Math.round(acLoad.voltage_v || acV)}V`;
    $("#ef-ac-load-kw").textContent = `${fmtNum(acLoad.p_kw, 1)} kW`;
    $("#ef-ac-load-sub").textContent = `占总负荷 ${fmtNum(acLoad.share_pct, 1)}%`;
    setFlowLineState("ef-line-ac-load", (acLoad.p_kw || 0) > 0.2 ? "fwd" : "idle");

    // 右侧环境与数据源面板
    const sampleDt = data.latest_sample_time ? new Date(data.latest_sample_time) : new Date();
    $("#ef-sample-time").textContent = Number.isNaN(sampleDt.getTime())
      ? "--:--:--"
      : sampleDt.toLocaleTimeString("zh-CN", { hour12: false });
    $("#ef-env-wind").textContent = env.wind_speed_ms != null ? `${fmtNum(env.wind_speed_ms, 2)} m/s` : "--";
    $("#ef-env-irr").textContent = env.irradiance_wm2 != null ? `${fmtNum(env.irradiance_wm2, 1)} W/m²` : "--";
    $("#ef-env-dcu").textContent = `${fmtNum(env.dc_voltage_v, 1)} V`;
    $("#ef-env-acu").textContent = `${fmtNum(env.ac_voltage_v, 1)} V`;
    $("#ef-src-model").textContent = src.model_db || "—";
    $("#ef-src-d").textContent = src.d_db || "—";
    $("#ef-src-c").textContent = src.c_db || "—";
    $("#ef-src-rtu").textContent = `${src.rtu_online ?? 0} / ${src.rtu_total ?? 0}`;
    const pts = src.points || {};
    $("#ef-src-points").textContent = `YC:${pts.yc ?? 0} / YX:${pts.yx ?? 0}`;
    $("#ef-qual-ok").textContent = `${s.valid_measurements ?? 0}`;
    $("#ef-qual-limit").textContent = `${s.over_limit_count ?? 0} / ${s.under_limit_count ?? 0}`;
    $("#ef-qual-comm").textContent = `${s.comm_error_count ?? 0}`;
    // 同步刷新左侧边栏健康摘要与告警徽标，无需额外重复请求 /api/overview
    if ($("#health-rtus")) $("#health-rtus").textContent = `${s.rtu_online ?? 0} / ${s.rtu_total ?? 0}`;
    if ($("#health-points")) $("#health-points").textContent = `${s.valid_measurements ?? 0} / ${s.bound_measurements ?? 0}`;
    if ($("#health-gen")) $("#health-gen").textContent = `${fmtNum(s.total_gen_kw, 1)} kW`;
    if ($("#health-load")) $("#health-load").textContent = `${fmtNum(s.total_load_kw, 1)} kW`;
    updateAlarmBadge(s.alarm_unacked);
    updateCollectorBadge(s.collector_running);
  }

  // ==================== 3.5 告警与越限中心（支持按 RTU 折叠、历史回溯与导出 CSV） ====================
  let alarmReqSeq = 0;

  function alarmTypeBadge(type) {
    if (type === "over_max") return '<span class="badge over">越上限</span>';
    if (type === "under_min") return '<span class="badge under">越下限</span>';
    if (type === "control_failed") return '<span class="badge danger">指令失败</span>';
    return '<span class="badge danger">通信异常</span>';
  }

  function renderAlarmDetailRow(a, idx, childClass = "") {
    const isHist = state.alarmScope === "history";
    const valStr = a.value == null || a.value === ""
      ? "—"
      : `${typeof a.value === "number" ? fmtNum(a.value, 2) : esc(a.value)}${a.unit ? " " + esc(a.unit) : ""}`;
    const stateBadge = isHist
      ? (a.state === "recovered"
          ? `<span class="badge ok">已恢复</span>`
          : (a.acked ? '<span class="badge muted">已确认</span>' : '<span class="badge danger">活动中</span>'))
      : (a.acked ? '<span class="badge muted">已确认</span>' : '<span class="badge danger">未确认</span>');
    const timeText = isHist
      ? `${fmtTime(a.first_seen_ms)}${a.recovered_ms ? " → " + fmtTime(a.recovered_ms) : ""}`
      : fmtTime(a.time_ms || a.time_iso);
    const ackBtn = (!isHist && !a.acked)
      ? `<button type="button" class="btn secondary" data-alarm-ack="${idx}">确认</button>`
      : "";
    return `<tr class="${a.acked ? "alarm-row-acked " : ""}${childClass}">` +
      `<td>${alarmTypeBadge(a.type)}</td>` +
      `<td><strong>${esc(a.device_name)}</strong> <small class="muted">${esc(a.table_name)} #${a.idx}</small></td>` +
      `<td>${esc(a.attr_label)} <small class="muted">${esc(a.attr)}</small></td>` +
      `<td class="num">${valStr}</td>` +
      `<td>${esc(a.detail)}</td>` +
      `<td class="num">RTU ${a.rtu_id ?? "-"} / #${a.point_no ?? "-"}</td>` +
      `<td>${esc(timeText)}</td>` +
      `<td>${stateBadge}</td>` +
      `<td><span class="alarm-ops">${ackBtn}` +
      `<button type="button" class="btn secondary" data-alarm-loc="${idx}">定位</button>` +
      `<button type="button" class="btn secondary" data-alarm-dev="${idx}">曲线</button>` +
      `</span></td></tr>`;
  }

  function exportAlarmsCsv() {
    if (!state.alarmItems.length) {
      setConnection(false, "当前无可导出的告警记录");
      return;
    }
    const isHist = state.alarmScope === "history";
    const escCsv = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const header = isHist
      ? ["类型", "设备", "表名", "索引", "属性", "值", "详情", "RTU", "点号", "状态", "首次触发", "恢复时间", "确认时间"]
      : ["类型", "设备", "表名", "索引", "属性", "值", "详情", "RTU", "点号", "状态", "时间", "确认时间"];
    const rows = state.alarmItems.map((a) => isHist
      ? [a.title, a.device_name, a.table_name, a.idx, a.attr, a.value, a.detail, a.rtu_id, a.point_no, a.state, fmtTime(a.first_seen_ms), fmtTime(a.recovered_ms), a.acked_at || ""]
      : [a.title, a.device_name, a.table_name, a.idx, a.attr, a.value, a.detail, a.rtu_id, a.point_no, a.acked ? "已确认" : "未确认", fmtTime(a.time_ms || a.time_iso), a.acked_at || ""]);
    const csv = "\uFEFF" + [header.map(escCsv).join(","), ...rows.map((r) => r.map(escCsv).join(","))].join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `scada-alarms-${state.alarmScope}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function loadAlarms() {
    const seq = ++alarmReqSeq;
    const isHist = state.alarmScope === "history";
    const groupWrap = $("#alarm-group-wrap");
    const ackAllBtn = $("#alarm-ack-all");
    const ackSel = $("#alarm-ack-filter");
    if (groupWrap) groupWrap.hidden = isHist;
    if (ackAllBtn) ackAllBtn.hidden = isHist;
    if (ackSel && ackSel.dataset.mode !== state.alarmScope) {
      ackSel.dataset.mode = state.alarmScope;
      ackSel.innerHTML = isHist
        ? '<option value="">全部状态</option><option value="active">活动中</option><option value="recovered">已恢复</option>'
        : '<option value="">全部状态</option><option value="0">未确认</option><option value="1">已确认</option>';
    }

    const qs = new URLSearchParams();
    if (state.alarmType) qs.set("type", state.alarmType);
    const statusVal = ackSel?.value || "";
    if (statusVal !== "") qs.set(isHist ? "state" : "acked", statusVal);
    const kw = ($("#alarm-search")?.value || "").trim();
    if (kw) qs.set("search", kw);

    const endpoint = isHist ? "/api/alarms/history?" : "/api/alarms?";
    const data = await api(endpoint + qs.toString());
    if (seq !== alarmReqSeq) return;
    state.alarmItems = data.items || [];

    const sumEl = $("#alarm-summary");
    if (isHist) {
      const recCnt = state.alarmItems.filter((a) => a.state === "recovered").length;
      const actCnt = state.alarmItems.filter((a) => a.state === "active").length;
      if (sumEl) {
        sumEl.innerHTML =
          `<div class="comm-stat-item"><span class="cs-lbl">历史记录</span><strong>${data.total ?? state.alarmItems.length}</strong></div>` +
          `<div class="comm-stat-item"><span class="cs-lbl">活动中</span><strong style="color:var(--danger,#f43f5e)">${actCnt}</strong></div>` +
          `<div class="comm-stat-item"><span class="cs-lbl">已恢复</span><strong style="color:var(--ok,#34d399)">${recCnt}</strong></div>`;
      }
    } else {
      const sm = data.summary || {};
      updateAlarmBadge(sm.unacked);
      if (sumEl) {
        sumEl.innerHTML =
          `<div class="comm-stat-item"><span class="cs-lbl">告警</span><strong>${sm.total ?? 0}</strong></div>` +
          `<div class="comm-stat-item"><span class="cs-lbl">未确认</span><strong style="color:var(--danger,#f43f5e)">${sm.unacked ?? 0}</strong></div>` +
          `<div class="comm-stat-item"><span class="cs-lbl">越限</span><strong>${(sm.over_max ?? 0) + (sm.under_min ?? 0)}</strong></div>` +
          `<div class="comm-stat-item"><span class="cs-lbl">通信</span><strong>${sm.comm_error ?? 0}</strong></div>` +
          `<div class="comm-stat-item"><span class="cs-lbl">指令</span><strong>${sm.control_failed ?? 0}</strong></div>`;
      }
    }

    const tbody = $("#alarm-tbody");
    if (!tbody) return;
    if (!state.alarmItems.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty-hint">${isHist ? "暂无历史告警记录" : "当前筛选条件下无活动告警"}</td></tr>`;
      return;
    }

    const htmlRows = [];
    if (!isHist && state.alarmGroupRtu) {
      // 高优告警（越限 / 指令失败）始终置顶平铺；同 RTU 批量通信异常（>=5 条）折叠聚合防刷屏
      const commByRtu = new Map();
      state.alarmItems.forEach((a, idx) => {
        if (a.type === "comm_error") {
          const rkey = String(a.rtu_id ?? "unknown");
          if (!commByRtu.has(rkey)) commByRtu.set(rkey, []);
          commByRtu.get(rkey).push({ a, idx });
        } else {
          htmlRows.push(renderAlarmDetailRow(a, idx));
        }
      });
      for (const [rkey, entries] of commByRtu.entries()) {
        if (entries.length < 5) {
          entries.forEach(({ a, idx }) => htmlRows.push(renderAlarmDetailRow(a, idx)));
          continue;
        }
        const expanded = state.alarmExpandedRtus.has(rkey);
        const unackedEntries = entries.filter(({ a }) => !a.acked);
        const devSet = new Set(entries.map(({ a }) => a.dev_id));
        const latestMs = Math.max(...entries.map(({ a }) => Number(a.time_ms || 0)));
        const ackBadge = unackedEntries.length
          ? `<span class="badge danger">未确认 ${unackedEntries.length}</span>`
          : '<span class="badge muted">全部已确认</span>';
        htmlRows.push(
          `<tr class="alarm-group-row" data-alarm-rtu-toggle="${esc(rkey)}">` +
          `<td><span class="alarm-group-toggle">${expanded ? "▾" : "▸"}</span>${alarmTypeBadge("comm_error")}</td>` +
          `<td><strong>RTU ${esc(rkey)} 批量通信异常</strong> <small class="muted">${devSet.size} 台设备</small></td>` +
          `<td>共 ${entries.length} 项量测</td>` +
          `<td class="num">—</td>` +
          `<td>通信质量异常或数据超时（点击${expanded ? "收起" : "展开"}明细）</td>` +
          `<td class="num">RTU ${esc(rkey)}</td>` +
          `<td>${esc(latestMs ? fmtTime(latestMs) : "--")}</td>` +
          `<td>${ackBadge}</td>` +
          `<td><span class="alarm-ops">` +
          (unackedEntries.length ? `<button type="button" class="btn secondary" data-alarm-rtu-ack="${esc(rkey)}">确认本组</button>` : "") +
          `<button type="button" class="btn secondary" data-alarm-rtu-btn="${esc(rkey)}">${expanded ? "收起" : "展开"}</button>` +
          `</span></td></tr>`
        );
        if (expanded) {
          entries.slice(0, 100).forEach(({ a, idx }) => htmlRows.push(renderAlarmDetailRow(a, idx, "alarm-child-row")));
          if (entries.length > 100) {
            htmlRows.push(`<tr class="alarm-child-row"><td colspan="9" class="muted">仅展示前 100 条明细（该组共 ${entries.length} 条，可用搜索框精确定位或导出 CSV）</td></tr>`);
          }
        }
      }
    } else {
      state.alarmItems.forEach((a, idx) => htmlRows.push(renderAlarmDetailRow(a, idx)));
    }
    tbody.innerHTML = htmlRows.join("");

    tbody.querySelectorAll("[data-alarm-rtu-toggle]").forEach((tr) => {
      tr.addEventListener("click", (e) => {
        if (e.target.closest("[data-alarm-rtu-ack]")) return;
        const rk = tr.dataset.alarmRtuToggle;
        if (state.alarmExpandedRtus.has(rk)) state.alarmExpandedRtus.delete(rk);
        else state.alarmExpandedRtus.add(rk);
        loadAlarms().catch(() => {});
      });
    });
    tbody.querySelectorAll("[data-alarm-rtu-ack]").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const rk = btn.dataset.alarmRtuAck;
        const keys = state.alarmItems
          .filter((a) => a.type === "comm_error" && String(a.rtu_id ?? "unknown") === rk && !a.acked)
          .map((a) => a.key);
        if (!keys.length) return;
        await apiPost("/api/alarms/ack", { keys });
        await loadAlarms();
      });
    });
    tbody.querySelectorAll("[data-alarm-ack]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const a = state.alarmItems[Number(btn.dataset.alarmAck)];
        if (!a) return;
        await apiPost("/api/alarms/ack", { keys: [a.key] });
        await loadAlarms();
      });
    });
    tbody.querySelectorAll("[data-alarm-loc]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const a = state.alarmItems[Number(btn.dataset.alarmLoc)];
        if (!a) return;
        switchView("powerflow");
        await loadPowerflow();
        focusSvgDevice(a.dev_id);
      });
    });
    tbody.querySelectorAll("[data-alarm-dev]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const a = state.alarmItems[Number(btn.dataset.alarmDev)];
        if (!a) return;
        state.devCategory = a.category || "";
        state.devSelected = { table_name: a.table_name, idx: a.idx, dev_id: a.dev_id };
        state.compareMode = false;
        switchView("devices");
      });
    });
  }

  $$("#alarm-scope-tabs .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.alarmScope = btn.dataset.alarmScope || "active";
      $$("#alarm-scope-tabs .tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      loadAlarms().catch(() => {});
    });
  });
  $("#alarm-type-filter")?.addEventListener("change", (e) => {
    state.alarmType = e.target.value || "";
    loadAlarms().catch(() => {});
  });
  $("#alarm-group-rtu")?.addEventListener("change", (e) => {
    state.alarmGroupRtu = Boolean(e.target.checked);
    loadAlarms().catch(() => {});
  });
  $("#alarm-ack-filter")?.addEventListener("change", () => loadAlarms().catch(() => {}));
  $("#alarm-search")?.addEventListener("input", debounce(() => loadAlarms().catch(() => {}), 250));
  $("#alarm-export-csv")?.addEventListener("click", exportAlarmsCsv);
  $("#alarm-ack-all")?.addEventListener("click", async () => {
    const ok = typeof window.scadaConfirm === "function"
      ? await window.scadaConfirm("确认将当前全部未确认活动告警标记为已确认？", {
          title: "确认全部告警",
          confirmText: "全部确认",
          danger: false,
        })
      : false;
    if (!ok) return;
    await apiPost("/api/alarms/ack", { ack_all: true });
    await loadAlarms();
  });

  // 设备图快滤、详情分页标签、多设备对比与 CSV 导出事件绑定
  $$("#dev-detail-tabs [data-dev-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      applyDevDetailTab(btn.dataset.devTab || "monitor");
    });
  });
  $$("#dev-state-tabs .sub-tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.devFilter = btn.dataset.devFilter || "all";
      $$("#dev-state-tabs .sub-tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      loadDevices().catch(() => {});
    });
  });
  $("#compare-toggle-btn")?.addEventListener("click", () => {
    state.compareMode = !state.compareMode;
    updateCompareBarUI();
    if (state.compareMode) {
      loadCompareTrends().catch((err) => setConnection(false, err.message));
    } else if (state.devSelected) {
      loadDeviceTrend(state.devSelected).catch(() => {});
    }
  });
  $("#compare-clear-btn")?.addEventListener("click", () => {
    state.compareTargets = [];
    state.compareMode = false;
    updateCompareBarUI();
    if (state.devSelected) loadDeviceTrend(state.devSelected).catch(() => {});
  });
  $("#trend-export-csv")?.addEventListener("click", exportTrendCsv);

  // 全景流向右侧越限/异常项点击直达告警中心
  $$("#view-energyflow [data-jump-alarm]").forEach((el) => {
    el.addEventListener("click", () => {
      state.alarmScope = "active";
      state.alarmType = el.dataset.jumpAlarm || "";
      $$("#alarm-scope-tabs .tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.alarmScope === "active"));
      if ($("#alarm-type-filter")) $("#alarm-type-filter").value = state.alarmType;
      switchView("alarms");
    });
  });

  $$("#view-energyflow [data-jump-cat]").forEach((node) => {
    node.setAttribute("tabindex", "0");
    node.setAttribute("role", "button");
    const jump = () => {
      state.devCategory = node.dataset.jumpCat || "";
      switchView("devices");
    };
    node.addEventListener("click", jump);
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        jump();
      }
    });
  });
  $$("#view-energyflow [data-ef-view]").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.efView));
  });

  // ==================== 1.2 极地管控（三栏智慧微电网大屏） ====================
  function cockpitPalette() {
    const dark = isDarkTheme();
    const cs = getComputedStyle(document.documentElement);
    return {
      series: dark ? SERIES_COLORS_DARK : SERIES_COLORS_LIGHT,
      grid: dark ? "rgba(148, 163, 184, 0.14)" : "rgba(15, 23, 42, 0.08)",
      zero: dark ? "rgba(148, 163, 184, 0.42)" : "rgba(15, 23, 42, 0.32)",
      axis: cs.getPropertyValue("--text-secondary").trim() || (dark ? "#94A3B8" : "#64748B"),
      text: cs.getPropertyValue("--text-primary").trim() || (dark ? "#F8FAFC" : "#0F172A"),
      accent: cs.getPropertyValue("--accent").trim() || (dark ? "#38BDF8" : "#2563EB"),
      tipBg: dark ? "rgba(9, 17, 32, 0.94)" : "rgba(255, 255, 255, 0.96)",
      tipBorder: cs.getPropertyValue("--border-strong").trim() || (dark ? "rgba(56, 189, 248, 0.35)" : "#CBD5E1"),
    };
  }

  function setCkPulse(id, powerKw, { reverse = false, warn = false } = {}) {
    const el = $("#" + id);
    const beadId = id.replace("ck-pulse-", "ck-bead-");
    const bead = $("#" + beadId);
    const p = Math.abs(Number(powerKw || 0));
    const idle = p < 0.2;
    if (el) {
      el.classList.toggle("idle", idle);
      el.classList.toggle("reverse", Boolean(reverse));
      el.classList.toggle("warn", Boolean(warn));
      if (!idle) {
        const dur = Math.max(0.7, Math.min(2.6, 2.4 - Math.log10(p + 1) * 0.7));
        el.style.animationDuration = `${dur.toFixed(2)}s`;
      }
    }
    if (bead) {
      bead.classList.toggle("idle", idle);
      const motion = bead.querySelector("animateMotion");
      if (motion && !idle) {
        motion.setAttribute("keyPoints", reverse ? "1;0" : "0;1");
        motion.setAttribute("keyTimes", "0;1");
      }
    }
  }

  function renderCkMix(sourceMix, greenPct) {
    const legEl = $("#ck-mix-legend");
    const barsEl = $("#ck-mix-bars");
    const arcsEl = $("#ck-donut-arcs");
    if (!legEl || !barsEl || !arcsEl) return;
    const pal = cockpitPalette().series;
    const items = Array.isArray(sourceMix) ? sourceMix : [];
    const totalPct = items.reduce((acc, s) => acc + Math.max(0, Number(s.pct || 0)), 0) || 1;
    const maxVal = Math.max(...items.map((s) => Number(s.value_wkwh || 0)), 0.01);

    $("#ck-donut-green").textContent = `${fmtNum(greenPct, 1)}%`;

    legEl.innerHTML = items.map((s, i) => {
      const color = pal[i % pal.length];
      return `<span class="ck-mix-leg-item"><i class="ck-mix-dot" style="background:${color}"></i>${esc(s.label)} <strong>${fmtNum(s.pct, 1)}%</strong></span>`;
    }).join("");

    barsEl.innerHTML = items.map((s, i) => {
      const color = pal[i % pal.length];
      const w = Math.max(6, Math.min(100, (Number(s.value_wkwh || 0) / maxVal) * 100));
      return (
        `<div class="ck-bar-row">` +
        `<div class="ck-bar-top"><span>${esc(s.label)}</span><strong>${fmtNum(s.value_wkwh, 2)} 万kWh</strong></div>` +
        `<div class="ck-bar-track"><div class="ck-bar-fill" style="width:${w.toFixed(1)}%;background:linear-gradient(90deg, ${color}55 0%, ${color} 100%)"></div></div>` +
        `</div>`
      );
    }).join("");

    const circ = 2 * Math.PI * 41;
    let offset = 0;
    arcsEl.innerHTML = items.map((s, i) => {
      const frac = Math.max(0, Number(s.pct || 0)) / totalPct;
      const segLen = Math.max(0, frac * circ - 1.8);
      const color = pal[i % pal.length];
      const svg = `<circle class="ck-donut-arc" cx="60" cy="60" r="41" stroke="${color}" stroke-dasharray="${segLen.toFixed(2)} ${(circ - segLen).toFixed(2)}" stroke-dashoffset="${(-offset).toFixed(2)}" />`;
      offset += frac * circ;
      return svg;
    }).join("");
  }

  function renderCkLegend(containerId, seriesList, hiddenSet, onToggle) {
    const el = $("#" + containerId);
    if (!el) return;
    const pal = cockpitPalette().series;
    el.innerHTML = (seriesList || []).map((s, i) => {
      const color = pal[i % pal.length];
      const off = hiddenSet.has(s.key) ? " off" : "";
      return `<button type="button" class="ck-leg-chip${off}" data-ck-key="${esc(s.key)}" title="${esc(s.label)}"><i class="ck-leg-dot" style="background:${color}"></i>${esc(s.label)}</button>`;
    }).join("");
    el.querySelectorAll("[data-ck-key]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const k = btn.dataset.ckKey;
        if (hiddenSet.has(k)) hiddenSet.delete(k);
        else hiddenSet.add(k);
        onToggle();
      });
    });
  }

  function drawCockpitCurve(canvasId, seriesList, hiddenSet, hoverX, { showZero = false, areaFill = true } = {}) {
    const canvas = $("#" + canvasId);
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 40 || rect.height < 40) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const W = Math.round(rect.width);
    const H = Math.round(rect.height);
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const pal = cockpitPalette();
    const visible = (seriesList || []).map((s, i) => ({
      ...s,
      color: pal.series[i % pal.series.length],
    })).filter((s) => !hiddenSet.has(s.key) && Array.isArray(s.points) && s.points.length > 0);

    const padL = 44;
    const padR = 12;
    const padT = 14;
    const padB = 22;
    const plotW = Math.max(10, W - padL - padR);
    const plotH = Math.max(10, H - padT - padB);

    if (!visible.length) {
      ctx.fillStyle = pal.axis;
      ctx.font = "12px Inter, 'Microsoft YaHei', sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("暂无采样曲线", W / 2, H / 2);
      return;
    }

    let minT = Infinity;
    let maxT = -Infinity;
    let minV = showZero ? 0 : Infinity;
    let maxV = showZero ? 0 : -Infinity;
    for (const s of visible) {
      for (const pt of s.points) {
        const t = Number(pt[0]);
        const v = Number(pt[1]);
        if (t < minT) minT = t;
        if (t > maxT) maxT = t;
        if (v < minV) minV = v;
        if (v > maxV) maxV = v;
      }
    }
    if (!Number.isFinite(minT) || !Number.isFinite(minV)) return;
    if (maxT <= minT) {
      minT -= 60000;
      maxT += 60000;
    }
    if (Math.abs(maxV - minV) < 1e-3) {
      maxV += 5;
      minV = showZero ? Math.min(0, minV - 5) : Math.max(0, minV - 5);
    } else {
      const span = maxV - minV;
      maxV += span * 0.12;
      minV -= span * (showZero ? 0.12 : 0.08);
      if (!showZero && minV < 0 && visible.every((s) => s.points.every((p) => p[1] >= 0))) {
        minV = 0;
      }
    }

    const xOf = (t) => padL + ((t - minT) / (maxT - minT)) * plotW;
    const yOf = (v) => padT + ((maxV - v) / (maxV - minV)) * plotH;

    // 水平网格与 Y 轴刻度（单位 kW）
    const ticks = 4;
    ctx.font = "10.5px 'JetBrains Mono', Consolas, monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= ticks; i += 1) {
      const frac = i / ticks;
      const val = maxV - frac * (maxV - minV);
      const y = padT + frac * plotH;
      ctx.strokeStyle = pal.grid;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = pal.axis;
      ctx.fillText(Math.abs(val) >= 100 ? val.toFixed(0) : val.toFixed(1), padL - 5, y);
    }

    // 单位标识
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillStyle = pal.axis;
    ctx.fillText("kW", 6, 2);

    // 零轴强调线（双向储能/氢能曲线）
    if (showZero && minV < 0 && maxV > 0) {
      const y0 = yOf(0);
      ctx.strokeStyle = pal.zero;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.moveTo(padL, y0);
      ctx.lineTo(W - padR, y0);
      ctx.stroke();
    }

    // X 轴时间刻度
    const xTicks = 4;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = pal.axis;
    for (let i = 0; i <= xTicks; i += 1) {
      const t = minT + (i / xTicks) * (maxT - minT);
      const x = xOf(t);
      const d = new Date(t);
      const label = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
      ctx.fillText(label, Math.max(padL + 14, Math.min(W - padR - 14, x)), H - padB + 5);
    }

    // 绘制各曲线（平滑曲线 + 霓虹垂直渐变面积 + 轻柔发光丝带 + 末端实时光点）
    const dark = isDarkTheme();
    const baseLineY = showZero && minV < 0 && maxV > 0 ? yOf(0) : padT + plotH;
    const traceSmooth = (coords) => {
      ctx.moveTo(coords[0][0], coords[0][1]);
      if (coords.length === 2) {
        ctx.lineTo(coords[1][0], coords[1][1]);
        return;
      }
      for (let i = 0; i < coords.length - 1; i += 1) {
        const cur = coords[i];
        const nxt = coords[i + 1];
        const mx = (cur[0] + nxt[0]) * 0.5;
        const my = (cur[1] + nxt[1]) * 0.5;
        if (i === 0) ctx.lineTo(mx, my);
        else ctx.quadraticCurveTo(cur[0], cur[1], mx, my);
      }
      const last = coords[coords.length - 1];
      ctx.lineTo(last[0], last[1]);
    };

    for (const s of visible) {
      const pts = s.points;
      const coords = pts.map((p) => [xOf(p[0]), yOf(p[1])]);

      if (coords.length >= 2 && areaFill) {
        ctx.save();
        ctx.beginPath();
        traceSmooth(coords);
        ctx.lineTo(coords[coords.length - 1][0], baseLineY);
        ctx.lineTo(coords[0][0], baseLineY);
        ctx.closePath();
        const grad = showZero && minV < 0 && maxV > 0
          ? ctx.createLinearGradient(0, padT, 0, padT + plotH)
          : ctx.createLinearGradient(0, padT, 0, baseLineY);
        if (showZero && minV < 0 && maxV > 0) {
          const zFrac = Math.max(0.05, Math.min(0.95, (baseLineY - padT) / plotH));
          grad.addColorStop(0, s.color + (dark ? "54" : "30"));
          grad.addColorStop(zFrac, s.color + "06");
          grad.addColorStop(1, s.color + (dark ? "54" : "30"));
        } else {
          grad.addColorStop(0, s.color + (dark ? "58" : "2e"));
          grad.addColorStop(1, s.color + "04");
        }
        ctx.fillStyle = grad;
        ctx.fill();
        ctx.restore();
      }

      ctx.save();
      ctx.beginPath();
      if (coords.length === 1) {
        ctx.arc(coords[0][0], coords[0][1], 3, 0, Math.PI * 2);
        ctx.fillStyle = s.color;
        ctx.fill();
      } else {
        traceSmooth(coords);
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 2;
        if (dark) {
          ctx.shadowColor = s.color;
          ctx.shadowBlur = 8;
        }
        ctx.stroke();
        // 末端发光实时光点
        const last = coords[coords.length - 1];
        ctx.beginPath();
        ctx.arc(last[0], last[1], 3.2, 0, Math.PI * 2);
        ctx.fillStyle = dark ? "#ffffff" : s.color;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(last[0], last[1], 5.5, 0, Math.PI * 2);
        ctx.fillStyle = s.color + "40";
        ctx.fill();
      }
      ctx.restore();
    }

    // 十字准星与半透明圆角 Tooltip
    if (hoverX !== null && hoverX >= padL && hoverX <= W - padR) {
      const targetT = minT + ((hoverX - padL) / plotW) * (maxT - minT);
      ctx.strokeStyle = pal.accent;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(hoverX, padT);
      ctx.lineTo(hoverX, padT + plotH);
      ctx.stroke();
      ctx.setLineDash([]);

      const rows = [];
      let snapTs = targetT;
      for (const s of visible) {
        let best = s.points[0];
        let bestDist = Math.abs(best[0] - targetT);
        for (let i = 1; i < s.points.length; i += 1) {
          const d = Math.abs(s.points[i][0] - targetT);
          if (d < bestDist) {
            bestDist = d;
            best = s.points[i];
          }
        }
        snapTs = best[0];
        const bx = xOf(best[0]);
        const by = yOf(best[1]);
        ctx.fillStyle = s.color;
        ctx.beginPath();
        ctx.arc(bx, by, 3, 0, Math.PI * 2);
        ctx.fill();
        rows.push({ label: s.label, val: `${Number(best[1]).toFixed(1)} kW`, color: s.color });
      }
      const d = new Date(snapTs);
      const timeStr = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
      const lineH = 15;
      const boxW = 152;
      const boxH = 22 + rows.length * lineH;
      const bx = hoverX + 10 + boxW > W - 4 ? Math.max(padL + 2, hoverX - boxW - 10) : hoverX + 10;
      const by = padT + 2;

      ctx.fillStyle = pal.tipBg;
      ctx.strokeStyle = pal.tipBorder;
      ctx.lineWidth = 1;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(bx, by, boxW, boxH, 6);
      else ctx.rect(bx, by, boxW, boxH);
      ctx.fill();
      ctx.stroke();

      ctx.font = "10.5px 'JetBrains Mono', Consolas, monospace";
      ctx.fillStyle = pal.axis;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(timeStr, bx + 8, by + 5);

      rows.forEach((r, i) => {
        const ry = by + 20 + i * lineH;
        ctx.fillStyle = r.color;
        ctx.beginPath();
        ctx.arc(bx + 11, ry + 6, 3, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = pal.text;
        ctx.textAlign = "left";
        ctx.fillText(r.label.slice(0, 10), bx + 18, ry);
        ctx.textAlign = "right";
        ctx.fillText(r.val, bx + boxW - 8, ry);
      });
    }
  }

  function drawCockpitCharts() {
    const data = state.cockpitData;
    if (!data) return;
    renderCkMix(data.source_mix, data.polar_stats?.green_ratio_pct);
    renderCkLegend("ck-power-legend", data.power_curves, state.cockpitHiddenPower, drawCockpitCharts);
    renderCkLegend("ck-sched-legend", data.storage_schedule, state.cockpitHiddenSched, drawCockpitCharts);
    drawCockpitCurve("ck-power-canvas", data.power_curves, state.cockpitHiddenPower, state.cockpitPowerHoverX, {
      showZero: false,
      areaFill: true,
    });
    drawCockpitCurve("ck-storage-canvas", data.storage_schedule, state.cockpitHiddenSched, state.cockpitSchedHoverX, {
      showZero: true,
      areaFill: true,
    });
  }

  function renderPolarCockpit(data) {
    state.cockpitData = data;
    const cum = data.cumulative || {};
    const res = data.resilience || {};
    const hub = data.hub || {};
    const n = hub.nodes || {};
    const st = data.polar_stats || {};
    const rn = data.renewables || {};

    // 左上 L1
    $("#ck-run-days").textContent = `${data.running_days ?? "--"} 天`;
    $("#ck-total-supply").textContent = fmtNum(cum.total_supply_wkwh, 2);
    $("#ck-total-load").textContent = fmtNum(cum.total_load_wkwh, 2);
    $("#ck-diesel-saved").textContent = fmtNum(cum.diesel_saved_tons, 1);
    $("#ck-co2-reduced").textContent = fmtNum(cum.co2_reduced_tons, 1);

    // 左中 L2
    $("#ck-replace-pct").textContent = `替代率 ${fmtNum(res.diesel_replace_pct, 1)}%`;
    $("#ck-autonomy-h").textContent = fmtNum(res.autonomy_hours, 1);
    $("#ck-wind-saved").textContent = fmtNum(res.wind_saved_tons, 1);
    $("#ck-pv-saved").textContent = fmtNum(res.pv_saved_tons, 1);
    const pText = res.hydro_pressure_mpa != null ? ` · ${fmtNum(res.hydro_pressure_mpa, 2)} MPa` : "";
    $("#ck-h2-reserve").textContent = `${fmtNum(res.hydro_soc_pct, 1)}%${pText}`;

    // 中上 C1：能量枢纽 6 节点 + 光轨方向
    $("#ck-hub-green").textContent = `${fmtNum(hub.green_ratio_pct, 1)}%`;
    $("#ck-hub-gen").textContent = fmtNum(hub.total_gen_kw, 1);
    $("#ck-hub-load").textContent = fmtNum(hub.total_load_kw, 1);

    $("#ck-n-pv-p").textContent = `${fmtNum(n.pv?.p_kw, 1)} kW`;
    $("#ck-n-pv-sub").textContent = n.pv?.irradiance_wm2 != null
      ? `辐照 ${fmtNum(n.pv.irradiance_wm2, 0)} W/m²`
      : `在线 ${n.pv?.online ?? 0}/${n.pv?.count ?? 0}`;

    $("#ck-n-wind-p").textContent = `${fmtNum(n.wind?.p_kw, 1)} kW`;
    $("#ck-n-wind-sub").textContent = n.wind?.wind_speed_ms != null
      ? `风速 ${fmtNum(n.wind.wind_speed_ms, 1)} m/s`
      : `在线 ${n.wind?.online ?? 0}/${n.wind?.count ?? 0}`;

    const esStateText = n.storage?.state === "charge" ? "充电" : (n.storage?.state === "discharge" ? "放电" : "待机");
    $("#ck-n-es-p").textContent = `${esStateText} ${fmtNum(Math.abs(n.storage?.p_kw ?? 0), 1)} kW`;
    $("#ck-n-es-sub").textContent = `SOC ${fmtNum(n.storage?.soc_pct, 1)}%`;

    $("#ck-n-load-p").textContent = `${fmtNum(n.load?.p_kw, 1)} kW`;
    $("#ck-n-load-sub").textContent = `DC ${fmtNum(n.load?.dc_kw, 0)} / AC ${fmtNum(n.load?.ac_kw, 0)}`;

    const h2Net = Number(n.hydrogen?.gen_kw || 0) - Number(n.hydrogen?.prod_kw || 0);
    const h2Label = h2Net > 0.2 ? `氢电 ${fmtNum(n.hydrogen?.gen_kw, 1)} kW`
      : (Number(n.hydrogen?.prod_kw || 0) > 0.2 ? `制氢 ${fmtNum(n.hydrogen?.prod_kw, 1)} kW` : "待机 0.0 kW");
    $("#ck-n-h2-p").textContent = h2Label;
    $("#ck-n-h2-sub").textContent = `储氢 ${fmtNum(n.hydrogen?.soc_pct, 1)}%`;

    $("#ck-n-diesel-p").textContent = `${fmtNum(n.diesel?.p_kw, 1)} kW`;
    $("#ck-n-diesel-sub").textContent = `在线 ${n.diesel?.online ?? 0}/${n.diesel?.count ?? 0}`;

    setCkPulse("ck-pulse-pv", n.pv?.p_kw);
    setCkPulse("ck-pulse-wind", n.wind?.p_kw);
    setCkPulse("ck-pulse-storage", n.storage?.p_kw, { reverse: n.storage?.state === "charge" });
    setCkPulse("ck-pulse-load", n.load?.p_kw);
    setCkPulse("ck-pulse-hydrogen", Math.max(Number(n.hydrogen?.prod_kw || 0), Number(n.hydrogen?.gen_kw || 0)), {
      reverse: h2Net > 0.2,
    });
    setCkPulse("ck-pulse-diesel", n.diesel?.p_kw, { warn: true });

    // 右上 R1
    $("#ck-st-green").textContent = `${fmtNum(st.green_ratio_pct, 1)}%`;
    $("#ck-st-temp").textContent = st.temperature_c != null ? `${fmtNum(st.temperature_c, 1)} °C` : "—";
    $("#ck-st-wind").textContent = st.wind_speed_ms != null ? `${fmtNum(st.wind_speed_ms, 1)} m/s` : "—";
    $("#ck-st-irr").textContent = st.irradiance_wm2 != null ? `${fmtNum(st.irradiance_wm2, 0)} W/m²` : "—";
    $("#ck-st-bus").textContent = `${fmtNum(st.dc_voltage_v, 0)} / ${fmtNum(st.ac_voltage_v, 0)} V`;
    const healthEl = $("#ck-st-health");
    healthEl.textContent = `${st.alarm_unacked ?? 0} / ${st.rtu_online ?? 0}/${st.rtu_total ?? 0}`;
    healthEl.style.color = Number(st.alarm_unacked || 0) > 0 ? "var(--warn)" : "var(--ok)";

    // 右中 R2
    $("#ck-pyr-wind-total").textContent = fmtNum(rn.wind?.total_wkwh, 2);
    $("#ck-pyr-wind-live").textContent = `${fmtNum(rn.wind?.live_kw, 1)} kW`;
    $("#ck-pyr-wind-today").textContent = `${fmtNum(rn.wind?.today_kwh, 1)} kWh`;
    $("#ck-pyr-pv-total").textContent = fmtNum(rn.pv?.total_wkwh, 2);
    $("#ck-pyr-pv-live").textContent = `${fmtNum(rn.pv?.live_kw, 1)} kW`;
    $("#ck-pyr-pv-today").textContent = `${fmtNum(rn.pv?.today_kwh, 1)} kWh`;

    // 侧栏健康与告警徽标同步
    if (data.health) {
      $("#health-rtus").textContent = `${data.health.rtu_online} / ${data.health.rtu_total}`;
      $("#health-points").textContent = `${data.health.valid_measurements} / ${data.health.bound_measurements}`;
      $("#health-gen").textContent = `${fmtNum(hub.total_gen_kw, 1)} kW`;
      $("#health-load").textContent = `${fmtNum(hub.total_load_kw, 1)} kW`;
      updateAlarmBadge(data.health.alarm_unacked);
      updateCollectorBadge(data.health.collector_running);
    }

    drawCockpitCharts();
  }

  async function loadPolarCockpit() {
    const data = await api(`/api/polar-cockpit?hours=${encodeURIComponent(state.cockpitHours)}`);
    renderPolarCockpit(data);
  }

  // 极地管控交互绑定（节点跳转、时间窗切换、Canvas 十字准星）
  $$("#view-cockpit [data-ck-cat]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.devCategory = btn.dataset.ckCat || "";
      switchView("devices");
    });
  });
  $("#ck-st-alarm-btn")?.addEventListener("click", () => switchView("alarms"));
  $$("#ck-hours-seg [data-ck-hours]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.cockpitHours = Number(btn.dataset.ckHours || 6);
      $$("#ck-hours-seg [data-ck-hours]").forEach((b) => b.classList.toggle("active", b === btn));
      loadPolarCockpit().catch((err) => setConnection(false, err.message));
    });
  });
  function bindCkCanvasHover(canvasId, key) {
    const el = $("#" + canvasId);
    if (!el) return;
    el.addEventListener("mousemove", (e) => {
      const r = el.getBoundingClientRect();
      state[key] = e.clientX - r.left;
      drawCockpitCharts();
    });
    el.addEventListener("mouseleave", () => {
      state[key] = null;
      drawCockpitCharts();
    });
  }
  bindCkCanvasHover("ck-power-canvas", "cockpitPowerHoverX");
  bindCkCanvasHover("ck-storage-canvas", "cockpitSchedHoverX");

  // ==================== 轮询调度、快捷键盘与主题同步 ====================
  let pendingRefresh = false;
  let overviewTick = 0;

  async function refreshCurrentView() {
    if (state.refreshing) {
      pendingRefresh = true;
      return;
    }
    state.refreshing = true;
    const viewAtStart = state.view;
    try {
      const tasks = [];
      if (viewAtStart === "home") {
        tasks.push(loadOverview());
      } else if (viewAtStart === "cockpit") {
        tasks.push(loadPolarCockpit());
      } else if (viewAtStart === "energyflow") {
        tasks.push(loadEnergyFlow());
      } else {
        if (viewAtStart === "powerflow") tasks.push(loadPowerflow());
        else if (viewAtStart === "devices") {
          if (!document.activeElement?.matches(".ctrl-inline-input")) tasks.push(loadDevices());
        } else if (viewAtStart === "alarms") tasks.push(loadAlarms());
        else if (viewAtStart === "comm") tasks.push(loadComm());
        else if (viewAtStart === "logs") tasks.push(loadLogs());
        // 非首页每 3 轮顺带并行刷新一次侧栏健康摘要，避免每次轮询串行重复拉取 /api/overview
        if (overviewTick % 3 === 0) tasks.push(loadOverview());
      }
      overviewTick += 1;
      await Promise.all(tasks);
      const stamp = new Date().toLocaleTimeString("zh-CN", { hour12: false });
      setConnection(true, `实时在线 · ${stamp}`);
    } catch (err) {
      if (err?.name !== "AbortError") {
        setConnection(false, err.message || "连接异常");
      }
    } finally {
      state.refreshing = false;
      if (pendingRefresh) {
        pendingRefresh = false;
        refreshCurrentView();
      }
    }
  }

  // 全局高频工程快捷键：1-6 切换视图，/ 聚焦搜索框，Esc 关闭潮流图悬浮框
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "Escape") {
      hidePfPopovers?.();
      return;
    }
    const tag = (e.target?.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.target?.isContentEditable) return;
    if (e.key === "/") {
      const searchSel = state.view === "powerflow" ? "#pf-search"
        : state.view === "devices" ? "#dev-search"
          : state.view === "alarms" ? "#alarm-search"
            : state.view === "comm" ? "#comm-point-search"
              : state.view === "logs" ? "#log-keyword" : null;
      const input = searchSel ? $(searchSel) : null;
      if (input) {
        e.preventDefault();
        input.focus();
        input.select?.();
      }
      return;
    }
    const keyViewMap = { "1": "home", "2": "cockpit", "3": "energyflow", "4": "powerflow", "5": "devices", "6": "alarms", "7": "comm", "8": "logs" };
    if (keyViewMap[e.key]) {
      switchView(keyViewMap[e.key]);
    }
  });

  function resetTimer() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
    if (state.refreshMs > 0) {
      state.timer = setInterval(() => {
        if (!document.hidden) refreshCurrentView();
      }, state.refreshMs);
    }
  }

  $("#refresh-interval")?.addEventListener("change", (e) => {
    state.refreshMs = Number(e.target.value || 0);
    resetTimer();
  });
  $("#manual-refresh")?.addEventListener("click", refreshCurrentView);

  // 监听主题切换与窗口缩放，实时同步 SVG 和日曲线配色与尺寸
  new MutationObserver(() => {
    applySvgThemeColors();
    if (state.pfData?.flows) updatePowerflowArrowsData(state.pfData.flows);
    renderTrendChips();
    drawTrendChart();
    if (state.view === "cockpit") drawCockpitCharts();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPowerflowAnimation();
    else if (state.view === "powerflow" && state.pfShowArrows) startPowerflowAnimation();
  });

  window.addEventListener("resize", () => {
    if (state.view === "home") updateHeroFlowLines(lastRealtimeEnergy);
    if (state.view === "cockpit") drawCockpitCharts();
    if (state.view === "devices") drawTrendChart();
  });

  if (typeof ResizeObserver === "function" && $("#hero-stage")) {
    new ResizeObserver(() => {
      if (state.view === "home") updateHeroFlowLines(lastRealtimeEnergy);
    }).observe($("#hero-stage"));
  }

  // 启动入口（同时支持 #hash 与 /cockpit、/energy-flow、/alarms 等路径直达）
  const initialHash = location.hash.replace(/^#/, "");
  const pathViewMap = {
    "/cockpit": "cockpit",
    "/energy-flow": "energyflow",
    "/powerflow": "powerflow",
    "/devices": "devices",
    "/alarms": "alarms",
    "/comm": "comm",
    "/logs": "logs",
  };
  const validViews = ["home", "cockpit", "energyflow", "powerflow", "devices", "alarms", "comm", "logs"];
  const initialView = validViews.includes(initialHash)
    ? initialHash
    : (pathViewMap[location.pathname] || "home");
  switchView(initialView, false);
  resetTimer();
})();

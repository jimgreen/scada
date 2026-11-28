"use strict";

(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // 默认主题由 <html data-theme="sci-fi-cyan"> 与 ui-i18n.js 统一同步，避免 data-theme 与 data-color-scheme 失配

  const SERIES_COLORS_DARK = ["#38BDF8", "#34D399", "#FBBF24", "#FB7185", "#A78BFA", "#2DD4BF", "#F97316"];
  const SERIES_COLORS_LIGHT = ["#2563EB", "#0D9488", "#D97706", "#DC2626", "#7C3AED", "#0284C7", "#EA580C"];
  const savedPrefs = window.ScadaExtras ? window.ScadaExtras.loadPrefs() : {};

  const state = {
    view: "home",
    refreshMs: Number.isFinite(Number(savedPrefs.refreshMs)) ? Number(savedPrefs.refreshMs) : 5000,
    timer: null,
    refreshing: false,
    controlUnlocked: savedPrefs.controlUnlocked !== false,
    alarmAudio: Boolean(savedPrefs.alarmAudio),
    hideMaintAlarms: Boolean(savedPrefs.hideMaintAlarms),
    starredDevs: new Set(Array.isArray(savedPrefs.starredDevs) ? savedPrefs.starredDevs : []),
    lastUnackedCount: null,
    // 潮流图状态
    svgLoaded: false,
    svgWidth: 2518,
    svgHeight: 2143,
    pfScale: 0.4,
    pfX: 0,
    pfY: 0,
    pfMeta: null,
    pfMetaEtag: "",
    pfData: null,
    pfSelectedDev: null,
    pfHoverDev: null,
    pfPinned: false,
    pfCanvasMode: savedPrefs.pfCanvasMode || "native",
    pfFaultOnly: Boolean(savedPrefs.pfFaultOnly),
    pfPopTab: "",
    pfHoverMeas: null, // { devId, mt, slot }
    pfMpGran: "hour", // "minute" | "hour" | "day"
    pfMpOffset: 0, // 0=当前，-1=上一个单位
    pfMpWindow: null, // { startMs, endMs, label, gran }
    pfMpReqSeq: 0,
    pfMpTrendCache: new Map(), // key -> { ts, points }
    pfMpPoints: [],
    pfMpHoverX: null,
    pfPopHideTimer: null,
    pfShowArrows: savedPrefs.pfArrows !== false,
    pfArrowPaths: [], // [{ key, kind, voltageType, pathEl, length, pKw, dir, speed, scale, arrows: [{el, offset}] }]
    pfAnimRaf: null,
    pfLastFrameTs: 0,
    // 设备图状态
    devCategories: [],
    devCategory: "",
    devFilter: "all", // "all" | "alarm" | "running" | "starred"
    devDetailTab: "monitor", // "monitor" | "control"
    devItems: [],
    devSelected: null, // { table_name, idx, dev_id }
    trendData: null,
    trendHidden: new Set(),
    trendHoverX: null,
    trendAxisMode: savedPrefs.trendAxisMode || "dual",
    trendDate: "",
    trendComparePrev: false,
    trendZoomRange: null,
    trendBrushRange: null,
    trendPlotMeta: null,
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
    // 运行态势大屏状态
    dispatchHours: 6,
    dispatchFilter: "all",
    dispatchData: null,
    dispatchTrendHoverX: null,
  };

  function esc(val) {
    return String(val ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function tr(text) {
    if (window.scadaI18n && typeof window.scadaI18n.translateText === "function") {
      return window.scadaI18n.translateText(String(text ?? ""));
    }
    return String(text ?? "");
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

  const LIGHT_THEMES = new Set([
    "minimalist-light",
    "arctic-frost",
    "warm-paper",
    "slate-daylight",
    "bento-grid",
    "editorial",
  ]);
  function isDarkTheme() {
    const scheme = document.documentElement.dataset.colorScheme;
    if (scheme === "dark") return true;
    if (scheme === "light") return false;
    const t = document.documentElement.dataset.theme || "sci-fi-cyan";
    return !LIGHT_THEMES.has(t);
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

  function apiDelete(url, opts) {
    return fetchJson(url, { ...(opts || {}), method: "DELETE" });
  }

  function updateDataQualityPill(dq) {
    const el = $("#dq-pill");
    if (!el || !dq || dq.health_pct === undefined) return;
    const pct = Number(dq.health_pct ?? 100);
    el.textContent = `${pct.toFixed(1)}%`;
    el.className = `dq-pill${dq.level === "fault" ? " is-fault" : (dq.level === "warn" ? " is-warn" : "")}`;
    const tips = [
      `${tr("有效量测")}: ${dq.total_valid ?? 0}/${dq.total_bound ?? 0}`,
      dq.offline_count ? `${tr("离线设备")}: ${dq.offline_count}` : "",
      dq.stale_count ? `${tr("数据超时")}: ${dq.stale_count}` : "",
      dq.overlimit_count ? `${tr("越限告警")}: ${dq.overlimit_count}` : "",
      dq.disabled_points ? `${tr("禁用")}: ${dq.disabled_points}` : "",
    ].filter(Boolean);
    if (Array.isArray(dq.offline_devices) && dq.offline_devices.length) {
      tips.push(dq.offline_devices.slice(0, 5).map((d) => tr(d.name)).join(", "));
    }
    el.title = tips.join(" | ");
  }

  function applyControlLockUi() {
    const btn = $("#control-lock-btn");
    if (!btn) return;
    btn.classList.toggle("is-locked", !state.controlUnlocked);
    const label = state.controlUnlocked ? tr("值班控调") : tr("只读监盘");
    btn.title = label;
    btn.setAttribute("aria-label", label);
    btn.innerHTML = state.controlUnlocked
      ? '<svg class="ui-ico sm c-ok" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.2-2.4"/></svg>'
      : '<svg class="ui-ico sm c-warn" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';
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
    if (state.alarmAudio && state.lastUnackedCount !== null && count > state.lastUnackedCount) {
      window.ScadaExtras?.playAlarmBeep("high");
    }
    state.lastUnackedCount = count;
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
    if (!state.controlUnlocked) {
      const msg = "已锁定只读监盘模式，请先切换为「值班控调」后再下发指令";
      window.ScadaExtras?.showToast(msg, "warn");
      setConnection(false, tr(msg));
      return;
    }
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
      window.ScadaExtras?.showToast(res.warning, "warn");
    } else {
      const okTxt = `已下发 ${tr(dev.name)} · ${tr(ctrl.label || ctrl.control)} = ${actionDesc}`;
      setConnection(true, okTxt);
      window.ScadaExtras?.showToast(okTxt, "ok");
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
    const valid = ["home", "cockpit", "dispatch", "energyflow", "powerflow", "devices", "alarms", "comm", "logs"];
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
    if (view === "dispatch") {
      requestAnimationFrame(() => drawDispatchCharts());
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
    updateDataQualityPill(data.data_quality);

    const fmtCum = (v, digits = 2) => {
      if (v === null || v === undefined || v === "") return "--";
      const n = Number(v);
      if (!Number.isFinite(n)) return "--";
      if (n > 0 && n < 0.05) return n.toFixed(Math.max(digits, 3));
      return n.toFixed(digits);
    };

    $("#kpi-days").textContent = k.running_days ?? "--";
    $("#kpi-load-total").textContent = fmtCum(k.load_total_wkwh, 2);
    $("#kpi-diesel-total").textContent = fmtCum(k.diesel_total_wkwh, 2);
    $("#kpi-renew-total").textContent = fmtCum(k.renewable_total_wkwh, 2);
    $("#kpi-diesel-saved").textContent = fmtCum(k.diesel_saved_tons, 2);

    $("#kpi-wind-total").textContent = fmtCum(k.wind_total_wkwh, 2);
    $("#kpi-pv-total").textContent = fmtCum(k.pv_total_wkwh, 2);
    $("#kpi-storage-total").textContent = fmtCum(k.storage_total_wkwh, 2);
    $("#kpi-hydro-total").textContent = fmtCum(k.hydro_total_wkwh, 2);

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
    $("#rt-storage-soc").textContent = rt.storage_soc_pct != null ? `kW / ${fmtNum(rt.storage_soc_pct, 0)}%` : "kW / --%";
    $("#rt-load").textContent = fmtNum(rt.load_kw, 1);

    const effWx = data.weather?.effective || {};
    const cpl = data.coupling || {};
    const srcMap = effWx.sources || {};
    const setEmptyHint = (el, isEmpty, hint) => {
      if (!el) return;
      el.classList.toggle("is-empty", isEmpty);
      if (hint) el.title = hint;
    };
    if ($("#env-temp")) $("#env-temp").textContent = effWx.temperature_c != null ? fmtNum(effWx.temperature_c, 1) : "--";
    setEmptyHint(
      $("#env-temp-chip"),
      effWx.temperature_c == null,
      effWx.temperature_c == null
        ? tr("未接入气温传感器 · 点击补录")
        : `${tr("环境气温")} (${tr(srcMap.temperature === "manual" ? "手工补录" : "现场实测")}) · ${tr("点击调整")}`,
    );
    $("#env-wind").textContent = fmtNum(effWx.wind_speed_ms ?? rt.wind_speed, 1);
    setEmptyHint(
      $("#env-wind-chip"),
      (effWx.wind_speed_ms ?? rt.wind_speed) == null,
      (effWx.wind_speed_ms ?? rt.wind_speed) == null
        ? tr("未接入风速传感器 · 点击补录")
        : `${tr("瞬时风速")} (${tr(srcMap.wind_speed === "manual" ? "手工覆盖" : "现场实测")})`,
    );
    $("#env-irr").textContent = fmtNum(effWx.irradiance_wm2 ?? rt.irradiance, 0);
    setEmptyHint(
      $("#env-irr-chip"),
      (effWx.irradiance_wm2 ?? rt.irradiance) == null,
      (effWx.irradiance_wm2 ?? rt.irradiance) == null
        ? tr("未接入辐照传感器 · 点击补录")
        : `${tr("太阳辐照度")} (${tr(srcMap.irradiance === "manual" ? "手工覆盖" : "现场实测")})`,
    );
    $("#env-h2").textContent =
      rt.hydro_pressure_mpa !== null && rt.hydro_pressure_mpa !== undefined
        ? `${fmtNum(rt.hydro_pressure_mpa, 2)} MPa / ${rt.hydro_soc_pct != null ? `${fmtNum(rt.hydro_soc_pct, 0)}%` : "--%"}`
        : (rt.hydro_soc_pct != null ? `${fmtNum(rt.hydro_soc_pct, 0)}%` : "--");
    setEmptyHint($("#kpi-diesel-saved"), k.diesel_saved_tons == null, k.diesel_saved_tons == null ? tr("模型库未配置柴发油耗参数") : "");
    setEmptyHint($("#kpi-days"), k.running_days == null, k.running_days == null ? tr("历史库尚无采样记录") : "");
    if ($("#home-weather-tag")) {
      $("#home-weather-tag").textContent = tr(effWx.preset_label || (effWx.mode === "manual" ? "工况推演" : "现场实测"));
    }
    const homeWxBtn = $("#home-weather-btn");
    if (homeWxBtn) {
      homeWxBtn.classList.toggle("is-manual", effWx.mode === "manual");
      homeWxBtn.title = cpl.strategy ? `${tr(effWx.preset_label || "")} · ${tr(cpl.strategy)}` : tr("配置南极气象工况");
    }
    syncAllWeatherBadges(effWx, cpl);

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
  let pfMinimap = null;
  function applySvgTransform() {
    const stage = $("#pf-stage");
    if (!stage) return;
    stage.style.transform = `translate(${state.pfX}px, ${state.pfY}px) scale(${state.pfScale})`;
    const label = $("#pf-zoom-label");
    if (label) label.textContent = Math.round(state.pfScale * 100) + "%";
    pfMinimap?.update();
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

  function parseColorLuminance(raw) {
    const str = String(raw || "").trim().toLowerCase();
    if (!str || str === "none" || str === "transparent") return null;
    let r = 0, g = 0, b = 0;
    const hex = str.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/);
    if (hex) {
      const h = hex[1].length === 3
        ? hex[1].split("").map((c) => c + c).join("")
        : hex[1];
      r = parseInt(h.slice(0, 2), 16);
      g = parseInt(h.slice(2, 4), 16);
      b = parseInt(h.slice(4, 6), 16);
    } else {
      const rgb = str.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)/);
      if (!rgb) return null;
      r = Number(rgb[1]);
      g = Number(rgb[2]);
      b = Number(rgb[3]);
    }
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  }

  function isSvgBgDark(svgEl) {
    const svg = svgEl || $("#pf-stage svg");
    const bgRect = svg?.querySelector("#Background_Layer rect");
    const nativeFill = bgRect?.dataset?.nativeFill || bgRect?.getAttribute("fill") || "";
    const lum = parseColorLuminance(nativeFill);
    if (lum !== null) return lum < 0.5;
    return isDarkTheme();
  }

  function applySvgThemeColors() {
    const svg = $("#pf-stage svg");
    if (!svg) return;
    const bgRect = svg.querySelector("#Background_Layer rect");
    const wrap = $("#pf-canvas-wrap");
    let nativeFill = "";
    if (bgRect) {
      if (!bgRect.dataset.nativeFill) {
        const rawFill = (bgRect.getAttribute("fill") || "").trim();
        if (rawFill && rawFill.toLowerCase() !== "none" && rawFill.toLowerCase() !== "transparent") {
          bgRect.dataset.nativeFill = rawFill;
        }
      }
      nativeFill = bgRect.dataset.nativeFill || "";
      // 若 SVG 自带背景色则原样保留，仅在无原生背景色时按门户主题兜底
      if (nativeFill) {
        bgRect.setAttribute("fill", nativeFill);
      } else {
        bgRect.setAttribute("fill", isDarkTheme() ? "#071122" : "#f1f5f9");
      }
    }
    if (wrap) {
      if (nativeFill) wrap.style.setProperty("background", nativeFill, "important");
      else wrap.style.removeProperty("background");
    }
    const dark = isSvgBgDark(svg);
    svg.querySelectorAll("#Text_Layer text").forEach((t) => {
      t.setAttribute("fill", dark ? "#cfe6ff" : "#1e293b");
      t.setAttribute("stroke", dark ? "rgba(6, 16, 34, 0.9)" : "rgba(255, 255, 255, 0.92)");
    });
    svg.querySelectorAll("#Measurement_Layer g.mg rect").forEach((r) => {
      r.setAttribute("fill", dark ? "rgba(8, 22, 44, 0.72)" : "rgba(255, 255, 255, 0.88)");
      r.setAttribute("stroke", dark ? "rgba(0, 229, 255, 0.25)" : "rgba(100, 116, 139, 0.35)");
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
      if (state.pfPinned) return;
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
      if (!state.pfPinned) scheduleHidePfPopovers();
    });

    svg.addEventListener("click", (event) => {
      const devNode = event.target.closest("use[dev-id], text[dev-id]");
      if (!devNode) return;
      const devId = devNode.getAttribute("dev-id");
      if (!devId) return;
      // 单击设备锁定弹窗，方便切换耦合参数/实时量测与下发指令；双击或点「日曲线 →」跳转设备详情
      state.pfPinned = true;
      showDevicePopover(devId, event.clientX, event.clientY, true);
    });

    svg.addEventListener("dblclick", (event) => {
      const devNode = event.target.closest("use[dev-id], text[dev-id]");
      if (!devNode) return;
      const devId = devNode.getAttribute("dev-id");
      const dev = devId ? state.pfData?.devices?.[devId] : null;
      if (dev) {
        hidePfPopovers(true);
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
    const dark = isSvgBgDark();
    if (voltageType === "dc") return dark ? "#38bdf8" : "#2563eb";
    if (voltageType === "hydro") return dark ? "#c084fc" : "#7c3aed";
    return dark ? "#34f5a8" : "#0d9488";
  }

  function arrowStrokeFor() {
    return isSvgBgDark() ? "#04101f" : "#ffffff";
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
          const dark = isSvgBgDark(svg);
          mv.setAttribute("fill", entry.raw ? (dark ? "#00e6a8" : "#059669") : (dark ? "#ff4d6d" : "#dc2626"));
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

  function syncPinButtonUi() {
    const devPop = $("#pf-dev-popover");
    const pinBtn = $("#pf-pin-device");
    if (devPop) devPop.classList.toggle("is-pinned", state.pfPinned);
    if (pinBtn) {
      pinBtn.classList.toggle("active", state.pfPinned);
      pinBtn.title = state.pfPinned ? tr("取消固定") : tr("固定弹窗");
    }
  }

  function hidePfPopovers(force = false) {
    cancelHidePfPopovers();
    if (!force && state.pfPinned) return;
    state.pfPinned = false;
    syncPinButtonUi();
    const devPop = $("#pf-dev-popover");
    const measPop = $("#pf-meas-popover");
    if (devPop) devPop.hidden = true;
    if (measPop) measPop.hidden = true;
    state.pfHoverDev = null;
    state.pfHoverMeas = null;
  }

  function scheduleHidePfPopovers(delay = 180) {
    if (state.pfPinned) return;
    cancelHidePfPopovers();
    state.pfPopHideTimer = setTimeout(() => {
      hidePfPopovers(false);
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

  function renderDevicePopoverContent(devId, highlight = false, { resetTab = false } = {}) {
    const dev = state.pfData?.devices?.[devId];
    const svg = $("#pf-stage svg");
    if (highlight && svg) {
      svg.querySelectorAll(".pf-highlight").forEach((el) => el.classList.remove("pf-highlight"));
      const node = svg.querySelector(`use[dev-id="${CSS.escape(devId)}"]`);
      if (node) node.classList.add("pf-highlight");
    }
    const jumpBtn = $("#pf-jump-device");
    const tabsEl = $("#pf-dev-pop-tabs");
    const metricsEl = $("#pf-dev-pop-metrics");
    const ctrlHost = $("#pf-dev-pop-controls");
    if (!dev) {
      $("#pf-dev-pop-name").textContent = devId;
      $("#pf-dev-pop-meta").textContent = "暂无模型参数";
      if (tabsEl) { tabsEl.hidden = true; tabsEl.innerHTML = ""; }
      if (metricsEl) { metricsEl.hidden = false; metricsEl.innerHTML = '<div class="empty-hint">模型库无此设备参数</div>'; }
      if (ctrlHost) { ctrlHost.hidden = true; ctrlHost.innerHTML = ""; }
      if (jumpBtn) jumpBtn.disabled = true;
      return;
    }

    const params = dev.model_params || [];
    const rawParamTabs = Array.isArray(dev.model_param_tabs) && dev.model_param_tabs.length
      ? dev.model_param_tabs
      : (params.length ? [{ key: "params", label: "设备参数", params }] : []);
    const ctrls = dev.controls || [];
    const totalParamCount = rawParamTabs.reduce((acc, t) => acc + (t.params?.length || 0), 0);

    $("#pf-dev-pop-name").textContent = dev.name;
    $("#pf-dev-pop-meta").textContent = `${dev.table_name} #${dev.idx}`;

    if (jumpBtn) {
      jumpBtn.disabled = !dev.bound_count;
      jumpBtn.onclick = () => {
        hidePfPopovers(true);
        state.devCategory = dev.category;
        state.devSelected = { table_name: dev.table_name, idx: dev.idx, dev_id: dev.dev_id };
        switchView("devices");
      };
    }
    const pinBtn = $("#pf-pin-device");
    if (pinBtn) {
      pinBtn.onclick = () => {
        state.pfPinned = !state.pfPinned;
        syncPinButtonUi();
      };
    }
    const closeBtn = $("#pf-close-device");
    if (closeBtn) {
      closeBtn.onclick = () => hidePfPopovers(true);
    }
    syncPinButtonUi();

    // 实时量测分页（展示有效实时遥测/遥信，无需离开弹窗去 SVG 中找数值）
    const liveMetrics = (dev.metrics || []).filter((m) => m.value !== null && m.value !== undefined);
    const livePairs = liveMetrics.map((m) => {
      const valText = m.kind === "yx"
        ? (m.metric === "position" ? (Number(m.value) ? "合闸" : "分闸") : (Number(m.value) ? "运行" : "停运"))
        : `${fmtNum(m.display_value ?? m.value, 2)}${m.unit ? " " + m.unit : ""}`;
      return { label: m.label || m.metric, value: valText };
    });

    // 组装分页清单（实时量测 + 耦合/扩展参数各自独立成页 + 存在指令时增加「就地指令」分页）
    const tabs = [];
    if (livePairs.length) {
      tabs.push({ key: "__live__", label: "实时量测", count: livePairs.length, kind: "params", params: livePairs });
    }
    rawParamTabs.forEach((t) => {
      tabs.push({
        key: t.key,
        label: t.label,
        count: (t.params || []).length,
        kind: "params",
        params: t.params || [],
      });
    });
    if (ctrls.length) {
      tabs.push({ key: "__controls__", label: "就地指令", count: ctrls.length, kind: "controls" });
    }
    if (!tabs.length) {
      tabs.push({ key: "params", label: "设备参数", count: 0, kind: "params", params: [] });
    }

    if (resetTab || !tabs.some((t) => t.key === state.pfPopTab)) {
      state.pfPopTab = tabs[0].key;
    }

    const applyActiveTab = (tabKey) => {
      state.pfPopTab = tabKey;
      const active = tabs.find((t) => t.key === tabKey) || tabs[0];
      if (tabsEl) {
        tabsEl.querySelectorAll(".pf-pop-tab").forEach((btn) => {
          const isCur = btn.dataset.pfPopTab === active.key;
          btn.classList.toggle("active", isCur);
          btn.setAttribute("aria-selected", isCur ? "true" : "false");
        });
      }
      if (active.kind === "controls") {
        if (metricsEl) metricsEl.hidden = true;
        if (ctrlHost) ctrlHost.hidden = false;
      } else {
        if (ctrlHost) ctrlHost.hidden = true;
        if (metricsEl) {
          metricsEl.hidden = false;
          metricsEl.innerHTML = active.params.length
            ? buildKvTableHtml(active.params)
            : '<div class="empty-hint">暂无设备模型参数</div>';
        }
      }
    };

    if (tabsEl) {
      if (tabs.length > 1) {
        tabsEl.hidden = false;
        tabsEl.innerHTML = tabs.map((t) => {
          const act = t.key === state.pfPopTab ? " active" : "";
          return `<button type="button" class="pf-pop-tab${act}" role="tab" aria-selected="${act ? "true" : "false"}" data-pf-pop-tab="${esc(t.key)}">${esc(t.label)}<small>${t.count}</small></button>`;
        }).join("");
        tabsEl.querySelectorAll("[data-pf-pop-tab]").forEach((btn) => {
          btn.addEventListener("click", () => applyActiveTab(btn.dataset.pfPopTab));
        });
      } else {
        tabsEl.hidden = true;
        tabsEl.innerHTML = "";
      }
    }

    // 仅当用户未在指令输入框中编辑时才刷新指令区 DOM，避免定时刷新打断输入
    const editingCtrl = ctrlHost && document.activeElement && ctrlHost.contains(document.activeElement);
    if (ctrlHost && !editingCtrl) {
      if (!ctrls.length) {
        ctrlHost.innerHTML = "";
      } else {
        const targetDevFor = (c) => (
          c.owner_table && c.owner_idx != null
            ? { table_name: c.owner_table, idx: c.owner_idx, name: c.owner_name || dev.name }
            : dev
        );
        ctrlHost.innerHTML = ctrls.map((c, ci) => {
          const disabled = Boolean(c.disabled);
          const tDev = targetDevFor(c);
          const key = `${tDev.table_name}:${tDev.idx}:${c.control}`;
          const draft = state.ctrlDrafts.has(key) ? state.ctrlDrafts.get(key) : (c.value == null ? "" : String(c.value));
          const sideHtml = c.side_label ? `<span class="pf-pop-side-tag">${esc(c.side_label)}</span>` : "";
          const ops = c.kind === "yk"
            ? `<span class="ctrl-btn-group"><button type="button" class="btn secondary" data-pf-yk="${ci}" data-val="1"${disabled ? " disabled" : ""}>合/启</button><button type="button" class="btn secondary" data-pf-yk="${ci}" data-val="0"${disabled ? " disabled" : ""}>分/停</button></span>`
            : `<span class="ctrl-btn-group"><input class="ctrl-inline-input pf-pop-ctrl-yt" type="number" step="any" data-pf-yt-input="${ci}" value="${esc(draft)}" placeholder="${esc(c.unit || "设定值")}"${disabled ? " disabled" : ""}><button type="button" class="btn primary" data-pf-yt-send="${ci}"${disabled ? " disabled" : ""}>下发</button></span>`;
          return `<div class="pf-pop-ctrl-row"><span class="pf-pop-ctrl-name" title="${esc(c.control)}">${sideHtml}${esc(c.label || c.control)}</span>${ctrlStatusBadge(c)}${ops}</div>`;
        }).join("");
        ctrlHost.querySelectorAll("[data-pf-yt-input]").forEach((inp) => {
          inp.addEventListener("input", () => {
            const c = ctrls[Number(inp.dataset.pfYtInput)];
            if (!c) return;
            const tDev = targetDevFor(c);
            state.ctrlDrafts.set(`${tDev.table_name}:${tDev.idx}:${c.control}`, inp.value);
          });
          inp.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              const c = ctrls[Number(inp.dataset.pfYtInput)];
              if (c) dispatchPortalControl(targetDevFor(c), c, inp.value).catch((err) => setConnection(false, err.message));
            }
          });
        });
        ctrlHost.querySelectorAll("[data-pf-yk]").forEach((btn) => {
          btn.addEventListener("click", () => {
            const c = ctrls[Number(btn.dataset.pfYk)];
            if (c) dispatchPortalControl(targetDevFor(c), c, Number(btn.dataset.val)).catch((err) => setConnection(false, err.message));
          });
        });
        ctrlHost.querySelectorAll("[data-pf-yt-send]").forEach((btn) => {
          btn.addEventListener("click", () => {
            const ci = Number(btn.dataset.pfYtSend);
            const c = ctrls[ci];
            const inp = ctrlHost.querySelector(`[data-pf-yt-input="${ci}"]`);
            if (c) dispatchPortalControl(targetDevFor(c), c, inp ? inp.value : c?.value).catch((err) => setConnection(false, err.message));
          });
        });
      }
    }

    applyActiveTab(state.pfPopTab);
  }

  function showDevicePopover(devId, clientX, clientY, highlight = false) {
    const devPop = $("#pf-dev-popover");
    const measPop = $("#pf-meas-popover");
    if (!devPop) return;
    if (measPop) measPop.hidden = true;
    state.pfHoverMeas = null;
    const switchedDev = state.pfHoverDev !== devId;
    state.pfHoverDev = devId;
    renderDevicePopoverContent(devId, highlight, { resetTab: switchedDev });
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

  function applyPfCanvasModes() {
    const wrap = $("#pf-canvas-wrap");
    if (!wrap) return;
    wrap.classList.toggle("pf-dark-blend", state.pfCanvasMode === "dark-blend");
    wrap.classList.toggle("pf-fault-filter", state.pfFaultOnly);
  }

  async function ensurePowerflowMeta() {
    const headers = state.pfMetaEtag ? { "If-None-Match": state.pfMetaEtag } : {};
    const resp = await fetch("/api/powerflow/meta", { headers });
    if (resp.status === 304 && state.pfMeta) return state.pfMeta;
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const etag = resp.headers.get("ETag") || "";
    const meta = await resp.json();
    state.pfMeta = meta;
    state.pfMetaEtag = etag;
    return meta;
  }

  async function loadPowerflow() {
    await ensureSvgLoaded();
    const [meta, live] = await Promise.all([
      state.pfMeta ? Promise.resolve(state.pfMeta) : ensurePowerflowMeta(),
      api("/api/powerflow/live"),
    ]);
    // 将静态模型元数据与轻量实时快照合并为统一结构，保持所有下游渲染零侵入
    const mergedDevices = {};
    const metaDevs = meta?.devices || {};
    const liveDevs = live?.devices_live || {};
    const allIds = new Set([...Object.keys(metaDevs), ...Object.keys(liveDevs)]);
    allIds.forEach((devId) => {
      const m = metaDevs[devId] || {};
      const lv = liveDevs[devId] || {};
      mergedDevices[devId] = {
        ...m,
        ...lv,
        dev_id: devId,
        name: m.name || lv.name || devId,
        model_params: m.model_params || [],
        model_param_tabs: m.model_param_tabs || [],
        metrics: lv.metrics || [],
        controls: lv.controls || [],
      };
    });
    const pf = {
      timestamp: live.timestamp,
      svg_slots: live.svg_slots || {},
      breakers: live.breakers || {},
      flows: live.flows || {},
      devices: mergedDevices,
    };
    state.pfData = pf;
    updateSvgLiveValues(pf);
    // 标注异常或分闸设备，支持「仅异常/分闸」过滤高亮
    const svg = $("#pf-stage svg");
    if (svg) {
      svg.querySelectorAll("use[dev-id]").forEach((el) => {
        const id = el.getAttribute("dev-id");
        const d = mergedDevices[id];
        const abnormal = Boolean(d && (Number(d.alarm_count || 0) > 0 || d.position === 0));
        el.classList.toggle("pf-is-abnormal", abnormal);
      });
    }
    applyPfCanvasModes();
    pfMinimap?.update();
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
      if (state.devFilter === "starred") return state.starredDevs.has(d.dev_id);
      if (state.devFilter === "alarm") return Number(d.alarm_count || 0) > 0;
      if (state.devFilter === "running") {
        if (d.position !== null) return Boolean(d.position);
        return d.run !== 0;
      }
      return true;
    });
    // 收藏设备自动靠前展示，方便值班员快速切换关注目标
    filteredDevs.sort((a, b) => (state.starredDevs.has(b.dev_id) ? 1 : 0) - (state.starredDevs.has(a.dev_id) ? 1 : 0));

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
        const starred = state.starredDevs.has(d.dev_id);
        return (
          `<button type="button" class="dev-item ${selected ? "selected" : ""}" data-dev-id="${esc(d.dev_id)}">` +
          `<div class="dev-item-top"><strong title="${esc(d.name)}">${alarmDot}${esc(d.name)}</strong><span><span role="button" tabindex="0" class="dev-star-btn ${starred ? "active" : ""}" data-star-dev="${esc(d.dev_id)}" title="收藏关注">${starred ? "★" : "☆"}</span>${okBadge}</span></div>` +
          `<div class="dev-item-sub"><span>${esc(d.table_name)} #${d.idx}</span><span class="num">${esc(primaryMetricSummary(d))}</span></div>` +
          `</button>`
        );
      })
      .join("");

    listEl.querySelectorAll("[data-star-dev]").forEach((starEl) => {
      starEl.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const id = starEl.dataset.starDev;
        if (!id) return;
        if (state.starredDevs.has(id)) state.starredDevs.delete(id);
        else state.starredDevs.add(id);
        window.ScadaExtras?.savePrefs({ starredDevs: Array.from(state.starredDevs) });
        loadDevices().catch(() => {});
      });
    });

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
          `<span class="m-name" title="${esc(m.label)}">${esc(m.label)}</span>` +
          `${qBadge}` +
          `</div>` +
          `<div class="m-main"><span class="metric-val">${esc(valStr)}<small>${esc(m.unit || "")}</small></span></div>` +
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
    const qs = new URLSearchParams({ hours: String(hours) });
    if (state.trendDate) qs.set("date", state.trendDate);
    if (state.trendComparePrev) qs.set("compare", "prev_day");
    const data = await api(`/api/devices/${encodeURIComponent(dev.table_name)}/${dev.idx}/trend?${qs.toString()}`);
    if (seq !== trendReqSeq) return;
    state.trendData = data;
    state.trendZoomRange = null;
    if ($("#trend-reset-zoom")) $("#trend-reset-zoom").hidden = true;
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
    if (window.ScadaCharts && typeof window.ScadaCharts.drawTrend === "function") {
      state.trendPlotMeta = window.ScadaCharts.drawTrend({
        canvas,
        wrap,
        palette: chartPalette(),
        allSeries: state.trendData?.series || [],
        hiddenSet: state.trendHidden,
        axisMode: state.trendAxisMode || "dual",
        zoomRange: state.trendZoomRange,
        hoverX: state.trendHoverX,
        brushRange: state.trendBrushRange,
      });
      return;
    }
  }

  let trendHoverRaf = 0;
  let trendBrushing = false;
  let trendBrushStartX = 0;
  $("#trend-canvas")?.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || !state.trendPlotMeta) return;
    const rect = e.target.getBoundingClientRect();
    trendBrushing = true;
    trendBrushStartX = e.clientX - rect.left;
    state.trendBrushRange = [trendBrushStartX, trendBrushStartX];
  });
  $("#trend-canvas")?.addEventListener("mousemove", (e) => {
    const rect = e.target.getBoundingClientRect();
    const x = e.clientX - rect.left;
    state.trendHoverX = x;
    if (trendBrushing) state.trendBrushRange = [trendBrushStartX, x];
    if (!trendHoverRaf) {
      trendHoverRaf = requestAnimationFrame(() => {
        trendHoverRaf = 0;
        drawTrendChart();
      });
    }
  });
  window.addEventListener("mouseup", () => {
    if (!trendBrushing) return;
    trendBrushing = false;
    const br = state.trendBrushRange;
    state.trendBrushRange = null;
    const meta = state.trendPlotMeta;
    if (br && meta && Math.abs(br[1] - br[0]) >= 14) {
      const x1 = Math.max(meta.pad.left, Math.min(meta.pad.left + meta.plotW, Math.min(br[0], br[1])));
      const x2 = Math.max(meta.pad.left, Math.min(meta.pad.left + meta.plotW, Math.max(br[0], br[1])));
      if (x2 - x1 >= 10) {
        const t1 = meta.minT + ((x1 - meta.pad.left) / meta.plotW) * (meta.maxT - meta.minT);
        const t2 = meta.minT + ((x2 - meta.pad.left) / meta.plotW) * (meta.maxT - meta.minT);
        state.trendZoomRange = [t1, t2];
        if ($("#trend-reset-zoom")) $("#trend-reset-zoom").hidden = false;
      }
    }
    drawTrendChart();
  });
  $("#trend-canvas")?.addEventListener("dblclick", () => {
    if (!state.trendZoomRange) return;
    state.trendZoomRange = null;
    if ($("#trend-reset-zoom")) $("#trend-reset-zoom").hidden = true;
    drawTrendChart();
  });
  $("#trend-canvas")?.addEventListener("mouseleave", () => {
    state.trendHoverX = null;
    if (!trendBrushing) drawTrendChart();
  });
  $("#trend-reset-zoom")?.addEventListener("click", () => {
    state.trendZoomRange = null;
    if ($("#trend-reset-zoom")) $("#trend-reset-zoom").hidden = true;
    drawTrendChart();
  });
  $("#trend-axis-mode")?.addEventListener("change", (e) => {
    state.trendAxisMode = e.target.value || "dual";
    window.ScadaExtras?.savePrefs({ trendAxisMode: state.trendAxisMode });
    window.ScadaCharts?.clearCache();
    drawTrendChart();
  });
  $("#trend-date")?.addEventListener("change", (e) => {
    state.trendDate = e.target.value || "";
    if (state.devSelected && !state.compareMode) loadDeviceTrend(state.devSelected).catch(() => {});
  });
  $("#trend-compare-prev")?.addEventListener("change", (e) => {
    state.trendComparePrev = Boolean(e.target.checked);
    if (state.devSelected && !state.compareMode) loadDeviceTrend(state.devSelected).catch(() => {});
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
      const [chRes, topoRes] = await Promise.all([
        api("/api/comm/channels"),
        api("/api/comm/topology").catch(() => null),
      ]);
      const topoEl = $("#comm-topology-bar");
      if (topoEl && topoRes) {
        const links = topoRes.links || [];
        topoEl.innerHTML =
          `<div class="comm-topo-root"><strong>SCADA 主站</strong><small>${online}/${rtus.length} RTU</small></div>` +
          `<div class="comm-topo-links">` +
          (links.length
            ? links.map((lk) => {
                const onlineCls = lk.connection_state === "connected"
                  ? (lk.health_pct < 90 ? "is-online is-warn" : "is-online")
                  : "is-offline";
                const faultHint = lk.fault_devices?.length ? ` · 异常 ${lk.fault_devices.length}` : "";
                return (
                  `<div class="comm-topo-card ${onlineCls}">` +
                  `<div class="comm-topo-top"><strong>RTU ${lk.rtu_id} · ${esc(lk.rtu_name)}</strong><span>${lk.health_pct}%</span></div>` +
                  `<div class="muted">${esc(lk.channel_type)} · ${lk.device_count} 台设备${faultHint}</div>` +
                  `<div class="comm-topo-bar"><i style="width:${Math.max(0, Math.min(100, Number(lk.health_pct || 0)))}%"></i></div>` +
                  `</div>`
                );
              }).join("")
            : '<span class="muted">暂无链路</span>') +
          `</div>`;
      }
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

    // 交直流平衡栏与母线电压（严格源自实时数据库，无实测值显示 --）
    const dcVText = dc.voltage_v != null ? String(Math.round(dc.voltage_v)) : "--";
    const acVText = ac.voltage_v != null ? String(Math.round(ac.voltage_v)) : "--";
    if ($("#ef-dc-volt")) $("#ef-dc-volt").textContent = `DC ${dcVText}V`;
    if ($("#ef-dc-balance")) $("#ef-dc-balance").textContent = `发 ${fmtNum(dc.gen_kw, 1)} / 荷 ${fmtNum(dc.load_kw, 1)} kW`;
    if ($("#ef-ac-volt")) $("#ef-ac-volt").textContent = `AC ${acVText}V`;
    if ($("#ef-ac-balance")) $("#ef-ac-balance").textContent = `发 ${fmtNum(ac.gen_kw, 1)} / 荷 ${fmtNum(ac.load_kw, 1)} kW`;
    $("#ef-svg-dc-cap").textContent = `DC ${dcVText}V`;
    $("#ef-svg-ac-cap").textContent = `AC ${acVText}V`;

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
    const dcLoadV = dcLoad.voltage_v != null ? String(Math.round(dcLoad.voltage_v)) : dcVText;
    $("#ef-dc-load-v").textContent = `· ${dcLoadV}V`;
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
    $("#ef-fc-flow").textContent = `${fc.flow_nm3h != null ? fmtNum(fc.flow_nm3h, 2) : "--"} Nm³/h`;
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
    const acLoadV = acLoad.voltage_v != null ? String(Math.round(acLoad.voltage_v)) : acVText;
    $("#ef-ac-load-v").textContent = `· ${acLoadV}V`;
    $("#ef-ac-load-kw").textContent = `${fmtNum(acLoad.p_kw, 1)} kW`;
    $("#ef-ac-load-sub").textContent = `占总负荷 ${fmtNum(acLoad.share_pct, 1)}%`;
    setFlowLineState("ef-line-ac-load", (acLoad.p_kw || 0) > 0.2 ? "fwd" : "idle");

    // 右侧环境与数据源面板（含气象与能源系统物理耦合；无实测值严格显示 --）
    const sampleDt = data.latest_sample_time ? new Date(data.latest_sample_time) : null;
    $("#ef-sample-time").textContent = (!sampleDt || Number.isNaN(sampleDt.getTime()))
      ? "--:--:--"
      : sampleDt.toLocaleTimeString("zh-CN", { hour12: false });
    const efCpl = data.coupling || {};
    if ($("#ef-env-temp")) {
      $("#ef-env-temp").textContent = env.temperature_c != null ? `${fmtNum(env.temperature_c, 1)} °C` : "-- °C";
      $("#ef-env-temp").classList.toggle("is-empty", env.temperature_c == null);
      $("#ef-env-temp").title = env.wind_chill_c != null
        ? `${tr("风寒体感")} ${fmtNum(env.wind_chill_c, 1)} °C`
        : tr("未接入气温传感器 · 点击补录");
    }
    $("#ef-env-wind").textContent = env.wind_speed_ms != null ? `${fmtNum(env.wind_speed_ms, 2)} m/s` : "--";
    $("#ef-env-wind").classList.toggle("is-empty", env.wind_speed_ms == null);
    $("#ef-env-irr").textContent = env.irradiance_wm2 != null ? `${fmtNum(env.irradiance_wm2, 1)} W/m²` : "--";
    $("#ef-env-irr").classList.toggle("is-empty", env.irradiance_wm2 == null);
    if ($("#ef-env-ice")) {
      const iceParts = [env.icing_label ? tr(env.icing_label) : "", env.snow_label ? tr(env.snow_label) : ""].filter(Boolean);
      $("#ef-env-ice").textContent = iceParts.length ? iceParts.join(" · ") : "--";
      $("#ef-env-ice").classList.toggle("is-empty", !iceParts.length);
      $("#ef-env-ice").title = iceParts.length ? "" : tr("未录入覆冰/积雪工况 · 点击补录");
    }
    if ($("#ef-cpl-wb")) {
      const wTxt = efCpl.wind?.state === "超速切出"
        ? tr("切出")
        : (efCpl.wind?.avail_pct != null ? `${fmtNum(efCpl.wind.avail_pct, 0)}%` : "--");
      const bTxt = efCpl.bess?.cap_retention_pct != null ? `${fmtNum(efCpl.bess.cap_retention_pct, 0)}%` : "--";
      $("#ef-cpl-wb").textContent = `${wTxt} / ${bTxt}`;
      $("#ef-cpl-wb").classList.toggle("is-empty", efCpl.wind?.avail_pct == null && efCpl.bess?.cap_retention_pct == null);
      if (efCpl.bess?.cap_retention_pct == null) $("#ef-cpl-wb").title = tr("电储低温容量需气温参数 · 点击补录");
    }
    if ($("#ef-cpl-heat")) {
      const hRaw = efCpl.thermal?.heating_delta_kw;
      $("#ef-cpl-heat").classList.toggle("is-empty", hRaw == null);
      if (hRaw == null) {
        $("#ef-cpl-heat").textContent = "-- kW";
        $("#ef-cpl-heat").style.color = "";
        $("#ef-cpl-heat").title = tr("采暖增荷需气温参数 · 点击补录");
      } else {
        const hDelta = Number(hRaw);
        $("#ef-cpl-heat").textContent = `+${fmtNum(hDelta, 1)} kW`;
        $("#ef-cpl-heat").style.color = hDelta >= 35 ? "var(--danger)" : (hDelta >= 15 ? "var(--warn)" : "var(--accent)");
        $("#ef-cpl-heat").title = "";
      }
    }
    const efWxBtn = $("#ef-weather-btn");
    if (efWxBtn) {
      efWxBtn.classList.toggle("is-manual", env.mode === "manual");
      efWxBtn.title = env.mode === "manual" ? `${tr(env.preset_label || "工况推演")} · ${tr("工况输入")}` : tr("配置或补录南极气象工况");
    }
    syncAllWeatherBadges(env, efCpl);
    $("#ef-env-dcu").textContent = env.dc_voltage_v != null ? `${fmtNum(env.dc_voltage_v, 1)} V` : "-- V";
    $("#ef-env-dcu").classList.toggle("is-empty", env.dc_voltage_v == null);
    $("#ef-env-dcu").title = env.dc_voltage_v == null ? tr("未接入实测直流母线电压") : "";
    $("#ef-env-acu").textContent = env.ac_voltage_v != null ? `${fmtNum(env.ac_voltage_v, 1)} V` : "-- V";
    $("#ef-env-acu").classList.toggle("is-empty", env.ac_voltage_v == null);
    $("#ef-env-acu").title = env.ac_voltage_v == null ? tr("未接入实测交流母线电压") : "";
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
      : (a.maintenance
          ? `<span class="badge maint" title="${esc(a.maintenance_reason || "检修挂牌")}">已挂牌</span>`
          : (a.acked ? '<span class="badge muted">已确认</span>' : '<span class="badge danger">未确认</span>'));
    const timeText = isHist
      ? `${fmtTime(a.first_seen_ms)}${a.recovered_ms ? " → " + fmtTime(a.recovered_ms) : ""}`
      : fmtTime(a.time_ms || a.time_iso);
    const ackBtn = (!isHist && !a.acked)
      ? `<button type="button" class="btn secondary" data-alarm-ack="${idx}">确认</button>`
      : "";
    const maintBtn = !isHist
      ? (a.maintenance
          ? `<button type="button" class="btn secondary" data-alarm-unmaint="${idx}">解除挂牌</button>`
          : `<button type="button" class="btn secondary" data-alarm-maint="${idx}">检修挂牌</button>`)
      : "";
    const ackMeta = (isHist && (a.ack_operator || a.ack_note))
      ? ` <small class="muted">${esc([a.ack_operator, a.ack_note].filter(Boolean).join(" · "))}</small>`
      : "";
    return `<tr class="${a.acked ? "alarm-row-acked " : ""}${a.maintenance ? "is-maint-row " : ""}${childClass}">` +
      `<td>${alarmTypeBadge(a.type)}</td>` +
      `<td><strong>${esc(a.device_name)}</strong> <small class="muted">${esc(a.table_name)} #${a.idx}</small></td>` +
      `<td>${esc(a.attr_label)} <small class="muted">${esc(a.attr)}</small></td>` +
      `<td class="num">${valStr}</td>` +
      `<td>${esc(a.detail)}${ackMeta}</td>` +
      `<td class="num">RTU ${a.rtu_id ?? "-"} / #${a.point_no ?? "-"}</td>` +
      `<td>${esc(timeText)}</td>` +
      `<td>${stateBadge}</td>` +
      `<td><span class="alarm-ops">${ackBtn}${maintBtn}` +
      `<button type="button" class="btn secondary" data-alarm-loc="${idx}" title="定位潮流图">定位</button>` +
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
    if (!isHist && state.hideMaintAlarms) qs.set("hide_maintenance", "1");

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
        const noteRes = await openAlarmNoteDialog({
          title: "确认告警",
          meta: `${a.device_name} · ${a.attr_label}`,
          reasonPlaceholder: "处置备注（可选）",
        });
        if (!noteRes) return;
        await apiPost("/api/alarms/ack", { keys: [a.key], operator: noteRes.operator, note: noteRes.text });
        window.ScadaExtras?.showToast("告警已确认", "ok");
        await loadAlarms();
      });
    });
    tbody.querySelectorAll("[data-alarm-maint]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const a = state.alarmItems[Number(btn.dataset.alarmMaint)];
        if (!a) return;
        const noteRes = await openAlarmNoteDialog({
          title: "检修挂牌",
          meta: `${a.device_name}（${a.table_name} #${a.idx}）`,
          reasonPlaceholder: "挂牌原因",
        });
        if (!noteRes) return;
        await apiPost("/api/maintenance", {
          table_name: a.table_name,
          idx: a.idx,
          device_name: a.device_name,
          operator: noteRes.operator,
          reason: noteRes.text || "检修挂牌",
          hours: 8,
        });
        window.ScadaExtras?.showToast(`已挂牌：${a.device_name}`, "info");
        await loadAlarms();
      });
    });
    tbody.querySelectorAll("[data-alarm-unmaint]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const a = state.alarmItems[Number(btn.dataset.alarmUnmaint)];
        if (!a) return;
        await apiDelete(`/api/maintenance/${encodeURIComponent(a.dev_id)}`);
        window.ScadaExtras?.showToast(`已解除挂牌：${a.device_name}`, "ok");
        await loadAlarms();
      });
    });
    tbody.querySelectorAll("[data-alarm-loc]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const a = state.alarmItems[Number(btn.dataset.alarmLoc)];
        if (!a) return;
        switchView("powerflow");
        await loadPowerflow();
        state.pfPinned = true;
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
      ctx.fillText(tr("暂无采样曲线"), W / 2, H / 2);
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
        rows.push({ label: tr(s.label), val: `${Number(best[1]).toFixed(1)} kW`, color: s.color });
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
    const fmtCkCum = (v, d = 2) => {
      if (v === null || v === undefined || v === "") return "--";
      const num = Number(v);
      if (!Number.isFinite(num)) return "--";
      if (num > 0 && num < 0.05) return num.toFixed(Math.max(d, 3));
      return num.toFixed(d);
    };
    $("#ck-run-days").textContent = data.running_days != null ? `${data.running_days} 天` : "-- 天";
    $("#ck-total-supply").textContent = fmtCkCum(cum.total_supply_wkwh, 2);
    $("#ck-total-load").textContent = fmtCkCum(cum.total_load_wkwh, 2);
    $("#ck-diesel-saved").textContent = fmtCkCum(cum.diesel_saved_tons, 2);
    $("#ck-co2-reduced").textContent = fmtCkCum(cum.co2_reduced_tons, 2);

    // 左中 L2
    $("#ck-replace-pct").textContent = res.diesel_replace_pct != null ? `替代率 ${fmtNum(res.diesel_replace_pct, 1)}%` : "替代率 --%";
    $("#ck-autonomy-h").textContent = res.autonomy_hours != null ? fmtNum(res.autonomy_hours, 1) : "--";
    $("#ck-wind-saved").textContent = fmtCkCum(res.wind_saved_tons, 2);
    $("#ck-pv-saved").textContent = fmtCkCum(res.pv_saved_tons, 2);
    const pText = res.hydro_pressure_mpa != null ? `/${fmtNum(res.hydro_pressure_mpa, 2)}MPa` : "";
    $("#ck-h2-reserve").textContent = res.hydro_soc_pct != null
      ? `${fmtNum(res.hydro_soc_pct, 1)}%${pText}`
      : (res.hydro_pressure_mpa != null ? `${fmtNum(res.hydro_pressure_mpa, 2)}MPa` : "--");

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
    // 储能位于右中（路径由中心指向右），放电指向中心需 reverse
    setCkPulse("ck-pulse-storage", n.storage?.p_kw, { reverse: n.storage?.state === "discharge" });
    setCkPulse("ck-pulse-load", n.load?.p_kw);
    // 氢能位于左下（路径由左下指向中心），制氢背离中心需 reverse
    setCkPulse("ck-pulse-hydrogen", Math.max(Number(n.hydrogen?.prod_kw || 0), Number(n.hydrogen?.gen_kw || 0)), {
      reverse: h2Net < -0.2,
    });
    setCkPulse("ck-pulse-diesel", n.diesel?.p_kw, { warn: true });

    // 右上 R1
    $("#ck-st-green").textContent = `${fmtNum(st.green_ratio_pct, 1)}%`;
    $("#ck-st-temp").textContent = st.temperature_c != null ? `${fmtNum(st.temperature_c, 1)} °C` : "—";
    $("#ck-st-wind").textContent = st.wind_speed_ms != null ? `${fmtNum(st.wind_speed_ms, 1)} m/s` : "—";
    $("#ck-st-irr").textContent = st.irradiance_wm2 != null ? `${fmtNum(st.irradiance_wm2, 0)} W/m²` : "—";
    $("#ck-st-bus").textContent = `${fmtNum(st.dc_voltage_v, 0)}/${fmtNum(st.ac_voltage_v, 0)}V`;
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
    if (data.data_quality) {
      updateDataQualityPill(data.data_quality);
    }
    // 极地特色衍生指标与气象-能源耦合（风电耦合 / 电储温容 / 采暖增荷 / 柴油续航 / 气象状态）
    const drv = data.derived || {};
    const cpl = data.coupling || {};
    const effWx = data.weather?.effective || {};
    const wxBtn = $("#ck-weather-btn");
    if (wxBtn) {
      wxBtn.classList.toggle("is-manual", effWx.mode === "manual");
      wxBtn.title = effWx.mode === "manual" ? `${tr(effWx.preset_label || "工况推演")} · ${tr("工况输入")}` : tr("配置南极气象工况");
    }
    if ($("#ck-st-temp")) {
      const tipParts = [];
      if (effWx.wind_chill_c != null) tipParts.push(`${tr("风寒体感")} ${fmtNum(effWx.wind_chill_c, 1)} °C`);
      if (effWx.air_density_kgm3 != null) tipParts.push(`${tr("空气密度")} ${fmtNum(effWx.air_density_kgm3, 2)} kg/m³`);
      if (effWx.icing_level != null && Number(effWx.icing_level) > 0 && effWx.icing_label) tipParts.push(tr(effWx.icing_label));
      const tTip = tipParts.join(" · ") || tr("未接入气温传感器 · 点击补录");
      $("#ck-st-temp").classList.toggle("is-empty", effWx.temperature_c == null);
      $("#ck-st-temp").title = tTip;
      if ($("#ck-st-temp-btn")) $("#ck-st-temp-btn").title = tTip;
    }
    if ($("#ck-autonomy-h")) {
      $("#ck-autonomy-h").classList.toggle("is-empty", res.autonomy_hours == null);
      $("#ck-autonomy-h").title = res.autonomy_hours == null ? tr("模型库未配置电储容量或当前负荷为零") : "";
    }
    if ($("#ck-drv-wind")) {
      const wObj = cpl.wind || {};
      $("#ck-drv-wind").textContent = wObj.state === "超速切出"
        ? tr("切出")
        : (wObj.avail_pct != null ? `${fmtNum(wObj.avail_pct, 0)}%` : "--");
      $("#ck-drv-wind").classList.toggle("is-empty", wObj.avail_pct == null);
      $("#ck-drv-wind").style.color = wObj.level === "danger" ? "var(--danger)" : (wObj.level === "warn" ? "var(--warn)" : (wObj.avail_pct != null ? "var(--ok)" : ""));
      $("#ck-drv-wind").parentElement.title = wObj.avail_pct == null ? tr("未接入风速传感器 · 点击补录") : `${tr("风电耦合可用率")} (${tr(wObj.state || "")})`;
    }
    if ($("#ck-drv-bess")) {
      const bObj = cpl.bess || {};
      $("#ck-drv-bess").textContent = bObj.cap_retention_pct != null ? `${fmtNum(bObj.cap_retention_pct, 0)}%` : "--";
      $("#ck-drv-bess").classList.toggle("is-empty", bObj.cap_retention_pct == null);
      $("#ck-drv-bess").style.color = bObj.level === "danger" ? "var(--danger)" : (bObj.level === "warn" ? "var(--warn)" : (bObj.cap_retention_pct != null ? "var(--accent)" : ""));
      $("#ck-drv-bess").parentElement.title = bObj.cap_retention_pct == null ? tr("电储低温容量需气温参数 · 点击补录") : tr("电储能低温容量保持率");
    }
    if ($("#ck-drv-heat")) {
      const hRaw = cpl.thermal?.heating_delta_kw;
      $("#ck-drv-heat").classList.toggle("is-empty", hRaw == null);
      if (hRaw == null) {
        $("#ck-drv-heat").textContent = "--";
        $("#ck-drv-heat").style.color = "";
        $("#ck-drv-heat").parentElement.title = tr("极寒采暖增荷需气温参数 · 点击补录");
      } else {
        const hDelta = Number(hRaw);
        $("#ck-drv-heat").textContent = `+${fmtNum(hDelta, 0)}kW`;
        $("#ck-drv-heat").style.color = hDelta >= 35 ? "var(--danger)" : (hDelta >= 15 ? "var(--warn)" : "var(--text-primary)");
        $("#ck-drv-heat").parentElement.title = tr("极寒采暖负荷增量");
      }
    }
    if ($("#ck-drv-endurance")) {
      $("#ck-drv-endurance").textContent = drv.diesel_endurance_hours != null ? `${fmtNum(drv.diesel_endurance_hours, 1)}h` : "--";
      $("#ck-drv-endurance").classList.toggle("is-empty", drv.diesel_endurance_hours == null);
      $("#ck-drv-endurance").parentElement.title = drv.diesel_endurance_hours == null
        ? tr("模型库未配置柴发油箱或油耗参数")
        : `${tr("柴油储备续航")} (${fmtNum(drv.diesel_reserve_liters, 0)} L)`;
    }
    const wEl = $("#ck-drv-weather");
    if (wEl) {
      const wa = cpl.alert_level || drv.weather_alert || "normal";
      wEl.className = `ck-drv-badge${wa === "extreme" ? " is-extreme" : (wa === "elevated" ? " is-elevated" : "")}`;
      wEl.textContent = wa === "extreme" ? tr("极端气候保供") : (wa === "elevated" ? tr("大风寒潮关注") : tr("极地气象正常"));
      wEl.title = tr(cpl.strategy || "");
    }
    syncAllWeatherBadges(effWx, cpl);

    drawCockpitCharts();
  }

  async function loadPolarCockpit() {
    const data = await api(`/api/polar-cockpit?hours=${encodeURIComponent(state.cockpitHours)}`);
    renderPolarCockpit(data);
  }

  // 极地管控交互绑定（节点跳转、时间窗切换、Canvas 十字准星 rAF 节流）
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
  let ckHoverRaf = 0;
  function bindCkCanvasHover(canvasId, key) {
    const el = $("#" + canvasId);
    if (!el) return;
    el.addEventListener("mousemove", (e) => {
      const r = el.getBoundingClientRect();
      state[key] = e.clientX - r.left;
      if (!ckHoverRaf) {
        ckHoverRaf = requestAnimationFrame(() => {
          ckHoverRaf = 0;
          drawCockpitCharts();
        });
      }
    });
    el.addEventListener("mouseleave", () => {
      state[key] = null;
      drawCockpitCharts();
    });
  }
  bindCkCanvasHover("ck-power-canvas", "cockpitPowerHoverX");
  bindCkCanvasHover("ck-storage-canvas", "cockpitSchedHoverX");

  // ==================== 1.4 运行态势战情大屏（#view-dispatch） ====================
  const DS_GAUGE_ARC_LEN = 145;

  /**
   * 驱动运行态势大屏的 SVG 半圆弧形安全裕度仪表
   */
  function setDsGauge(arcId, valId, rawVal, min, max, digits, levelFn) {
    const arc = $("#" + arcId);
    const valEl = $("#" + valId);
    const num = rawVal === null || rawVal === undefined || Number.isNaN(Number(rawVal)) ? null : Number(rawVal);
    if (valEl) valEl.textContent = num === null ? "--" : fmtNum(num, digits);
    if (!arc) return "ok";
    const ratio = num === null ? 0 : Math.max(0.03, Math.min(1, (num - min) / Math.max(1e-6, max - min)));
    arc.style.strokeDashoffset = (DS_GAUGE_ARC_LEN * (1 - ratio)).toFixed(1);
    const lvl = num === null ? "" : (levelFn ? levelFn(num) : "ok");
    arc.classList.remove("ok", "warn", "danger");
    if (lvl) arc.classList.add(lvl);
    return lvl || "ok";
  }

  /**
   * 依据设备表名与分类归并至运行态势主设备矩阵的五大业务槽位
   */
  function classifyDispatchDevice(dev) {
    const cat = String(dev.category || "").toLowerCase();
    const tbl = String(dev.table_name || "").toLowerCase();
    if (cat === "wind" || cat === "pv" || cat === "diesel" || tbl.includes("generator") || tbl.includes("pv") || tbl.includes("wind")) {
      return "gen";
    }
    if (cat === "storage" || cat === "hydrogen" || tbl.includes("storage") || tbl.includes("hydro") || tbl.includes("electrolyzer") || tbl.includes("fuelcell")) {
      return "storage";
    }
    if (cat === "converter" || tbl.includes("dcac") || tbl.includes("dcdc") || tbl.includes("converter") || tbl.includes("transformer") || tbl.includes("pcs")) {
      return "converter";
    }
    if (cat === "breaker" || cat === "switch" || tbl.includes("breaker") || tbl.includes("switch")) {
      return "breaker";
    }
    return "other";
  }

  /**
   * 提取单台设备在态势磁贴上的主值、副值与运行健康状态
   */
  function summarizeDispatchTile(dev) {
    const metrics = dev.metrics || [];
    const byName = new Map();
    for (const m of metrics) {
      if (m && m.metric) byName.set(String(m.metric).toLowerCase(), m);
    }
    const alarmCnt = Number(dev.alarm_count || 0);
    const hasCommErr = metrics.some((m) => Number(m.quality) === 2 && !m.disabled);
    const hasLimitErr = metrics.some((m) => (Number(m.quality) === 3 || Number(m.quality) === 4) && !m.disabled);

    const pM = byName.get("p") || byName.get("p_gen") || byName.get("p_load") || byName.get("p_ac") || byName.get("p_dc");
    const socM = byName.get("soc");
    const uM = byName.get("u") || byName.get("uab") || byName.get("u_dc") || byName.get("u_ac");
    const posM = byName.get("position");
    const runM = byName.get("run") || byName.get("status");

    let mainText = "—";
    let subText = "";
    let isRunning = false;

    if (posM && posM.value !== null && posM.value !== undefined) {
      const closed = Number(posM.value) === 1;
      mainText = tr(closed ? "合闸" : "分闸");
      isRunning = closed;
      if (pM && pM.display_value != null) subText = `${fmtNum(pM.display_value, 1)} kW`;
      else if (uM && uM.display_value != null) subText = `${fmtNum(uM.display_value, 0)} V`;
    } else if (pM && pM.display_value != null) {
      const pv = Number(pM.display_value);
      mainText = `${fmtNum(pv, 1)} ${pM.unit || "kW"}`;
      isRunning = Math.abs(pv) > 0.2;
      if (socM && socM.display_value != null) subText = `SOC ${fmtNum(socM.display_value, 0)}%`;
      else if (uM && uM.display_value != null) subText = `${fmtNum(uM.display_value, 0)} V`;
    } else if (socM && socM.display_value != null) {
      mainText = `SOC ${fmtNum(socM.display_value, 1)}%`;
      isRunning = Number(socM.display_value) > 0;
    } else if (uM && uM.display_value != null) {
      mainText = `${fmtNum(uM.display_value, 1)} ${uM.unit || "V"}`;
      isRunning = Number(uM.display_value) > 10;
    } else if (runM && runM.value !== null && runM.value !== undefined) {
      isRunning = Number(runM.value) === 1;
      mainText = tr(isRunning ? "运行" : "停运");
    }

    if (runM && runM.value !== null && runM.value !== undefined) {
      isRunning = isRunning || Number(runM.value) === 1;
    }

    let stateCls = isRunning ? "" : "is-idle";
    if (hasLimitErr || alarmCnt > 0) stateCls = "is-fault";
    else if (hasCommErr) stateCls = "is-warn";

    if (alarmCnt > 0) {
      subText = `${tr("告警")} ${alarmCnt}`;
    } else if (!subText) {
      subText = `#${dev.idx ?? 0}`;
    }

    return {
      mainText,
      subText,
      stateCls,
      abnormal: stateCls === "is-fault" || stateCls === "is-warn",
      score: (stateCls === "is-fault" ? 300 : stateCls === "is-warn" ? 200 : isRunning ? 100 : 10),
    };
  }

  /**
   * 绘制左下角「实时源荷功率构成」横向对比条形图
   */
  function drawDispatchBalanceChart() {
    const canvas = $("#ds-balance-canvas");
    if (!canvas || !state.dispatchData?.cockpit) return;
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
    const dark = isDarkTheme();
    const n = state.dispatchData.cockpit.hub?.nodes || {};

    const esP = Number(n.storage?.p_kw || 0);
    const esDis = n.storage?.state === "discharge" ? Math.abs(esP) : 0;
    const esChg = n.storage?.state === "charge" ? Math.abs(esP) : 0;

    const rows = [
      { label: tr("风电"), val: Math.max(0, Number(n.wind?.p_kw || 0)), color: pal.series[0], group: "gen" },
      { label: tr("光伏"), val: Math.max(0, Number(n.pv?.p_kw || 0)), color: pal.series[1], group: "gen" },
      { label: tr("柴发"), val: Math.max(0, Number(n.diesel?.p_kw || 0)), color: pal.series[2], group: "gen" },
      { label: tr("电储放电"), val: esDis, color: pal.series[3], group: "gen" },
      { label: tr("氢电"), val: Math.max(0, Number(n.hydrogen?.gen_kw || 0)), color: "#2dd4bf", group: "gen" },
      { label: tr("交流负荷"), val: Math.max(0, Number(n.load?.ac_kw || 0)), color: pal.series[4], group: "load" },
      { label: tr("直流负荷"), val: Math.max(0, Number(n.load?.dc_kw || 0)), color: "#818cf8", group: "load" },
      { label: tr("制氢"), val: Math.max(0, Number(n.hydrogen?.prod_kw || 0)), color: "#34d399", group: "load" },
      { label: tr("电储充电"), val: esChg, color: "#fb923c", group: "load" },
    ];

    const maxV = Math.max(10, ...rows.map((r) => r.val));
    const padL = 62;
    const padR = 54;
    const padT = 8;
    const padB = 8;
    const sepGap = 8;
    const availH = Math.max(40, H - padT - padB - sepGap);
    const rowH = availH / rows.length;
    const barH = Math.max(5, Math.min(11, rowH * 0.56));
    const barW = Math.max(20, W - padL - padR);

    ctx.font = "10.5px Inter, 'Microsoft YaHei', sans-serif";
    let y = padT;
    rows.forEach((r, idx) => {
      if (idx === 5) {
        // 发电侧与负荷侧分隔虚线
        ctx.strokeStyle = pal.grid;
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(4, y + sepGap * 0.45);
        ctx.lineTo(W - 4, y + sepGap * 0.45);
        ctx.stroke();
        ctx.setLineDash([]);
        y += sepGap;
      }
      const cy = y + rowH * 0.5;
      // 左侧标签
      ctx.fillStyle = pal.axis;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(r.label.slice(0, 6), padL - 6, cy);

      // 轨道底色
      ctx.fillStyle = dark ? "rgba(148,163,184,0.12)" : "rgba(15,23,42,0.08)";
      const by = cy - barH * 0.5;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(padL, by, barW, barH, 3);
      else ctx.rect(padL, by, barW, barH);
      ctx.fill();

      // 柱体填充
      const fw = r.val > 0 ? Math.max(3, (r.val / maxV) * barW) : 0;
      if (fw > 0) {
        ctx.fillStyle = r.color;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(padL, by, fw, barH, 3);
        else ctx.rect(padL, by, fw, barH);
        ctx.fill();
      }

      // 右侧数值
      ctx.fillStyle = pal.text;
      ctx.font = "10.5px 'JetBrains Mono', Consolas, monospace";
      ctx.textAlign = "right";
      ctx.fillText(`${r.val.toFixed(1)} kW`, W - 4, cy);
      ctx.font = "10.5px Inter, 'Microsoft YaHei', sans-serif";
      y += rowH;
    });
  }

  /**
   * 渲染中上「全站主设备态势矩阵」磁贴并绑定点击跳转潮流图
   */
  function renderDispatchMatrix() {
    const grid = $("#ds-matrix-grid");
    const cntEl = $("#ds-matrix-cnt");
    if (!grid) return;
    const allDevs = state.dispatchData?.devices || [];
    const filter = state.dispatchFilter || "all";

    const entries = allDevs.map((dev) => ({
      dev,
      group: classifyDispatchDevice(dev),
      info: summarizeDispatchTile(dev),
    }));

    const filtered = entries
      .filter((e) => {
        if (filter === "all") return e.group !== "other" || e.info.abnormal;
        if (filter === "abnormal") return e.info.abnormal;
        return e.group === filter;
      })
      .sort((a, b) => b.info.score - a.info.score);

    const list = filtered.length || filter !== "all" ? filtered : entries;
    if (cntEl) cntEl.textContent = `${list.length} ${tr("台")}`;

    if (!list.length) {
      grid.innerHTML = `<div class="empty-hint" style="grid-column:1/-1;padding:18px 0;">${esc(tr(filter === "abnormal" ? "当前无异常设备" : "暂无设备"))}</div>`;
      return;
    }

    grid.innerHTML = list.slice(0, 48).map(({ dev, info }) => {
      const name = tr(dev.name || dev.dev_id || "");
      return (
        `<button type="button" class="ds-tile ${info.stateCls}" data-ds-dev="${esc(dev.dev_id)}" title="${esc(name)} (${esc(dev.table_name)} #${dev.idx})">` +
        `<div class="ds-tile-top"><span class="ds-tile-name">${esc(name)}</span><i class="ds-tile-dot"></i></div>` +
        `<div class="ds-tile-bot"><span class="ds-tile-val">${esc(info.mainText)}</span><span class="ds-tile-sub">${esc(info.subText)}</span></div>` +
        `</button>`
      );
    }).join("");

    grid.querySelectorAll("[data-ds-dev]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const devId = btn.dataset.dsDev;
        if (!devId) return;
        switchView("powerflow");
        await loadPowerflow();
        state.pfPinned = true;
        focusSvgDevice(devId);
      });
    });
  }

  /**
   * 绘制中下「全站源荷与绿电态势曲线」（左轴：总发电/总负荷 kW，右轴：绿电占比 %）
   */
  function drawDispatchTrendChart() {
    const canvas = $("#ds-trend-canvas");
    if (!canvas || !state.dispatchData?.cockpit) return;
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
    const dark = isDarkTheme();
    const curves = state.dispatchData.cockpit.power_curves || [];
    const byKey = new Map(curves.map((c) => [c.key, c.points || []]));

    // 汇总每个时间戳上的风电/光伏/柴发/储能放电与总负荷
    const bucket = new Map();
    const addPts = (key, field) => {
      for (const pt of (byKey.get(key) || [])) {
        const ts = Number(pt[0]);
        const val = Number(pt[1]);
        if (!Number.isFinite(ts) || !Number.isFinite(val)) continue;
        // 按 30 秒粒度对齐聚合
        const k = Math.round(ts / 30000) * 30000;
        let row = bucket.get(k);
        if (!row) {
          row = { ts: k, wind: 0, pv: 0, diesel: 0, storage: 0, load: 0 };
          bucket.set(k, row);
        }
        row[field] = val;
      }
    };
    addPts("wind", "wind");
    addPts("pv", "pv");
    addPts("diesel", "diesel");
    addPts("storage", "storage");
    addPts("load", "load");

    const rows = Array.from(bucket.values()).sort((a, b) => a.ts - b.ts);
    const padL = 44;
    const padR = 38;
    const padT = 14;
    const padB = 22;
    const plotW = Math.max(10, W - padL - padR);
    const plotH = Math.max(10, H - padT - padB);

    if (!rows.length) {
      ctx.fillStyle = pal.axis;
      ctx.font = "12px Inter, 'Microsoft YaHei', sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(tr("暂无采样曲线"), W / 2, H / 2);
      return;
    }

    const genPts = [];
    const loadPts = [];
    const greenPts = [];
    let minT = rows[0].ts;
    let maxT = rows[rows.length - 1].ts;
    let maxKw = 10;

    for (const r of rows) {
      const greenKw = Math.max(0, r.wind) + Math.max(0, r.pv);
      const genKw = greenKw + Math.max(0, r.diesel) + Math.max(0, r.storage);
      const loadKw = Math.max(0, r.load);
      const greenPct = genKw > 0.5 ? Math.max(0, Math.min(100, (greenKw / genKw) * 100)) : 0;
      genPts.push([r.ts, genKw]);
      loadPts.push([r.ts, loadKw]);
      greenPts.push([r.ts, greenPct]);
      if (genKw > maxKw) maxKw = genKw;
      if (loadKw > maxKw) maxKw = loadKw;
    }
    if (maxT <= minT) {
      minT -= 60000;
      maxT += 60000;
    }
    maxKw = maxKw * 1.15;

    const xOf = (t) => padL + ((t - minT) / (maxT - minT)) * plotW;
    const yKw = (v) => padT + ((maxKw - v) / maxKw) * plotH;
    const yPct = (p) => padT + ((100 - p) / 100) * plotH;

    // 水平网格 + 左右双 Y 轴刻度
    const ticks = 4;
    ctx.font = "10.5px 'JetBrains Mono', Consolas, monospace";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= ticks; i += 1) {
      const frac = i / ticks;
      const y = padT + frac * plotH;
      const kwVal = maxKw * (1 - frac);
      const pctVal = 100 * (1 - frac);
      ctx.strokeStyle = pal.grid;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = pal.axis;
      ctx.textAlign = "right";
      ctx.fillText(kwVal >= 100 ? kwVal.toFixed(0) : kwVal.toFixed(1), padL - 5, y);
      ctx.textAlign = "left";
      ctx.fillStyle = "#34d399";
      ctx.fillText(`${Math.round(pctVal)}%`, W - padR + 5, y);
    }

    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillStyle = pal.axis;
    ctx.fillText("kW", 6, 2);

    // X 轴时间刻度
    const xTicks = 4;
    ctx.textAlign = "center";
    ctx.fillStyle = pal.axis;
    for (let i = 0; i <= xTicks; i += 1) {
      const t = minT + (i / xTicks) * (maxT - minT);
      const x = xOf(t);
      const d = new Date(t);
      const lbl = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
      ctx.fillText(lbl, Math.max(padL + 14, Math.min(W - padR - 14, x)), H - padB + 5);
    }

    const drawSeries = (pts, yMap, color, { fill = false, dashed = false } = {}) => {
      if (!pts.length) return;
      const coords = pts.map((p) => [xOf(p[0]), yMap(p[1])]);
      if (fill && coords.length >= 2) {
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(coords[0][0], coords[0][1]);
        for (let i = 1; i < coords.length; i += 1) ctx.lineTo(coords[i][0], coords[i][1]);
        ctx.lineTo(coords[coords.length - 1][0], padT + plotH);
        ctx.lineTo(coords[0][0], padT + plotH);
        ctx.closePath();
        const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
        grad.addColorStop(0, color + (dark ? "40" : "24"));
        grad.addColorStop(1, color + "04");
        ctx.fillStyle = grad;
        ctx.fill();
        ctx.restore();
      }
      ctx.save();
      ctx.beginPath();
      if (coords.length === 1) {
        ctx.arc(coords[0][0], coords[0][1], 3, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
      } else {
        ctx.moveTo(coords[0][0], coords[0][1]);
        for (let i = 1; i < coords.length; i += 1) ctx.lineTo(coords[i][0], coords[i][1]);
        ctx.strokeStyle = color;
        ctx.lineWidth = dashed ? 1.8 : 2;
        if (dashed) ctx.setLineDash([5, 3]);
        if (dark && !dashed) {
          ctx.shadowColor = color;
          ctx.shadowBlur = 7;
        }
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.restore();
    };

    drawSeries(genPts, yKw, "#38bdf8", { fill: true });
    drawSeries(loadPts, yKw, "#a78bfa", { fill: true });
    drawSeries(greenPts, yPct, "#34d399", { fill: false, dashed: true });

    // 悬停十字准星与 Tooltip
    const hoverX = state.dispatchTrendHoverX;
    if (hoverX !== null && hoverX >= padL && hoverX <= W - padR) {
      const targetT = minT + ((hoverX - padL) / plotW) * (maxT - minT);
      let bestIdx = 0;
      let bestDist = Math.abs(genPts[0][0] - targetT);
      for (let i = 1; i < genPts.length; i += 1) {
        const d = Math.abs(genPts[i][0] - targetT);
        if (d < bestDist) {
          bestDist = d;
          bestIdx = i;
        }
      }
      const snapT = genPts[bestIdx][0];
      const sx = xOf(snapT);
      ctx.strokeStyle = pal.accent;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(sx, padT);
      ctx.lineTo(sx, padT + plotH);
      ctx.stroke();
      ctx.setLineDash([]);

      const tipRows = [
        { label: tr("总发电"), val: `${genPts[bestIdx][1].toFixed(1)} kW`, color: "#38bdf8", y: yKw(genPts[bestIdx][1]) },
        { label: tr("总负荷"), val: `${loadPts[bestIdx][1].toFixed(1)} kW`, color: "#a78bfa", y: yKw(loadPts[bestIdx][1]) },
        { label: tr("绿电占比"), val: `${greenPts[bestIdx][1].toFixed(1)}%`, color: "#34d399", y: yPct(greenPts[bestIdx][1]) },
      ];
      tipRows.forEach((trw) => {
        ctx.fillStyle = trw.color;
        ctx.beginPath();
        ctx.arc(sx, trw.y, 3.2, 0, Math.PI * 2);
        ctx.fill();
      });

      const d = new Date(snapT);
      const timeStr = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
      const boxW = 154;
      const boxH = 22 + tipRows.length * 15;
      const bx = sx + 10 + boxW > W - padR ? Math.max(padL + 2, sx - boxW - 10) : sx + 10;
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
      tipRows.forEach((r, idx) => {
        const ry = by + 20 + idx * 15;
        ctx.fillStyle = r.color;
        ctx.beginPath();
        ctx.arc(bx + 11, ry + 6, 3, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = pal.text;
        ctx.textAlign = "left";
        ctx.fillText(r.label.slice(0, 8), bx + 18, ry);
        ctx.textAlign = "right";
        ctx.fillText(r.val, bx + boxW - 8, ry);
      });
    }
  }

  function drawDispatchCharts() {
    if (!state.dispatchData) return;
    drawDispatchBalanceChart();
    drawDispatchTrendChart();
  }

  /**
   * 渲染右上「实时高优告警快报」
   */
  function renderDispatchAlarms(alarmRes) {
    const listEl = $("#ds-alarm-list");
    if (!listEl) return;
    const rawItems = (alarmRes?.items || []).filter((a) => !a.acked && !a.maintenance);
    const prio = (t) => (t === "control_failed" ? 0 : (t === "over_max" || t === "under_min") ? 1 : 2);
    const sorted = rawItems.slice().sort((a, b) => prio(a.type) - prio(b.type) || Number(b.time_ms || 0) - Number(a.time_ms || 0));

    if (!sorted.length) {
      listEl.innerHTML = `<div class="empty-hint" style="padding:20px 0;">${esc(tr("当前无未确认告警"))} · ${esc(tr("系统运行正常"))}</div>`;
      return;
    }

    const topItems = sorted.slice(0, 12);
    listEl.innerHTML = topItems.map((a, idx) => {
      const warnCls = a.type === "comm_error" ? " is-warn" : "";
      const valStr = a.value == null || a.value === ""
        ? ""
        : ` · ${typeof a.value === "number" ? fmtNum(a.value, 1) : esc(a.value)}${a.unit ? " " + esc(a.unit) : ""}`;
      return (
        `<div class="ds-alarm-card${warnCls}">` +
        `<div class="ds-alarm-top"><strong>${esc(tr(a.device_name || a.dev_id))}</strong>${alarmTypeBadge(a.type)}</div>` +
        `<div class="ds-alarm-bot">` +
        `<span>${esc(tr(a.attr_label || a.metric || ""))}${valStr}</span>` +
        `<span class="ds-alarm-ops">` +
        `<button type="button" class="ui-ico-btn xs" data-ds-aloc="${idx}" title="${esc(tr("定位"))}" aria-label="${esc(tr("定位"))}"><svg class="ui-ico sm" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg></button>` +
        `<button type="button" class="ui-ico-btn xs" data-ds-aack="${idx}" title="${esc(tr("确认"))}" aria-label="${esc(tr("确认"))}"><svg class="ui-ico sm c-ok" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 13 4 4L19 7"/></svg></button>` +
        `</span>` +
        `</div>` +
        `</div>`
      );
    }).join("");

    listEl.querySelectorAll("[data-ds-aloc]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const a = topItems[Number(btn.dataset.dsAloc)];
        if (!a?.dev_id) return;
        switchView("powerflow");
        await loadPowerflow();
        state.pfPinned = true;
        focusSvgDevice(a.dev_id);
      });
    });
    listEl.querySelectorAll("[data-ds-aack]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const a = topItems[Number(btn.dataset.dsAack)];
        if (!a?.key) return;
        const noteRes = await openAlarmNoteDialog({
          title: "确认告警",
          meta: `${a.device_name} · ${a.attr_label}`,
          reasonPlaceholder: "处置备注（可选）",
        });
        if (!noteRes) return;
        await apiPost("/api/alarms/ack", { keys: [a.key], operator: noteRes.operator, note: noteRes.text });
        window.ScadaExtras?.showToast("告警已确认", "ok");
        await loadDispatchView();
      });
    });
  }

  /**
   * 渲染右下「通信链路健康雷达」
   */
  function renderDispatchComm(topoRes) {
    const listEl = $("#ds-comm-list");
    if (!listEl) return;
    const links = topoRes?.links || [];
    if (!links.length) {
      listEl.innerHTML = `<div class="empty-hint" style="padding:18px 0;">${esc(tr("暂无链路"))}</div>`;
      return;
    }
    listEl.innerHTML = links.map((lk) => {
      const online = lk.connection_state === "connected";
      const pct = Math.max(0, Math.min(100, Number(lk.health_pct || 0)));
      const cls = !online ? "is-offline" : (pct < 90 ? "is-warn" : "");
      const faultCnt = (lk.fault_devices || []).length;
      const barColor = !online ? "var(--danger, #f87171)" : (pct < 90 ? "var(--warn, #fbbf24)" : "var(--ok, #34d399)");
      return (
        `<div class="ds-comm-card ${cls}">` +
        `<div class="ds-comm-top"><strong>RTU ${lk.rtu_id} · ${esc(tr(lk.rtu_name || ""))}</strong><span style="font-family:var(--font-mono,monospace);font-weight:700;color:${barColor}">${pct.toFixed(0)}%</span></div>` +
        `<div class="ds-comm-meta"><span>${esc(lk.channel_type || "--")} · ${lk.device_count ?? 0} ${esc(tr("台"))}</span><span>${faultCnt ? `${esc(tr("异常"))} ${faultCnt}` : esc(tr(online ? "在线" : "离线"))}</span></div>` +
        `<div class="comm-topo-bar"><i style="width:${pct}%;background:${barColor}"></i></div>` +
        `</div>`
      );
    }).join("");
  }

  /**
   * 统一渲染运行态势大屏（顶部战情条、弧形仪表、源荷平衡、设备矩阵、双轴曲线、告警与通信）
   */
  function renderDispatchView(cockpit, devRes, alarmRes, topoRes) {
    state.dispatchData = {
      cockpit,
      devices: devRes?.items || [],
      alarms: alarmRes,
      topo: topoRes,
    };

    const hub = cockpit?.hub || {};
    const n = hub.nodes || {};
    const st = cockpit?.polar_stats || {};
    const res = cockpit?.resilience || {};
    const health = cockpit?.health || {};

    // 从全站设备量测中提取实时交流系统频率均值
    const freqVals = [];
    for (const dev of state.dispatchData.devices) {
      for (const m of (dev.metrics || [])) {
        if (!m || m.disabled) continue;
        const mn = String(m.metric || "").toLowerCase();
        const unit = String(m.unit || "").toLowerCase();
        if ((mn === "f" || mn === "freq" || unit === "hz") && m.display_value != null) {
          const v = Number(m.display_value);
          if (Number.isFinite(v) && v >= 45 && v <= 55) freqVals.push(v);
        }
      }
    }
    const freqAvg = freqVals.length ? freqVals.reduce((a, b) => a + b, 0) / freqVals.length : null;

    const acV = st.ac_voltage_v;
    const dcV = st.dc_voltage_v;
    const genKw = Number(hub.total_gen_kw ?? 0);
    const loadKw = Number(hub.total_load_kw ?? 0);
    const balKw = genKw - loadKw;
    const greenPct = hub.green_ratio_pct ?? st.green_ratio_pct;
    const unacked = Number(alarmRes?.summary?.unacked ?? health.alarm_unacked ?? 0);

    // 顶部单行战情指标条
    if ($("#ds-tk-freq")) $("#ds-tk-freq").textContent = freqAvg != null ? `${fmtNum(freqAvg, 2)} Hz` : "-- Hz";
    if ($("#ds-tk-acv")) $("#ds-tk-acv").textContent = `${fmtNum(acV, 1)} V`;
    if ($("#ds-tk-dcv")) $("#ds-tk-dcv").textContent = `${fmtNum(dcV, 1)} V`;
    if ($("#ds-tk-gen")) $("#ds-tk-gen").textContent = `${fmtNum(genKw, 1)} kW`;
    if ($("#ds-tk-load")) $("#ds-tk-load").textContent = `${fmtNum(loadKw, 1)} kW`;
    if ($("#ds-tk-bal")) {
      $("#ds-tk-bal").textContent = `${balKw >= 0 ? "+" : ""}${fmtNum(balKw, 1)} kW`;
      $("#ds-tk-bal").style.color = Math.abs(balKw) > Math.max(30, loadKw * 0.25) ? "var(--warn)" : "var(--ok)";
    }
    if ($("#ds-tk-green")) $("#ds-tk-green").textContent = `${fmtNum(greenPct, 1)}%`;
    if ($("#ds-tk-rtu")) $("#ds-tk-rtu").textContent = `${health.rtu_online ?? st.rtu_online ?? 0} / ${health.rtu_total ?? st.rtu_total ?? 0}`;
    if ($("#ds-tk-alarm")) $("#ds-tk-alarm").textContent = String(unacked);
    $("#ds-tk-alarm-btn")?.classList.toggle("is-ok", unacked <= 0);

    // 顶部气象工况按钮与左栏气象-能源耦合条
    const effWx = cockpit?.weather?.effective || {};
    const cpl = cockpit?.coupling || {};
    const wxTickBtn = $("#ds-tk-weather-btn");
    if (wxTickBtn) {
      const lvl = cpl.alert_level || "normal";
      wxTickBtn.classList.toggle("is-elevated", lvl === "elevated");
      wxTickBtn.classList.toggle("is-extreme", lvl === "extreme");
      wxTickBtn.title = tr(cpl.strategy || "点击配置南极气象工况");
    }
    if ($("#ds-tk-wlabel")) {
      $("#ds-tk-wlabel").textContent = tr(effWx.preset_label || (effWx.mode === "manual" ? "工况推演" : "现场实测"));
    }
    if ($("#ds-tk-wval")) {
      const tV = effWx.temperature_c ?? st.temperature_c;
      const wV = effWx.wind_speed_ms ?? st.wind_speed_ms;
      $("#ds-tk-wval").textContent = `${tV != null ? fmtNum(tV, 0) : "--"}°C / ${wV != null ? fmtNum(wV, 1) : "--"}m/s`;
    }
    if ($("#ds-wc-wind")) {
      const wObj = cpl.wind || {};
      $("#ds-wc-wind").textContent = wObj.state === "超速切出"
        ? tr("切出")
        : (wObj.avail_pct != null ? `${fmtNum(wObj.avail_pct, 0)}%` : "--");
      $("#ds-wc-wind").style.color = wObj.level === "danger" ? "var(--danger)" : (wObj.level === "warn" ? "var(--warn)" : (wObj.avail_pct != null ? "var(--ok)" : ""));
    }
    if ($("#ds-wc-pv")) {
      const pObj = cpl.pv || {};
      $("#ds-wc-pv").textContent = pObj.eff_pct != null ? `${fmtNum(pObj.eff_pct, 0)}%` : "--";
      $("#ds-wc-pv").style.color = pObj.level === "warn" ? "var(--warn)" : (pObj.eff_pct != null ? "var(--accent)" : "");
    }
    if ($("#ds-wc-bess")) {
      const bObj = cpl.bess || {};
      $("#ds-wc-bess").textContent = bObj.cap_retention_pct != null ? `${fmtNum(bObj.cap_retention_pct, 0)}%` : "--";
      $("#ds-wc-bess").style.color = bObj.level === "danger" ? "var(--danger)" : (bObj.level === "warn" ? "var(--warn)" : (bObj.cap_retention_pct != null ? "var(--ok)" : ""));
    }
    if ($("#ds-wc-heat")) {
      const hRaw = cpl.thermal?.heating_delta_kw;
      if (hRaw == null) {
        $("#ds-wc-heat").textContent = "--";
        $("#ds-wc-heat").style.color = "";
      } else {
        const hDelta = Number(hRaw);
        $("#ds-wc-heat").textContent = `+${fmtNum(hDelta, 0)}kW`;
        $("#ds-wc-heat").style.color = hDelta >= 35 ? "var(--danger)" : (hDelta >= 15 ? "var(--warn)" : "var(--text-primary)");
      }
    }
    syncAllWeatherBadges(effWx, cpl);

    // 左上：4 枚电气安全裕度弧形仪表
    const lvls = [
      setDsGauge("ds-g-acv", "ds-gv-acv", acV, 320, 440, 1, (v) => (Math.abs(v - 380) > 28 ? "danger" : Math.abs(v - 380) > 15 ? "warn" : "ok")),
      setDsGauge("ds-g-dcv", "ds-gv-dcv", dcV, 650, 850, 1, (v) => (Math.abs(v - 750) > 45 ? "danger" : Math.abs(v - 750) > 25 ? "warn" : "ok")),
      setDsGauge("ds-g-soc", "ds-gv-soc", n.storage?.soc_pct, 0, 100, 1, (v) => (v < 15 ? "danger" : v < 25 ? "warn" : "ok")),
      setDsGauge("ds-g-h2", "ds-gv-h2", res.hydro_soc_pct ?? n.hydrogen?.soc_pct, 0, 100, 1, (v) => (v < 15 ? "danger" : v < 25 ? "warn" : "ok")),
    ];
    const secEl = $("#ds-sec-status");
    if (secEl) {
      const hasDanger = lvls.includes("danger");
      const hasWarn = lvls.includes("warn");
      secEl.textContent = tr(hasDanger ? "越限预警" : hasWarn ? "需关注" : "运行稳定");
      secEl.style.color = hasDanger ? "var(--danger)" : hasWarn ? "var(--warn)" : "var(--ok)";
    }

    if ($("#ds-bal-meta")) {
      $("#ds-bal-meta").textContent = `${fmtNum(genKw, 1)} / ${fmtNum(loadKw, 1)} kW`;
    }

    // 侧栏健康与告警徽标同步
    if (cockpit?.health) {
      if ($("#health-rtus")) $("#health-rtus").textContent = `${health.rtu_online} / ${health.rtu_total}`;
      if ($("#health-points")) $("#health-points").textContent = `${health.valid_measurements} / ${health.bound_measurements}`;
      if ($("#health-gen")) $("#health-gen").textContent = `${fmtNum(genKw, 1)} kW`;
      if ($("#health-load")) $("#health-load").textContent = `${fmtNum(loadKw, 1)} kW`;
      updateAlarmBadge(unacked);
      updateCollectorBadge(health.collector_running);
    }
    if (cockpit?.data_quality) {
      updateDataQualityPill(cockpit.data_quality);
    }

    renderDispatchMatrix();
    renderDispatchAlarms(alarmRes);
    renderDispatchComm(topoRes);
    drawDispatchCharts();
  }

  async function loadDispatchView() {
    const [cockpit, devRes, alarmRes, topoRes] = await Promise.all([
      api(`/api/polar-cockpit?hours=${encodeURIComponent(state.dispatchHours)}`),
      api("/api/devices"),
      api("/api/alarms?acked=0"),
      api("/api/comm/topology"),
    ]);
    renderDispatchView(cockpit, devRes, alarmRes, topoRes);
  }

  // 运行态势交互绑定（设备分类快滤、时间窗切换、快捷跳转、趋势图十字准星）
  $$("#ds-matrix-tabs [data-ds-cat]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.dispatchFilter = btn.dataset.dsCat || "all";
      $$("#ds-matrix-tabs [data-ds-cat]").forEach((b) => b.classList.toggle("active", b === btn));
      renderDispatchMatrix();
    });
  });
  $$("#ds-hours-seg [data-ds-hours]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.dispatchHours = Number(btn.dataset.dsHours || 6);
      $$("#ds-hours-seg [data-ds-hours]").forEach((b) => b.classList.toggle("active", b === btn));
      loadDispatchView().catch((err) => setConnection(false, err.message));
    });
  });
  $("#ds-tk-alarm-btn")?.addEventListener("click", () => switchView("alarms"));
  $("#ds-jump-alarms")?.addEventListener("click", () => switchView("alarms"));
  $("#ds-jump-comm")?.addEventListener("click", () => switchView("comm"));

  let dsHoverRaf = 0;
  const dsTrendCanvas = $("#ds-trend-canvas");
  if (dsTrendCanvas) {
    dsTrendCanvas.addEventListener("mousemove", (e) => {
      const r = dsTrendCanvas.getBoundingClientRect();
      state.dispatchTrendHoverX = e.clientX - r.left;
      if (!dsHoverRaf) {
        dsHoverRaf = requestAnimationFrame(() => {
          dsHoverRaf = 0;
          drawDispatchTrendChart();
        });
      }
    });
    dsTrendCanvas.addEventListener("mouseleave", () => {
      state.dispatchTrendHoverX = null;
      drawDispatchTrendChart();
    });
  }

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
      } else if (viewAtStart === "dispatch") {
        tasks.push(loadDispatchView());
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
      hidePfPopovers?.(true);
      const drawer = $("#control-audit-drawer");
      if (drawer && !drawer.hidden) drawer.hidden = true;
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
    const keyViewMap = { "1": "home", "2": "cockpit", "3": "dispatch", "4": "energyflow", "5": "powerflow", "6": "devices", "7": "alarms", "8": "comm", "9": "logs" };
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
    window.ScadaExtras?.savePrefs({ refreshMs: state.refreshMs });
    resetTimer();
  });
  $("#manual-refresh")?.addEventListener("click", refreshCurrentView);

  // ==================== 告警备注弹窗、控调审计抽屉、小地图、命令面板与 SSE ====================
  function openAlarmNoteDialog({ title, meta, reasonPlaceholder }) {
    const dialog = $("#alarm-note-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve({ operator: "", text: "" });
    }
    $("#alarm-note-title").textContent = tr(title || "确认告警");
    $("#alarm-note-meta").textContent = meta || "";
    const opInput = $("#alarm-note-operator");
    const txtInput = $("#alarm-note-text");
    opInput.value = localStorage.getItem("scada_last_operator") || "";
    txtInput.value = "";
    txtInput.placeholder = tr(reasonPlaceholder || "处置备注（可选）");

    return new Promise((resolve) => {
      let settled = false;
      const done = (res) => {
        if (settled) return;
        settled = true;
        if (dialog.open) dialog.close();
        resolve(res);
      };
      $("#alarm-note-close").onclick = () => done(null);
      $("#alarm-note-cancel").onclick = () => done(null);
      dialog.oncancel = (ev) => { ev.preventDefault(); done(null); };
      $("#alarm-note-form").onsubmit = (ev) => {
        ev.preventDefault();
        const op = opInput.value.trim();
        if (op) {
          try { localStorage.setItem("scada_last_operator", op); } catch (_) {}
        }
        done({ operator: op, text: txtInput.value.trim() });
      };
      dialog.showModal();
      txtInput.focus();
    });
  }

  async function loadControlAuditDrawer() {
    const body = $("#audit-drawer-body");
    if (!body) return;
    body.innerHTML = `<div class="empty-hint">${tr("正在读取…")}</div>`;
    try {
      const res = await api("/api/controls/audit?limit=50");
      const items = res.items || [];
      if (!items.length) {
        body.innerHTML = `<div class="empty-hint">${tr("暂无控调审计记录")}</div>`;
        return;
      }
      body.innerHTML = items.map((it) => {
        const stBadge = it.status === "pending"
          ? '<span class="badge ok">已提交</span>'
          : '<span class="badge warn">待转发</span>';
        return (
          `<div class="audit-row">` +
          `<div class="audit-row-top"><strong>${esc(it.device_name)} · ${esc(it.control_label)}</strong>${stBadge}</div>` +
          `<div class="audit-row-meta"><span>${esc(it.kind)} = <strong>${esc(it.target_value)}</strong></span><span>${esc(fmtTime(it.ts_ms))}</span></div>` +
          `<div class="audit-row-meta"><span>${esc(it.detail || "")}</span><span>${esc(it.operator || "")}</span></div>` +
          `</div>`
        );
      }).join("");
    } catch (err) {
      body.innerHTML = `<div class="empty-hint">${esc(err.message)}</div>`;
    }
  }

  // 偏好恢复与控件绑定
  if ($("#refresh-interval")) $("#refresh-interval").value = String(state.refreshMs);
  if ($("#pf-show-arrows")) $("#pf-show-arrows").checked = state.pfShowArrows;
  if ($("#pf-canvas-mode")) $("#pf-canvas-mode").value = state.pfCanvasMode;
  if ($("#pf-fault-only")) $("#pf-fault-only").checked = state.pfFaultOnly;
  if ($("#trend-axis-mode")) $("#trend-axis-mode").value = state.trendAxisMode;
  if ($("#alarm-hide-maint")) $("#alarm-hide-maint").checked = state.hideMaintAlarms;
  if ($("#alarm-audio-toggle")) $("#alarm-audio-toggle").checked = state.alarmAudio;
  applyControlLockUi();
  applyPfCanvasModes();

  $("#control-lock-btn")?.addEventListener("click", () => {
    state.controlUnlocked = !state.controlUnlocked;
    window.ScadaExtras?.savePrefs({ controlUnlocked: state.controlUnlocked });
    applyControlLockUi();
    window.ScadaExtras?.showToast(state.controlUnlocked ? "值班控调" : "只读监盘", state.controlUnlocked ? "ok" : "warn");
  });
  $("#control-audit-btn")?.addEventListener("click", () => {
    const drawer = $("#control-audit-drawer");
    if (!drawer) return;
    drawer.hidden = !drawer.hidden;
    if (!drawer.hidden) loadControlAuditDrawer();
  });
  $("#audit-close-btn")?.addEventListener("click", () => {
    const drawer = $("#control-audit-drawer");
    if (drawer) drawer.hidden = true;
  });
  $("#audit-refresh-btn")?.addEventListener("click", loadControlAuditDrawer);

  $("#pf-canvas-mode")?.addEventListener("change", (e) => {
    state.pfCanvasMode = e.target.value || "native";
    window.ScadaExtras?.savePrefs({ pfCanvasMode: state.pfCanvasMode });
    applyPfCanvasModes();
  });
  $("#pf-fault-only")?.addEventListener("change", (e) => {
    state.pfFaultOnly = Boolean(e.target.checked);
    window.ScadaExtras?.savePrefs({ pfFaultOnly: state.pfFaultOnly });
    applyPfCanvasModes();
  });
  $("#alarm-hide-maint")?.addEventListener("change", (e) => {
    state.hideMaintAlarms = Boolean(e.target.checked);
    window.ScadaExtras?.savePrefs({ hideMaintAlarms: state.hideMaintAlarms });
    loadAlarms().catch(() => {});
  });
  $("#alarm-audio-toggle")?.addEventListener("change", (e) => {
    state.alarmAudio = Boolean(e.target.checked);
    window.ScadaExtras?.savePrefs({ alarmAudio: state.alarmAudio });
    if (state.alarmAudio) window.ScadaExtras?.playAlarmBeep("normal");
  });

  // 初始化潮流图缩略小地图
  if (window.ScadaExtras && $("#pf-minimap")) {
    pfMinimap = window.ScadaExtras.createMinimap({
      hostEl: $("#pf-minimap"),
      getSvgMeta: () => ({ w: state.svgWidth, h: state.svgHeight }),
      getViewport: () => {
        const wrap = $("#pf-canvas-wrap");
        if (!wrap || !state.pfScale) return null;
        const r = wrap.getBoundingClientRect();
        return {
          x: -state.pfX / state.pfScale,
          y: -state.pfY / state.pfScale,
          w: r.width / state.pfScale,
          h: r.height / state.pfScale,
        };
      },
      onPanTo: (cx, cy) => {
        const wrap = $("#pf-canvas-wrap");
        if (!wrap) return;
        const r = wrap.getBoundingClientRect();
        state.pfX = r.width / 2 - cx * state.pfScale;
        state.pfY = r.height / 2 - cy * state.pfScale;
        applySvgTransform();
      },
    });
  }

  // 全局命令面板 (Ctrl+K)
  const palette = window.ScadaExtras?.createCommandPalette(() => {
    const items = [
      { group: "视图", title: "系统首页", sub: "1", action: () => switchView("home") },
      { group: "视图", title: "极地管控", sub: "2", action: () => switchView("cockpit") },
      { group: "视图", title: "运行态势", sub: "3", action: () => switchView("dispatch") },
      { group: "视图", title: "全景流向", sub: "4", action: () => switchView("energyflow") },
      { group: "视图", title: "潮流图", sub: "5", action: () => switchView("powerflow") },
      { group: "视图", title: "设备图", sub: "6", action: () => switchView("devices") },
      { group: "视图", title: "告警中心", sub: "7", action: () => switchView("alarms") },
      { group: "视图", title: "通信图", sub: "8", action: () => switchView("comm") },
      { group: "视图", title: "运行日志", sub: "9", action: () => switchView("logs") },
      {
        group: "操作",
        title: state.controlUnlocked ? "只读监盘" : "值班控调",
        sub: "LOCK",
        action: () => $("#control-lock-btn")?.click(),
      },
      { group: "操作", title: "操作审计", sub: "AUDIT", action: () => $("#control-audit-btn")?.click() },
      { group: "操作", title: "南极气象工况输入", sub: "WX", action: () => openWeatherDialog() },
      { group: "操作", title: "打印报表", sub: "REPORT", action: () => window.open("/api/report/daily?format=html", "_blank", "noopener") },
      { group: "操作", title: "导出 CSV", sub: "CSV", action: () => { location.href = "/api/report/daily?format=csv"; } },
    ];
    const devs = Object.values(state.pfData?.devices || {}).length
      ? Object.values(state.pfData.devices)
      : (state.devItems || []);
    devs.slice(0, 120).forEach((d) => {
      items.push({
        group: "设备",
        title: d.name || d.dev_id,
        sub: `${d.table_name} #${d.idx}`,
        action: async () => {
          switchView("powerflow");
          await loadPowerflow();
          state.pfPinned = true;
          focusSvgDevice(d.dev_id);
        },
      });
    });
    return items;
  });
  $("#cmd-palette-btn")?.addEventListener("click", () => palette?.open());

  // 启动 SSE 实时事件流（有告警/指令/采样更新时即时触发轻量刷新，断线自动退避并由轮询兜底）
  let sseThrottleTimer = null;
  const sseClient = window.ScadaExtras
    ? new window.ScadaExtras.SseClient("/api/events", (ev, payload) => {
        if (document.hidden) return;
        if (ev === "control") {
          window.ScadaExtras.showToast(
            `${tr(payload.device_name || "")} · ${tr(payload.label || payload.control || "")} = ${payload.value ?? ""}`,
            "info"
          );
          if (!$("#control-audit-drawer")?.hidden) loadControlAuditDrawer();
        }
        if (ev === "alarm" || ev === "maintenance" || ev === "control" || ev === "weather") {
          if (!sseThrottleTimer) {
            sseThrottleTimer = setTimeout(() => {
              sseThrottleTimer = null;
              refreshCurrentView();
            }, 400);
          }
        }
      })
    : null;
  sseClient?.start();

  // 监听语言切换，实时重绘 Canvas 图表、顶栏动态状态与极地管控/运行态势面板
  document.addEventListener("scada:lang-change", () => {
    applyControlLockUi();
    syncPinButtonUi();
    window.ScadaCharts?.clearCache();
    drawTrendChart();
    if (state.view === "cockpit") drawCockpitCharts();
    if (state.view === "dispatch") {
      renderDispatchMatrix();
      if (state.dispatchData?.alarms) renderDispatchAlarms(state.dispatchData.alarms);
      if (state.dispatchData?.topo) renderDispatchComm(state.dispatchData.topo);
      drawDispatchCharts();
    }
    syncAllWeatherBadges();
  });

  // 监听主题切换与窗口缩放，实时同步 SVG 和日曲线配色与尺寸
  new MutationObserver(() => {
    applySvgThemeColors();
    if (state.pfData?.flows) updatePowerflowArrowsData(state.pfData.flows);
    renderTrendChips();
    drawTrendChart();
    if (state.view === "cockpit") drawCockpitCharts();
    if (state.view === "dispatch") drawDispatchCharts();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPowerflowAnimation();
    else if (state.view === "powerflow" && state.pfShowArrows) startPowerflowAnimation();
  });

  window.addEventListener("resize", () => {
    if (state.view === "home") updateHeroFlowLines(lastRealtimeEnergy);
    if (state.view === "cockpit") drawCockpitCharts();
    if (state.view === "dispatch") drawDispatchCharts();
    if (state.view === "devices") drawTrendChart();
  });

  if (typeof ResizeObserver === "function" && $("#hero-stage")) {
    new ResizeObserver(() => {
      if (state.view === "home") updateHeroFlowLines(lastRealtimeEnergy);
    }).observe($("#hero-stage"));
  }

  // ==================== 南极气象与环境工况输入弹窗与实时耦合预览 ====================
  let lastEffectiveWeather = null;
  let lastWeatherCoupling = null;

  /**
   * 同步全站所有主画面（潮流图/设备图/告警/通信/日志工具栏与侧栏健康速览）的气象胶囊
   */
  function syncAllWeatherBadges(effWx, cpl) {
    if (effWx) lastEffectiveWeather = effWx;
    if (cpl) lastWeatherCoupling = cpl;
    const wx = effWx || lastEffectiveWeather;
    const cp = cpl || lastWeatherCoupling;
    if (!wx) return;
    const labelTxt = tr(wx.preset_label || (wx.mode === "manual" ? "工况推演" : "现场实测"));
    const tStr = wx.temperature_c != null ? `${fmtNum(wx.temperature_c, 0)}°C` : "--°C";
    const wStr = wx.wind_speed_ms != null ? `${fmtNum(wx.wind_speed_ms, 1)}m/s` : "--m/s";
    const valTxt = `${tStr} · ${wStr}`;
    const windTxt = cp?.wind?.state === "超速切出"
      ? tr("切出")
      : (cp?.wind?.avail_pct != null ? `${fmtNum(cp.wind.avail_pct, 0)}%` : "--");
    const lvl = cp?.alert_level || "normal";
    const icingTip = wx.icing_label ? ` · ${tr(wx.icing_label)}` : "";
    const tip = `${labelTxt} · ${tStr} / ${wStr}${icingTip}${cp?.strategy ? ` · ${tr(cp.strategy)}` : ""}`;

    $$(".wx-inline-pill").forEach((pill) => {
      pill.classList.toggle("is-elevated", lvl === "elevated");
      pill.classList.toggle("is-extreme", lvl === "extreme");
      pill.title = tip;
      const lblEl = pill.querySelector("[data-wx-badge-label]");
      const valEl = pill.querySelector("[data-wx-badge-val]");
      const cplEl = pill.querySelector("[data-wx-badge-cpl]");
      if (lblEl) lblEl.textContent = labelTxt;
      if (valEl) valEl.textContent = valTxt;
      if (cplEl) cplEl.textContent = windTxt;
    });

    const sbVal = $("#health-weather-val");
    if (sbVal) {
      sbVal.textContent = `${tStr} / ${wStr}`;
      sbVal.style.color = lvl === "extreme" ? "var(--danger)" : (lvl === "elevated" ? "var(--warn)" : "var(--accent)");
    }
    if ($("#health-weather-row")) $("#health-weather-row").title = tip;
  }

  const WX_PRESET_DEFAULTS = {
    live: { mode: "measured", temperature_c: null, wind_speed_ms: null, irradiance_wm2: null, pressure_hpa: null, icing_level: null, snow_condition: null },
    summer_sunny: { mode: "manual", temperature_c: -4, wind_speed_ms: 8.5, irradiance_wm2: 780, pressure_hpa: 988, icing_level: 0, snow_condition: "clear" },
    katabatic_gale: { mode: "manual", temperature_c: -22, wind_speed_ms: 26.5, irradiance_wm2: 180, pressure_hpa: 968, icing_level: 1, snow_condition: "blowing_snow" },
    polar_night_cold: { mode: "manual", temperature_c: -38, wind_speed_ms: 6.0, irradiance_wm2: 0, pressure_hpa: 978, icing_level: 2, snow_condition: "overcast" },
    blizzard_icing: { mode: "manual", temperature_c: -31, wind_speed_ms: 21.0, irradiance_wm2: 35, pressure_hpa: 962, icing_level: 3, snow_condition: "blizzard" },
    calm_overcast: { mode: "manual", temperature_c: -14, wind_speed_ms: 2.2, irradiance_wm2: 95, pressure_hpa: 992, icing_level: 0, snow_condition: "overcast" },
  };
  const WX_FIELD_SPECS = [
    { key: "temperature_c", numId: "wx-temp", rngId: "wx-temp-range", tagId: "wx-src-temp", kind: "num" },
    { key: "wind_speed_ms", numId: "wx-wind", rngId: "wx-wind-range", tagId: "wx-src-wind", kind: "num" },
    { key: "irradiance_wm2", numId: "wx-irr", rngId: "wx-irr-range", tagId: "wx-src-irr", kind: "num" },
    { key: "pressure_hpa", numId: "wx-pres", rngId: "wx-pres-range", tagId: "wx-src-pres", kind: "num" },
    { key: "icing_level", selId: "wx-icing", tagId: "wx-src-icing", kind: "int" },
    { key: "snow_condition", selId: "wx-snow", tagId: "wx-src-snow", kind: "str" },
  ];
  let wxDialogState = {
    mode: "measured",
    preset: "live",
    measured: {},
    overrides: { temperature_c: null, wind_speed_ms: null, irradiance_wm2: null, pressure_hpa: null, icing_level: null, snow_condition: null },
  };

  function readOptNum(id) {
    const raw = $("#" + id)?.value;
    if (raw === undefined || raw === null || String(raw).trim() === "") return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }

  function hasAnyWxOverride() {
    return Object.values(wxDialogState.overrides).some((v) => v !== null && v !== undefined && v !== "");
  }

  function syncWxFieldBadges() {
    const m = wxDialogState.measured || {};
    const ov = wxDialogState.overrides || {};
    WX_FIELD_SPECS.forEach((sp) => {
      const box = $(`[data-wx-field="${sp.key}"]`);
      const tag = $("#" + sp.tagId);
      const clr = box?.querySelector(`[data-wx-clear="${sp.key}"]`);
      const hasOv = ov[sp.key] !== null && ov[sp.key] !== undefined && ov[sp.key] !== "";
      const hasMeas = m[sp.key] !== null && m[sp.key] !== undefined && m[sp.key] !== "";
      const isEmpty = !hasOv && !hasMeas;
      box?.classList.toggle("is-empty", isEmpty);
      if (tag) {
        tag.className = `wx-src-tag${hasOv ? " is-manual" : (hasMeas ? " is-measured" : "")}`;
        tag.textContent = hasOv ? tr("手工") : (hasMeas ? tr("实测") : tr(sp.kind === "num" ? "未接入" : "未录入"));
      }
      if (clr) {
        clr.hidden = !hasOv;
        clr.title = hasMeas ? tr("清除覆盖并恢复实时实测") : tr("清除手工补录");
      }
    });
  }

  function updateWeatherPreview() {
    const t = readOptNum("wx-temp");
    const wsRaw = readOptNum("wx-wind");
    const ws = wsRaw != null ? Math.max(0, wsRaw) : null;
    const irrRaw = readOptNum("wx-irr");
    const irr = irrRaw != null ? Math.max(0, irrRaw) : null;
    const pres = readOptNum("wx-pres");
    const icingRaw = $("#wx-icing")?.value;
    const icing = (icingRaw !== undefined && icingRaw !== "") ? Number(icingRaw) : null;
    const snowRaw = $("#wx-snow")?.value;
    const snow = snowRaw ? String(snowRaw) : null;

    let wc = null;
    if (t != null && ws != null) {
      const vKmh = ws * 3.6;
      wc = t <= 10 && vKmh >= 4.8
        ? (13.12 + 0.6215 * t - 11.37 * Math.pow(vKmh, 0.16) + 0.3965 * t * Math.pow(vKmh, 0.16))
        : t;
    } else if (t != null) {
      wc = t;
    }
    const rho = (pres != null && t != null) ? ((pres * 100) / (287.05 * Math.max(180, t + 273.15))) : null;
    const rhoFactor = rho != null ? Math.min(1.22, Math.max(0.85, rho / 1.225)) : 1.0;
    const iceFactor = icing != null ? ([1.0, 0.88, 0.72, 0.45][icing] ?? 1.0) : 1.0;

    let windTxt = "--";
    let windColor = "";
    if (ws != null) {
      if (ws >= 25) {
        windTxt = "超速切出 (0%)";
        windColor = "var(--danger)";
      } else if (ws < 3) {
        windTxt = "低风待机 (0%)";
        windColor = "var(--text-secondary)";
      } else {
        const pct = Math.min(100, Math.pow((ws - 3) / 9, 3) * rhoFactor * iceFactor * 100);
        windTxt = `${pct.toFixed(0)}%${icing != null && icing >= 2 ? " 覆冰" : ""}`;
        windColor = icing != null && icing >= 2 ? "var(--warn)" : "var(--ok)";
      }
    }

    const pvTempFactor = t != null ? Math.min(1.24, Math.max(0.85, 1 + (25 - t) * 0.0038)) : null;
    const snowFactor = snow != null ? ({ clear: 1.0, overcast: 0.96, blowing_snow: 0.75, blizzard: 0.30 }[snow] ?? 1.0) : 1.0;
    let pvTxt = "--";
    if (irr != null && irr < 5) {
      pvTxt = tr("无光照");
    } else if (pvTempFactor != null) {
      pvTxt = `${(pvTempFactor * snowFactor * 100).toFixed(0)}%`;
    } else if (snow != null) {
      pvTxt = `${(snowFactor * 100).toFixed(0)}%`;
    }

    let bessPct = null;
    if (t != null) {
      bessPct = 98;
      if (t < -5 && t >= -20) bessPct = 98 - (-5 - t) * 0.8;
      else if (t < -20 && t >= -40) bessPct = 86 - (-20 - t) * 1.05;
      else if (t < -40) bessPct = Math.max(55, 65 - (-40 - t) * 0.5);
    }

    const heatDelta = (t != null && wc != null)
      ? (Math.max(0, -10 - wc) * 1.45 + (snow === "blizzard" ? 8 : snow === "blowing_snow" ? 3.5 : 0))
      : null;

    let alertLvl = "normal";
    let strat = "绿电优先消纳";
    if (ws != null && ws >= 25) {
      alertLvl = "extreme";
      strat = "风机超速切出 · 柴储顶峰保供";
    } else if ((icing != null && icing >= 3) || snow === "blizzard" || (t != null && t <= -32)) {
      alertLvl = "extreme";
      strat = "极寒重冰保供 · 启动采暖与柴发备载";
    } else if ((ws != null && ws >= 17.2) || (t != null && t <= -24) || icing === 2) {
      alertLvl = "elevated";
      strat = "大风寒潮关注 · 预留储能热备";
    } else if (irr != null && irr < 5 && ws != null && ws < 3.5) {
      alertLvl = "elevated";
      strat = "无风无光低出力 · 氢电柴发协同";
    } else if (irr != null && irr >= 650 && ws != null && ws >= 6) {
      alertLvl = "normal";
      strat = "风光充沛 · 制氢储能消纳";
    }

    if ($("#wx-pv-wc")) $("#wx-pv-wc").textContent = wc != null ? `${wc.toFixed(1)} °C` : "--°C";
    if ($("#wx-pv-rho")) $("#wx-pv-rho").textContent = rho != null ? `${rho.toFixed(3)} kg/m³` : "-- kg/m³";
    if ($("#wx-pv-wind")) {
      $("#wx-pv-wind").textContent = windTxt;
      $("#wx-pv-wind").style.color = windColor;
    }
    if ($("#wx-pv-pv")) $("#wx-pv-pv").textContent = pvTxt;
    if ($("#wx-pv-bess")) {
      $("#wx-pv-bess").textContent = bessPct != null ? `${bessPct.toFixed(1)}%` : "--%";
      $("#wx-pv-bess").style.color = bessPct == null ? "" : (bessPct < 72 ? "var(--danger)" : (bessPct < 85 ? "var(--warn)" : "var(--ok)"));
    }
    if ($("#wx-pv-heat")) {
      $("#wx-pv-heat").textContent = heatDelta != null ? `+${heatDelta.toFixed(1)} kW` : "-- kW";
      $("#wx-pv-heat").style.color = heatDelta == null ? "" : (heatDelta >= 35 ? "var(--danger)" : (heatDelta >= 15 ? "var(--warn)" : "var(--text-primary)"));
    }
    const alEl = $("#wx-pv-alert");
    if (alEl) {
      alEl.className = `ck-drv-badge${alertLvl === "extreme" ? " is-extreme" : (alertLvl === "elevated" ? " is-elevated" : "")}`;
      alEl.textContent = alertLvl === "extreme" ? tr("极端气候保供") : (alertLvl === "elevated" ? tr("大风寒潮关注") : tr("极地气象正常"));
    }
    if ($("#wx-pv-strategy")) $("#wx-pv-strategy").textContent = tr(strat);
  }

  function syncWeatherFormUi() {
    $$("#wx-mode-seg [data-wx-mode]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.wxMode === wxDialogState.mode);
    });
    $$("#wx-presets [data-wx-preset]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.wxPreset === wxDialogState.preset);
    });
    syncWxFieldBadges();
    updateWeatherPreview();
  }

  function setWxNumeric(id, rangeId, val) {
    const inp = $("#" + id);
    const rng = $("#" + rangeId);
    if (val === null || val === undefined || val === "") {
      if (inp) inp.value = "";
      return;
    }
    const n = Number(val);
    if (!Number.isFinite(n)) return;
    if (inp) inp.value = String(n);
    if (rng) {
      const lo = Number(rng.min);
      const hi = Number(rng.max);
      rng.value = String(Math.max(lo, Math.min(hi, n)));
    }
  }

  function fillWxInputsFromState() {
    const m = wxDialogState.measured || {};
    const ov = wxDialogState.overrides || {};
    WX_FIELD_SPECS.forEach((sp) => {
      const val = ov[sp.key] !== null && ov[sp.key] !== undefined ? ov[sp.key] : (m[sp.key] ?? null);
      if (sp.kind === "num") {
        setWxNumeric(sp.numId, sp.rngId, val);
      } else if ($("#" + sp.selId)) {
        $("#" + sp.selId).value = val != null ? String(val) : "";
      }
    });
  }

  async function openWeatherDialog(focusId = "") {
    const dlg = $("#weather-dialog");
    if (!dlg || typeof dlg.showModal !== "function") return;
    const errEl = $("#wx-error");
    if (errEl) errEl.hidden = true;
    try {
      const res = await api("/api/weather");
      const eff = res.effective || {};
      const prof = res.profile || {};
      const mode = prof.mode || eff.mode || "measured";
      wxDialogState = {
        mode,
        preset: prof.preset || eff.preset || "live",
        measured: res.measured || {},
        overrides: mode === "manual"
          ? {
              temperature_c: prof.temperature_c ?? null,
              wind_speed_ms: prof.wind_speed_ms ?? null,
              irradiance_wm2: prof.irradiance_wm2 ?? null,
              pressure_hpa: prof.pressure_hpa ?? null,
              icing_level: prof.icing_level ?? null,
              snow_condition: prof.snow_condition ?? null,
            }
          : { temperature_c: null, wind_speed_ms: null, irradiance_wm2: null, pressure_hpa: null, icing_level: null, snow_condition: null },
      };
      fillWxInputsFromState();
      syncWeatherFormUi();
    } catch (err) {
      if (errEl) {
        errEl.textContent = err.message || tr("加载气象配置失败");
        errEl.hidden = false;
      }
    }
    if (!dlg.open) dlg.showModal();
    if (focusId) {
      const targetEl = $("#" + focusId);
      if (targetEl && typeof targetEl.focus === "function") {
        requestAnimationFrame(() => {
          targetEl.focus();
          if (typeof targetEl.select === "function") targetEl.select();
        });
      }
    }
  }

  // 绑定全站所有 [data-open-weather] 入口与弹窗控件联动
  $$("[data-open-weather]").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      openWeatherDialog(el.dataset.wxFocus || "");
    });
  });
  $("#wx-close")?.addEventListener("click", () => $("#weather-dialog")?.close());
  $("#wx-cancel")?.addEventListener("click", () => $("#weather-dialog")?.close());

  $$("#wx-mode-seg [data-wx-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      wxDialogState.mode = btn.dataset.wxMode === "manual" ? "manual" : "measured";
      if (wxDialogState.mode === "measured") {
        wxDialogState.preset = "live";
        wxDialogState.overrides = { temperature_c: null, wind_speed_ms: null, irradiance_wm2: null, pressure_hpa: null, icing_level: null, snow_condition: null };
        fillWxInputsFromState();
      } else if (wxDialogState.preset === "live") {
        wxDialogState.preset = "custom";
      }
      syncWeatherFormUi();
    });
  });

  $$("#wx-presets [data-wx-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.dataset.wxPreset || "live";
      const def = WX_PRESET_DEFAULTS[key];
      if (!def) return;
      wxDialogState.preset = key;
      wxDialogState.mode = def.mode;
      wxDialogState.overrides = key === "live"
        ? { temperature_c: null, wind_speed_ms: null, irradiance_wm2: null, pressure_hpa: null, icing_level: null, snow_condition: null }
        : {
            temperature_c: def.temperature_c,
            wind_speed_ms: def.wind_speed_ms,
            irradiance_wm2: def.irradiance_wm2,
            pressure_hpa: def.pressure_hpa,
            icing_level: def.icing_level,
            snow_condition: def.snow_condition,
          };
      fillWxInputsFromState();
      syncWeatherFormUi();
    });
  });

  const markWxFieldEdited = (key, val) => {
    wxDialogState.overrides[key] = (val === "" || val === undefined) ? null : val;
    if (hasAnyWxOverride()) {
      wxDialogState.mode = "manual";
      wxDialogState.preset = "custom";
    } else {
      wxDialogState.mode = "measured";
      wxDialogState.preset = "live";
    }
    syncWeatherFormUi();
  };

  WX_FIELD_SPECS.filter((s) => s.kind === "num").forEach((sp) => {
    $("#" + sp.rngId)?.addEventListener("input", (e) => {
      if ($("#" + sp.numId)) $("#" + sp.numId).value = e.target.value;
      markWxFieldEdited(sp.key, Number(e.target.value));
    });
    $("#" + sp.numId)?.addEventListener("input", (e) => {
      const raw = e.target.value;
      setWxNumeric(sp.numId, sp.rngId, raw);
      const num = readOptNum(sp.numId);
      markWxFieldEdited(sp.key, num);
    });
  });
  $("#wx-icing")?.addEventListener("change", (e) => {
    markWxFieldEdited("icing_level", e.target.value === "" ? null : Number(e.target.value));
  });
  $("#wx-snow")?.addEventListener("change", (e) => {
    markWxFieldEdited("snow_condition", e.target.value || null);
  });

  $$("[data-wx-clear]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.dataset.wxClear;
      if (!key) return;
      wxDialogState.overrides[key] = null;
      fillWxInputsFromState();
      if (!hasAnyWxOverride()) {
        wxDialogState.mode = "measured";
        wxDialogState.preset = "live";
      } else {
        wxDialogState.preset = "custom";
      }
      syncWeatherFormUi();
    });
  });

  $$("[data-wx-pick]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const [id, rawVal] = String(btn.dataset.wxPick || "").split(":");
      const sp = WX_FIELD_SPECS.find((x) => x.numId === id);
      if (!sp) return;
      const num = Number(rawVal);
      if (!Number.isFinite(num)) return;
      setWxNumeric(sp.numId, sp.rngId, num);
      markWxFieldEdited(sp.key, num);
    });
  });

  $("#wx-reset-live")?.addEventListener("click", async () => {
    const errEl = $("#wx-error");
    try {
      const res = await apiPut("/api/weather", { mode: "measured", preset: "live" });
      syncAllWeatherBadges(res?.effective, res?.coupling);
      $("#weather-dialog")?.close();
      window.ScadaExtras?.showToast(tr("已恢复现场实测气象"), "ok");
      refreshCurrentView();
    } catch (err) {
      if (errEl) {
        errEl.textContent = err.message || tr("恢复失败");
        errEl.hidden = false;
      }
    }
  });

  $("#weather-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const errEl = $("#wx-error");
    if (errEl) errEl.hidden = true;
    try {
      const useMeasured = wxDialogState.mode === "measured" || !hasAnyWxOverride();
      const payload = useMeasured
        ? { mode: "measured", preset: "live" }
        : {
            mode: "manual",
            preset: wxDialogState.preset === "live" ? "custom" : wxDialogState.preset,
            ...wxDialogState.overrides,
          };
      const res = await apiPut("/api/weather", payload);
      syncAllWeatherBadges(res?.effective, res?.coupling);
      $("#weather-dialog")?.close();
      window.ScadaExtras?.showToast(tr(useMeasured ? "已恢复现场实测气象" : "南极气象工况已应用生效"), "ok");
      refreshCurrentView();
    } catch (err) {
      if (errEl) {
        errEl.textContent = err.message || tr("保存失败");
        errEl.hidden = false;
      }
    }
  });

  // 左侧导航栏模式控制：支持「悬浮 / 内嵌」切换，两种模式均可隐藏（双端共享偏好）
  (() => {
    const SB_KEY = "scada_sidebar_prefs";
    const shell = $("#portal-shell");
    const sidebar = $("#portal-sidebar");
    const trigger = $("#sidebar-edge-trigger");
    const hideBtn = $("#sidebar-hide-btn");
    if (!shell || !sidebar) return;

    let prefs = { mode: "floating", hidden: true };
    try {
      const saved = JSON.parse(localStorage.getItem(SB_KEY) || "null");
      if (saved && (saved.mode === "floating" || saved.mode === "docked")) {
        prefs.mode = saved.mode;
        prefs.hidden = saved.mode === "floating" ? true : Boolean(saved.hidden);
      }
    } catch (_) {}

    let hideTimer = null;
    const persist = () => {
      try { localStorage.setItem(SB_KEY, JSON.stringify(prefs)); } catch (_) {}
    };
    const triggerLayoutReflow = () => {
      requestAnimationFrame(() => {
        window.dispatchEvent(new Event("resize"));
        if (state.view === "powerflow" && state.svgLoaded) pfMinimap?.update();
      });
    };

    const syncUi = () => {
      const isDocked = prefs.mode === "docked";
      shell.classList.toggle("sidebar-docked", isDocked);
      shell.classList.toggle("sidebar-hidden", isDocked && prefs.hidden);
      if (isDocked) {
        sidebar.classList.remove("is-open");
      }
      sidebar.querySelectorAll("[data-sb-mode]").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.sbMode === prefs.mode);
      });
      triggerLayoutReflow();
    };

    const openFloating = () => {
      if (prefs.mode !== "floating") return;
      if (hideTimer) {
        clearTimeout(hideTimer);
        hideTimer = null;
      }
      sidebar.classList.add("is-open");
    };

    const scheduleCloseFloating = (delay = 220) => {
      if (prefs.mode !== "floating") return;
      if (hideTimer) clearTimeout(hideTimer);
      hideTimer = setTimeout(() => {
        hideTimer = null;
        sidebar.classList.remove("is-open");
      }, delay);
    };

    sidebar.querySelectorAll("[data-sb-mode]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const nextMode = btn.dataset.sbMode === "docked" ? "docked" : "floating";
        prefs.mode = nextMode;
        prefs.hidden = nextMode === "floating";
        persist();
        syncUi();
      });
    });

    hideBtn?.addEventListener("click", () => {
      if (prefs.mode === "docked") {
        prefs.hidden = true;
        persist();
        syncUi();
      } else {
        if (hideTimer) clearTimeout(hideTimer);
        sidebar.classList.remove("is-open");
      }
    });

    trigger?.addEventListener("mouseenter", () => {
      if (prefs.mode === "floating") openFloating();
    });
    trigger?.addEventListener("click", () => {
      if (prefs.mode === "docked") {
        prefs.hidden = false;
        persist();
        syncUi();
      } else {
        openFloating();
      }
    });
    sidebar.addEventListener("mouseenter", () => {
      if (prefs.mode === "floating") openFloating();
    });
    sidebar.addEventListener("mouseleave", () => {
      if (prefs.mode === "floating") scheduleCloseFloating(220);
    });
    sidebar.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (prefs.mode === "floating") scheduleCloseFloating(120);
      });
    });
    document.addEventListener("mousemove", (e) => {
      if (prefs.mode !== "floating") return;
      if (e.clientX <= 14 && e.clientY >= 54) {
        openFloating();
      } else if (sidebar.classList.contains("is-open") && e.clientX > sidebar.offsetWidth + 24 && !sidebar.matches(":hover")) {
        scheduleCloseFloating(200);
      }
    });

    syncUi();
  })();

  // 启动入口（同时支持 #hash 与 /cockpit、/energy-flow、/alarms 等路径直达）
  const initialHash = location.hash.replace(/^#/, "");
  const pathViewMap = {
    "/cockpit": "cockpit",
    "/dispatch": "dispatch",
    "/energy-flow": "energyflow",
    "/powerflow": "powerflow",
    "/devices": "devices",
    "/alarms": "alarms",
    "/comm": "comm",
    "/logs": "logs",
  };
  const validViews = ["home", "cockpit", "dispatch", "energyflow", "powerflow", "devices", "alarms", "comm", "logs"];
  const initialView = validViews.includes(initialHash)
    ? initialHash
    : (pathViewMap[location.pathname] || "home");
  switchView(initialView, false);
  resetTimer();
})();

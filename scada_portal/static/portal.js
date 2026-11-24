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
    // 设备图状态
    devCategories: [],
    devCategory: "",
    devItems: [],
    devSelected: null, // { table_name, idx, dev_id }
    trendData: null,
    trendHidden: new Set(),
    trendHoverX: null,
    // 通信图状态
    commTab: "rtus",
    commPointOffset: 0,
    commPointLimit: 50,
    commPointTotal: 0,
    commRtuOptionsLoaded: false,
    // 日志状态
    logRecord: "runtime",
    logFilename: "",
    logLines: [],
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

  async function api(url) {
    const resp = await fetch(url, { headers: { Accept: "application/json" } });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
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
    const valid = ["home", "powerflow", "devices", "comm", "logs"];
    if (!valid.includes(view)) view = "home";
    state.view = view;
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
    if (view === "powerflow" && state.svgLoaded) {
      requestAnimationFrame(() => fitSvgView());
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

  // ==================== 1. 系统首页（输入底图实时刷新） ====================
  function updateFlowPath(id, powerKw, { reverseOnNeg = false, alwaysReverse = false } = {}) {
    const el = $("#" + id);
    if (!el) return;
    const val = Number(powerKw || 0);
    const abs = Math.abs(val);
    el.classList.remove("idle", "reverse");
    if (abs < 0.05) {
      el.classList.add("idle");
      el.style.animationDuration = "";
      return;
    }
    if (alwaysReverse || (reverseOnNeg && val < 0)) {
      el.classList.add("reverse");
    }
    const dur = Math.max(0.45, Math.min(2.2, 2.2 - Math.min(abs, 250) / 150));
    el.style.animationDuration = dur.toFixed(2) + "s";
  }

  async function loadOverview() {
    const data = await api("/api/overview");
    if (data.station_name && $("#station-title")) $("#station-title").textContent = data.station_name;
    if (data.scene && $("#station-scene")) $("#station-scene").textContent = data.scene;

    const k = data.kpis || {};
    const rt = data.realtime || {};
    const h = data.health || {};

    $("#kpi-days").textContent = k.running_days ?? "--";
    $("#kpi-load-total").textContent = fmtNum(k.load_total_wkwh, 1);
    $("#kpi-diesel-total").textContent = fmtNum(k.diesel_total_wkwh, 1);
    $("#kpi-renew-total").textContent = fmtNum(k.renewable_total_wkwh, 1);
    $("#kpi-diesel-saved").textContent = fmtNum(k.diesel_saved_tons, 1);

    $("#kpi-wind-total").textContent = fmtNum(k.wind_total_wkwh, 2);
    $("#kpi-pv-total").textContent = fmtNum(k.pv_total_wkwh, 2);
    $("#kpi-storage-total").textContent = fmtNum(k.storage_total_wkwh, 2);
    $("#kpi-hydro-total").textContent = fmtNum(k.hydro_total_wkwh, 2);
    $("#kpi-renew-share").textContent = fmtNum(k.renewable_share_pct, 1);

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

    // 动态能量流动画联动
    updateFlowPath("flow-pv", rt.pv_kw);
    updateFlowPath("flow-wind", rt.wind_kw);
    updateFlowPath("flow-h2prod", rt.hydrogen_prod_kw, { alwaysReverse: true });
    updateFlowPath("flow-h2gen", rt.hydrogen_gen_kw);
    updateFlowPath("flow-trunk", (rt.pv_kw || 0) + (rt.wind_kw || 0) + (rt.hydrogen_gen_kw || 0) - (rt.hydrogen_prod_kw || 0), { reverseOnNeg: true });
    updateFlowPath("flow-diesel", rt.diesel_kw);
    updateFlowPath("flow-storage", rt.storage_kw, { reverseOnNeg: true });
    updateFlowPath("flow-load", rt.load_kw);
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

  async function ensureSvgLoaded() {
    if (state.svgLoaded) return;
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
    state.svgLoaded = true;
    applySvgThemeColors();
    fitSvgView();

    svg.addEventListener("click", (event) => {
      const target = event.target.closest("use[dev-id], g.mg[dev], text[dev-id]");
      if (!target) return;
      const devId = target.getAttribute("dev-id") || target.getAttribute("dev");
      if (devId) openPowerflowDrawer(devId);
    });
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

    const stat = $("#pf-stat");
    if (stat) {
      stat.textContent = `已刷新 ${updatedSlots} 项量测 · 断路器合闸 ${closedCnt}/${totalBreakers}`;
      stat.className = "badge ok";
    }
    if (state.pfSelectedDev) {
      openPowerflowDrawer(state.pfSelectedDev, false);
    }
  }

  function openPowerflowDrawer(devId, highlight = true) {
    state.pfSelectedDev = devId;
    const drawer = $("#pf-drawer");
    const dev = state.pfData?.devices?.[devId];
    const svg = $("#pf-stage svg");
    if (highlight && svg) {
      svg.querySelectorAll(".pf-highlight").forEach((el) => el.classList.remove("pf-highlight"));
      const node = svg.querySelector(`use[dev-id="${CSS.escape(devId)}"]`);
      if (node) node.classList.add("pf-highlight");
    }
    if (!dev) {
      drawer.hidden = false;
      $("#pf-drawer-name").textContent = devId;
      $("#pf-drawer-meta").textContent = "该图元暂无绑定量测";
      $("#pf-drawer-metrics").innerHTML = '<div class="empty-hint">无实时绑定量测</div>';
      $("#pf-jump-device").disabled = true;
      return;
    }
    drawer.hidden = false;
    $("#pf-drawer-name").textContent = dev.name;
    $("#pf-drawer-meta").textContent = `${dev.table_name} #${dev.idx} · 有效 ${dev.valid_count}/${dev.bound_count}`;
    $("#pf-jump-device").disabled = false;
    $("#pf-jump-device").onclick = () => {
      state.devCategory = dev.category;
      state.devSelected = { table_name: dev.table_name, idx: dev.idx, dev_id: dev.dev_id };
      switchView("devices");
    };
    const rows = (dev.metrics || []).map((m) => {
      const valText = m.kind === "yx"
        ? (m.metric === "position" ? (m.value ? "合闸" : "分闸") : (m.value ? "运行" : "停运"))
        : `${fmtNum(m.display_value, 2)} ${m.unit || ""}`.trim();
      return `<div class="drawer-row"><span>${esc(m.label)}</span><strong>${esc(valText)}</strong></div>`;
    });
    $("#pf-drawer-metrics").innerHTML = rows.length ? rows.join("") : '<div class="empty-hint">无量测</div>';
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
    openPowerflowDrawer(devId, true);
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
      if (e.button !== 0 || e.target.closest("use[dev-id], g.mg[dev]")) return;
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
      e.preventDefault();
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

    $("#pf-zoom-in")?.addEventListener("click", () => {
      state.pfScale = Math.min(3.5, state.pfScale * 1.2);
      applySvgTransform();
    });
    $("#pf-zoom-out")?.addEventListener("click", () => {
      state.pfScale = Math.max(0.15, state.pfScale / 1.2);
      applySvgTransform();
    });
    $("#pf-zoom-fit")?.addEventListener("click", fitSvgView);
    $("#pf-drawer-close")?.addEventListener("click", () => {
      $("#pf-drawer").hidden = true;
      state.pfSelectedDev = null;
    });
    $("#pf-show-meas")?.addEventListener("change", (e) => {
      $("#pf-stage").classList.toggle("hide-meas", !e.target.checked);
    });
    $("#pf-show-labels")?.addEventListener("change", (e) => {
      $("#pf-stage").classList.toggle("hide-labels", !e.target.checked);
    });
    $("#pf-search")?.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const q = e.target.value.trim().toLowerCase();
      if (!q || !state.pfData?.devices) return;
      const match = Object.values(state.pfData.devices).find(
        (d) => d.name.toLowerCase().includes(q) || d.dev_id.toLowerCase().includes(q)
      );
      if (match) focusSvgDevice(match.dev_id);
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

  async function loadDevices() {
    const search = ($("#dev-search")?.value || "").trim();
    const qs = new URLSearchParams();
    if (state.devCategory) qs.set("category", state.devCategory);
    if (search) qs.set("search", search);
    const data = await api("/api/devices?" + qs.toString());
    state.devCategories = data.categories || [];
    if (!state.devCategory && state.devCategories.length) {
      state.devCategory = state.devCategories[0].key;
      return loadDevices();
    }
    state.devItems = data.items || [];

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
    $("#dev-list-count").textContent = String(state.devItems.length);

    // 选中默认或保持原选中设备
    let activeDev = state.devSelected
      ? state.devItems.find((d) => d.dev_id === state.devSelected.dev_id)
      : null;
    if (!activeDev && state.devItems.length) {
      activeDev = state.devItems[0];
      state.devSelected = { table_name: activeDev.table_name, idx: activeDev.idx, dev_id: activeDev.dev_id };
      state.trendHidden.clear();
    }

    const listEl = $("#dev-list");
    if (!state.devItems.length) {
      listEl.innerHTML = '<div class="empty-hint">无匹配设备</div>';
      renderDeviceDetail(null);
      return;
    }

    listEl.innerHTML = state.devItems
      .map((d) => {
        const selected = activeDev && d.dev_id === activeDev.dev_id;
        const okBadge =
          d.position !== null
            ? d.position ? '<span class="badge ok">合闸</span>' : '<span class="badge danger">分闸</span>'
            : d.run === 0
              ? '<span class="badge warn">停运</span>'
              : '<span class="badge ok">运行</span>';
        return (
          `<button type="button" class="dev-item ${selected ? "selected" : ""}" data-dev-id="${esc(d.dev_id)}">` +
          `<div class="dev-item-top"><strong title="${esc(d.name)}">${esc(d.name)}</strong>${okBadge}</div>` +
          `<div class="dev-item-sub"><span>${esc(d.table_name)} #${d.idx}</span><span class="num">${esc(primaryMetricSummary(d))}</span></div>` +
          `</button>`
        );
      })
      .join("");

    listEl.querySelectorAll(".dev-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        const dev = state.devItems.find((x) => x.dev_id === btn.dataset.devId);
        if (!dev) return;
        state.devSelected = { table_name: dev.table_name, idx: dev.idx, dev_id: dev.dev_id };
        state.trendHidden.clear();
        listEl.querySelectorAll(".dev-item").forEach((el) => el.classList.toggle("selected", el === btn));
        renderDeviceDetail(dev);
        loadDeviceTrend(dev);
      });
    });

    renderDeviceDetail(activeDev);
    if (activeDev) await loadDeviceTrend(activeDev);
  }

  function renderDeviceDetail(dev) {
    const grid = $("#dev-metric-grid");
    const locBtn = $("#dev-locate-svg");
    if (!dev) {
      $("#dev-detail-name").textContent = "请选择左侧设备";
      $("#dev-detail-meta").textContent = "";
      grid.innerHTML = "";
      locBtn.disabled = true;
      state.trendData = null;
      drawTrendChart();
      return;
    }
    $("#dev-detail-name").textContent = dev.name;
    $("#dev-detail-meta").textContent = `${dev.table_name} · 编号 #${dev.idx} · 有效量测 ${dev.valid_count}/${dev.bound_count}`;
    locBtn.disabled = false;
    locBtn.onclick = async () => {
      switchView("powerflow");
      await loadPowerflow();
      focusSvgDevice(dev.dev_id);
    };
    grid.innerHTML = (dev.metrics || [])
      .map((m) => {
        const valStr = m.kind === "yx"
          ? (m.metric === "position" ? (m.value ? "合闸" : "分闸") : (m.value ? "运行" : "停运"))
          : fmtNum(m.display_value, 2);
        const qBadge = m.valid ? '<span class="badge ok">正常</span>' : '<span class="badge danger">无效</span>';
        return (
          `<div class="metric-box">` +
          `<div class="m-label"><span>${esc(m.label)}</span>${qBadge}</div>` +
          `<div class="metric-val">${esc(valStr)}<small>${esc(m.unit || "")}</small></div>` +
          `<div class="m-foot"><span>RTU ${m.rtu_id ?? "-"} / #${m.point_no ?? "-"}</span><span>${esc(m.metric)}</span></div>` +
          `</div>`
        );
      })
      .join("");
  }

  async function loadDeviceTrend(dev) {
    const hours = Number($("#trend-hours")?.value || 24);
    const data = await api(`/api/devices/${encodeURIComponent(dev.table_name)}/${dev.idx}/trend?hours=${hours}`);
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
        const color = palette.colors[idx % palette.colors.length];
        const hidden = state.trendHidden.has(s.metric);
        return (
          `<button type="button" class="series-chip ${hidden ? "muted" : ""}" data-metric="${esc(s.metric)}">` +
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
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);

    const pal = chartPalette();
    const allSeries = state.trendData?.series || [];
    const visible = allSeries
      .map((s, idx) => ({ ...s, color: pal.colors[idx % pal.colors.length] }))
      .filter((s) => !state.trendHidden.has(s.metric) && s.points && s.points.length);

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

  $("#trend-canvas")?.addEventListener("mousemove", (e) => {
    const rect = e.target.getBoundingClientRect();
    state.trendHoverX = e.clientX - rect.left;
    drawTrendChart();
  });
  $("#trend-canvas")?.addEventListener("mouseleave", () => {
    state.trendHoverX = null;
    drawTrendChart();
  });
  $("#trend-hours")?.addEventListener("change", () => {
    if (state.devSelected) loadDeviceTrend(state.devSelected);
  });
  $("#dev-search")?.addEventListener("input", () => loadDevices());

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
      `<div class="kpi-card"><div class="kpi-label">RTU 总数</div><div class="kpi-val"><strong>${rtus.length}</strong><span>台</span></div></div>` +
      `<div class="kpi-card"><div class="kpi-label">在线连接</div><div class="kpi-val"><strong>${online}</strong><span>/ ${rtus.length}</span></div></div>` +
      `<div class="kpi-card"><div class="kpi-label">累计接收帧</div><div class="kpi-val"><strong>${totalRx.toLocaleString()}</strong><span>帧</span></div></div>` +
      `<div class="kpi-card"><div class="kpi-label">通信错误</div><div class="kpi-val"><strong>${totalErr.toLocaleString()}</strong><span>次</span></div></div>`;

    if (!state.commRtuOptionsLoaded) {
      const sel = $("#comm-rtu-filter");
      sel.innerHTML = '<option value="">全部 RTU</option>' +
        rtus.map((r) => `<option value="${r.rtu_id}">RTU ${r.rtu_id} · ${esc(r.name)}</option>`).join("");
      state.commRtuOptionsLoaded = true;
    }

    if (state.commTab === "rtus") {
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
    } else if (state.commTab === "channels") {
      const chRes = await api("/api/comm/channels");
      const items = chRes.items || [];
      $("#comm-channels-body").innerHTML = items.length
        ? items.map((c) => {
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
        kind: $("#comm-kind-filter").value || "yc",
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
      $("#comm-rtus-panel").hidden = state.commTab !== "rtus";
      $("#comm-channels-panel").hidden = state.commTab !== "channels";
      $("#comm-points-panel").hidden = state.commTab !== "points";
      $("#comm-point-filters").hidden = state.commTab !== "points";
      loadComm();
    });
  });
  $("#comm-rtu-filter")?.addEventListener("change", () => { state.commPointOffset = 0; loadComm(); });
  $("#comm-kind-filter")?.addEventListener("change", () => { state.commPointOffset = 0; loadComm(); });
  $("#comm-point-search")?.addEventListener("input", () => { state.commPointOffset = 0; loadComm(); });
  $("#comm-prev")?.addEventListener("click", () => {
    state.commPointOffset = Math.max(0, state.commPointOffset - state.commPointLimit);
    loadComm();
  });
  $("#comm-next")?.addEventListener("click", () => {
    if (state.commPointOffset + state.commPointLimit < state.commPointTotal) {
      state.commPointOffset += state.commPointLimit;
      loadComm();
    }
  });

  // ==================== 5. 运行日志 ====================
  async function loadLogs() {
    const filesRes = await api(`/api/logs/files?record=${encodeURIComponent(state.logRecord)}`);
    const files = filesRes.items || [];
    $("#log-file-count").textContent = String(filesRes.total || 0);

    const procSel = $("#log-process");
    const prevProc = procSel.value;
    procSel.innerHTML = '<option value="">全部进程</option>' +
      (filesRes.processes || []).map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("");
    if ([...procSel.options].some((o) => o.value === prevProc)) procSel.value = prevProc;

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
        loadLogs();
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
    const lines = (res.items || []).slice().reverse(); // 按时间正序阅读，最新在底部
    state.logLines = lines;
    $("#log-active-file").textContent = state.logFilename || "全部最新日志";
    $("#log-line-count").textContent = `${lines.length} 条`;

    const stream = $("#log-stream");
    stream.innerHTML = lines.length
      ? lines.map((l) => `<div class="log-line ${esc(l.level || "")}">${esc(l.text)}</div>`).join("")
      : '<div class="empty-hint">当前筛选条件下暂无日志</div>';
    if ($("#log-follow").checked) {
      stream.scrollTop = stream.scrollHeight;
    }
  }

  $$("#log-record-tabs .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.logRecord = btn.dataset.logRecord;
      state.logFilename = "";
      $$("#log-record-tabs .tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      $("#log-direction").disabled = state.logRecord !== "packet";
      if (state.logRecord !== "packet") $("#log-direction").value = "";
      loadLogs();
    });
  });
  ["#log-process", "#log-level", "#log-direction"].forEach((id) => {
    $(id)?.addEventListener("change", loadLogs);
  });
  $("#log-keyword")?.addEventListener("input", loadLogs);
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

  // ==================== 轮询调度与主题同步 ====================
  async function refreshCurrentView() {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      await loadOverview();
      if (state.view === "powerflow") await loadPowerflow();
      else if (state.view === "devices") await loadDevices();
      else if (state.view === "comm") await loadComm();
      else if (state.view === "logs") await loadLogs();
      setConnection(true, "实时在线");
    } catch (err) {
      setConnection(false, err.message || "连接异常");
    } finally {
      state.refreshing = false;
    }
  }

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
    renderTrendChips();
    drawTrendChart();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  window.addEventListener("resize", () => {
    if (state.view === "devices") drawTrendChart();
  });

  // 启动入口
  const initialHash = location.hash.replace(/^#/, "");
  switchView(["home", "powerflow", "devices", "comm", "logs"].includes(initialHash) ? initialHash : "home", false);
  resetTimer();
})();

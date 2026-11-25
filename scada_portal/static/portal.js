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
    pfShowArrows: true,
    pfArrowPaths: [], // [{ key, kind, voltageType, pathEl, length, pKw, dir, speed, scale, arrows: [{el, offset}] }]
    pfAnimRaf: null,
    pfLastFrameTs: 0,
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
    commPointKind: "yc",
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
    if (view === "powerflow" && state.svgLoaded) {
      requestAnimationFrame(() => fitSvgView());
      startPowerflowAnimation();
    } else if (view !== "powerflow") {
      stopPowerflowAnimation();
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
    initPowerflowArrowPaths(svg);
    applySvgThemeColors();
    fitSvgView();

    svg.addEventListener("click", (event) => {
      const target = event.target.closest("use[dev-id], g.mg[dev], text[dev-id]");
      if (!target) return;
      const devId = target.getAttribute("dev-id") || target.getAttribute("dev");
      if (devId) openPowerflowDrawer(devId);
    });
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
      $("#comm-rtus-panel").hidden = state.commTab !== "rtus";
      $("#comm-channels-panel").hidden = state.commTab !== "channels";
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
    if (state.pfData?.flows) updatePowerflowArrowsData(state.pfData.flows);
    renderTrendChips();
    drawTrendChart();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPowerflowAnimation();
    else if (state.view === "powerflow" && state.pfShowArrows) startPowerflowAnimation();
  });

  window.addEventListener("resize", () => {
    if (state.view === "home") updateHeroFlowLines(lastRealtimeEnergy);
    if (state.view === "devices") drawTrendChart();
  });

  if (typeof ResizeObserver === "function" && $("#hero-stage")) {
    new ResizeObserver(() => {
      if (state.view === "home") updateHeroFlowLines(lastRealtimeEnergy);
    }).observe($("#hero-stage"));
  }

  // 启动入口
  const initialHash = location.hash.replace(/^#/, "");
  switchView(["home", "powerflow", "devices", "comm", "logs"].includes(initialHash) ? initialHash : "home", false);
  resetTimer();
})();

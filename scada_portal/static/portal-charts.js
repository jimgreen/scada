/* SCADA 综合门户专业级曲线引擎：自动双 Y 轴 / 归一化、开关量阶梯线、通信断点留白、上下限警戒线、框选放大与离屏缓存 */
(() => {
  "use strict";

  const tr = (text) =>
    window.scadaI18n && typeof window.scadaI18n.translateText === "function"
      ? window.scadaI18n.translateText(String(text))
      : String(text);

  // 离屏静态底图缓存（按 canvas id 隔离），滑动准星时免重算网格与全量曲线
  const baseCache = new Map();

  function computeGapThreshold(points) {
    if (!points || points.length < 3) return 180000;
    const diffs = [];
    for (let i = 1; i < Math.min(points.length, 60); i += 1) {
      const d = points[i][0] - points[i - 1][0];
      if (d > 0) diffs.push(d);
    }
    if (!diffs.length) return 180000;
    diffs.sort((a, b) => a - b);
    const median = diffs[Math.floor(diffs.length / 2)];
    return Math.max(120000, Math.min(900000, median * 8));
  }

  function assignAxes(visible, axisMode) {
    if (axisMode === "norm" || axisMode === "single" || visible.length <= 1) {
      return {
        useRight: false,
        leftUnit: visible[0]?.unit || "",
        rightUnit: "",
        sideOf: () => "left",
      };
    }
    const unitOrder = [];
    for (const s of visible) {
      const u = s.unit || (s.kind === "yx" ? "state" : "");
      if (!unitOrder.includes(u)) unitOrder.push(u);
    }
    if (unitOrder.length <= 1) {
      return {
        useRight: false,
        leftUnit: unitOrder[0] || "",
        rightUnit: "",
        sideOf: () => "left",
      };
    }
    const primary = unitOrder[0];
    const secondary = unitOrder[1];
    return {
      useRight: true,
      leftUnit: primary,
      rightUnit: unitOrder.length > 2 ? `${secondary}…` : secondary,
      sideOf: (s) => ((s.unit || (s.kind === "yx" ? "state" : "")) === primary ? "left" : "right"),
    };
  }

  function calcDomain(seriesList, minT, maxT, { includeLimits = false } = {}) {
    let minV = Infinity;
    let maxV = -Infinity;
    for (const s of seriesList) {
      for (const [t, v] of s.points) {
        if (t < minT || t > maxT) continue;
        if (v < minV) minV = v;
        if (v > maxV) maxV = v;
      }
      if (includeLimits) {
        if (s.max_limit != null && Number.isFinite(Number(s.max_limit))) {
          maxV = Math.max(maxV, Number(s.max_limit));
        }
        if (s.min_limit != null && Number.isFinite(Number(s.min_limit))) {
          minV = Math.min(minV, Number(s.min_limit));
        }
      }
    }
    if (!Number.isFinite(minV) || !Number.isFinite(maxV)) {
      return { minV: 0, maxV: 1 };
    }
    if (maxV <= minV) {
      const delta = Math.max(1, Math.abs(minV) * 0.1);
      return { minV: minV - delta, maxV: maxV + delta };
    }
    const margin = (maxV - minV) * 0.08;
    return { minV: minV - margin, maxV: maxV + margin };
  }

  /**
   * 渲染设备趋势曲线，返回当前绘图元信息供拖拽框选放大与准星反查。
   */
  function drawTrend(opts) {
    const {
      canvas,
      wrap,
      palette,
      allSeries,
      hiddenSet,
      axisMode = "dual",
      zoomRange = null,
      hoverX = null,
      brushRange = null,
    } = opts;
    if (!canvas || !wrap) return null;
    const rect = wrap.getBoundingClientRect();
    if (rect.width < 40 || rect.height < 40) return null;

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

    const visible = (allSeries || [])
      .map((s, idx) => ({ ...s, color: palette.colors[idx % palette.colors.length] }))
      .filter((s) => !hiddenSet.has(s.key || s.metric) && s.points && s.points.length);

    if (!visible.length) {
      ctx.fillStyle = palette.axis;
      ctx.font = "13px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(tr("暂无可绘制的日曲线采样数据"), rect.width / 2, rect.height / 2);
      return null;
    }

    const axisInfo = assignAxes(visible, axisMode);
    const pad = { top: 18, right: axisInfo.useRight ? 58 : 20, bottom: 28, left: 60 };
    const plotW = Math.max(10, rect.width - pad.left - pad.right);
    const plotH = Math.max(10, rect.height - pad.top - pad.bottom);

    let fullMinT = Infinity;
    let fullMaxT = -Infinity;
    for (const s of visible) {
      for (const [t] of s.points) {
        if (t < fullMinT) fullMinT = t;
        if (t > fullMaxT) fullMaxT = t;
      }
    }
    let minT = zoomRange ? zoomRange[0] : fullMinT;
    let maxT = zoomRange ? zoomRange[1] : fullMaxT;
    if (maxT <= minT) {
      minT -= 60000;
      maxT += 60000;
    }

    const leftSeries = visible.filter((s) => axisInfo.sideOf(s) === "left");
    const rightSeries = axisInfo.useRight ? visible.filter((s) => axisInfo.sideOf(s) === "right") : [];

    // 归一化模式统计各曲线自身范围
    const normBounds = new Map();
    if (axisMode === "norm") {
      for (const s of visible) {
        const d = calcDomain([s], minT, maxT);
        normBounds.set(s, d);
      }
    }

    const showLimits = visible.length === 1 && axisMode !== "norm";
    const leftDom = axisMode === "norm" ? { minV: 0, maxV: 100 } : calcDomain(leftSeries, minT, maxT, { includeLimits: showLimits });
    const rightDom = axisInfo.useRight ? calcDomain(rightSeries, minT, maxT) : null;

    const xFor = (t) => pad.left + ((t - minT) / (maxT - minT)) * plotW;
    const yForSeries = (s, v) => {
      if (axisMode === "norm") {
        const b = normBounds.get(s) || { minV: 0, maxV: 1 };
        const span = Math.max(1e-9, b.maxV - b.minV);
        const pct = ((v - b.minV) / span) * 100;
        return pad.top + (1 - pct / 100) * plotH;
      }
      const dom = axisInfo.sideOf(s) === "right" && rightDom ? rightDom : leftDom;
      return pad.top + (1 - (v - dom.minV) / Math.max(1e-9, dom.maxV - dom.minV)) * plotH;
    };

    // 检查静态底图离屏缓存，滑动十字准星时直接复用
    const cacheKey = [
      targetW,
      targetH,
      axisMode,
      minT,
      maxT,
      palette.axis,
      palette.grid,
      document.documentElement.lang,
      visible.map((s) => `${s.key || s.metric}:${s.points.length}:${s.points[s.points.length - 1][0]}:${s.points[s.points.length - 1][1]}`).join("|"),
    ].join("#");

    const cid = canvas.id || "default-trend";
    let cached = baseCache.get(cid);
    if (!cached || cached.key !== cacheKey) {
      const off = document.createElement("canvas");
      off.width = targetW;
      off.height = targetH;
      const octx = off.getContext("2d");
      octx.setTransform(dpr, 0, 0, dpr, 0, 0);

      // 1. 网格线与左 Y 轴刻度
      octx.strokeStyle = palette.grid;
      octx.lineWidth = 1;
      octx.setLineDash([3, 3]);
      octx.fillStyle = palette.axis;
      octx.font = '11px "JetBrains Mono", Consolas, monospace';
      octx.textAlign = "right";
      octx.textBaseline = "middle";
      const yTicks = 5;
      for (let i = 0; i <= yTicks; i += 1) {
        const ratio = i / yTicks;
        const y = pad.top + ratio * plotH;
        const val = leftDom.maxV - ratio * (leftDom.maxV - leftDom.minV);
        octx.beginPath();
        octx.moveTo(pad.left, y);
        octx.lineTo(pad.left + plotW, y);
        octx.stroke();
        const txt = axisMode === "norm" ? `${Math.round(val)}%` : val.toFixed(Math.abs(val) >= 100 ? 1 : 2);
        octx.fillText(txt, pad.left - 8, y);
      }

      // 左轴单位标签
      const leftLabel = axisMode === "norm" ? "%" : axisInfo.leftUnit;
      if (leftLabel) {
        octx.textAlign = "left";
        octx.textBaseline = "top";
        octx.fillStyle = palette.axis;
        octx.fillText(leftLabel, pad.left + 4, 2);
      }

      // 右 Y 轴刻度（双轴模式）
      if (axisInfo.useRight && rightDom) {
        octx.textAlign = "left";
        octx.textBaseline = "middle";
        octx.fillStyle = rightSeries[0]?.color || palette.axis;
        for (let i = 0; i <= yTicks; i += 1) {
          const ratio = i / yTicks;
          const y = pad.top + ratio * plotH;
          const val = rightDom.maxV - ratio * (rightDom.maxV - rightDom.minV);
          octx.fillText(val.toFixed(Math.abs(val) >= 100 ? 1 : 2), pad.left + plotW + 8, y);
        }
        if (axisInfo.rightUnit) {
          octx.textAlign = "right";
          octx.textBaseline = "top";
          octx.fillText(axisInfo.rightUnit, pad.left + plotW - 4, 2);
        }
      }

      // 2. X 轴时间刻度
      octx.setLineDash([]);
      octx.fillStyle = palette.axis;
      octx.textAlign = "center";
      octx.textBaseline = "top";
      const xTicks = Math.max(2, Math.min(6, Math.floor(plotW / 110)));
      for (let i = 0; i <= xTicks; i += 1) {
        const t = minT + (i / xTicks) * (maxT - minT);
        const x = xFor(t);
        const d = new Date(t);
        const txt = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
        octx.fillText(txt, x, pad.top + plotH + 8);
      }

      // 3. 单曲线上下限警戒虚线
      if (showLimits) {
        const s0 = visible[0];
        const drawLimitLine = (val, color, tag) => {
          if (val == null || !Number.isFinite(Number(val))) return;
          const num = Number(val);
          const ly = yForSeries(s0, num);
          if (ly < pad.top || ly > pad.top + plotH) return;
          octx.save();
          octx.strokeStyle = color;
          octx.lineWidth = 1.1;
          octx.setLineDash([6, 4]);
          octx.beginPath();
          octx.moveTo(pad.left, ly);
          octx.lineTo(pad.left + plotW, ly);
          octx.stroke();
          octx.setLineDash([]);
          octx.fillStyle = color;
          octx.font = '10px "JetBrains Mono", Consolas, monospace';
          octx.textAlign = "right";
          octx.textBaseline = "bottom";
          octx.fillText(`${tag} ${num}`, pad.left + plotW - 4, ly - 2);
          octx.restore();
        };
        drawLimitLine(s0.max_limit, "rgba(248, 113, 113, 0.85)", "MAX");
        drawLimitLine(s0.min_limit, "rgba(251, 191, 36, 0.85)", "MIN");
      }

      // 4. 裁剪绘图区并绘制各条曲线（支持开关量阶梯线 + 通信中断留白 Gap + 昨日同比虚线）
      octx.save();
      octx.beginPath();
      octx.rect(pad.left, pad.top, plotW, plotH);
      octx.clip();

      for (const s of visible) {
        const isStep = s.kind === "yx";
        const gapMs = computeGapThreshold(s.points);
        octx.strokeStyle = s.color;
        octx.fillStyle = s.color;
        octx.lineWidth = s.dashed ? 1.6 : 2;
        octx.setLineDash(s.dashed ? [5, 4] : []);
        octx.beginPath();
        let prevT = null;
        let prevY = null;
        let drawn = 0;
        for (const [t, v] of s.points) {
          if (t < minT - gapMs || t > maxT + gapMs) continue;
          const x = xFor(t);
          const y = yForSeries(s, v);
          const isGap = prevT !== null && (t - prevT) > gapMs;
          if (prevT === null || isGap) {
            octx.moveTo(x, y);
          } else if (isStep) {
            octx.lineTo(x, prevY);
            octx.lineTo(x, y);
          } else {
            octx.lineTo(x, y);
          }
          prevT = t;
          prevY = y;
          drawn += 1;
        }
        octx.stroke();
        octx.setLineDash([]);

        if (drawn <= 2) {
          for (const [t, v] of s.points) {
            if (t < minT || t > maxT) continue;
            octx.beginPath();
            octx.arc(xFor(t), yForSeries(s, v), 3.5, 0, Math.PI * 2);
            octx.fill();
          }
        }
      }
      octx.restore();

      cached = { key: cacheKey, canvas: off };
      baseCache.set(cid, cached);
    }

    // 直接贴上离屏底图
    ctx.drawImage(cached.canvas, 0, 0, rect.width, rect.height);

    // 5. 拖拽框选区域高亮
    if (brushRange && Math.abs(brushRange[1] - brushRange[0]) >= 6) {
      const bx1 = Math.max(pad.left, Math.min(pad.left + plotW, Math.min(brushRange[0], brushRange[1])));
      const bx2 = Math.max(pad.left, Math.min(pad.left + plotW, Math.max(brushRange[0], brushRange[1])));
      ctx.fillStyle = "rgba(56, 189, 248, 0.16)";
      ctx.strokeStyle = "rgba(56, 189, 248, 0.7)";
      ctx.lineWidth = 1;
      ctx.fillRect(bx1, pad.top, bx2 - bx1, plotH);
      ctx.strokeRect(bx1, pad.top, bx2 - bx1, plotH);
    }

    // 6. 悬停十字准星与三语提示框
    if (hoverX !== null && hoverX >= pad.left && hoverX <= pad.left + plotW) {
      const hoverT = minT + ((hoverX - pad.left) / plotW) * (maxT - minT);
      ctx.strokeStyle = palette.crosshair;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(hoverX, pad.top);
      ctx.lineTo(hoverX, pad.top + plotH);
      ctx.stroke();
      ctx.setLineDash([]);

      const rows = [];
      let snapT = hoverT;
      for (const s of visible) {
        let best = null;
        let bestDist = Infinity;
        for (const pt of s.points) {
          if (pt[0] < minT || pt[0] > maxT) continue;
          const dist = Math.abs(pt[0] - hoverT);
          if (dist < bestDist) {
            bestDist = dist;
            best = pt;
          }
        }
        if (!best) continue;
        snapT = best[0];
        const px = xFor(best[0]);
        const py = yForSeries(s, best[1]);
        ctx.fillStyle = s.color;
        ctx.beginPath();
        ctx.arc(px, py, 4, 0, Math.PI * 2);
        ctx.fill();
        const label = tr(s.label || s.metric);
        const valTxt = s.kind === "yx" ? (best[1] ? "1" : "0") : best[1].toFixed(2);
        rows.push({ color: s.color, text: `${label}: ${valTxt} ${s.unit || ""}`.trim() });
      }

      if (rows.length) {
        const d = new Date(snapT);
        const header = d.toLocaleTimeString(document.documentElement.lang || "zh-CN", { hour12: false });
        ctx.font = '11px "JetBrains Mono", Consolas, monospace';
        const maxTextW = Math.max(ctx.measureText(header).width, ...rows.map((r) => ctx.measureText(r.text).width + 14));
        const boxW = maxTextW + 22;
        const boxH = 22 + rows.length * 17 + 6;
        const bx = hoverX + 12 + boxW > rect.width ? hoverX - 12 - boxW : hoverX + 12;
        const by = pad.top + 6;

        ctx.fillStyle = palette.tooltipBg;
        ctx.strokeStyle = palette.tooltipBorder;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.roundRect(bx, by, boxW, boxH, 6);
        ctx.fill();
        ctx.stroke();

        ctx.fillStyle = palette.tooltipText;
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.fillText(header, bx + 10, by + 6);
        rows.forEach((r, idx) => {
          const ry = by + 23 + idx * 17;
          ctx.fillStyle = r.color;
          ctx.beginPath();
          ctx.arc(bx + 13, ry + 5, 3.5, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = palette.tooltipText;
          ctx.fillText(r.text, bx + 22, ry);
        });
      }
    }

    return { pad, plotW, plotH, minT, maxT, fullMinT, fullMaxT };
  }

  window.ScadaCharts = {
    drawTrend,
    clearCache: () => baseCache.clear(),
  };
})();

/* Real-time trends for the selected point keys, independent of table pagination. */
(() => {
  'use strict';

  const LIMIT = 24;
  const DEFAULT_INTERVAL_SECONDS = 1;
  const MIN_INTERVAL_SECONDS = 1;
  const MAX_INTERVAL_SECONDS = 3600;
  const DEFAULT_RETENTION_POINTS = 10000;
  const MIN_RETENTION_POINTS = 1;
  const MAX_RETENTION_POINTS = 100000;
  const DAY = 86400000;
  const HISTORY_URL = '/api/point-trend/history';
  // scientific-dashboard.md 色盲友好序列配色（浅色 / 暗黑）
  const LIGHT_COLORS = ['#2563EB', '#D97706', '#0D9488', '#DC2626', '#7C3AED', '#64748B', '#0891B2', '#DB2777'];
  const DARK_COLORS = ['#38BDF8', '#FBBF24', '#34D399', '#FB7185', '#A78BFA', '#94A3B8', '#22D3EE', '#F472B6'];
  const COLORS = LIGHT_COLORS;
  const TIME_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400];
  const FULL_TIME = new Intl.DateTimeFormat(undefined, {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  const TIME = new Intl.DateTimeFormat(undefined, {hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  const AXIS_NAMES = {left:'左轴', right:'右轴'};
  const DARK_THEMES = new Set(['precision-dark', 'sci-fi-cyan', 'sci-fi-purple', 'sci-fi-matrix', 'glassmorphism']);
  const isDarkTheme = () => document.documentElement.dataset.colorScheme === 'dark' || DARK_THEMES.has(document.documentElement.dataset.theme || '');
  const themeAxisColors = () => {
    const theme = document.documentElement.dataset.theme;
    if (theme === 'sci-fi-cyan') return {left:'#00E5FF', right:'#FFB020'};
    if (theme === 'sci-fi-purple') return {left:'#C084FC', right:'#38BDF8'};
    if (theme === 'sci-fi-matrix') return {left:'#00E676', right:'#FBBF24'};
    if (theme === 'editorial') return {left:'#9F1239', right:'#B45309'};
    return isDarkTheme() ? {left:'#38BDF8', right:'#FBBF24'} : {left:'#2563EB', right:'#D97706'};
  };
  const AXIS_COLORS = {left:'#2563EB', right:'#D97706'};
  let nextMount = 0;

  function make(tag, attributes = {}) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    return node;
  }

  function label(text, control) {
    const node = make('label');
    node.append(text, control);
    return node;
  }

  function numberInput(value, min, max, step, placeholder = '') {
    const input = make('input', {type:'number', step, placeholder});
    if (min !== null) input.min = min;
    if (max !== null) input.max = max;
    input.value = value;
    return input;
  }

  function select(pairs) {
    const node = make('select');
    for (const [value, text] of pairs) node.add(new Option(text, value));
    return node;
  }

  function pointKey(definition) {
    if ('measurement_id' in definition) return {measurement_id:String(definition.measurement_id)};
    const {kind, pnt_no} = definition;
    if ('channel_id' in definition) return {kind, channel_id:String(definition.channel_id), pnt_no:String(pnt_no)};
    return {kind, rtu_id:String(definition.rtu_id), pnt_no:String(pnt_no)};
  }

  function niceStep(span, divisions) {
    const raw = span / divisions;
    const base = 10 ** Math.floor(Math.log10(raw));
    if (!(base > 0) || !Number.isFinite(base)) return 1;
    return ([1, 2, 2.5, 5, 10].find(value => value >= raw / base) ?? 10) * base;
  }

  function formatValue(value) {
    if (value !== 0 && (Math.abs(value) < 0.0001 || Math.abs(value) >= 10000000)) return value.toExponential(3);
    return Number(value.toPrecision(5)).toLocaleString(undefined, {maximumSignificantDigits:5});
  }

  function translateText(text) {
    return window.scadaI18n?.t ? window.scadaI18n.t(text, text) : text;
  }

  // 按给定最大像素宽度安全截断 Canvas 文本，避免挤压变形或溢出遮挡。
  function fitCanvasText(context, text, maxWidth) {
    if (maxWidth <= 8) return '';
    if (context.measureText(text).width <= maxWidth) return text;
    const ellipsis = '…';
    const ellipsisW = context.measureText(ellipsis).width;
    if (ellipsisW >= maxWidth) return '';
    let low = 0, high = text.length;
    while (low < high) {
      const mid = (low + high + 1) >>> 1;
      if (context.measureText(text.slice(0, mid)).width + ellipsisW <= maxWidth) low = mid;
      else high = mid - 1;
    }
    return low > 0 ? text.slice(0, low) + ellipsis : ellipsis;
  }

  // 绘制曲线标题左侧的线型图标（显示态为曲线彩色实线/阶跃线，隐藏态为灰色虚线）。
  function drawSeriesIcon(context, x, y, color, digital, thickness = 2, hidden = false) {
    const iconW = 16;
    const lw = hidden ? 1.5 : Math.min(2.6, Math.max(1.6, Number(thickness) || 2));
    context.save();
    context.strokeStyle = color;
    context.fillStyle = color;
    context.lineWidth = lw;
    context.lineCap = 'round';
    context.lineJoin = 'round';
    context.setLineDash(hidden ? [3, 2.5] : []);
    context.beginPath();
    if (digital) {
      context.moveTo(x, y + 3.5);
      context.lineTo(x + iconW * 0.45, y + 3.5);
      context.lineTo(x + iconW * 0.45, y - 3.5);
      context.lineTo(x + iconW, y - 3.5);
      context.stroke();
    } else {
      context.moveTo(x, y);
      context.lineTo(x + iconW, y);
      context.stroke();
      if (!hidden) {
        context.beginPath();
        context.arc(x + iconW / 2, y, Math.min(3, lw + 0.8), 0, Math.PI * 2);
        context.fill();
      }
    }
    context.restore();
  }

  // 构造单条曲线的图标标题文本（显示态标明 [左]/[右]，隐藏态标明 [隐藏]）。
  function buildSeriesTitle(item) {
    const stateTag = item.visible.checked
      ? translateText(item.axis.value === 'right' ? '右' : '左')
      : translateText('隐藏');
    const unitPart = item.unit && !item.name.includes(item.unit) ? ' (' + item.unit + ')' : '';
    return item.name + unitPart + ' [' + stateTag + ']';
  }

  // 规划顶部曲线图标标题区排版（流式折行，超出最大行数时以 +N 汇总）。
  function layoutSeriesHeader(context, items, width, maxRows) {
    if (!items.length || maxRows <= 0 || width < 140) return {rows:[], height:0};
    const padX = 10;
    const iconW = 16;
    const iconGap = 5;
    const itemGap = 14;
    const rowH = 18;
    const maxLineW = width - padX * 2;
    const maxItemTextW = Math.max(72, Math.min(240, Math.floor(maxLineW * 0.48) - iconW - iconGap));
    const entries = items.map(item => {
      const label = fitCanvasText(context, buildSeriesTitle(item), maxItemTextW);
      const textW = context.measureText(label).width;
      return {item, label, width: iconW + iconGap + textW};
    });
    const rows = [];
    let current = [];
    let lineW = 0;
    let index = 0;
    while (index < entries.length) {
      const entry = entries[index];
      const nextW = current.length ? lineW + itemGap + entry.width : entry.width;
      if (!current.length || nextW <= maxLineW) {
        current.push(entry);
        lineW = nextW;
        index++;
        continue;
      }
      if (rows.length + 1 < maxRows) {
        rows.push(current);
        current = [entry];
        lineW = entry.width;
        index++;
        continue;
      }
      // 达到最大行数：腾出末尾空间显示 +N 剩余曲线计数。
      let hiddenCount = entries.length - index;
      while (current.length > 0) {
        const moreText = '+' + hiddenCount;
        const moreW = context.measureText(moreText).width + 10;
        const usedW = current.reduce((sum, e, i) => sum + e.width + (i ? itemGap : 0), 0);
        if (usedW + itemGap + moreW <= maxLineW) {
          current.push({more: moreText, width: moreW});
          break;
        }
        current.pop();
        hiddenCount++;
      }
      if (!current.length) {
        const moreText = '+' + hiddenCount;
        current.push({more: moreText, width: context.measureText(moreText).width + 10});
      }
      break;
    }
    if (current.length) rows.push(current);
    return {rows, height: rows.length ? 6 + rows.length * rowH + 4 : 0, rowH, padX, iconW, iconGap, itemGap};
  }

  function nearestSample(samples, time) {
    let first = 0, last = samples.length;
    while (first < last) {
      const mid = (first + last) >>> 1;
      if (samples[mid].time < time) first = mid + 1;
      else last = mid;
    }
    const before = samples[first - 1], after = samples[first];
    if (before && (!after || time - before.time <= after.time - time)) return before;
    return after;
  }

  function localTime(time) {
    const date = new Date(time);
    const offset = date.getTimezoneOffset() * 60000;
    return new Date(time - offset).toISOString().slice(0, 19);
  }

  function makeAxis(side) {
    const section = make('fieldset');
    const title = make('legend');
    title.textContent = AXIS_NAMES[side];
    title.style.color = AXIS_COLORS[side];
    const show = make('input', {type:'checkbox', 'aria-label':'显示' + AXIS_NAMES[side]});
    show.checked = true;
    const mode = select([['auto', '自动范围'], ['manual', '自定义范围']]);
    const min = numberInput('', null, null, 'any', '最小');
    const max = numberInput('', null, null, 'any', '最大');
    const step = numberInput('', 0, null, 'any', '自动');
    min.disabled = max.disabled = true;
    section.append(title, label('显示', show), label('范围', mode), label('下限', min), label('上限', max), label('刻度间隔', step));
    return {section, show, mode, min, max, step};
  }

  function axisScale(control, low, high, divisions) {
    const manual = control.mode.value === 'manual';
    const manualLow = control.min.valueAsNumber;
    const manualHigh = control.max.valueAsNumber;
    let error = manual && (!Number.isFinite(manualLow) || !Number.isFinite(manualHigh) ||
                            manualLow >= manualHigh || !Number.isFinite(manualHigh - manualLow));
    const fixed = manual && !error;
    if (fixed) {
      low = manualLow;
      high = manualHigh;
    } else if (!Number.isFinite(low) || !Number.isFinite(high)) {
      low = 0;
      high = 1;
    } else if (low === high) {
      const margin = Math.abs(low) * 0.05 || 1;
      low -= margin;
      high += margin;
    }

    const span = high - low;
    const configuredStep = control.step.valueAsNumber;
    const customStep = Number.isFinite(configuredStep) && configuredStep > 0 && span / configuredStep <= 24;
    if (control.step.value !== '' && !customStep) error = true;
    const step = customStep ? configuredStep : niceStep(span, divisions);
    if (!fixed) {
      low = Math.floor(low / step) * step;
      high = Math.ceil(high / step) * step;
    }
    const ticks = [];
    const first = Math.ceil(low / step - 1e-10);
    const last = Math.floor(high / step + 1e-10);
    for (let offset = 0; offset <= last - first && offset <= 26; offset++) ticks.push((first + offset) * step);
    return {low, high, ticks, error};
  }

  function mount(container, {management = false, onChange} = {}) {
    const mountId = String(++nextMount);
    const panel = make('details', {class:'point-trend'});
    panel.open = management;
    panel.innerHTML =
      '<summary><strong>实时曲线</strong><span class="point-trend-count"></span><span class="point-trend-meta"></span></summary>' +
      '<div class="point-trend-body">' +
        '<div class="point-trend-toolbar"></div>' +
        '<div class="point-trend-chart" tabindex="0"><canvas role="img" aria-label="已选测点实时值双轴曲线"></canvas>' +
          '<div class="point-trend-empty"></div><div class="point-trend-cursor" hidden></div>' +
          '<div class="point-trend-tooltip" hidden></div></div>' +
        '<p class="point-trend-error" role="alert" hidden></p>' +
      '</div>';
    container.append(panel);
    container.classList.add('point-trend-host');
    const find = selector => panel.querySelector(selector);
    const summary = find('.point-trend-count');
    const metaStatus = find('.point-trend-meta');
    const toolbar = find('.point-trend-toolbar');
    const chart = find('.point-trend-chart');
    const canvas = find('canvas');
    const empty = find('.point-trend-empty');
    const cursor = find('.point-trend-cursor');
    const tooltip = find('.point-trend-tooltip');
    const warning = find('.point-trend-error');

    // 将纵轴设置与曲线设置移入独立模态弹窗，通过右键菜单按需唤出，不占用主页面空间。
    function makeSettingDialog(titleText, bodyNode, extraClass = '') {
      const dialog = make('dialog', {class:('point-trend-dialog ' + extraClass).trim()});
      const header = make('div', {class:'point-trend-dialog-head'});
      const heading = make('h3');
      heading.textContent = titleText;
      const closeBtn = make('button', {type:'button', class:'point-trend-dialog-close', 'aria-label':'关闭'});
      closeBtn.textContent = '×';
      header.append(heading, closeBtn);
      const footer = make('div', {class:'point-trend-dialog-foot'});
      const doneBtn = make('button', {type:'button', class:'point-trend-dialog-done'});
      doneBtn.textContent = '完成';
      footer.append(doneBtn);
      dialog.append(header, bodyNode, footer);
      const close = () => { if (dialog.open) dialog.close(); };
      closeBtn.addEventListener('click', close);
      doneBtn.addEventListener('click', close);
      dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
      document.body.append(dialog);
      return dialog;
    }

    const axesBody = make('div', {class:'point-trend-axis-body'});
    const axes = {left:makeAxis('left'), right:makeAxis('right')};
    axesBody.append(axes.left.section, axes.right.section);
    const axesDialog = makeSettingDialog('纵轴设置', axesBody, 'point-trend-dialog-axis');

    const curves = make('div', {class:'point-trend-series', 'aria-label':'曲线设置'});
    const seriesDialog = makeSettingDialog('曲线设置', curves, 'point-trend-dialog-series');

    const mode = select([['rolling', '跟随当前时刻'], ['fixed', '固定起止']]);
    const length = numberInput(10, 1, 1440, 1);
    const span = label('窗口（分钟）', length);
    const alignment = select([['auto','自动'], ['5','5 秒'], ['15','15 秒'], ['30','30 秒'], ['60','60 秒']]);
    const alignmentLabel = label('对齐步长', alignment);
    const from = make('input', {type:'datetime-local', step:'1'});
    const until = make('input', {type:'datetime-local', step:'1'});
    const fromLabel = label('开始', from);
    const untilLabel = label('结束', until);
    fromLabel.hidden = untilLabel.hidden = true;
    const zoomInBtn = make('button', {type:'button', title:'放大时间窗口'});
    zoomInBtn.textContent = '放大';
    const zoomOutBtn = make('button', {type:'button', title:'缩小时间窗口'});
    zoomOutBtn.textContent = '缩小';
    const resetZoomBtn = make('button', {type:'button', title:'重置为跟随当前 10 分钟窗口'});
    resetZoomBtn.textContent = '重置';
    const exportCsvBtn = make('button', {type:'button', title:'导出当前时段采样数据为 CSV'});
    exportCsvBtn.textContent = '导出 CSV';
    toolbar.append(label('时间区间', mode), span, alignmentLabel, fromLabel, untilLabel, zoomInBtn, zoomOutBtn, resetZoomBtn, exportCsvBtn);

    const series = new Map();
    let recordingKeys = new Set();
    let historyTimer, rollTimer, frame, lastSample = 0, colorIndex = 0;
    let duration = 600000, fixed = null;
    let databaseChanged = false, pageHidden = false, wasReadingActive = false;
    let ready = false, reading = false, restoring = false, pendingSaves = 0;
    let settingsVersion = 0, saveQueue = Promise.resolve(), queuedSettings = '', serverSettings = '', serverRevision = '';
    let sourceOpen = false, sourceAxesOpen = false;
    let plotRange = null;
    let headerHitboxes = [];
    let openSelect = null, deferredPublish = false;
    let fetchError = '', saveError = '', settingsError = '', selectionError = '';
    const isSelectPopupActive = () => Boolean(openSelect && openSelect.isConnected);
    const releaseOpenSelect = () => {
      if (!openSelect) return;
      openSelect = null;
      if (deferredPublish) {
        deferredPublish = false;
        scheduleDraw();
        publishChange();
      }
      if (!reading && !pendingSaves) queueHistory(0);
    };
    document.addEventListener('pointerdown', event => {
      if (event.target?.tagName === 'SELECT') {
        openSelect = event.target;
      } else if (openSelect && event.target !== openSelect) {
        releaseOpenSelect();
      }
    }, true);
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        releaseOpenSelect();
      } else if (event.target?.tagName === 'SELECT' && [' ', 'Enter', 'ArrowDown', 'ArrowUp'].includes(event.key)) {
        openSelect = event.target;
      }
    }, true);
    document.addEventListener('change', event => {
      if (event.target?.tagName === 'SELECT') releaseOpenSelect();
    }, true);
    document.addEventListener('blur', event => {
      if (event.target === openSelect) releaseOpenSelect();
    }, true);
    const pageVisible = () => !pageHidden && !document.hidden && panel.getClientRects().length > 0;
    const canDraw = () => panel.open && pageVisible();
    const canRead = () => (management || panel.open) && pageVisible();
    const pollDelayMs = () => {
      if (!series.size) return 2000;
      let fastest = MAX_INTERVAL_SECONDS;
      for (const item of series.values()) {
        fastest = Math.min(fastest, item.intervalSeconds || DEFAULT_INTERVAL_SECONDS);
      }
      return Math.max(1000, fastest * 1000);
    };

    function itemDisplayState(item) {
      if (!item.visible.checked) return 'hidden';
      return item.axis.value === 'right' ? 'right' : 'left';
    }

    function applyDisplayState(item, displayState) {
      if (displayState === 'hidden') {
        item.visible.checked = false;
      } else {
        item.visible.checked = true;
        item.axis.value = displayState === 'right' ? 'right' : 'left';
      }
      if (item.displaySelect) item.displaySelect.value = itemDisplayState(item);
    }

    function publishChange() {
      if (typeof onChange !== 'function' || restoring) return;
      const items = [...series.values()];
      const minIntervalSeconds = items.length
        ? Math.min(...items.map(item => item.intervalSeconds || DEFAULT_INTERVAL_SECONDS))
        : DEFAULT_INTERVAL_SECONDS;
      onChange({
        ready:ready && !databaseChanged, error:[selectionError, settingsError, fetchError, saveError].filter(Boolean).join('；'),
        saving:pendingSaves > 0,
        minIntervalSeconds,
        lastSampleTime:lastSample || null,
        series:items.map(item => {
          const latest = item.samples.at(-1);
          return {key:item.key, name:item.name, kind:item.kind, point:{...item.point}, unit:item.unit,
                  axis:item.axis.value, visible:item.visible.checked, displayState:itemDisplayState(item),
                  color:item.color.value, width:item.thickness,
                  intervalSeconds:item.intervalSeconds, retentionPoints:item.retentionPoints,
                  value:latest?.value ?? null, invalid:latest?.invalid ?? false, quality:latest?.quality ?? null,
                  timeMs:latest?.time ?? null, sourceTimeMs:latest?.sourceTime ?? null, recording:recordingKeys.has(item.key)};
        }),
      });
    }

    function timeRange(now = Date.now()) {
      if (fixed && mode.value === 'fixed') return fixed;
      const target = Math.min(60000, Math.max(1000, duration / 40));
      const quantum = (alignment.value === 'auto'
        ? TIME_STEPS.find(seconds => seconds * 1000 >= target)
        : Number(alignment.value)) * 1000;
      const end = Math.ceil(now / quantum) * quantum;
      return {start:end - duration, end};
    }

    function updateStatus(axisErrors, start, end) {
      const message = [selectionError, settingsError, ...axisErrors, fetchError, saveError].filter(Boolean).join('；');
      if (warning.textContent !== message) warning.textContent = message;
      warning.hidden = !message;
      const rangeText = FULL_TIME.format(start) + ' – ' + FULL_TIME.format(end);
      metaStatus.textContent = lastSample
        ? rangeText + ' · 采样 ' + TIME.format(lastSample)
        : rangeText;
      for (const item of series.values()) {
        const latest = item.samples.at(-1);
        const text = latest?.value != null
          ? formatValue(latest.value) + (latest.invalid ? '（无效）' : '') : '—';
        item.latest.textContent = text;
      }
    }

    function draw() {
      frame = 0;
      if (!canDraw()) return;
      const {start, end} = timeRange();
      const allSeries = [...series.values()];
      const visible = allSeries.filter(item => item.visible.checked);
      const datasets = visible.map(item => ({item, data:item.samples.filter(point => point.time >= start && point.time <= end)}));
      empty.textContent = !series.size ? (management ? '请在测点页面加入实时曲线' : '右键表格中的测点行设为采样点')
        : !visible.length ? '当前曲线均已隐藏'
        : datasets.some(({data}) => data.some(point => point.value !== null)) ? '' : '当前时段暂无样本';
      empty.hidden = !empty.textContent;
      const context = canvas.getContext('2d');
      const width = chart.clientWidth, height = chart.clientHeight;
      if (!context || width < 100 || height < 80) return;
      const scale = Math.min(window.devicePixelRatio || 1, 2);
      const pixelsWide = Math.round(width * scale), pixelsHigh = Math.round(height * scale);
      if (canvas.width !== pixelsWide || canvas.height !== pixelsHigh) {
        canvas.width = pixelsWide;
        canvas.height = pixelsHigh;
      }
      context.setTransform(scale, 0, 0, scale, 0, 0);
      context.clearRect(0, 0, width, height);
      const dark = isDarkTheme();
      const gridColor = dark ? 'rgba(255, 255, 255, 0.08)' : '#E2E8F0';
      const axisLineColor = dark ? 'rgba(148, 163, 184, 0.35)' : '#CBD5E1';
      const tickTextColor = dark ? '#94A3B8' : '#64748B';
      const titleTextColor = dark ? '#E2E8F0' : '#1E293B';
      const headerSepColor = dark ? 'rgba(148, 163, 184, 0.16)' : 'rgba(148, 163, 184, 0.28)';
      const axisPalette = themeAxisColors();
      const tickFont = '11px "JetBrains Mono", "Segoe UI", "Roboto", "Microsoft YaHei", sans-serif';
      const titleFont = '500 11px "Segoe UI", "Roboto", "Microsoft YaHei", sans-serif';

      // 1. 顶部全部已选曲线图标标题区排版（含已隐藏曲线，便于点击恢复），按画布高度自适应行数。
      context.font = titleFont;
      const maxHeaderRows = height < 150 ? 0 : height < 215 ? 1 : 2;
      let header = layoutSeriesHeader(context, allSeries, width, maxHeaderRows);
      const showXAxisTitle = height >= 135;
      const bottomPad = showXAxisTitle ? 40 : 26;
      const axisTitleBand = 24;
      if (header.height > 0 && height - header.height - axisTitleBand - bottomPad < 52) {
        header = layoutSeriesHeader(context, allSeries, width, Math.max(0, maxHeaderRows - 1));
        if (header.height > 0 && height - header.height - axisTitleBand - bottomPad < 48) {
          header = {rows:[], height:0};
        }
      }

      // 2. 结合可用绘图区高度计算各纵轴刻度与自适应左右留白，确保纵坐标数字与单位完整展示。
      context.font = tickFont;
      const top = header.height + axisTitleBand;
      const bottom = height - bottomPad;
      const plotHeight = Math.max(40, bottom - top);
      const domains = {}, units = {}, errors = [];
      for (const [side, control] of Object.entries(axes)) {
        let low = Infinity, high = -Infinity;
        const axisUnits = new Set();
        for (const {item, data} of datasets) {
          if (item.axis.value !== side) continue;
          axisUnits.add(item.unit);
          for (const point of data) {
            if (point.value === null) continue;
            low = Math.min(low, point.value);
            high = Math.max(high, point.value);
          }
        }
        domains[side] = axisScale(control, low, high, Math.max(3, Math.floor(plotHeight / 34)));
        units[side] = axisUnits.size === 1 ? [...axisUnits][0] : '';
        if (domains[side].error) errors.push(AXIS_NAMES[side] + '范围或刻度间隔无效，已使用有效刻度');
      }
      updateStatus(errors, start, end);

      const maxTickWidth = side => {
        if (!axes[side].show.checked) return 0;
        let widest = 0;
        for (const tick of domains[side].ticks) {
          widest = Math.max(widest, context.measureText(formatValue(tick)).width);
        }
        return widest;
      };
      const rawMargin = side => axes[side].show.checked
        ? Math.min(Math.max(54, Math.ceil(maxTickWidth(side)) + 18), Math.floor(width * 0.26))
        : 16;
      let leftMargin = rawMargin('left');
      let rightMargin = rawMargin('right');
      const minPlotWidth = 110;
      if (width - leftMargin - rightMargin < minPlotWidth) {
        const available = Math.max(24, width - minPlotWidth);
        const total = leftMargin + rightMargin || 1;
        leftMargin = Math.max(12, Math.floor(available * leftMargin / total));
        rightMargin = Math.max(12, available - leftMargin);
      }
      const left = leftMargin, right = width - rightMargin;
      plotRange = {start, end, left, right, top, bottom};
      const x = time => left + (right - left) * (time - start) / (end - start);
      const y = (value, domain) => bottom - (bottom - top) * (value - domain.low) / (domain.high - domain.low);
      const gridSide = axes.left.show.checked ? 'left' : 'right';

      // 3. 绘制顶部曲线图标标题区并记录点击命中区（点击可切换显隐，隐藏项呈半透明删除线）。
      headerHitboxes = [];
      if (header.rows.length) {
        context.save();
        context.font = titleFont;
        context.textBaseline = 'middle';
        context.textAlign = 'left';
        header.rows.forEach((rowItems, rowIndex) => {
          const rowTop = 6 + rowIndex * header.rowH;
          const rowY = rowTop + header.rowH / 2;
          let cursorX = header.padX;
          for (const entry of rowItems) {
            if (entry.more) {
              context.fillStyle = tickTextColor;
              context.fillText(entry.more, cursorX + 2, rowY);
              headerHitboxes.push({more:true, x:cursorX - 2, y:rowTop, w:entry.width + 4, h:header.rowH});
            } else {
              const hiddenSeries = !entry.item.visible.checked;
              const hiddenColor = dark ? '#64748B' : '#94A3B8';
              const activeColor = entry.item.color.value || titleTextColor;
              context.save();
              drawSeriesIcon(
                context,
                cursorX,
                rowY,
                hiddenSeries ? hiddenColor : activeColor,
                entry.item.digital,
                entry.item.thickness,
                hiddenSeries,
              );
              const textX = cursorX + header.iconW + header.iconGap;
              context.fillStyle = hiddenSeries ? hiddenColor : activeColor;
              context.fillText(entry.label, textX, rowY);
              if (hiddenSeries) {
                const labelW = context.measureText(entry.label).width;
                context.strokeStyle = hiddenColor;
                context.lineWidth = 1;
                context.beginPath();
                context.moveTo(textX, rowY);
                context.lineTo(textX + labelW, rowY);
                context.stroke();
              }
              context.restore();
              headerHitboxes.push({item:entry.item, x:cursorX - 3, y:rowTop, w:entry.width + 6, h:header.rowH});
            }
            cursorX += entry.width + header.itemGap;
          }
        });
        context.strokeStyle = headerSepColor;
        context.lineWidth = 1;
        context.beginPath();
        context.moveTo(8, header.height - 1.5);
        context.lineTo(width - 8, header.height - 1.5);
        context.stroke();
        context.restore();
      }

      // 4. 测量时间文本实际宽度，计算防重叠 X 轴步长并安全绘制横坐标刻度与横轴标题。
      context.font = tickFont;
      const clock = end - start >= 3600000 || new Date(start).toDateString() !== new Date(end).toDateString() ? FULL_TIME : TIME;
      const sampleTimeW = Math.ceil(Math.max(
        context.measureText(clock.format(start)).width,
        context.measureText(clock.format(end)).width,
      ));
      const minTickSpacing = Math.max(68, sampleTimeW + 18);
      const tickTarget = Math.max(1000, (end - start) / Math.max(2, Math.floor((right - left) / minTickSpacing)));
      const tickStep = Math.max(1000, (TIME_STEPS.find(seconds => seconds * 1000 >= tickTarget)
        || Math.max(1, Math.ceil(tickTarget / DAY)) * DAY / 1000) * 1000);
      const grid = (x1, y1, x2, y2, color = gridColor, dashed = false) => {
        context.strokeStyle = color;
        context.lineWidth = 1;
        context.setLineDash(dashed ? [3, 3] : []);
        context.beginPath();
        context.moveTo(x1, y1);
        context.lineTo(x2, y2);
        context.stroke();
        if (dashed) context.setLineDash([]);
      };
      context.textBaseline = 'middle';
      let lastXLabelRight = -Infinity;
      for (let time = Math.ceil(start / tickStep) * tickStep; time <= end; time += tickStep) {
        const tickX = x(time);
        grid(tickX, bottom, tickX, bottom + 4, axisLineColor);
        const text = clock.format(time);
        const labelW = context.measureText(text).width;
        let labelLeft = tickX - labelW / 2;
        let labelRight = tickX + labelW / 2;
        let drawX = tickX;
        let align = 'center';
        if (labelLeft < 4) {
          labelLeft = 4;
          labelRight = 4 + labelW;
          drawX = 4;
          align = 'left';
        } else if (labelRight > width - 4) {
          labelRight = width - 4;
          labelLeft = labelRight - labelW;
          drawX = width - 4;
          align = 'right';
        }
        if (labelLeft >= lastXLabelRight + 10 && labelRight <= width - 4) {
          context.fillStyle = tickTextColor;
          context.textAlign = align;
          context.fillText(text, drawX, bottom + 15);
          lastXLabelRight = labelRight;
        }
      }
      grid(left, bottom, right, bottom, axisLineColor);
      if (showXAxisTitle) {
        context.fillStyle = tickTextColor;
        context.textAlign = 'center';
        context.fillText(translateText('时间'), (left + right) / 2, height - 10, Math.max(40, right - left));
      }

      // 5. 绘制左/右纵坐标轴线、网格、防重叠数值刻度及独立于最高刻度之上的轴标题。
      const axisTitleY = header.height + 11;
      for (const [side, control] of Object.entries(axes)) {
        if (!control.show.checked) continue;
        const isLeft = side === 'left', edge = isLeft ? left : right;
        const sideMargin = isLeft ? leftMargin : rightMargin;
        const color = axisPalette[side];
        grid(edge, top, edge, bottom, color);
        context.fillStyle = color;
        context.textAlign = isLeft ? 'right' : 'left';
        let lastLabelY = Infinity;
        for (const tick of domains[side].ticks) {
          const tickY = y(tick, domains[side]);
          if (tickY < top - 1 || tickY > bottom + 1) continue;
          if (side === gridSide) grid(left, tickY, right, tickY, gridColor, true);
          grid(edge, tickY, edge + (isLeft ? -4 : 4), tickY, color);
          if (lastLabelY - tickY >= 15) {
            context.fillText(formatValue(tick), edge + (isLeft ? -8 : 8), tickY, Math.max(24, sideMargin - 12));
            lastLabelY = tickY;
          }
        }
        context.textAlign = isLeft ? 'left' : 'right';
        const axisLabel = translateText(AXIS_NAMES[side]);
        const axisTitle = axisLabel + (units[side] ? ' (' + units[side] + ')' : '');
        const maxAxisTitleW = Math.max(40, width / 2 - 14);
        context.fillText(fitCanvasText(context, axisTitle, maxAxisTitleW), isLeft ? 8 : width - 8, axisTitleY);
      }
      context.save();
      context.beginPath();
      context.rect(left, top, right - left, bottom - top);
      context.clip();
      for (const {item, data} of datasets) {
        const domain = domains[item.axis.value];
        const itemMs = (item.intervalSeconds || DEFAULT_INTERVAL_SECONDS) * 1000;
        const expectedStep = data.length > 2
          ? Math.max(itemMs, (end - start) / Math.max(1, data.length - 1))
          : itemMs;
        const gapMs = Math.max(itemMs * 3, expectedStep * 3, 5000);
        context.strokeStyle = item.color.value;
        context.fillStyle = item.color.value;
        context.lineWidth = item.thickness;
        context.lineJoin = 'round';
        context.lineCap = 'round';
        for (const invalid of [false, true]) {
          let previous = null, connected = false;
          context.setLineDash(invalid ? [5, 4] : []);
          context.beginPath();
          for (let i = 0; i < data.length; i++) {
            const point = data[i];
            const gapped = previous !== null && (point.time - previous.time > gapMs);
            if (gapped) {
              previous = null;
              connected = false;
            }
            const drawSegment = point.value !== null && (point.invalid || !!previous?.invalid) === invalid;
            if (drawSegment) {
              const pointX = x(point.time), pointY = y(point.value, domain);
              const lastY = previous ? y(previous.value, domain) : pointY;
              if (!connected) context.moveTo(previous ? x(previous.time) : pointX, lastY);
              if (previous && item.digital) context.lineTo(pointX, lastY);
              context.lineTo(previous ? pointX : pointX + 0.1, pointY);
            }
            connected = drawSegment;
            previous = point.value === null ? null : point;
          }
          context.stroke();
        }
        // 为孤立单点绘制清晰圆点，避免 0.1px 线段不可见。
        context.setLineDash([]);
        for (let i = 0; i < data.length; i++) {
          const point = data[i];
          if (point.value === null) continue;
          const prev = i > 0 ? data[i - 1] : null;
          const next = i + 1 < data.length ? data[i + 1] : null;
          const leftDisconnected = !prev || prev.value === null || (point.time - prev.time > gapMs);
          const rightDisconnected = !next || next.value === null || (next.time - point.time > gapMs);
          if (leftDisconnected && rightDisconnected) {
            context.beginPath();
            context.arc(x(point.time), y(point.value, domain), Math.max(2.5, item.thickness), 0, Math.PI * 2);
            context.fill();
          }
        }
      }
      context.restore();
    }

    function scheduleDraw() {
      if (isSelectPopupActive()) {
        deferredPublish = true;
        return;
      }
      if (!frame && canDraw()) frame = requestAnimationFrame(draw);
    }

    function hideHover() {
      cursor.hidden = tooltip.hidden = true;
    }

    function findHeaderHit(offsetX, offsetY) {
      for (const box of headerHitboxes) {
        if (offsetX >= box.x && offsetX <= box.x + box.w && offsetY >= box.y && offsetY <= box.y + box.h) {
          return box;
        }
      }
      return null;
    }

    canvas.addEventListener('pointerleave', () => {
      canvas.style.cursor = '';
      hideHover();
    });
    canvas.addEventListener('click', event => {
      const hit = findHeaderHit(event.offsetX, event.offsetY);
      if (!hit) return;
      if (hit.more) {
        openDialog(seriesDialog);
        return;
      }
      if (hit.item && series.has(hit.item.key)) {
        openSelect = null;
        applyDisplayState(hit.item, hit.item.visible.checked ? 'hidden' : (hit.item.axis.value === 'right' ? 'right' : 'left'));
        scheduleDraw();
        saveSettings();
      }
    });
    canvas.addEventListener('pointermove', event => {
      const {offsetX:x, offsetY:y} = event;
      const headerHit = findHeaderHit(x, y);
      canvas.style.cursor = headerHit ? 'pointer' : '';
      if (headerHit || !plotRange) {
        hideHover();
        return;
      }
      const {start, end, left, right, top, bottom} = plotRange;
      if (x < left || x > right || y < top || y > bottom) {
        hideHover();
        return;
      }
      const time = start + (x - left) * (end - start) / (right - left);
      const readings = [];
      for (const item of series.values()) {
        if (!item.visible.checked) continue;
        const point = nearestSample(item.samples, time);
        const hitTolerance = Math.max(2000, (item.intervalSeconds || DEFAULT_INTERVAL_SECONDS) * 1000);
        if (point && point.time >= start && point.time <= end && Math.abs(point.time - time) <= hitTolerance) {
          readings.push(item.name + '  ' + (point.value === null ? '—' : formatValue(point.value)) +
                        (item.unit ? ' ' + item.unit : '') + (point.invalid ? '（无效）' : ''));
        }
      }
      if (!readings.length) {
        hideHover();
        return;
      }
      cursor.style.left = x + 'px';
      cursor.style.top = top + 'px';
      cursor.style.height = (bottom - top) + 'px';
      tooltip.style.top = (top + 6) + 'px';
      tooltip.style.maxHeight = Math.max(48, bottom - top - 12) + 'px';
      tooltip.textContent = FULL_TIME.format(time) + '\n' + readings.join('\n');
      tooltip.classList.toggle('point-trend-tooltip-left', x > chart.clientWidth / 2);
      cursor.hidden = tooltip.hidden = false;
    });

    function scheduleRoll() {
      clearTimeout(rollTimer);
      if (!canDraw() || mode.value !== 'rolling') return;
      const delay = Math.max(50, timeRange().end - Date.now() + 15);
      rollTimer = setTimeout(() => {
        scheduleDraw();
        scheduleRoll();
      }, delay);
    }

    function trimItemSamples(item) {
      const limit = Math.min(MAX_RETENTION_POINTS, Math.max(MIN_RETENTION_POINTS, item.retentionPoints || DEFAULT_RETENTION_POINTS));
      const discard = item.samples.length - limit;
      if (discard > 0) item.samples.splice(0, discard);
    }

    function appendSample(item, time, value, quality, sourceTime) {
      if (item.samples.at(-1)?.time >= time) return;
      const badQuality = quality !== null && quality !== undefined && Number(quality) !== 0;
      item.samples.push({time, value, quality, sourceTime, invalid: badQuality});
      trimItemSamples(item);
    }

    function currentSettings() {
      return {
        series: [...series.values()].map(item => ({point:item.point, name:item.name, unit:item.unit, kind:item.kind,
          visible:item.visible.checked, axis:item.axis.value, color:item.color.value, width:item.thickness,
          interval_seconds:item.intervalSeconds, retention_points:item.retentionPoints})),
        view: {open:management ? sourceOpen : panel.open, axes_open:false,
          mode:mode.value, duration_minutes:duration / 60000,
          alignment:alignment.value, from:from.value, until:until.value,
          axes:Object.fromEntries(Object.entries(axes).map(([side, control]) => [side, {
            show:control.show.checked, mode:control.mode.value, min:control.min.value,
            max:control.max.value, step:control.step.value,
          }]))},
      };
    }

    function restoreSettings(settings) {
      settings ??= {series:[]};
      restoring = true;
      try {
        const view = settings.view || {};
        mode.value = view.mode === 'fixed' ? 'fixed' : 'rolling';
        length.value = view.duration_minutes ?? 10;
        alignment.value = view.alignment || 'auto';
        from.value = view.from || '';
        until.value = view.until || '';
        fixed = null;
        for (const [side, control] of Object.entries(axes)) {
          const saved = view.axes?.[side] || {};
          control.show.checked = saved.show !== false;
          control.mode.value = saved.mode === 'manual' ? 'manual' : 'auto';
          control.min.value = saved.min ?? '';
          control.max.value = saved.max ?? '';
          control.step.value = saved.step ?? '';
          control.min.disabled = control.max.disabled = control.mode.value !== 'manual';
        }
        const savedPoints = (settings.series || []).map(entry => {
          const def = {...entry.point, name:entry.name, unit:entry.unit, kind:entry.kind || entry.point.kind};
          const point = pointKey(def);
          return {entry, def, point, key:JSON.stringify(point)};
        });
        const wanted = new Set(savedPoints.map(row => row.key));
        for (const item of series.values()) if (!wanted.has(item.key)) remove(item);
        for (const {entry, def, point, key} of savedPoints) {
          const item = series.get(key) || add(def, key, point, 'hidden');
          applyDisplayState(item, entry.visible === false ? 'hidden' : (entry.axis === 'right' ? 'right' : 'left'));
          if (entry.color) item.color.value = entry.color;
          item.width.value = item.thickness = Number(entry.width) || 2;
          const nextInterval = Number.isInteger(entry.interval_seconds)
            ? Math.min(MAX_INTERVAL_SECONDS, Math.max(MIN_INTERVAL_SECONDS, entry.interval_seconds))
            : DEFAULT_INTERVAL_SECONDS;
          const nextRetention = Number.isInteger(entry.retention_points)
            ? Math.min(MAX_RETENTION_POINTS, Math.max(MIN_RETENTION_POINTS, entry.retention_points))
            : DEFAULT_RETENTION_POINTS;
          item.intervalInput.value = item.intervalSeconds = nextInterval;
          item.retentionInput.value = item.retentionPoints = nextRetention;
          trimItemSamples(item);
        }
        sourceOpen = view.open === true;
        sourceAxesOpen = false;
        if (!management) {
          panel.open = sourceOpen;
        }
        settingsChanged();
        changedSelection();
      } finally {
        restoring = false;
      }
    }

    function dataSignature(settings) {
      if (!settings) return '';
      return JSON.stringify((settings.series || []).map(entry => ({
        point: pointKey(entry.point),
        interval_seconds: entry.interval_seconds ?? DEFAULT_INTERVAL_SECONDS,
        retention_points: entry.retention_points ?? DEFAULT_RETENTION_POINTS,
      })));
    }

    function confirmSettings({settings, revision}) {
      serverSettings = JSON.stringify(settings);
      serverRevision = typeof revision === 'string' ? revision : '';
      recordingKeys = new Set((settings?.series || []).map(entry => JSON.stringify(pointKey(entry.point))));
    }

    async function historyApi(path, options) {
      const database = await window.scadaDatabaseReady;
      const headers = new Headers(options?.headers);
      if (database?.token) headers.set('X-Scada-Database', database.token);
      const response = await fetch(HISTORY_URL + path, {...options, headers, cache:'no-store'});
      const result = await response.json();
      if (!response.ok) {
        databaseChanged = result.error?.code === 'database_changed';
        throw new Error(result.error?.message || '曲线请求失败（HTTP ' + response.status + '）');
      }
      return result;
    }

    function queueHistory(delay) {
      clearTimeout(historyTimer);
      if (!databaseChanged && (!ready || (canRead() && (management || series.size)))) {
        historyTimer = setTimeout(loadHistory, delay);
      }
    }

    async function loadHistory() {
      if (reading || databaseChanged) return;
      if (pendingSaves) { queueHistory(pollDelayMs()); return; }
      if (isSelectPopupActive()) { queueHistory(400); return; }
      reading = true;
      const version = settingsVersion;
      const after = lastSample;
      let nextDelay = pollDelayMs();
      try {
        const result = await historyApi('?after_ms=' + after, {signal:AbortSignal.timeout(12000)});
        if (version !== settingsVersion) { nextDelay = 0; return; }
        const prevDataSig = serverSettings ? dataSignature(JSON.parse(serverSettings)) : '';
        const nextDataSig = dataSignature(result.settings);
        const settingsChangedOnServer = JSON.stringify(result.settings) !== serverSettings;
        const dataChangedOnServer = nextDataSig !== prevDataSig;
        if (settingsChangedOnServer) {
          restoreSettings(result.settings);
          queuedSettings = JSON.stringify(currentSettings());
        }
        confirmSettings(result);
        fetchError = '';
        saveError = '';
        ready = true;
        syncCheckboxes();
        nextDelay = pollDelayMs();
        // 仅当测点集合或单点采样周期/留存点数变更时才重载全量样本，折叠/改颜色等纯视图变化不重拉。
        if (dataChangedOnServer && after) {
          lastSample = 0;
          nextDelay = 0;
          return;
        }
        const items = (result.settings?.series || []).map(entry =>
          series.get(JSON.stringify(pointKey(entry.point))));
        if (!after) for (const item of items) if (item) item.samples.length = 0;
        const perSeries = Array.isArray(result.series_samples) ? result.series_samples : [];
        for (const [index, item] of items.entries()) {
          if (!item) continue;
          const rows = Array.isArray(perSeries[index]) ? perSeries[index] : [];
          for (const pt of rows) {
            const value = Number.isFinite(pt?.value) ? pt.value : null;
            appendSample(item, pt.time_ms, value, pt?.quality ?? null, pt?.source_time_ms ?? null);
            lastSample = Math.max(lastSample, pt.time_ms);
          }
        }
      } catch (error) {
        fetchError = '读取曲线失败：' + error.message;
      } finally {
        reading = false;
        if (isSelectPopupActive()) {
          deferredPublish = true;
          queueHistory(400);
        } else {
          scheduleDraw();
          queueHistory(nextDelay);
          publishChange();
        }
      }
    }

    function saveSettings() {
      if (!ready || restoring || databaseChanged) return;
      const body = JSON.stringify(currentSettings());
      if (body === queuedSettings) return;
      queuedSettings = body;
      const version = ++settingsVersion;
      pendingSaves++;
      saveQueue = saveQueue.then(async () => {
        try {
          if (version !== settingsVersion) return;
          const headers = {'Content-Type':'application/json'};
          if (serverRevision) headers['If-Match'] = serverRevision;
          const result = await historyApi('', {method:'PUT', keepalive:true, headers, body});
          confirmSettings(result);
          syncCheckboxes();
          saveError = '';
        } catch (error) {
          ++settingsVersion;
          serverSettings = queuedSettings = '';
          lastSample = 0;
          ready = false;
          syncCheckboxes();
          saveError = '保存曲线设置失败：' + error.message;
        } finally {
          pendingSaves--;
          scheduleDraw();
          if (!pendingSaves) queueHistory(0);
          publishChange();
        }
      });
      publishChange();
    }

    const rowBindings = new WeakMap();
    let rowMenu, menuRow, menuReturnFocus;
    let revealTrendPane = () => {};

    function syncRow(row) {
      const binding = rowBindings.get(row);
      if (!binding) return;
      const item = series.get(binding.key);
      const selected = Boolean(item || recordingKeys.has(binding.key));
      row.classList.toggle('point-trend-row-active', selected);
      binding.cell?.classList.toggle('point-trend-cell-active', selected);
      const markerHost = binding.cell || row.cells?.[0];
      if (markerHost) {
        let dot = markerHost.querySelector(':scope > .point-trend-row-dot');
        if (selected) {
          if (!dot) {
            dot = make('span', {class:'point-trend-row-dot', 'aria-hidden':'true', title:'已设为采样点（右键可管理或删除）'});
            markerHost.prepend(dot);
          }
          dot.style.background = item?.color?.value || 'var(--accent, #2563eb)';
        } else if (dot) {
          dot.remove();
        }
      }
    }

    function closeRowMenu(restoreFocus = false) {
      if (!rowMenu || rowMenu.hidden) return;
      rowMenu.hidden = true;
      menuRow = null;
      if (restoreFocus && menuReturnFocus?.isConnected) menuReturnFocus.focus({preventScroll:true});
      menuReturnFocus = null;
    }

    function ensureRowMenu() {
      if (rowMenu) return;
      rowMenu = make('div', {class:'point-trend-menu', role:'menu', 'aria-label':'采样点菜单'});
      document.body.append(rowMenu);
      document.addEventListener('pointerdown', event => {
        if (!rowMenu.contains(event.target)) closeRowMenu();
      });
      document.addEventListener('keydown', event => {
        if (rowMenu.hidden) return;
        if (event.key === 'Escape') {
          event.preventDefault();
          closeRowMenu(true);
        } else if (event.key === 'Tab') {
          closeRowMenu(true);
        }
      });
      window.addEventListener('resize', () => closeRowMenu());
      document.addEventListener('scroll', () => closeRowMenu(), true);
    }

    function updateRowMenu(force = false) {
      if (!menuRow?.isConnected) {
        closeRowMenu();
        return;
      }
      const binding = rowBindings.get(menuRow);
      if (!binding) {
        closeRowMenu();
        return;
      }
      const {definition, key, point} = binding;
      const existing = series.get(key);
      const signature = [ready ? '1' : '0', key, existing ? itemDisplayState(existing) : 'none'].join('|');
      if (!force && rowMenu.dataset.signature === signature) return;
      rowMenu.dataset.signature = signature;
      rowMenu.replaceChildren();
      const addItem = (text, onClick, {disabled = false, danger = false, checked = false} = {}) => {
        const btn = make('button', {
          type:'button',
          role:'menuitem',
          tabindex:'-1',
          class: [danger ? 'is-danger' : '', checked ? 'is-checked' : ''].filter(Boolean).join(' '),
        });
        btn.textContent = text;
        if (disabled) btn.setAttribute('aria-disabled', 'true');
        btn.addEventListener('click', () => {
          if (disabled) return;
          closeRowMenu(true);
          onClick();
        });
        rowMenu.append(btn);
      };
      const addSep = () => rowMenu.append(make('div', {class:'point-trend-menu-sep', role:'separator'}));
      if (!ready) {
        addItem('正在读取曲线设置…', () => {}, {disabled:true});
      } else if (!existing) {
        addItem('设为采样点', () => requestPoint(definition, key, point, 'hidden'));
        addSep();
        addItem('左轴显示', () => requestPoint(definition, key, point, 'left'));
        addItem('右轴显示', () => requestPoint(definition, key, point, 'right'));
        addItem('隐藏', () => {}, {disabled:true});
      } else {
        const curState = itemDisplayState(existing);
        addItem('左轴显示', () => {
          applyDisplayState(existing, 'left');
          revealTrendPane();
          panel.open = true;
          scheduleDraw();
          saveSettings();
        }, {checked: curState === 'left'});
        addItem('右轴显示', () => {
          applyDisplayState(existing, 'right');
          revealTrendPane();
          panel.open = true;
          scheduleDraw();
          saveSettings();
        }, {checked: curState === 'right'});
        addItem('隐藏', () => {
          applyDisplayState(existing, 'hidden');
          scheduleDraw();
          saveSettings();
        }, {checked: curState === 'hidden'});
        addSep();
        addItem('删除采样点', async () => {
          const ok = typeof window.scadaConfirm === 'function'
            ? await window.scadaConfirm('确认删除该采样点并移出实时曲线？', {title:'删除采样点', confirmText:'确认删除', danger:true})
            : true;
          if (ok && series.has(key)) remove(series.get(key));
        }, {danger:true});
      }
      window.scadaI18n?.translateRoot?.(rowMenu);
    }

    function requestPoint(definition, key, point, displayState = 'hidden') {
      if (!ready) return;
      const existing = series.get(key);
      if (existing) {
        applyDisplayState(existing, displayState);
        if (displayState !== 'hidden') {
          revealTrendPane();
          panel.open = true;
        }
        scheduleDraw();
        saveSettings();
        return;
      }
      if (series.size >= LIMIT) {
        selectionError = '每个曲线板最多选择 ' + LIMIT + ' 条测点';
        revealTrendPane();
        panel.open = true;
        syncCheckboxes();
        scheduleDraw();
        return;
      }
      if (displayState !== 'hidden') revealTrendPane();
      add(definition, key, point, displayState);
    }

    function openRowMenu(row, x, y) {
      ensureRowMenu();
      menuRow = row;
      menuReturnFocus = row.contains(document.activeElement) ? document.activeElement : row;
      updateRowMenu(true);
      rowMenu.hidden = false;
      rowMenu.style.left = Math.max(4, Math.min(x, document.documentElement.clientWidth - rowMenu.offsetWidth - 4)) + 'px';
      rowMenu.style.top = Math.max(4, Math.min(y, document.documentElement.clientHeight - rowMenu.offsetHeight - 4)) + 'px';
      rowMenu.querySelector('button:not([aria-disabled="true"])')?.focus({preventScroll:true});
    }

    function bindRow(row, definition, activeCell) {
      const previous = rowBindings.get(row);
      const point = pointKey(definition), key = JSON.stringify(point);
      const cell = activeCell || row.querySelector('.point-trend-cell') || row.cells?.[0];
      if (previous?.cell && previous.cell !== cell) {
        previous.cell.classList.remove('point-trend-cell-active');
        previous.cell.querySelector(':scope > .point-trend-row-dot')?.remove();
      }
      if (menuRow === row && previous?.key !== key) closeRowMenu();
      rowBindings.set(row, {definition, point, key, cell});
      if (!previous) {
        row.dataset.pointTrendRow = mountId;
        row.addEventListener('contextmenu', event => {
          if (event.defaultPrevented || event.target.closest('input:not([type=checkbox]), textarea, select, [contenteditable]')) return;
          event.preventDefault();
          openRowMenu(row, event.clientX, event.clientY);
        });
        row.addEventListener('keydown', event => {
          if (event.defaultPrevented || !(event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10'))) return;
          event.preventDefault();
          const bounds = event.target.getBoundingClientRect();
          openRowMenu(row, bounds.left, bounds.bottom);
        });
      }
      syncRow(row);
    }

    function syncCheckboxes() {
      for (const box of document.querySelectorAll('[data-point-trend="' + mountId + '"]')) {
        box.checked = series.has(box.dataset.pointKey);
        box.disabled = !ready;
      }
      for (const row of document.querySelectorAll('[data-point-trend-row="' + mountId + '"]')) syncRow(row);
      if (menuRow) updateRowMenu();
    }

    function changedSelection() {
      summary.textContent = series.size ? '已选 ' + series.size + ' 条' : '选取测点';
      selectionError = '';
      syncCheckboxes();
      scheduleDraw();
      saveSettings();
    }

    function remove(item) {
      series.delete(item.key);
      item.controls.remove();
      if (!series.size) {
        lastSample = 0;
        if (!databaseChanged) fetchError = '';
        if (seriesDialog.open) seriesDialog.close();
      }
      changedSelection();
    }

    function add(definition, key, point, displayState = 'hidden') {
      const name = String(definition.name || definition.metric || (definition.pnt_no ?? definition.measurement_id));
      const unit = definition.unit || name.match(/[（(]([^）()]{1,16})[）)]$/)?.[1] || '';
      const row = make('div', {class:'point-trend-series-row'});
      const visible = {checked: displayState !== 'hidden'};
      const axis = {value: displayState === 'right' ? 'right' : 'left'};
      const displaySelect = select([['hidden', '隐藏'], ['left', '左轴显示'], ['right', '右轴显示']]);
      displaySelect.value = displayState === 'right' ? 'right' : displayState === 'left' ? 'left' : 'hidden';
      const title = make('span', {class:'point-trend-name'});
      title.textContent = name + (definition.unit && !name.includes('(' + definition.unit + ')') ? ' · ' + definition.unit : '');
      title.title = title.textContent;
      const latest = make('output', {class:'point-trend-latest', 'aria-label':'最近采样 · ' + name, 'aria-live':'off'});
      latest.textContent = '—';
      const color = make('input', {type:'color', 'aria-label':'曲线颜色 · ' + name, value:COLORS[colorIndex++ % COLORS.length]});
      const width = numberInput(2, 1, 8, 0.5);
      const intervalInput = numberInput(DEFAULT_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS, 1);
      intervalInput.className = 'point-trend-interval-input';
      const retentionInput = numberInput(DEFAULT_RETENTION_POINTS, MIN_RETENTION_POINTS, MAX_RETENTION_POINTS, 1);
      retentionInput.className = 'point-trend-retention-input';
      const removeButton = make('button', {type:'button', 'aria-label':'移除曲线 · ' + name});
      removeButton.textContent = '移除';
      const digital = ['yx', 'yk'].includes(definition.kind);
      const item = {key, point, name, latest, samples:[], controls:row, visible, axis, displaySelect, color, width, thickness:2,
                    intervalInput, intervalSeconds:DEFAULT_INTERVAL_SECONDS,
                    retentionInput, retentionPoints:DEFAULT_RETENTION_POINTS,
                    unit, kind:definition.kind, digital};
      series.set(key, item);
      curves.append(row);
      row.append(
        title, latest,
        label('曲线状态', displaySelect),
        label('采样周期（秒）', intervalInput),
        label('后台留存（点数）', retentionInput),
        label('颜色', color), label('线宽', width),
        removeButton,
      );
      row.addEventListener('change', event => {
        if (event.target === displaySelect) {
          applyDisplayState(item, displaySelect.value);
        }
        if (width.value !== '' && width.validity.valid) item.thickness = width.valueAsNumber;
        else width.value = item.thickness;
        if (intervalInput.value !== '' && intervalInput.validity.valid && Number.isInteger(intervalInput.valueAsNumber)) {
          item.intervalSeconds = intervalInput.valueAsNumber;
        } else {
          intervalInput.value = item.intervalSeconds;
        }
        if (retentionInput.value !== '' && retentionInput.validity.valid && Number.isInteger(retentionInput.valueAsNumber)) {
          item.retentionPoints = retentionInput.valueAsNumber;
          trimItemSamples(item);
        } else {
          retentionInput.value = item.retentionPoints;
        }
        if (event.target === retentionInput || event.target === intervalInput) lastSample = 0;
        scheduleDraw();
        saveSettings();
      });
      color.addEventListener('input', () => {
        syncCheckboxes();
        scheduleDraw();
      });
      removeButton.addEventListener('click', () => remove(item));
      if (!restoring) {
        lastSample = 0;
        if (displayState !== 'hidden') panel.open = true;
        changedSelection();
      }
      return item;
    }

    function checkbox(definition) {
      const point = pointKey(definition), key = JSON.stringify(point);
      const box = make('input', {type:'checkbox', class:'point-trend-checkbox',
                                 'data-point-trend':mountId, 'data-point-key':key,
                                 'aria-label':'加入实时曲线 · ' + (definition.name || (definition.pnt_no ?? definition.measurement_id))});
      box.checked = series.has(key);
      box.disabled = !ready;
      box.addEventListener('change', () => {
        if (!box.checked) {
          const item = series.get(key);
          if (item) remove(item);
        } else {
          requestPoint(definition, key, point);
        }
      });
      return box;
    }

    function settingsChanged() {
      settingsError = '';
      if (length.value !== '' && length.validity.valid) duration = length.valueAsNumber * 60000;
      else length.value = duration / 60000;
      for (const option of alignment.options) {
        const shouldDisable = Number(option.value) * 4000 > duration;
        if (option.disabled !== shouldDisable) option.disabled = shouldDisable;
      }
      if (alignment.selectedOptions[0]?.disabled) alignment.value = 'auto';
      const rolling = mode.value === 'rolling';
      span.hidden = alignmentLabel.hidden = !rolling;
      fromLabel.hidden = untilLabel.hidden = rolling;
      if (!rolling) {
        const previousWindow = timeRange();
        if (!from.value) from.value = localTime(previousWindow.start);
        if (!until.value) until.value = localTime(previousWindow.end);
        const start = new Date(from.value).getTime(), end = new Date(until.value).getTime();
        if (Number.isFinite(start) && Number.isFinite(end) && start < end) fixed = {start, end};
        else settingsError = '开始时刻必须早于结束时刻';
      }
      scheduleDraw();
      scheduleRoll();
    }

    function toolbarChanged(control) {
      settingsChanged();
      if (control !== alignment) lastSample = 0;
      saveSettings();
    }

    function updateSeriesConfig(key, {intervalSeconds, retentionPoints, displayState} = {}) {
      const item = series.get(key);
      if (!ready || databaseChanged || !item) return false;
      let changed = false;
      let dataChanged = false;
      if (intervalSeconds !== undefined) {
        if (!Number.isInteger(intervalSeconds) || intervalSeconds < MIN_INTERVAL_SECONDS || intervalSeconds > MAX_INTERVAL_SECONDS) {
          return false;
        }
        if (item.intervalSeconds !== intervalSeconds) {
          item.intervalSeconds = intervalSeconds;
          item.intervalInput.value = intervalSeconds;
          changed = true;
          dataChanged = true;
        }
      }
      if (retentionPoints !== undefined) {
        if (!Number.isInteger(retentionPoints) || retentionPoints < MIN_RETENTION_POINTS || retentionPoints > MAX_RETENTION_POINTS) {
          return false;
        }
        if (item.retentionPoints !== retentionPoints) {
          item.retentionPoints = retentionPoints;
          item.retentionInput.value = retentionPoints;
          trimItemSamples(item);
          changed = true;
          dataChanged = true;
        }
      }
      if (displayState !== undefined) {
        if (!['hidden', 'left', 'right'].includes(displayState)) return false;
        if (itemDisplayState(item) !== displayState) {
          applyDisplayState(item, displayState);
          if (displayState !== 'hidden') {
            revealTrendPane();
            panel.open = true;
          }
          changed = true;
        }
      }
      if (!changed) return true;
      if (dataChanged) lastSample = 0;
      scheduleDraw();
      saveSettings();
      return true;
    }

    function removeSeries(key) {
      const item = series.get(key);
      if (!ready || databaseChanged || !item) return false;
      remove(item);
      return true;
    }

    function showSeries(key) {
      const item = series.get(key);
      if (!ready || databaseChanged || !item) return false;
      revealTrendPane();
      panel.open = true;
      if (!item.visible.checked) {
        applyDisplayState(item, item.axis.value === 'right' ? 'right' : 'left');
        saveSettings();
      }
      updateActivity();
      requestAnimationFrame(() => chart.scrollIntoView({block:'nearest'}));
      return true;
    }

    toolbar.addEventListener('change', event => toolbarChanged(event.target));
    let zoomSaveTimer = 0;
    function zoomWindow(factor) {
      const prevStart = timeRange().start;
      if (mode.value === 'fixed') {
        const cur = timeRange();
        const mid = (cur.start + cur.end) / 2;
        const nextSpan = Math.min(1440 * 60000, Math.max(10000, (cur.end - cur.start) * factor));
        const nextStart = Math.round(mid - nextSpan / 2);
        const nextEnd = Math.round(mid + nextSpan / 2);
        fixed = {start:nextStart, end:nextEnd};
        from.value = localTime(nextStart);
        until.value = localTime(nextEnd);
      } else {
        const curMin = Math.max(1, Math.min(1440, Math.round((duration / 60000) * factor)));
        length.value = curMin;
      }
      settingsChanged();
      // 滚轮缩放仅在时间窗向左扩展出已缓存范围时按 350ms 防抖补拉历史，避免高频滚轮触发请求风暴。
      const nextStart = timeRange().start;
      clearTimeout(zoomSaveTimer);
      zoomSaveTimer = setTimeout(() => {
        if (nextStart < prevStart) lastSample = 0;
        saveSettings();
      }, 350);
    }
    zoomInBtn.addEventListener('click', () => zoomWindow(0.5));
    zoomOutBtn.addEventListener('click', () => zoomWindow(2));
    resetZoomBtn.addEventListener('click', () => {
      mode.value = 'rolling';
      length.value = 10;
      fixed = null;
      toolbarChanged(mode);
    });
    chart.addEventListener('wheel', event => {
      if (!canDraw() || !series.size) return;
      event.preventDefault();
      zoomWindow(event.deltaY < 0 ? 0.8 : 1.25);
    }, {passive:false});
    exportCsvBtn.addEventListener('click', () => {
      const {start, end} = timeRange();
      const visible = [...series.values()].filter(item => item.visible.checked);
      const csvEscape = value => '"' + String(value ?? '').replace(/"/g, '""') + '"';
      const lines = ['时间,毫秒时间戳,测点,数值,品质'];
      for (const item of visible) {
        for (const pt of item.samples) {
          if (pt.time < start || pt.time > end) continue;
          lines.push([
            csvEscape(new Date(pt.time).toISOString()),
            pt.time,
            csvEscape(item.name),
            pt.value ?? '',
            pt.quality ?? '',
          ].join(','));
        }
      }
      const blob = new Blob(['\ufeff', lines.join('\r\n') + '\r\n'], {type:'text/csv;charset=utf-8'});
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `scada-trend-${Date.now()}.csv`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    for (const control of Object.values(axes)) {
      control.section.addEventListener('change', () => {
        control.min.disabled = control.max.disabled = control.mode.value !== 'manual';
        scheduleDraw();
        saveSettings();
      });
    }

    function updateActivity() {
      scheduleDraw();
      if (!canDraw()) hideHover();
      scheduleRoll();
      const readingActive = canRead();
      if (readingActive === wasReadingActive) return;
      wasReadingActive = readingActive;
      if (readingActive) loadHistory();
      else if (ready) clearTimeout(historyTimer);
    }

    function refreshAfterResume() {
      updateActivity();
      if (!canRead() && pageVisible()) loadHistory();
    }

    // 曲线画布与图例右键菜单：集中承载纵轴设置、曲线设置与视图快捷操作。
    let chartMenu;
    function closeChartMenu() {
      if (chartMenu && !chartMenu.hidden) chartMenu.hidden = true;
    }
    function openDialog(dialog) {
      if (typeof dialog.showModal === 'function') {
        if (!dialog.open) dialog.showModal();
      } else {
        dialog.setAttribute('open', '');
      }
    }
    function openChartMenu(x, y, targetItem = null) {
      closeRowMenu();
      if (!chartMenu) {
        chartMenu = make('div', {class:'point-trend-menu', role:'menu', 'aria-label':'实时曲线菜单'});
        document.body.append(chartMenu);
        document.addEventListener('pointerdown', event => {
          if (!chartMenu.contains(event.target)) closeChartMenu();
        });
        document.addEventListener('keydown', event => {
          if (!chartMenu.hidden && (event.key === 'Escape' || event.key === 'Tab')) {
            closeChartMenu();
          }
        });
        window.addEventListener('resize', closeChartMenu);
        document.addEventListener('scroll', closeChartMenu, true);
      }
      chartMenu.replaceChildren();
      const addAction = (labelText, handler, {disabled = false, danger = false, checked = false} = {}) => {
        const btn = make('button', {type:'button', role:'menuitem', tabindex:'-1'});
        btn.textContent = labelText;
        if (disabled) btn.setAttribute('aria-disabled', 'true');
        if (danger) btn.classList.add('is-danger');
        if (checked) btn.classList.add('is-checked');
        btn.addEventListener('click', () => {
          if (disabled) return;
          closeChartMenu();
          handler();
        });
        chartMenu.append(btn);
      };
      const addSep = () => chartMenu.append(make('div', {class:'point-trend-menu-sep', role:'separator'}));

      if (targetItem && series.has(targetItem.key)) {
        const curState = itemDisplayState(targetItem);
        addAction('左轴显示', () => {
          applyDisplayState(targetItem, 'left');
          scheduleDraw();
          saveSettings();
        }, {checked: curState === 'left'});
        addAction('右轴显示', () => {
          applyDisplayState(targetItem, 'right');
          scheduleDraw();
          saveSettings();
        }, {checked: curState === 'right'});
        addAction('隐藏', () => {
          applyDisplayState(targetItem, 'hidden');
          scheduleDraw();
          saveSettings();
        }, {checked: curState === 'hidden'});
        addAction('删除采样点', async () => {
          const ok = typeof window.scadaConfirm === 'function'
            ? await window.scadaConfirm('确认删除该采样点并移出实时曲线？', {title:'删除采样点', confirmText:'确认删除', danger:true})
            : true;
          if (ok && series.has(targetItem.key)) remove(targetItem);
        }, {danger:true});
        addSep();
      }
      addAction('纵轴设置…', () => openDialog(axesDialog));
      addAction('曲线设置…', () => openDialog(seriesDialog), {disabled:!series.size});
      addSep();
      addAction('放大', () => zoomWindow(0.5), {disabled:!series.size});
      addAction('缩小', () => zoomWindow(2), {disabled:!series.size});
      addAction('重置视图', () => resetZoomBtn.click());
      addAction('导出 CSV', () => exportCsvBtn.click(), {disabled:!series.size});

      chartMenu.hidden = false;
      window.scadaI18n?.translateRoot?.(chartMenu);
      chartMenu.style.left = Math.max(4, Math.min(x, document.documentElement.clientWidth - chartMenu.offsetWidth - 4)) + 'px';
      chartMenu.style.top = Math.max(4, Math.min(y, document.documentElement.clientHeight - chartMenu.offsetHeight - 4)) + 'px';
      chartMenu.querySelector('button:not([aria-disabled="true"])')?.focus({preventScroll:true});
    }

    chart.addEventListener('contextmenu', event => {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const hit = findHeaderHit(event.clientX - rect.left, event.clientY - rect.top);
      openChartMenu(event.clientX, event.clientY, hit?.item || null);
    });
    chart.addEventListener('keydown', event => {
      if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
        event.preventDefault();
        const rect = chart.getBoundingClientRect();
        openChartMenu(rect.left + rect.width / 2, rect.top + 36, null);
      }
    });

    // 在表格/采样点列表与实时曲线区之间挂载可拖拽、可双向折叠/展开的水平分隔条（同时覆盖数据表页与采样管理页）。
    if (container.parentElement) {
      const parent = container.parentElement;
      const SPLIT_KEY = 'scada_point_trend_split_v1:' + location.pathname;
      const splitter = make('div', {
        class:'point-trend-splitter',
        role:'separator',
        'aria-orientation':'horizontal',
        'aria-label':'拖动调整表格与实时曲线高度；点击按钮可折叠或展开上下区域',
        title:'拖动调整高度 · 双击复位',
      });
      const btnUp = make('button', {
        type:'button',
        class:'point-trend-splitter-btn',
        'data-split-action':'up',
        'aria-label':'折叠上方表格 / 展开曲线',
        title:'折叠上方表格（实时曲线全屏）',
      });
      btnUp.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 7.5 6 4l3.5 3.5"/></svg>';
      const btnDown = make('button', {
        type:'button',
        class:'point-trend-splitter-btn',
        'data-split-action':'down',
        'aria-label':'折叠下方曲线 / 展开表格',
        title:'隐藏下方实时曲线（表格全屏）',
      });
      btnDown.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4.5 6 8l3.5-3.5"/></svg>';
      const grip = make('span', {class:'point-trend-splitter-grip', 'aria-hidden':'true'});
      splitter.append(btnUp, grip, btnDown);
      parent.insertBefore(splitter, container);

      const getUpperPanes = () => {
        const list = [];
        let node = splitter.previousElementSibling;
        while (node) {
          if (node.matches('.table-wrap, .point-pages, #point-panels, .pager, .trend-point-list')) list.push(node);
          node = node.previousElementSibling;
        }
        return list;
      };
      const getTablePane = () => {
        let node = splitter.previousElementSibling;
        while (node) {
          if (node.matches('.table-wrap, .point-pages, #point-panels, .trend-point-list')) return node;
          node = node.previousElementSibling;
        }
        return null;
      };

      // 按父容器实际剩余高度动态计算分栏模式下允许的最大安全画布高度，防止把横坐标顶出底边。
      const computeMaxChartHeight = () => {
        const parentRect = parent.getBoundingClientRect();
        if (parentRect.height <= 0) return Math.max(150, window.innerHeight - 260);
        const parentStyle = getComputedStyle(parent);
        const padTop = parseFloat(parentStyle.paddingTop) || 0;
        const padBottom = parseFloat(parentStyle.paddingBottom) || 0;
        const rowGap = parseFloat(parentStyle.rowGap) || 0;
        const innerByParent = parent.clientHeight - padTop - padBottom;
        const innerByViewport = window.innerHeight - parentRect.top - padTop - padBottom - 4;
        const availableH = Math.min(innerByParent > 0 ? innerByParent : innerByViewport, innerByViewport);
        const tablePane = getTablePane();

        let visibleChildren = 0;
        let fixedSiblingsH = 0;
        for (const child of parent.children) {
          if (child === container || child === tablePane) {
            visibleChildren++;
            continue;
          }
          const cs = getComputedStyle(child);
          if (child.hidden || cs.display === 'none') continue;
          visibleChildren++;
          fixedSiblingsH += child.getBoundingClientRect().height
            + (parseFloat(cs.marginTop) || 0)
            + (parseFloat(cs.marginBottom) || 0);
        }
        const totalGaps = Math.max(0, visibleChildren - 1) * rowGap;

        let tableMinH = 0;
        if (tablePane) {
          const ts = getComputedStyle(tablePane);
          const cssMinH = parseFloat(ts.minHeight) || 0;
          tableMinH = Math.max(72, cssMinH) + (parseFloat(ts.marginTop) || 0) + (parseFloat(ts.marginBottom) || 0);
        }

        const hostStyle = getComputedStyle(container);
        const hostMargins = (parseFloat(hostStyle.marginTop) || 0) + (parseFloat(hostStyle.marginBottom) || 0);
        const containerRect = container.getBoundingClientRect();
        const chartRect = chart.getBoundingClientRect();
        const chromeH = (containerRect.height > 0 && chartRect.height > 0)
          ? Math.max(60, containerRect.height - chartRect.height) + hostMargins
          : 92 + hostMargins;

        return Math.max(120, Math.floor(availableH - fixedSiblingsH - totalGaps - chromeH - tableMinH));
      };

      const clampChartHeight = value => {
        if (!Number.isFinite(value)) return null;
        const maxH = computeMaxChartHeight();
        const minH = Math.min(150, maxH);
        return Math.max(minH, Math.min(maxH, Math.round(value)));
      };

      let splitMode = 'split'; // 'split' | 'table-hidden' | 'trend-hidden'
      let chartHeight = null;
      try {
        const saved = JSON.parse(localStorage.getItem(SPLIT_KEY) || 'null');
        if (saved && ['split', 'table-hidden', 'trend-hidden'].includes(saved.mode)) splitMode = saved.mode;
        if (saved && Number.isFinite(saved.height) && saved.height >= 120 && saved.height <= 900) chartHeight = saved.height;
      } catch (_) { /* ignore */ }

      const persistSplit = () => {
        try {
          localStorage.setItem(SPLIT_KEY, JSON.stringify({mode: splitMode, height: chartHeight}));
        } catch (_) { /* ignore */ }
      };

      const applySplit = (save = false) => {
        const hostHidden = container.hasAttribute('hidden');
        splitter.hidden = hostHidden;
        const upperPanes = getUpperPanes();
        const effectiveMode = hostHidden ? 'split' : splitMode;

        upperPanes.forEach(el => el.classList.toggle('point-trend-pane-hidden', effectiveMode === 'table-hidden'));
        container.classList.toggle('point-trend-pane-hidden', !hostHidden && effectiveMode === 'trend-hidden');
        container.classList.toggle('is-trend-expanded', !hostHidden && effectiveMode === 'table-hidden');
        splitter.dataset.mode = effectiveMode;

        if (!hostHidden && effectiveMode === 'table-hidden') {
          panel.open = true;
          chart.style.height = '';
        } else if (!hostHidden && effectiveMode === 'split') {
          if (chartHeight) {
            const safeH = clampChartHeight(chartHeight);
            chart.style.height = safeH ? `${safeH}px` : '';
          } else {
            chart.style.height = '';
          }
        }

        btnUp.classList.toggle('is-active', effectiveMode === 'table-hidden');
        btnDown.classList.toggle('is-active', effectiveMode === 'trend-hidden');
        btnUp.title = effectiveMode === 'table-hidden'
          ? '恢复显示上方表格'
          : effectiveMode === 'trend-hidden'
            ? '展开下方实时曲线'
            : '折叠上方表格（实时曲线全屏）';
        btnDown.title = effectiveMode === 'trend-hidden'
          ? '恢复显示下方实时曲线'
          : effectiveMode === 'table-hidden'
            ? '恢复显示上方表格'
            : '隐藏下方实时曲线（表格全屏）';

        if (save) persistSplit();
        updateActivity();
        scheduleDraw();
      };

      revealTrendPane = () => {
        if (splitMode === 'trend-hidden') {
          splitMode = 'split';
          panel.open = true;
          applySplit(true);
        }
      };

      btnUp.addEventListener('click', event => {
        event.stopPropagation();
        if (splitMode === 'trend-hidden') {
          splitMode = 'split';
          panel.open = true;
        } else if (splitMode === 'table-hidden') {
          splitMode = 'split';
        } else {
          splitMode = 'table-hidden';
          panel.open = true;
        }
        applySplit(true);
      });

      btnDown.addEventListener('click', event => {
        event.stopPropagation();
        if (splitMode === 'table-hidden') {
          splitMode = 'split';
        } else if (splitMode === 'trend-hidden') {
          splitMode = 'split';
          panel.open = true;
        } else {
          splitMode = 'trend-hidden';
        }
        applySplit(true);
      });

      splitter.addEventListener('dblclick', event => {
        if (event.target.closest('button')) return;
        splitMode = 'split';
        chartHeight = null;
        panel.open = true;
        applySplit(true);
      });

      let drag = null;
      splitter.addEventListener('pointerdown', event => {
        if (event.button !== 0 || event.target.closest('button')) return;
        const tablePane = getTablePane();
        drag = {
          y: event.clientY,
          mode: splitMode,
          chartH: chart.getBoundingClientRect().height || chartHeight || 220,
          tableH: tablePane ? tablePane.getBoundingClientRect().height : 260,
          maxSplitH: computeMaxChartHeight(),
        };
        splitter.classList.add('is-dragging');
        document.body.classList.add('point-trend-resizing');
        splitter.setPointerCapture?.(event.pointerId);
        event.preventDefault();
      });

      splitter.addEventListener('pointermove', event => {
        if (!drag) return;
        const dy = event.clientY - drag.y;
        const maxH = drag.maxSplitH || computeMaxChartHeight();
        const minH = Math.min(150, maxH);
        if (drag.mode === 'trend-hidden') {
          if (dy < -12) {
            drag.mode = splitMode = 'split';
            panel.open = true;
            chartHeight = Math.max(minH, Math.min(maxH, -dy));
            applySplit(false);
          }
          return;
        }
        if (drag.mode === 'table-hidden') {
          if (dy > 12) {
            drag.mode = splitMode = 'split';
            chartHeight = Math.max(minH, Math.min(maxH, drag.chartH - dy));
            applySplit(false);
          }
          return;
        }
        if (!panel.open && dy < -10) {
          panel.open = true;
        }
        const nextTableH = drag.tableH + dy;
        const nextChartH = drag.chartH - dy;
        if (nextTableH < 48) {
          splitMode = 'table-hidden';
          applySplit(false);
          return;
        }
        if (nextChartH < 72) {
          splitMode = 'trend-hidden';
          applySplit(false);
          return;
        }
        splitMode = 'split';
        chartHeight = Math.max(minH, Math.min(maxH, nextChartH));
        applySplit(false);
      });

      const endDrag = () => {
        if (!drag) return;
        drag = null;
        splitter.classList.remove('is-dragging');
        document.body.classList.remove('point-trend-resizing');
        if (splitMode === 'split' && chartHeight) chartHeight = clampChartHeight(chartHeight);
        persistSplit();
        scheduleDraw();
      };
      splitter.addEventListener('pointerup', endDrag);
      splitter.addEventListener('pointercancel', endDrag);

      const clampOnParentResize = () => {
        if (drag || splitMode !== 'split' || !chartHeight || !panel.open || container.hasAttribute('hidden')) return;
        const safeH = clampChartHeight(chartHeight);
        if (safeH && chart.style.height !== `${safeH}px`) {
          chart.style.height = `${safeH}px`;
          scheduleDraw();
        }
      };
      window.addEventListener('resize', clampOnParentResize);
      new ResizeObserver(clampOnParentResize).observe(parent);

      new MutationObserver(() => applySplit(false)).observe(container, {
        attributes: true,
        attributeFilter: ['hidden'],
      });
      applySplit(false);
    }

    panel.addEventListener('toggle', () => { updateActivity(); if (!management) saveSettings(); });
    document.addEventListener('visibilitychange', refreshAfterResume);
    document.addEventListener('scada:theme-change', scheduleDraw);
    document.addEventListener('scada:lang-change', scheduleDraw);
    window.addEventListener('pagehide', () => { pageHidden = true; updateActivity(); });
    window.addEventListener('pageshow', () => { pageHidden = false; refreshAfterResume(); });
    const resizeObserver = new ResizeObserver(updateActivity);
    resizeObserver.observe(panel);
    resizeObserver.observe(chart);
    changedSelection();
    loadHistory();
    return {checkbox, bindRow, syncCheckboxes, updateSeriesConfig, removeSeries, showSeries};
  }

  window.scadaPointTrend = {mount};
})();

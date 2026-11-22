/* Real-time trends for the selected point keys, independent of table pagination. */
(() => {
  'use strict';

  const LIMIT = 24;
  const SAMPLE_LIMIT = 7200;
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
        '<div class="point-trend-legend" aria-label="已选曲线图例"></div>' +
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
    const legend = find('.point-trend-legend');
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

    const interval = numberInput(5, 1, 60, 1);
    const retention = numberInput(60, 1, 1440, 1);
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
    toolbar.append(label('刷新周期（秒）', interval));
    if (!management) toolbar.append(label('后台保留（分钟）', retention));
    toolbar.append(label('时间区间', mode), span, alignmentLabel, fromLabel, untilLabel, zoomInBtn, zoomOutBtn, resetZoomBtn, exportCsvBtn);

    const series = new Map();
    let recordingKeys = new Set();
    let historyTimer, rollTimer, frame, lastSample = 0, colorIndex = 0;
    let refreshMs = 5000, retentionMs = 3600000, duration = 600000, fixed = null;
    let databaseChanged = false, pageHidden = false, wasReadingActive = false;
    let ready = false, reading = false, restoring = false, pendingSaves = 0;
    let settingsVersion = 0, saveQueue = Promise.resolve(), queuedSettings = '', serverSettings = '', serverRevision = '';
    let sourceOpen = false, sourceAxesOpen = false;
    let plotRange = null;
    let fetchError = '', saveError = '', settingsError = '', selectionError = '';
    const pageVisible = () => !pageHidden && !document.hidden && panel.getClientRects().length > 0;
    const canDraw = () => panel.open && pageVisible();
    const canRead = () => (management || panel.open) && pageVisible();

    function publishChange() {
      if (typeof onChange !== 'function' || restoring) return;
      onChange({
        ready:ready && !databaseChanged, error:[selectionError, settingsError, fetchError, saveError].filter(Boolean).join('；'),
        saving:pendingSaves > 0,
        intervalSeconds:refreshMs / 1000, retentionMinutes:retentionMs / 60000, lastSampleTime:lastSample || null,
        series:[...series.values()].map(item => {
          const latest = item.samples.at(-1);
          return {key:item.key, name:item.name, kind:item.kind, point:{...item.point}, unit:item.unit,
                  axis:item.axis.value, visible:item.visible.checked, color:item.color.value, width:item.thickness,
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
        if (item.chipValue) item.chipValue.textContent = text;
      }
    }

    function draw() {
      frame = 0;
      if (!canDraw()) return;
      const {start, end} = timeRange();
      const visible = [...series.values()].filter(item => item.visible.checked);
      const datasets = visible.map(item => ({item, data:item.samples.filter(point => point.time >= start && point.time <= end)}));
      empty.textContent = !series.size ? (management ? '请在测点页面加入实时曲线' : '勾选表格中的测点开始采样')
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
      const axisPalette = themeAxisColors();
      context.font = '11px "JetBrains Mono", "Segoe UI", "Roboto", "Microsoft YaHei", sans-serif';
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
        domains[side] = axisScale(control, low, high, Math.max(3, Math.floor(height / 40)));
        units[side] = axisUnits.size === 1 ? [...axisUnits][0] : '';
        if (domains[side].error) errors.push(AXIS_NAMES[side] + '范围或刻度间隔无效，已使用有效刻度');
      }
      updateStatus(errors, start, end);

      const margin = side => axes[side].show.checked ? (width < 440 ? 50 : 72) : 12;
      const left = margin('left'), right = width - margin('right'), top = 24, bottom = height - 27;
      plotRange = {start, end, left, right, top, bottom};
      const x = time => left + (right - left) * (time - start) / (end - start);
      const y = (value, domain) => bottom - (bottom - top) * (value - domain.low) / (domain.high - domain.low);
      const gridSide = axes.left.show.checked ? 'left' : 'right';
      const tickTarget = Math.max(1000, (end - start) / Math.max(2, Math.floor((right - left) / 108)));
      const tickStep = Math.max(1000, (TIME_STEPS.find(seconds => seconds * 1000 >= tickTarget)
        || Math.max(1, Math.ceil(tickTarget / DAY)) * DAY / 1000) * 1000);
      const clock = end - start >= 3600000 || new Date(start).toDateString() !== new Date(end).toDateString() ? FULL_TIME : TIME;
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
      context.textAlign = 'center';
      for (let time = Math.ceil(start / tickStep) * tickStep; time <= end; time += tickStep) {
        const tickX = x(time);
        grid(tickX, bottom, tickX, bottom + 4, axisLineColor);
        context.fillStyle = tickTextColor;
        const maxTickLabel = Math.min(110, 2 * Math.min(tickX - 2, width - tickX - 2));
        context.fillText(clock.format(time), tickX, bottom + 14, maxTickLabel);
      }
      grid(left, bottom, right, bottom, axisLineColor);
      for (const [side, control] of Object.entries(axes)) {
        if (!control.show.checked) continue;
        const isLeft = side === 'left', edge = isLeft ? left : right;
        const color = axisPalette[side];
        grid(edge, top, edge, bottom, color);
        context.fillStyle = color;
        context.textAlign = isLeft ? 'right' : 'left';
        let lastLabelY = Infinity;
        for (const tick of domains[side].ticks) {
          const tickY = y(tick, domains[side]);
          if (side === gridSide) grid(left, tickY, right, tickY, gridColor, true);
          grid(edge, tickY, edge + (isLeft ? -4 : 4), tickY, color);
          if (lastLabelY - tickY >= 13) {
            context.fillText(formatValue(tick), edge + (isLeft ? -8 : 8), tickY, margin(side) - 11);
            lastLabelY = tickY;
          }
        }
        context.textAlign = isLeft ? 'left' : 'right';
        const axisLabel = window.scadaI18n?.t(AXIS_NAMES[side], AXIS_NAMES[side]) || AXIS_NAMES[side];
        context.fillText(axisLabel + (units[side] ? ' (' + units[side] + ')' : ''), isLeft ? 7 : width - 7, 10, width / 2 - 8);
      }
      context.save();
      context.beginPath();
      context.rect(left, top, right - left, bottom - top);
      context.clip();
      for (const {item, data} of datasets) {
        const domain = domains[item.axis.value];
        const expectedStep = data.length > 2
          ? Math.max(refreshMs, (end - start) / Math.max(1, data.length - 1))
          : refreshMs;
        const gapMs = Math.max(refreshMs * 3, expectedStep * 3, 5000);
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
      if (!frame && canDraw()) frame = requestAnimationFrame(draw);
    }

    function hideHover() {
      cursor.hidden = tooltip.hidden = true;
    }

    canvas.addEventListener('pointerleave', hideHover);
    canvas.addEventListener('pointermove', event => {
      if (!plotRange) return;
      const {start, end, left, right, top, bottom} = plotRange;
      const {offsetX:x, offsetY:y} = event;
      if (x < left || x > right || y < top || y > bottom) {
        hideHover();
        return;
      }
      const time = start + (x - left) * (end - start) / (right - left);
      const readings = [];
      for (const item of series.values()) {
        if (!item.visible.checked) continue;
        const point = nearestSample(item.samples, time);
        if (point && point.time >= start && point.time <= end && Math.abs(point.time - time) <= refreshMs) {
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

    function appendSample(item, time, value, quality, sourceTime) {
      if (item.samples.at(-1)?.time >= time) return;
      const badQuality = quality !== null && quality !== undefined && Number(quality) !== 0;
      item.samples.push({time, value, quality, sourceTime, invalid: badQuality});
      const firstRetained = item.samples.findIndex(point => point.time >= time - retentionMs);
      const discard = Math.max(item.samples.length - SAMPLE_LIMIT, firstRetained);
      if (discard > 0) item.samples.splice(0, discard);
    }

    function currentSettings() {
      return {
        interval_seconds: refreshMs / 1000,
        retention_seconds: retentionMs / 1000,
        series: [...series.values()].map(item => ({point:item.point, name:item.name, unit:item.unit, kind:item.kind,
          visible:item.visible.checked, axis:item.axis.value, color:item.color.value, width:item.thickness})),
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
      settings ??= {interval_seconds:5, retention_seconds:3600, series:[]};
      restoring = true;
      try {
        interval.value = settings.interval_seconds;
        retention.value = settings.retention_seconds / 60;
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
        const savedPoints = settings.series.map(entry => {
          const def = {...entry.point, name:entry.name, unit:entry.unit, kind:entry.kind || entry.point.kind};
          const point = pointKey(def);
          return {entry, def, point, key:JSON.stringify(point)};
        });
        const wanted = new Set(savedPoints.map(row => row.key));
        for (const item of series.values()) if (!wanted.has(item.key)) remove(item);
        for (const {entry, def, point, key} of savedPoints) {
          const item = series.get(key) || add(def, key, point);
          item.visible.checked = entry.visible !== false;
          item.axis.value = entry.axis === 'right' ? 'right' : 'left';
          if (entry.color) item.color.value = entry.color;
          item.width.value = item.thickness = Number(entry.width) || 2;
          syncLegendChip(item);
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
      return JSON.stringify({
        interval: settings.interval_seconds,
        retention: settings.retention_seconds,
        points: (settings.series || []).map(entry => pointKey(entry.point)),
      });
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
      if (pendingSaves) { queueHistory(refreshMs); return; }
      reading = true;
      const version = settingsVersion;
      const after = lastSample;
      let nextDelay = refreshMs;
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
        ready = true;
        syncCheckboxes();
        // 仅当采样周期、留存时长或测点集合真正变更时才重载全量样本，折叠/改颜色等纯视图变化不重拉。
        if (dataChangedOnServer && after) {
          lastSample = 0;
          nextDelay = 0;
          return;
        }
        const items = (result.settings?.series || []).map(entry =>
          series.get(JSON.stringify(pointKey(entry.point))));
        if (!after) for (const item of items) if (item) item.samples.length = 0;
        for (const sample of result.samples) {
          for (const [index, item] of items.entries()) {
            if (!item) continue;
            const point = sample.values[index];
            const value = Number.isFinite(point?.value) ? point.value : null;
            appendSample(item, sample.time_ms, value, point?.quality ?? null, point?.time_ms ?? null);
          }
          lastSample = Math.max(lastSample, sample.time_ms);
        }
      } catch (error) {
        fetchError = '读取曲线失败：' + error.message;
      } finally {
        reading = false;
        scheduleDraw();
        queueHistory(nextDelay);
        publishChange();
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
    let rowMenu, menuAction, menuRow, menuReturnFocus;

    function syncRow(row) {
      const binding = rowBindings.get(row);
      if (!binding) return;
      const selected = recordingKeys.has(binding.key);
      row.classList.toggle('point-trend-row-active', selected);
      binding.cell?.classList.toggle('point-trend-cell-active', selected);
    }

    function closeRowMenu(restoreFocus = false) {
      if (!rowMenu || rowMenu.hidden) return;
      rowMenu.hidden = true;
      menuRow = null;
      if (restoreFocus && menuReturnFocus?.isConnected) menuReturnFocus.focus({preventScroll:true});
      menuReturnFocus = null;
    }

    function updateRowMenu() {
      if (!menuRow?.isConnected) {
        closeRowMenu();
        return;
      }
      const selected = series.has(rowBindings.get(menuRow).key);
      menuAction.textContent = !ready ? '正在读取曲线设置' : selected ? '已加入曲线' : '加入实时曲线';
      menuAction.setAttribute('aria-disabled', String(!ready || selected));
    }

    function requestPoint(definition, key, point) {
      if (!ready || series.has(key)) return;
      if (series.size >= LIMIT) {
        selectionError = '每个曲线板最多选择 ' + LIMIT + ' 条测点';
        panel.open = true;
        syncCheckboxes();
        scheduleDraw();
        return;
      }
      add(definition, key, point);
    }

    function openRowMenu(row, x, y) {
      if (!rowMenu) {
        rowMenu = make('div', {class:'point-trend-menu', role:'menu', 'aria-label':'测点曲线操作'});
        menuAction = make('button', {type:'button', role:'menuitem', tabindex:'-1'});
        rowMenu.append(menuAction);
        document.body.append(rowMenu);
        menuAction.addEventListener('click', () => {
          if (!menuRow?.isConnected || menuAction.getAttribute('aria-disabled') === 'true') return;
          const {definition, key, point} = rowBindings.get(menuRow);
          closeRowMenu(true);
          requestPoint(definition, key, point);
        });
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
      menuRow = row;
      menuReturnFocus = row.contains(document.activeElement) ? document.activeElement : row.querySelector('.point-trend-checkbox');
      rowMenu.hidden = false;
      updateRowMenu();
      rowMenu.style.left = Math.max(4, Math.min(x, document.documentElement.clientWidth - rowMenu.offsetWidth - 4)) + 'px';
      rowMenu.style.top = Math.max(4, Math.min(y, document.documentElement.clientHeight - rowMenu.offsetHeight - 4)) + 'px';
      menuAction.focus({preventScroll:true});
    }

    function bindRow(row, definition, activeCell) {
      const previous = rowBindings.get(row);
      const point = pointKey(definition), key = JSON.stringify(point);
      const cell = activeCell || row.querySelector('.point-trend-cell');
      if (previous?.cell && previous.cell !== cell) previous.cell.classList.remove('point-trend-cell-active');
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

    function syncLegendChip(item) {
      if (!item?.chip) return;
      item.chip.classList.toggle('is-hidden', !item.visible.checked);
      item.chipDot.style.background = item.color.value;
      item.chipAxis.textContent = item.axis.value === 'right' ? '右' : '左';
      item.chipAxis.classList.toggle('is-right', item.axis.value === 'right');
      item.chip.setAttribute('aria-pressed', String(item.visible.checked));
    }

    function remove(item) {
      series.delete(item.key);
      item.controls.remove();
      item.chip?.remove();
      if (!series.size) {
        lastSample = 0;
        if (!databaseChanged) fetchError = '';
        if (seriesDialog.open) seriesDialog.close();
      }
      changedSelection();
    }

    function add(definition, key, point) {
      const name = String(definition.name || definition.metric || (definition.pnt_no ?? definition.measurement_id));
      const unit = definition.unit || name.match(/[（(]([^）()]{1,16})[）)]$/)?.[1] || '';
      const row = make('div', {class:'point-trend-series-row'});
      const visible = make('input', {type:'checkbox', 'aria-label':'显示曲线 · ' + name});
      visible.checked = true;
      const title = make('span', {class:'point-trend-name'});
      title.textContent = name + (definition.unit && !name.includes('(' + definition.unit + ')') ? ' · ' + definition.unit : '');
      title.title = title.textContent;
      const latest = make('output', {class:'point-trend-latest', 'aria-label':'最近采样 · ' + name, 'aria-live':'off'});
      latest.textContent = '—';
      const color = make('input', {type:'color', 'aria-label':'曲线颜色 · ' + name, value:COLORS[colorIndex++ % COLORS.length]});
      const width = numberInput(2, 1, 8, 0.5);
      const axis = select(Object.entries(AXIS_NAMES));
      const removeButton = make('button', {type:'button', 'aria-label':'移除曲线 · ' + name});
      removeButton.textContent = '移除';
      const chip = make('button', {type:'button', class:'point-trend-chip', title:title.textContent + '（左键显隐，右键设置）'});
      const chipDot = make('span', {class:'point-trend-chip-dot', 'aria-hidden':'true'});
      const chipName = make('span', {class:'point-trend-chip-name'});
      chipName.textContent = name;
      const chipAxis = make('span', {class:'point-trend-chip-axis'});
      const chipValue = make('span', {class:'point-trend-chip-val'});
      chipValue.textContent = '—';
      chip.append(chipDot, chipName, chipAxis, chipValue);
      const item = {key, point, name, latest, samples:[], controls:row, visible, axis, color, width, thickness:2,
                    unit, kind:definition.kind, digital:['yx','yk'].includes(definition.kind),
                    chip, chipDot, chipAxis, chipValue};
      series.set(key, item);
      curves.append(row);
      legend.append(chip);
      row.append(visible, title, latest, label('坐标轴', axis), label('颜色', color), label('线宽', width), removeButton);
      syncLegendChip(item);
      chip.addEventListener('click', () => {
        visible.checked = !visible.checked;
        syncLegendChip(item);
        scheduleDraw();
        saveSettings();
      });
      chip.addEventListener('contextmenu', event => {
        event.preventDefault();
        event.stopPropagation();
        openChartMenu(event.clientX, event.clientY, item);
      });
      row.addEventListener('change', () => {
        if (width.value !== '' && width.validity.valid) item.thickness = width.valueAsNumber;
        else width.value = item.thickness;
        syncLegendChip(item);
        scheduleDraw();
        saveSettings();
      });
      color.addEventListener('input', () => {
        syncLegendChip(item);
        scheduleDraw();
      });
      removeButton.addEventListener('click', () => remove(item));
      if (!restoring) {
        lastSample = 0;
        panel.open = true;
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
      if (interval.value !== '' && interval.validity.valid) refreshMs = interval.valueAsNumber * 1000;
      else interval.value = refreshMs / 1000;
      if (retention.value !== '' && retention.validity.valid) retentionMs = retention.valueAsNumber * 60000;
      else retention.value = retentionMs / 60000;
      if (length.value !== '' && length.validity.valid) duration = length.valueAsNumber * 60000;
      else length.value = duration / 60000;
      for (const option of alignment.options) option.disabled = Number(option.value) * 4000 > duration;
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
      if (control !== interval && control !== alignment) lastSample = 0;
      saveSettings();
    }

    function setRetention(minutes) {
      if (!ready || databaseChanged || !Number.isInteger(minutes) || minutes < 1 || minutes > 1440) return false;
      retention.value = minutes;
      toolbarChanged(retention);
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
      panel.open = true;
      if (!item.visible.checked) {
        item.visible.checked = true;
        syncLegendChip(item);
        saveSettings();
      }
      updateActivity();
      requestAnimationFrame(() => chart.scrollIntoView({block:'nearest'}));
      return true;
    }

    toolbar.addEventListener('change', event => toolbarChanged(event.target));
    function zoomWindow(factor) {
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
      toolbarChanged(length);
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
      const addAction = (labelText, handler, {disabled = false, danger = false} = {}) => {
        const btn = make('button', {type:'button', role:'menuitem', tabindex:'-1'});
        btn.textContent = labelText;
        if (disabled) btn.setAttribute('aria-disabled', 'true');
        if (danger) btn.classList.add('is-danger');
        btn.addEventListener('click', () => {
          if (disabled) return;
          closeChartMenu();
          handler();
        });
        chartMenu.append(btn);
      };
      const addSep = () => chartMenu.append(make('div', {class:'point-trend-menu-sep', role:'separator'}));

      if (targetItem && series.has(targetItem.key)) {
        const nextAxis = targetItem.axis.value === 'right' ? 'left' : 'right';
        addAction('切换至' + AXIS_NAMES[nextAxis], () => {
          targetItem.axis.value = nextAxis;
          syncLegendChip(targetItem);
          scheduleDraw();
          saveSettings();
        });
        addAction(targetItem.visible.checked ? '隐藏此曲线' : '显示此曲线', () => {
          targetItem.visible.checked = !targetItem.visible.checked;
          syncLegendChip(targetItem);
          scheduleDraw();
          saveSettings();
        });
        addAction('移除此曲线', () => remove(targetItem), {danger:true});
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
      chartMenu.style.left = Math.max(4, Math.min(x, document.documentElement.clientWidth - chartMenu.offsetWidth - 4)) + 'px';
      chartMenu.style.top = Math.max(4, Math.min(y, document.documentElement.clientHeight - chartMenu.offsetHeight - 4)) + 'px';
      chartMenu.querySelector('button:not([aria-disabled="true"])')?.focus({preventScroll:true});
    }

    chart.addEventListener('contextmenu', event => {
      event.preventDefault();
      openChartMenu(event.clientX, event.clientY, null);
    });
    chart.addEventListener('keydown', event => {
      if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
        event.preventDefault();
        const rect = chart.getBoundingClientRect();
        openChartMenu(rect.left + rect.width / 2, rect.top + 36, null);
      }
    });

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
    return {checkbox, bindRow, syncCheckboxes, setRetention, removeSeries, showSeries};
  }

  window.scadaPointTrend = {mount};
})();

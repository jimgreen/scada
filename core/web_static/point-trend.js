/* Real-time trends for the selected point keys, independent of table pagination. */
(() => {
  'use strict';

  const LIMIT = 24;
  const SAMPLE_LIMIT = 7200;
  const HISTORY_URL = '/api/point-trend/history';
  const COLORS = ['#057f8c', '#9e4831', '#4754af', '#7d53a2', '#2b754d', '#a96314', '#b04470', '#416b90'];
  const TIME_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400];
  const FULL_TIME = new Intl.DateTimeFormat(undefined, {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  const TIME = new Intl.DateTimeFormat(undefined, {hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  const AXIS_NAMES = {left:'左轴', right:'右轴'};
  const AXIS_COLORS = {left:'#226270', right:'#87503a'};
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
      '<summary><strong>实时曲线</strong><span class="point-trend-count"></span></summary>' +
      '<div class="point-trend-body">' +
        '<div class="point-trend-toolbar"></div>' +
        '<div class="point-trend-chart"><canvas role="img" aria-label="已选测点实时值双轴曲线"></canvas>' +
          '<div class="point-trend-empty"></div><div class="point-trend-cursor" hidden></div>' +
          '<div class="point-trend-tooltip" hidden></div></div>' +
        '<p class="point-trend-range"></p><p class="point-trend-status"></p><p class="point-trend-error" role="alert" hidden></p>' +
        '<details class="point-trend-axis"><summary>纵轴设置</summary><div></div></details>' +
        '<div class="point-trend-series" aria-label="曲线设置"></div>' +
      '</div>';
    container.append(panel);
    container.classList.add('point-trend-host');
    const find = selector => panel.querySelector(selector);
    const summary = find('.point-trend-count');
    const toolbar = find('.point-trend-toolbar');
    const chart = find('.point-trend-chart');
    const canvas = find('canvas');
    const empty = find('.point-trend-empty');
    const cursor = find('.point-trend-cursor');
    const tooltip = find('.point-trend-tooltip');
    const rangeStatus = find('.point-trend-range');
    const status = find('.point-trend-status');
    const warning = find('.point-trend-error');
    const curves = find('.point-trend-series');
    const axesPanel = find('.point-trend-axis');
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
    toolbar.append(label('刷新周期（秒）', interval));
    if (!management) toolbar.append(label('后台保留（分钟）', retention));
    toolbar.append(label('时间区间', mode), span, alignmentLabel, fromLabel, untilLabel);
    const axes = {left:makeAxis('left'), right:makeAxis('right')};
    axesPanel.querySelector('div').append(axes.left.section, axes.right.section);

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
      rangeStatus.textContent = FULL_TIME.format(start) + ' – ' + FULL_TIME.format(end);
      status.textContent = lastSample
        ? '最近采样 ' + FULL_TIME.format(lastSample) + ' · 后台保留 ' + retentionMs / 60000 + ' 分钟 · 虚线表示无效品质'
        : '加入曲线的测点由后台持续采样';
      for (const item of series.values()) {
        const latest = item.samples.at(-1);
        item.latest.textContent = latest?.value != null
          ? formatValue(latest.value) + (latest.invalid ? '（无效）' : '') : '—';
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
      context.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';
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
      const tickTarget = (end - start) / Math.max(2, Math.floor((right - left) / 108));
      const tickStep = (TIME_STEPS.find(seconds => seconds * 1000 >= tickTarget)
        || Math.ceil(tickTarget / DAY) * DAY / 1000) * 1000;
      const clock = end - start >= 3600000 || new Date(start).toDateString() !== new Date(end).toDateString() ? FULL_TIME : TIME;
      const grid = (x1, y1, x2, y2, color = '#e1e9ef') => {
        context.strokeStyle = color;
        context.lineWidth = 1;
        context.beginPath();
        context.moveTo(x1, y1);
        context.lineTo(x2, y2);
        context.stroke();
      };
      context.textBaseline = 'middle';
      context.textAlign = 'center';
      for (let time = Math.ceil(start / tickStep) * tickStep; time <= end; time += tickStep) {
        const tickX = x(time);
        grid(tickX, top, tickX, bottom);
        context.fillStyle = '#546b7c';
        const maxTickLabel = Math.min(110, 2 * Math.min(tickX - 2, width - tickX - 2));
        context.fillText(clock.format(time), tickX, bottom + 13, maxTickLabel);
      }
      grid(left, bottom, right, bottom, '#a2b5c3');
      for (const [side, control] of Object.entries(axes)) {
        if (!control.show.checked) continue;
        const isLeft = side === 'left', edge = isLeft ? left : right;
        const color = AXIS_COLORS[side];
        grid(edge, top, edge, bottom, color);
        context.fillStyle = color;
        context.textAlign = isLeft ? 'right' : 'left';
        let lastLabelY = Infinity;
        for (const tick of domains[side].ticks) {
          const tickY = y(tick, domains[side]);
          if (side === gridSide) grid(left, tickY, right, tickY);
          grid(edge, tickY, edge + (isLeft ? -4 : 4), tickY, color);
          if (lastLabelY - tickY >= 13) {
            context.fillText(formatValue(tick), edge + (isLeft ? -8 : 8), tickY, margin(side) - 11);
            lastLabelY = tickY;
          }
        }
        context.textAlign = isLeft ? 'left' : 'right';
        context.fillText(AXIS_NAMES[side] + (units[side] ? ' · ' + units[side] : ''), isLeft ? 7 : width - 7, 10, width / 2 - 8);
      }
      context.save();
      context.beginPath();
      context.rect(left, top, right - left, bottom - top);
      context.clip();
      for (const {item, data} of datasets) {
        const domain = domains[item.axis.value];
        context.strokeStyle = item.color.value;
        context.lineWidth = item.thickness;
        context.lineJoin = 'round';
        context.lineCap = 'round';
        for (const invalid of [false, true]) {
          let previous = null, connected = false;
          context.setLineDash(invalid ? [5, 4] : []);
          context.beginPath();
          for (const point of data) {
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
      item.samples.push({time, value, quality, sourceTime, invalid:!!((quality ?? 0) & 128)});
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
        view: {open:management ? sourceOpen : panel.open, axes_open:management ? sourceAxesOpen : axesPanel.open,
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
        }
        sourceOpen = view.open === true;
        sourceAxesOpen = view.axes_open === true;
        if (!management) {
          panel.open = sourceOpen;
          axesPanel.open = sourceAxesOpen;
        }
        settingsChanged();
        changedSelection();
      } finally {
        restoring = false;
      }
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
        const settingsChangedOnServer = JSON.stringify(result.settings) !== serverSettings;
        if (settingsChangedOnServer) {
          restoreSettings(result.settings);
          queuedSettings = JSON.stringify(currentSettings());
        }
        confirmSettings(result);
        fetchError = '';
        ready = true;
        syncCheckboxes();
        if (settingsChangedOnServer && after) {
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

    function remove(item) {
      series.delete(item.key);
      item.controls.remove();
      if (!series.size) {
        lastSample = 0;
        if (!databaseChanged) fetchError = '';
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
      const item = {key, point, name, latest, samples:[], controls:row, visible, axis, color, width, thickness:2,
                    unit, kind:definition.kind, digital:['yx','yk'].includes(definition.kind)};
      series.set(key, item);
      curves.append(row);
      row.append(visible, title, latest, label('坐标轴', axis), label('颜色', color), label('线宽', width), removeButton);
      row.addEventListener('change', () => {
        if (width.value !== '' && width.validity.valid) item.thickness = width.valueAsNumber;
        else width.value = item.thickness;
        scheduleDraw();
        saveSettings();
      });
      color.addEventListener('input', scheduleDraw);
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
        saveSettings();
      }
      updateActivity();
      requestAnimationFrame(() => chart.scrollIntoView({block:'nearest'}));
      return true;
    }

    toolbar.addEventListener('change', event => toolbarChanged(event.target));
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

    panel.addEventListener('toggle', () => { updateActivity(); if (!management) saveSettings(); });
    if (!management) axesPanel.addEventListener('toggle', saveSettings);
    document.addEventListener('visibilitychange', refreshAfterResume);
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

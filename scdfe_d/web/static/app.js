(() => {
  'use strict';

  const $ = selector => document.querySelector(selector);
  const html = value => String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
  const mode = location.pathname;
  const mappingMode = mode === '/mappings';
  const queryMode = mode === '/measurements' || mode === '/devices';
  const parameterMode = mode === '/parameters';
  const controlDispatchMode = mode === '/controls';
  const controlMappingMode = mode === '/control-mappings';
  const controlMode = controlDispatchMode || controlMappingMode;
  const logsMode = mode === '/logs';
  const deviceMode = mappingMode || queryMode || parameterMode;
  const jumpPortalLink = $('#jump-portal-link');
  if (jumpPortalLink) {
    jumpPortalLink.href = location.protocol + '//' + location.hostname + ':8981/';
  }
  const pointTrendHost = $('#point-trend');
  const pointTrend = queryMode && pointTrendHost ? window.scadaPointTrend?.mount(pointTrendHost) : null;
  const liveColumnOffset = 0;
  if (pointTrend) pointTrendHost.hidden = false;
  const pointKinds = ['', 'yc', 'yx'];
  const reasons = {
    bad_quality: '通信异常（源质量不合格）', stale: '通信异常（数据超时）', missing_point: '通信异常（测点缺失）',
    source_error: '通信异常（源库不可用）', comm_error: '通信异常', disabled: '映射停用', notuse: '量测禁用',
    invalid_value: '通信异常（数据无效）', missing_time: '通信异常（无源时标）', future_time: '通信异常（源时标超前）',
    uncollected: '尚未采集', definition_changed: '定义已修改，等待采集',
    unmapped: '无配置', unbound: '无配置', no_mapping: '无配置', invalid_config: '无配置',
    over_upper_limit: '越上限', under_lower_limit: '越下限',
  };
  const qualityLabels = { 0: '0 · 正常', 1: '1 · 无配置', 2: '2 · 通信异常', 3: '3 · 越上限', 4: '4 · 越下限' };
  const qualityText = value => value == null ? '—' : (qualityLabels[Number(value)] ?? number(value));

  let overview;
  let catalog;
  let devices = [];
  let tables = [];
  let rows = [];
  let detail;
  let page = 1;
  let kind = '';
  let sequence = 0;
  let editing = null;
  let sourcePage = 1;
  let sourceSequence = 0;
  let historyMode = 'refreshes';
  let parentFolder = null;
  let browserMode = 'source';
  let ready = false;
  let polling = false;
  let saving = false;
  let parameterNotice = '';
  let overviewRequest = 0;
  let collectorActionPending = false;
  let intervalDraft = null;
  let intervalNotice = '';
  const drafts = new Map();
  const chosen = new Set();
  const parameters = ['enabled', 'notuse', 'weight', 'max_age_seconds', 'min_value', 'max_value'];
  const optionalLimitFields = new Set(['min_value', 'max_value']);
  const scopeFields = ['category', 'table', 'idx', 'device_id'];

  const number = value => value == null ? '—'
    : Number.isFinite(Number(value)) ? String(Number(Number(value).toPrecision(10))) : String(value);
  const parseLimitInput = value => {
    if (value == null || String(value).trim() === '') return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : Number.NaN;
  };
  const limitRangeError = (maxRaw, minRaw) => {
    const hi = parseLimitInput(maxRaw);
    const lo = parseLimitInput(minRaw);
    if (Number.isNaN(hi) || Number.isNaN(lo)) return '量测上下限必须为有限数值或留空';
    if (hi !== null && lo !== null && hi <= lo) return '量测上限必须大于下限，无法修改';
    return '';
  };
  const formatLimitRange = row => (row.min_value == null && row.max_value == null)
    ? '—' : number(row.min_value) + ' ~ ' + number(row.max_value);
  const time = value => {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
  };
  const option = (value, label) => '<option value="' + html(value) + '">' + html(label) + '</option>';
  const kindLabel = value => value === 'yx' ? '遥信 YX' : value === 'yc' ? '遥测 YC' : '—';
  const attached = row => row.measurement_id != null;
  const bound = row => Boolean(row.source_database && Number(row.rtu_id) > 0 && Number(row.pnt_no) > 0);
  const configurable = row => attached(row) && bound(row);
  const attrs = (row, field) => 'data-id="' + html(row.measurement_id) + '" data-param="' + field + '"';
  const parameterValue = (row, field) => drafts.get(row.measurement_id)?.[field] ?? row[field];

  function error(selector, message) {
    const element = $(selector);
    element.textContent = message || '';
    element.hidden = !message;
  }

  async function api(url, method = 'GET', body) {
    const options = { method };
    if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
      options.signal = AbortSignal.timeout(12000);
    }
    if (body !== undefined) {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    const text = await response.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_) {
        if (!response.ok) {
          const failure = new Error('请求失败（HTTP ' + response.status + '）');
          failure.status = response.status;
          throw failure;
        }
        throw new Error('服务端返回了非 JSON 响应');
      }
    }
    if (!response.ok) {
      const failure = new Error(data.error?.message || '请求失败（HTTP ' + response.status + '）');
      failure.status = response.status;
      throw failure;
    }
    return data;
  }

  const run = fn => async (...args) => {
    error('#alert', '');
    try {
      await fn(...args);
    } catch (exception) {
      error('#alert', exception.message);
    }
  };

  function renderCollectorControls() {
    const state = overview.collector;
    const busy = state.running || state.external_running;
    $('#collector-state').textContent = state.external_running ? '独立后台采集中' : state.running ? '采集中' : '已停止';
    $('#collector-state').classList.toggle('ok', busy);
    $('#collect-start').disabled = collectorActionPending || busy;
    $('#collect-once').disabled = collectorActionPending || busy;
    $('#collect-stop').disabled = collectorActionPending || !state.running;
    const interval = $('#interval');
    const modified = intervalDraft !== null && Number(intervalDraft) !== overview.interval_seconds;
    if (intervalDraft === null && interval.value !== String(overview.interval_seconds)) {
      interval.value = overview.interval_seconds;
    }
    interval.disabled = collectorActionPending;
    $('#interval-save').disabled = collectorActionPending || !modified;
    $('#interval-effective').textContent = `当前周期 ${number(overview.interval_seconds)} 秒`;
    $('#interval-feedback').textContent = intervalNotice || (modified ? '未保存' : '');
    $('#collector-error').textContent = state.last_error || '';
  }

  async function summary() {
    const request = ++overviewRequest;
    const data = await api('/api/overview');
    if (request !== overviewRequest) return;
    overview = data;
    const counts = overview.counts;
    $('#device-count').textContent = counts.device;
    $('#property-count').textContent = counts.device_measurement;
    $('#mapping-count').textContent = counts.scada_point_mapping;
    $('#valid-count').textContent = counts.valid;
    if ($('#control-count')) $('#control-count').textContent = counts.control ?? 0;
    renderCollectorControls();
    const last = overview.last_refresh;
    $('#last-refresh').textContent = last
      ? '最近批次 ' + last.refresh_id + ' · ' + time(last.finished_at) + ' · ' + last.total_count
        + ' 个属性 · ' + last.valid_count + ' 个可用 · ' + (last.total_count - last.valid_count) + ' 个不可用'
      : '尚未执行采集';
    $('#category-rows').innerHTML = overview.categories.map(row => '<tr><td>'
      + html(overview.category_labels[row.category] || row.category) + '</td><td>' + html(row.count)
      + '</td><td class="quantity-value">' + html(row.valid_count ?? 0) + ' / ' + html(row.bound_count ?? 0)
      + '</td><td class="quantity-value">' + html(row.control_count ?? 0)
      + (row.control_pending ? ' <span class="ctrl-status s-1">待回执 ' + html(row.control_pending) + '</span>' : '')
      + '</td><td><span class="cat-links"><a href="/measurements?category=' + encodeURIComponent(row.category)
      + '">量测 →</a>'
      + ((row.control_count ?? 0) > 0 ? '<a href="/controls?category=' + encodeURIComponent(row.category) + '">指令 →</a>' : '')
      + '</span></td></tr>').join('') || '<tr><td colspan="5" class="empty">暂无设备。</td></tr>';
    const recentCtrls = overview.recent_controls || [];
    const recentWrap = $('#overview-recent-ctrl');
    if (recentWrap) {
      recentWrap.hidden = !recentCtrls.length;
      if (recentCtrls.length) {
        $('#overview-ctrl-rows').innerHTML = recentCtrls.map(r => '<tr><td>' + html(time(r.at))
          + '</td><td>' + html(r.source === 'db_watch' ? '库监视' : 'Web')
          + '</td><td class="name">' + html(r.device_name || r.model_table)
          + '</td><td><code>' + html(r.control) + '</code></td><td class="quantity-value">' + html(number(r.value))
          + '</td><td><span class="ctrl-status s-' + html(r.cmd_status) + '">' + html(r.status_label) + '</span></td></tr>').join('');
      }
    }
  }

  async function controlCollector(action) {
    if (!overview || collectorActionPending) return;
    collectorActionPending = true;
    overviewRequest += 1;
    renderCollectorControls();
    try {
      await action();
    } finally {
      collectorActionPending = false;
      renderCollectorControls();
    }
  }

  async function saveInterval(event) {
    event.preventDefault();
    if (!overview || collectorActionPending || intervalDraft === null) return;
    const interval = Number(intervalDraft);
    if (interval === overview.interval_seconds) return;
    error('#interval-error', '');
    if (!Number.isInteger(interval) || interval < 1 || interval > 60) {
      error('#interval-error', '采集周期必须是 1–60 秒的整数');
      return;
    }
    intervalNotice = '正在保存…';
    await controlCollector(async () => {
      try {
        const data = await api('/api/collector/interval', 'PUT', { interval_seconds: interval });
        overview.interval_seconds = data.interval_seconds;
        overview.collector = data;
        intervalDraft = null;
        intervalNotice = '已保存';
      } catch (exception) {
        intervalNotice = '';
        error('#interval-error', exception.message);
      }
    });
  }

  function currentDevice() {
    const device = devices.find(row => String(row.device_id) === $('#device').value);
    const member = device?.tables?.find(row => row.table === $('#model-table').value);
    return member ? { table: member.table, idx: member.idx, device_id: device.device_id, name: device.name } : null;
  }

  function syncDeviceScope() {
    const selected = currentDevice();
    const scope = {
      category: $('#category').value,
      table: $('#model-table').value,
      idx: selected?.idx,
      device_id: selected ? '' : $('#device').value,
    };
    const withScope = url => {
      for (const field of scopeFields) {
        const value = scope[field];
        if (value == null || value === '') url.searchParams.delete(field);
        else url.searchParams.set(field, value);
      }
      return url.pathname + url.search + url.hash;
    };
    const current = location.pathname + location.search + location.hash;
    const target = withScope(new URL(location.href));
    if (target !== current) history.replaceState(history.state, '', target);
    for (const link of document.querySelectorAll('.workbench-sidebar a.nav')) {
      const destination = new URL(link.href);
      if (['/mappings', '/measurements', '/parameters'].includes(destination.pathname)) {
        link.href = withScope(destination);
      }
    }
  }

  function updateDeviceOptions(preferredDevice = $('#device').value, requestedIndex) {
    const category = $('#category').value;
    const table = $('#model-table').value;
    const matches = devices.filter(device => (!category || device.category === category)
      && (!table || device.tables.some(member => member.table === table)));
    const allowAll = mappingMode || queryMode;
    $('#device').innerHTML = (allowAll ? option('', '全部设备') : '')
      + matches.map(device => {
        const member = device.tables.find(row => row.table === table);
        const label = member ? member.idx + ' · ' + device.name : device.name;
        return option(device.device_id, label);
      }).join('');
    if (requestedIndex != null && table) {
      const wanted = matches.find(device => device.tables.some(member =>
        member.table === table && String(member.idx) === String(requestedIndex)));
      if (!wanted) {
        $('#device').selectedIndex = -1;
        return;
      }
      preferredDevice = String(wanted.device_id);
    }
    if (allowAll && (preferredDevice === '' || preferredDevice == null)) {
      $('#device').value = '';
    } else if (matches.some(device => String(device.device_id) === String(preferredDevice))) {
      $('#device').value = preferredDevice;
    }
  }

  function updateTableOptions(preferredTable = $('#model-table').value) {
    const category = $('#category').value;
    const matchingTables = category ? new Set(devices
      .filter(device => device.category === category)
      .flatMap(device => device.tables.map(member => member.table))) : null;
    const available = tables.filter(table => !matchingTables || matchingTables.has(table.table_name));
    const allowAll = mappingMode || queryMode;
    $('#model-table').innerHTML = (allowAll ? option('', '全部设备表') : '')
      + available.map(row => option(row.table_name, row.table_name + ' (' + row.count + ')')).join('');
    if (allowAll && (preferredTable === '' || preferredTable == null)) {
      $('#model-table').value = '';
    } else if (available.some(table => table.table_name === preferredTable)) {
      $('#model-table').value = preferredTable;
    } else if (!allowAll && available.some(table => table.table_name === 'ACGenerator' && table.count)) {
      $('#model-table').value = 'ACGenerator';
    } else if (!allowAll && available.some(table => table.count)) {
      $('#model-table').value = available.find(table => table.count).table_name;
    }
  }

  async function loadDevices(requested) {
    try {
      catalog = await api('/api/device-catalog');
    } catch (exception) {
      if (exception.status !== 404) throw exception;
      // Static assets can reload while the previous Web process is still running.
      const [previousOverview, previousDevices, previousTables] = await Promise.all([
        api('/api/overview'), api('/api/devices'), api('/api/device-tables'),
      ]);
      catalog = {
        devices: previousDevices.items, tables: previousTables.items,
        category_labels: previousOverview.category_labels, sources: previousOverview.sources,
      };
    }
    devices = catalog.devices;
    tables = catalog.tables;
    const requestedCategory = requested?.get('category') ?? $('#category').value;
    const categoryOptions = Object.entries(catalog.category_labels)
      .map(([value, label]) => option(value, label)).join('');
    $('#category').innerHTML = option('', '全部类型') + categoryOptions;
    if (Object.hasOwn(catalog.category_labels, requestedCategory)) $('#category').value = requestedCategory;
    $('#device-category').innerHTML = categoryOptions;
    const requestedDevice = requested?.get('device_id');
    let requestedTable = requested?.get('table');
    if (!requestedTable && requestedDevice && !mappingMode) {
      requestedTable = devices.find(device => String(device.device_id) === requestedDevice)?.tables[0]?.table;
    }
    updateTableOptions(requestedTable ?? $('#model-table').value);
    updateDeviceOptions(requestedDevice ?? $('#device').value, requested?.get('idx'));
    if (requestedTable && $('#model-table').value !== requestedTable) {
      $('#model-table').selectedIndex = -1;
      $('#device').selectedIndex = -1;
    }
    if (mappingMode) {
      $('#edit-device').innerHTML = devices.map(device => option(device.device_id,
        (catalog.category_labels[device.category] || device.category) + ' · ' + device.name)).join('');
    }
    syncDeviceScope();
  }

  function filtered(items) {
    if (parameterMode && $('#only-draft').checked) {
      return items.filter(row => drafts.has(row.measurement_id));
    }
    const keyword = $('#search').value.trim().toLowerCase();
    return items.filter(row => (!kind || row.kind === kind)
      && (!$('#only-invalid').checked || !row.valid)
      && (!keyword || [row.metric, row.name].some(value => String(value || '').toLowerCase().includes(keyword))));
  }

  function emptyRow(headers, message) {
    return '<tr><td colspan="' + headers.length + '" class="empty">' + html(message) + '</td></tr>';
  }

  function statusMarkup(row) {
    const mapped = bound(row);
    const q = row.quality == null ? null : Number(row.quality);
    let label = '异常';
    let color = 'comm';
    let title = reasons[row.reason] || row.reason || '';
    if (!mapped || q === 1 || row.reason === 'unmapped' || row.reason === 'invalid_config') {
      label = '无配置';
      color = 'unbound';
    } else if (q === 2 || ['bad_quality', 'stale', 'missing_point', 'source_error', 'comm_error', 'invalid_value', 'missing_time', 'future_time'].includes(row.reason)) {
      label = '通信异常';
      color = 'comm';
    } else if (q === 3 || row.reason === 'over_upper_limit') {
      label = '越上限';
      color = 'over';
    } else if (q === 4 || row.reason === 'under_lower_limit') {
      label = '越下限';
      color = 'under';
    } else if (!row.enabled) {
      label = '映射停用';
      color = 'unbound';
    } else if (row.notuse) {
      label = '量测禁用';
      color = 'unbound';
    } else if (row.valid || q === 0) {
      label = '正常';
      color = 'good';
    } else {
      label = reasons[row.reason] || row.reason || '不可用';
      color = 'comm';
    }
    const tip = title && title !== label ? ' title="' + html(title) + '"' : '';
    return '<span class="measure-status ' + color + '"' + tip + '>' + html(label) + '</span>';
  }

  function quantityMarkup(row, includeDevice) {
    const primary = String(row.name || row.metric || '');
    const metric = String(row.metric || '');
    const tableName = row.model_table || detail?.device?.table || '';
    const idx = row.model_idx ?? detail?.device?.idx ?? '';
    const devName = row.device_name || detail?.device?.name || '';
    const subParts = [];
    if (includeDevice) {
      const code = (tableName ? `${tableName}[${idx}].` : '') + metric;
      if (code) subParts.push(`<code>${html(code)}</code>`);
      if (devName) subParts.push(html(devName));
    } else if (metric && metric !== primary) {
      subParts.push(`<code>${html(metric)}</code>`);
    }
    const subHtml = subParts.length ? `<small>${subParts.join(' · ')}</small>` : '';
    const plainTitle = [
      primary,
      includeDevice ? `${tableName ? `${tableName}[${idx}].` : ''}${metric}` : (metric !== primary ? metric : ''),
      includeDevice ? devName : '',
    ].filter(Boolean).join(' · ');
    return `<td class="quantity-identity" title="${html(plainTitle)}"><strong>${html(primary)}</strong>${subHtml}</td>`;
  }

  function sourceMarkup(row) {
    return '<td>' + (bound(row) ? html(row.rtu_id) + ' / ' + html(row.pnt_no) : '—') + '</td>';
  }

  function mappingRow(row, index) {
    const source = html(row.source_database || '—');
    const mapped = bound(row);
    const label = mapped ? (row.enabled ? '启用' : '停用') : '未绑定';
    return '<tr>' + quantityMarkup(row, true) + '<td>' + kindLabel(row.kind) + '</td>'
      + '<td class="path" title="' + source + '">' + source + '</td>' + sourceMarkup(row)
      + '<td>' + (mapped ? '× ' + html(number(row.scale)) + ' + ' + html(number(row.offset)) : '—') + '</td>'
      + '<td>' + label + '</td><td><button data-edit="' + index + '">' + (attached(row) ? '编辑' : '绑定') + '</button>'
      + (attached(row) ? ' <button data-delete="' + index + '">删除</button>' : '') + '</td></tr>';
  }

  function liveRow(row) {
    return '<tr>'
      + quantityMarkup(row, !detail) + '<td>' + kindLabel(row.kind) + '</td>'
      + '<td class="quantity-value col-num">' + html(number(row.value)) + (row.unit ? ' ' + html(row.unit) : '') + '</td>'
      + '<td class="limit-range col-num">' + html(formatLimitRange(row)) + '</td>'
      + '<td class="col-num">' + html(time(row.source_time_ms)) + '</td><td class="col-num">' + html(time(row.collected_at)) + '</td>'
      + '<td>' + html(qualityText(row.quality)) + '</td><td>' + statusMarkup(row) + '</td>'
      + '<td class="col-num">' + html(number(row.previous_value)) + '</td>' + sourceMarkup(row) + '</tr>';
  }

  function updateLiveRows(previous, items) {
    if (previous.length !== items.length || !items.length) return false;
    const body = $('#measure-rows');
    if (body.rows.length !== items.length || !items.every((row, index) => {
      const prior = previous[index];
      return ['measurement_id', 'device_id', 'model_table', 'model_idx', 'metric', 'kind', 'name', 'unit']
        .every(field => row[field] === prior[field]);
    })) return false;
    items.forEach((row, index) => {
      const prior = previous[index];
      const cells = body.rows[index].cells;
      if (row.value !== prior.value) cells[2 + liveColumnOffset].textContent = number(row.value) + (row.unit ? ' ' + row.unit : '');
      if (row.min_value !== prior.min_value || row.max_value !== prior.max_value) {
        cells[3 + liveColumnOffset].textContent = formatLimitRange(row);
      }
      if (row.source_time_ms !== prior.source_time_ms) cells[4 + liveColumnOffset].textContent = time(row.source_time_ms);
      if (row.collected_at !== prior.collected_at) cells[5 + liveColumnOffset].textContent = time(row.collected_at);
      if (row.quality !== prior.quality) cells[6 + liveColumnOffset].textContent = qualityText(row.quality);
      if (['enabled', 'notuse', 'valid', 'reason', 'quality'].some(field => row[field] !== prior[field])
          || bound(row) !== bound(prior)) cells[7 + liveColumnOffset].innerHTML = statusMarkup(row);
      if (row.previous_value !== prior.previous_value) cells[8 + liveColumnOffset].textContent = number(row.previous_value);
      if (row.rtu_id !== prior.rtu_id || row.pnt_no !== prior.pnt_no || bound(row) !== bound(prior)) {
        cells[9 + liveColumnOffset].textContent = bound(row) ? row.rtu_id + ' / ' + row.pnt_no : '—';
      }
    });
    return true;
  }

  function configRow(row) {
    const identity = quantityMarkup(row, false) + '<td>' + kindLabel(row.kind) + '</td>' + sourceMarkup(row);
    if (!configurable(row)) {
      const destination = new URLSearchParams({ table: detail.device.table, idx: detail.device.idx, metric: row.metric });
      return '<tr><td></td>' + identity + '<td colspan="6"><a href="/mappings?' + html(destination.toString())
        + '">绑定测点后配置</a></td></tr>';
    }
    const label = html(row.name || row.metric);
    const checkbox = (field, heading) => '<td class="parameter-check"><input ' + attrs(row, field)
      + ' type="checkbox" aria-label="' + label + ' · ' + heading + '"'
      + (parameterValue(row, field) ? ' checked' : '') + (saving ? ' disabled' : '') + '></td>';
    const numberField = (field, heading, min) => '<td class="parameter-cell"><input ' + attrs(row, field)
      + ' class="parameter-field cell-inline-control" type="number" step="any" min="' + min + '" required aria-label="'
      + label + ' · ' + heading + '" value="' + html(parameterValue(row, field)) + '"'
      + (saving ? ' disabled' : '') + '></td>';
    const minVal = parameterValue(row, 'min_value');
    const maxVal = parameterValue(row, 'max_value');
    const limitInvalid = Boolean(limitRangeError(maxVal, minVal));
    const limitField = (field, heading, val) => '<td class="parameter-cell"><input ' + attrs(row, field)
      + ' class="parameter-field cell-inline-control' + (limitInvalid ? ' limit-invalid' : '') + '" type="number" step="any" placeholder="—"'
      + ' aria-label="' + label + ' · ' + heading + '" value="' + html(val == null ? '' : val) + '"'
      + (saving ? ' disabled' : '') + '></td>';
    const selection = '<td class="parameter-check"><input type="checkbox" data-choose="' + html(row.measurement_id)
      + '" aria-label="选择 ' + label + '"' + (chosen.has(row.measurement_id) ? ' checked' : '')
      + (saving ? ' disabled' : '') + '></td>';
    return '<tr' + (drafts.has(row.measurement_id) ? ' class="changed"' : '') + '>' + selection + identity
      + checkbox('enabled', '采集映射启用') + checkbox('notuse', '禁用量测')
      + numberField('weight', '权重', '0') + numberField('max_age_seconds', '超时阈值（秒）', '0')
      + limitField('min_value', '量测下限', minVal) + limitField('max_value', '量测上限', maxVal) + '</tr>';
  }

  function renderRows(items, total, limit) {
    const previous = rows;
    rows = items;
    if (parameterMode) {
      const visible = new Set(items.filter(configurable).map(row => row.measurement_id));
      for (const id of chosen) if (!visible.has(id)) chosen.delete(id);
    }
    const headers = mappingMode
      ? ['目标设备量测', '类型', '源数据库', 'RTU / 点号', '变换', '映射状态', '操作']
      : parameterMode
        ? ['<input id="choose-all" type="checkbox" aria-label="选择当前显示的可配置量测" title="全选当前显示的可配置量测">', '量测属性', '类型', 'RTU / 点号', '启用采集映射', '禁用量测', '量测权重', '超时阈值（秒）', '下限', '上限']
        : ['量测属性', '类型', '实时值', '上下限', '源时间', '采集时间', '质量', '状态', '前次值', 'RTU / 点号'];
    const renderer = mappingMode ? mappingRow : parameterMode ? configRow : liveRow;
    if (!$('#measure-head').firstElementChild) {
      $('#measure-head').innerHTML = '<tr>' + headers.map(label => '<th scope="col">' + label + '</th>').join('') + '</tr>';
    }
    if (!queryMode || !updateLiveRows(previous, items)) {
      const body = $('#measure-rows');
      body.innerHTML = rows.map(renderer).join('')
        || emptyRow(headers, detail || mappingMode || queryMode ? '没有匹配的量测' : '请选择设备表和设备');
      if (pointTrend) rows.forEach((row, index) => {
        const element = body.rows[index];
        if (!element || !attached(row)) return;
        const quantity = row.name || row.metric;
        const devName = detail?.device?.name || row.device_name || '';
        const name = devName ? `${devName} · ${quantity}` : quantity;
        const definition = {measurement_id: row.measurement_id, kind: row.kind, name, unit: row.unit};
        pointTrend.bindRow?.(element, definition, element.cells[0]);
      });
    }
    const count = total ?? rows.length;
    const tableHint = $('#table-hint');
    if (tableHint) {
      tableHint.textContent = detail?.device
        ? detail.device.name + ' · ' + detail.device.table + '[' + detail.device.idx + ']'
        : queryMode
          ? [$('#category').selectedOptions[0]?.textContent, $('#model-table').value || '全部设备表', '全部设备'].filter(Boolean).join(' · ')
          : '';
    }
    $('#page-summary').textContent = limit && count
      ? '共 ' + count + ' 项 · 当前 ' + ((page - 1) * limit + 1) + '–' + Math.min(page * limit, count)
      : '共 ' + count + ' 项量测';
    $('#page-actions').hidden = !limit;
    if (limit) {
      $('#page-number').textContent = page + ' / ' + Math.max(1, Math.ceil(count / limit));
      $('#previous').disabled = page <= 1;
      $('#next').disabled = page * limit >= count;
    }
    if (parameterMode) updateParameterButtons();
  }

  async function loadRows() {
    const request = ++sequence;
    const selection = currentDevice();
    if (parameterMode && !selection) {
      detail = null;
      renderRows([], 0);
      return;
    }
    try {
      if (selection) {
        const query = new URLSearchParams({ table: selection.table, idx: selection.idx });
        if (queryMode) query.set('view', 'live');
        const response = await api('/api/device-measurements?' + query);
        if (request !== sequence) return;
        detail = response;
        renderRows(filtered(detail.items));
        return;
      }

      const query = new URLSearchParams({
        category: $('#category').value, table: $('#model-table').value, device_id: $('#device').value,
        kind, q: $('#search').value.trim(), page, invalid: $('#only-invalid').checked ? '1' : '0',
      });
      const response = await api('/api/measurements?' + query);
      if (request !== sequence) return;
      detail = null;
      if (page > 1 && !response.items.length) {
        page = Math.max(1, Math.ceil(response.total / (response.limit || 50)));
        await loadRows();
        return;
      }
      renderRows(response.items, response.total, response.limit || 50);
    } catch (exception) {
      if (request === sequence) throw exception;
    }
  }

  function refreshRowsForFilter() {
    page = 1;
    if (detail && currentDevice()) {
      renderRows(filtered(detail.items));
      return;
    }
    return loadRows();
  }

  function validateDraftLimits() {
    if (!detail) return { message: '', invalidIds: new Set() };
    const invalidIds = new Set();
    let firstMessage = '';
    for (const row of detail.items) {
      if (!drafts.has(row.measurement_id)) continue;
      const err = limitRangeError(parameterValue(row, 'max_value'), parameterValue(row, 'min_value'));
      if (err) {
        invalidIds.add(row.measurement_id);
        if (!firstMessage) firstMessage = (row.name || row.metric) + '：' + err;
      }
    }
    return { message: firstMessage, invalidIds };
  }

  function updateParameterButtons() {
    const { message: limitAlert, invalidIds } = validateDraftLimits();
    for (const input of $('#measure-rows').querySelectorAll('input[data-param="min_value"], input[data-param="max_value"]')) {
      input.classList.toggle('limit-invalid', invalidIds.has(Number(input.dataset.id)));
    }
    if (limitAlert) error('#alert', limitAlert);
    else if ($('#alert').textContent.includes('上限必须大于下限') || $('#alert').textContent.includes('上下限必须为有限数值')) {
      error('#alert', '');
    }
    $('#save-parameters').disabled = saving || !drafts.size || Boolean(limitAlert);
    $('#discard-parameters').disabled = saving || !drafts.size;
    for (const selector of ['#category', '#model-table', '#device', '#refresh']) $(selector).disabled = saving || !!drafts.size;
    $('#parameter-status').textContent = saving ? '正在保存…'
      : limitAlert ? '存在非法上下限，请修正后保存'
      : drafts.size ? drafts.size + ' 项未保存 · 保存或撤销后可切换设备' : parameterNotice;
    $('#draft-filter').hidden = !drafts.size;
    const selectable = rows.filter(configurable).length;
    const all = $('#choose-all');
    all.checked = selectable > 0 && chosen.size === selectable;
    all.indeterminate = chosen.size > 0 && chosen.size < selectable;
    all.disabled = saving || !selectable;
    $('#bulk-parameters').disabled = saving || !chosen.size;
    const selectionCount = $('#parameter-selection-count');
    selectionCount.textContent = '已选 ' + chosen.size + '/' + selectable;
    selectionCount.title = '已选 ' + chosen.size + ' 项 · 当前显示 ' + selectable + ' 项可配置量测';
  }

  function setDraft(row, changes) {
    const next = { ...drafts.get(row.measurement_id) };
    for (const [field, value] of Object.entries(changes)) {
      if (optionalLimitFields.has(field)) {
        const curNorm = row[field] == null ? null : Number(row[field]);
        const newNorm = value == null || String(value).trim() === '' ? null : Number(value);
        if (curNorm === newNorm) delete next[field];
        else next[field] = value == null ? '' : value;
      } else if (value !== '' && Number(value) === Number(row[field])) {
        delete next[field];
      } else {
        next[field] = value;
      }
    }
    if (Object.keys(next).length) drafts.set(row.measurement_id, next);
    else drafts.delete(row.measurement_id);
  }

  function updateDraft(event) {
    if (saving) return;
    const input = event.target.closest('input[data-param]');
    if (!input || !detail) return;
    const id = Number(input.dataset.id);
    const row = detail.items.find(item => item.measurement_id === id);
    if (!row) return;
    const field = input.dataset.param;
    const value = input.type === 'checkbox' ? Number(input.checked) : input.value;
    setDraft(row, { [field]: value });
    input.closest('tr').classList.toggle('changed', drafts.has(id));
    parameterNotice = '';
    if ($('#only-draft').checked && !drafts.has(id)) {
      if (!drafts.size) $('#only-draft').checked = false;
      renderRows(filtered(detail.items));
      return;
    }
    updateParameterButtons();
  }

  function openBulkParameters() {
    if (!detail || !chosen.size || saving) return;
    $('#bulk-parameter-form').reset();
    error('#bulk-parameter-error', '');
    const selected = rows.filter(row => chosen.has(row.measurement_id));
    $('#bulk-parameter-scope').textContent = detail.device.name + ' · ' + detail.device.table
      + '[' + detail.device.idx + '] · ' + selected.length + ' 项';
    $('#bulk-parameter-items').innerHTML = selected.map(row =>
      '<li><code>' + html(row.metric) + '</code> ' + html(row.name) + '</li>').join('');
    $('#bulk-parameter-editor').showModal();
  }

  function applyBulkParameters(event) {
    event.preventDefault();
    const changes = {};
    for (const field of parameters) {
      const input = $('#bulk-' + field);
      if (!input) continue;
      if (input.validity.badInput) {
        error('#bulk-parameter-error', '请输入有效数字');
        return;
      }
      if (input.value === '') continue;
      const value = Number(input.value);
      if (!Number.isFinite(value) || ((field === 'weight' || field === 'max_age_seconds') && value <= 0)) {
        error('#bulk-parameter-error', '权重和超时阈值须为正数');
        return;
      }
      changes[field] = value;
    }
    if (!Object.keys(changes).length) {
      error('#bulk-parameter-error', '请选择至少一项要修改的参数');
      return;
    }
    if ('max_value' in changes || 'min_value' in changes) {
      if ('max_value' in changes && 'min_value' in changes && changes.max_value <= changes.min_value) {
        error('#bulk-parameter-error', '量测上限必须大于下限，无法修改');
        return;
      }
      for (const row of rows) {
        if (!configurable(row) || !chosen.has(row.measurement_id)) continue;
        const nextMax = 'max_value' in changes ? changes.max_value : parameterValue(row, 'max_value');
        const nextMin = 'min_value' in changes ? changes.min_value : parameterValue(row, 'min_value');
        const err = limitRangeError(nextMax, nextMin);
        if (err) {
          error('#bulk-parameter-error', (row.name || row.metric) + '：' + err);
          return;
        }
      }
    }
    for (const row of rows) {
      if (configurable(row) && chosen.has(row.measurement_id)) setDraft(row, changes);
    }
    if (!drafts.size) $('#only-draft').checked = false;
    $('#bulk-parameter-editor').close();
    parameterNotice = drafts.size ? '' : '所选参数与当前值相同';
    renderRows(filtered(detail.items));
  }

  async function saveParameters() {
    if (!detail || !drafts.size || saving) return;
    const items = detail.items.filter(row => drafts.has(row.measurement_id)).map(row => {
      const values = { measurement_id: row.measurement_id, version: row.version };
      for (const field of parameters) {
        const value = parameterValue(row, field);
        if (optionalLimitFields.has(field)) {
          const parsed = parseLimitInput(value);
          if (Number.isNaN(parsed)) {
            throw new Error((row.name || row.metric) + '：量测上下限必须为有限数值或留空');
          }
          values[field] = parsed;
          continue;
        }
        if (value == null || value === '' || !Number.isFinite(Number(value))
            || (field === 'weight' && Number(value) <= 0)
            || (field === 'max_age_seconds' && Number(value) <= 0)) {
          throw new Error((row.name || row.metric) + '：权重和超时阈值须为正数');
        }
        values[field] = Number(value);
      }
      const rangeErr = limitRangeError(values.max_value, values.min_value);
      if (rangeErr) throw new Error((row.name || row.metric) + '：' + rangeErr);
      return values;
    });
    saving = true;
    renderRows(filtered(detail.items));
    try {
      detail = await api('/api/device-parameters', 'PUT', { table: detail.device.table, idx: detail.device.idx, items });
      drafts.clear();
      $('#only-draft').checked = false;
      parameterNotice = '已保存 ' + items.length + ' 项量测参数';
    } finally {
      saving = false;
      renderRows(filtered(detail.items));
    }
  }

  async function loadHistory() {
    const data = await api('/api/history');
    if (historyMode === 'controls') {
      const headers = ['时间', '来源', '设备', '属性', '类型', 'RTU / 点号', '设定值', '状态', '详情'];
      $('#history-head').innerHTML = '<tr>' + headers.map(heading => '<th scope="col">' + heading + '</th>').join('') + '</tr>';
      const items = data.controls || [];
      $('#history-rows').innerHTML = items.map(row => '<tr><td>' + html(time(row.at))
        + '</td><td>' + html(row.source === 'db_watch' ? '库监视' : 'Web')
        + '</td><td class="name">' + html(row.device_name || row.model_table)
        + '</td><td><code>' + html(row.control) + '</code></td><td><span class="ctrl-kind-tag ' + html(row.kind) + '">' + html((row.kind || '').toUpperCase())
        + '</span></td><td>' + html(row.rtu_id == null ? '—' : row.rtu_id + ' / ' + row.pnt_no)
        + '</td><td class="quantity-value">' + html(number(row.value))
        + '</td><td><span class="ctrl-status s-' + html(row.cmd_status) + '">' + html(row.status_label)
        + '</span></td><td class="wrap">' + html(row.detail || '—') + '</td></tr>').join('')
        || emptyRow(headers, '暂无指令流水记录');
      return;
    }
    const changes = historyMode === 'changes';
    const headers = changes ? ['时间', '对象', '编号', '修改内容']
      : ['批次', '开始时间', '完成时间', '属性数', '可用数', '异常'];
    $('#history-head').innerHTML = '<tr>' + headers.map(heading => '<th scope="col">' + heading + '</th>').join('') + '</tr>';
    $('#history-rows').innerHTML = (data[historyMode] || []).map(row => changes
      ? '<tr><td>' + html(time(row.changed_at)) + '</td><td>' + html(row.entity) + '</td><td>' + html(number(row.entity_id))
        + '</td><td class="wrap">' + html(row.after_json) + '</td></tr>'
      : '<tr><td>' + html(row.refresh_id) + '</td><td>' + html(time(row.started_at)) + '</td><td>'
        + html(time(row.finished_at)) + '</td><td>' + html(row.total_count) + '</td><td>' + html(row.valid_count)
        + '</td><td class="wrap">' + html(row.error || '—') + '</td></tr>').join('')
      || emptyRow(headers, '暂无记录');
  }

  async function refresh() {
    if (deviceMode) await loadRows();
    else if (controlMode) await loadControls();
    else if (logsMode) await loadLogsView({ append: false });
    else if (mode === '/history') await loadHistory();
    else await summary();
  }

  function openEditor(row) {
    editing = row && attached(row) ? row : null;
    error('#editor-error', '');
    $('#mapping-form').reset();
    $('#editor-title').textContent = editing ? '编辑量测关联' : '新增量测关联';
    $('#edit-device').value = row?.device_id || detail?.device.device_id || $('#device').value || devices[0]?.device_id || '';
    const fields = [
      ['metric', 'metric', ''], ['kind', 'kind', kind || 'yc'], ['name', 'name', ''], ['unit', 'unit', ''],
      ['weight', 'weight', 1], ['min', 'min_value', ''], ['max', 'max_value', ''],
      ['scale', 'scale', 1], ['offset', 'offset', 0], ['age', 'max_age_seconds', 120],
      ['source', 'source_database', catalog.sources[0] || ''], ['rtu', 'rtu_id', ''], ['point', 'pnt_no', ''],
    ];
    for (const [id, key, fallback] of fields) {
      const value = row?.[key];
      $('#edit-' + id).value = value == null || (id === 'source' && !value) ? fallback : value;
    }
    $('#edit-enabled').checked = !editing || editing.enabled == null || Boolean(editing.enabled);
    $('#edit-notuse').checked = Boolean(editing?.notuse);
    $('#source-rows').replaceChildren();
    $('#source-page').textContent = '';
    $('#source-message').textContent = '';
    $('#source-prev').disabled = true;
    $('#source-next').disabled = true;
    sourcePage = 1;
    sourceSequence += 1;
    $('#mapping-editor').showModal();
  }

  async function findSource() {
    const request = ++sourceSequence;
    error('#editor-error', '');
    const query = new URLSearchParams({
      source_database: $('#edit-source').value.trim(), kind: $('#edit-kind').value,
      q: $('#source-search').value.trim(), page: sourcePage,
    });
    try {
      const data = await api('/api/source-points?' + query);
      if (request !== sourceSequence) return;
      $('#source-rows').innerHTML = data.items.map((row, index) => '<tr><td>' + html(row.rtu_id)
        + ' / ' + html(row.pnt_no) + '</td><td class="name">' + html(row.name)
        + '</td><td><button type="button" data-source="' + index + '">选择</button></td></tr>').join('');
      for (const button of $('#source-rows').querySelectorAll('[data-source]')) {
        button.addEventListener('click', () => {
          const row = data.items[Number(button.dataset.source)];
          $('#edit-rtu').value = row.rtu_id;
          $('#edit-point').value = row.pnt_no;
          if (!$('#edit-name').value) $('#edit-name').value = row.name;
          $('#source-message').textContent = '已选：' + row.name + ' · RTU ' + row.rtu_id + ' / 点号 ' + row.pnt_no;
        });
      }
      $('#source-message').textContent = '找到 ' + data.total + ' 个 ' + $('#edit-kind').value.toUpperCase() + ' 测点';
      $('#source-page').textContent = sourcePage + ' / ' + Math.max(1, Math.ceil(data.total / 50));
      $('#source-prev').disabled = sourcePage <= 1;
      $('#source-next').disabled = sourcePage * 50 >= data.total;
    } catch (exception) {
      if (request === sourceSequence) error('#editor-error', exception.message);
    }
  }

  function confirmDanger(title, message, confirmText = '确认') {
    const dialog = $('#confirm-dialog');
    if (!dialog || typeof dialog.showModal !== 'function') {
      return typeof window.scadaConfirm === 'function'
        ? window.scadaConfirm(message, { title, confirmText, danger: true })
        : Promise.resolve(false);
    }
    $('#confirm-title').textContent = title;
    $('#confirm-message').textContent = message;
    $('#confirm-ok').textContent = confirmText;
    return new Promise(resolve => {
      let settled = false;
      const done = value => {
        if (settled) return;
        settled = true;
        if (dialog.open) dialog.close();
        resolve(value);
      };
      $('#confirm-cancel').onclick = () => done(false);
      $('#confirm-ok').onclick = () => done(true);
      dialog.oncancel = () => done(false);
      dialog.showModal();
      $('#confirm-cancel').focus();
    });
  }

  async function browse(folder) {
    error('#browser-error', '');
    try {
      const data = await api('/api/database/tree?directory=' + encodeURIComponent(folder));
      parentFolder = data.parent;
      $('#source-up').disabled = parentFolder === null;
      $('#source-folder').textContent = data.directory;
      const ICON_DIR = '<span class="tree-icon" aria-hidden="true"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0 1 14 6v6a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 12V4.5Z"/></svg></span>';
      const ICON_FILE = '<span class="tree-icon" aria-hidden="true"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="8" cy="4" rx="5" ry="2"/><path d="M3 4v8c0 1.1 2.24 2 5 2s5-.9 5-2V4"/><path d="M3 8c0 1.1 2.24 2 5 2s5-.9 5-2"/></svg></span>';
      $('#source-files').innerHTML = data.entries.map((row, index) => '<button data-file="' + index
        + '">' + (row.directory ? ICON_DIR : ICON_FILE) + html(row.name) + '</button>').join('')
        || '<p class="muted">目录为空</p>';
      for (const button of $('#source-files').querySelectorAll('button')) {
        button.addEventListener('click', run(async () => {
          const row = data.entries[Number(button.dataset.file)];
          if (row.directory) {
            await browse(row.path);
          } else if (browserMode === 'import') {
            $('#source-browser').close();
            const ok = await confirmDanger(
              '确认导入设备模型',
              '将从 ' + row.path + ' 导入设备模型，可能覆盖当前同名设备与表结构。确认继续？',
              '确认导入',
            );
            if (!ok) return;
            await api('/api/model/import', 'POST', { path: row.path });
            await loadDevices();
            await refresh();
          } else if (browserMode === 'ctrl-source') {
            $('#ctrl-edit-source').value = row.path;
            $('#source-browser').close();
            ctrlSourcePage = 1;
            findControlSource();
          } else {
            $('#edit-source').value = row.path;
            $('#source-browser').close();
            sourcePage = 1;
            findSource();
          }
        }));
      }
    } catch (exception) {
      error('#browser-error', exception.message);
    }
  }

  async function changeScope({ changeTable = false, resetDevice = false } = {}) {
    page = 1;
    parameterNotice = '';
    if (changeTable) updateTableOptions(mappingMode || queryMode ? '' : $('#model-table').value);
    if (changeTable || resetDevice) updateDeviceOptions('');
    chosen.clear();
    syncDeviceScope();
    detail = null;
    renderRows([], 0);
    await loadRows();
  }

  // ==================== 控调下发、控调定义与运行日志视图 ====================
  let ctrlRows = [];
  let ctrlPage = 1;
  let ctrlKind = '';
  let ctrlSeq = 0;
  let ctrlEditing = null;
  let ctrlSourcePage = 1;
  let ctrlSourceSeq = 0;
  const ctrlDraftValues = new Map();

  let logSeq = 0;
  let logCursor = null;
  let logItems = [];
  let logTab = 'files'; // 'files' | 'controls' | 'refreshes' | 'changes'

  function renderHistoryTableInto(headSel, bodySel, data, tab) {
    if (tab === 'controls') {
      const headers = ['时间', '来源', '设备', '属性', '类型', 'RTU / 点号', '设定值', '状态', '详情'];
      $(headSel).innerHTML = '<tr>' + headers.map(h => '<th scope="col">' + h + '</th>').join('') + '</tr>';
      const items = data.controls || [];
      $(bodySel).innerHTML = items.map(row => '<tr><td>' + html(time(row.at))
        + '</td><td>' + html(row.source === 'db_watch' ? '库监视' : 'Web')
        + '</td><td class="name">' + html(row.device_name || row.model_table)
        + '</td><td><code>' + html(row.control) + '</code></td><td><span class="ctrl-kind-tag ' + html(row.kind) + '">' + html((row.kind || '').toUpperCase())
        + '</span></td><td>' + html(row.rtu_id == null ? '—' : row.rtu_id + ' / ' + row.pnt_no)
        + '</td><td class="quantity-value">' + html(number(row.value))
        + '</td><td><span class="ctrl-status s-' + html(row.cmd_status) + '">' + html(row.status_label)
        + '</span></td><td class="wrap">' + html(row.detail || '—') + '</td></tr>').join('')
        || emptyRow(headers, '暂无指令流水记录');
      return;
    }
    const changes = tab === 'changes';
    const headers = changes ? ['时间', '对象', '编号', '修改内容']
      : ['批次', '开始时间', '完成时间', '属性数', '可用数', '异常'];
    $(headSel).innerHTML = '<tr>' + headers.map(h => '<th scope="col">' + h + '</th>').join('') + '</tr>';
    $(bodySel).innerHTML = (data[tab] || []).map(row => changes
      ? '<tr><td>' + html(time(row.changed_at)) + '</td><td>' + html(row.entity) + '</td><td>' + html(number(row.entity_id))
        + '</td><td class="wrap">' + html(row.after_json) + '</td></tr>'
      : '<tr><td>' + html(row.refresh_id) + '</td><td>' + html(time(row.started_at)) + '</td><td>'
        + html(time(row.finished_at)) + '</td><td>' + html(row.total_count) + '</td><td>' + html(row.valid_count)
        + '</td><td class="wrap">' + html(row.error || '—') + '</td></tr>').join('')
      || emptyRow(headers, '暂无记录');
  }

  async function loadLogsView({ append = false } = {}) {
    const isFileTab = logTab === 'files';
    $('#log-process').hidden = !isFileTab;
    $('#log-level').hidden = !isFileTab;
    $('#log-file').hidden = !isFileTab;
    $('#log-search').hidden = !isFileTab;
    $('#log-export').hidden = !isFileTab;
    $('#logs-file-wrap').hidden = !isFileTab;
    $('#logs-file-pager').hidden = !isFileTab;
    $('#logs-history-wrap').hidden = isFileTab;
    if (isFileTab) {
      await loadLogs({ append });
      return;
    }
    const data = await api('/api/history');
    renderHistoryTableInto('#logs-history-head', '#logs-history-rows', data, logTab);
    const cnt = (data[logTab] || []).length;
    $('#log-meta').textContent = '内存记录 ' + cnt + ' 条';
  }

  async function runSyncBindings(scope) {
    const preview = await api('/api/sync-bindings', 'POST', { scope, dry_run: true });
    if (!preview.to_add && !preview.to_update) {
      error('#alert', '源库 ' + preview.source_database + ' 扫描完成（' + preview.total_source + ' 点），所有可匹配点位均已最新，无需变更。');
      return;
    }
    const sampleLines = (preview.preview || []).slice(0, 6).map(
      op => (op.action === 'add' ? '[新增] ' : '[更新] ') + op.table + '.' + op.device_name + '.' + op.attr
        + ' (' + op.kind.toUpperCase() + ' RTU ' + op.rtu_id + '/' + op.pnt_no + ')'
    ).join('\n');
    const ok = await confirmDanger(
      '确认一键同步绑定',
      '源库：' + preview.source_database + '\n'
        + '扫描源点：' + preview.total_source + ' 条 · 预计新增 ' + preview.to_add + ' 条 · 更新 ' + preview.to_update
        + ' 条 · 已一致 ' + preview.unchanged + ' 条 · 未匹配 ' + preview.unmatched + ' 条\n'
        + (sampleLines ? '\n示例：\n' + sampleLines : ''),
      '确认执行同步',
    );
    if (!ok) return;
    const res = await api('/api/sync-bindings', 'POST', { scope, dry_run: false });
    error('#alert', '');
    if (controlMode) await loadControls();
    else await refresh();
    if (res.to_add || res.to_update) {
      $('#alert').hidden = false;
      $('#alert').textContent = '同步完成：新增 ' + res.to_add + ' 条，更新 ' + res.to_update + ' 条。';
    }
  }

  function downloadCsv(filename, header, rowList) {
    const escCsv = v => '"' + String(v ?? '').replace(/"/g, '""') + '"';
    const lines = [header.map(escCsv).join(',')].concat(rowList.map(r => r.map(escCsv).join(',')));
    const blob = new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function exportMappingsCsv(isControl) {
    if (isControl) {
      const data = await api('/api/controls?limit=500&page=1');
      downloadCsv(
        'scdfe_d-control-mappings.csv',
        ['设备表', '索引', '设备名称', '指令属性', '类型', 'RTU', '点号', '下限', '上限', '禁用'],
        (data.items || []).map(r => [r.model_table, r.model_idx, r.device_name, r.control, r.kind, r.rtu_id, r.pnt_no, r.min_value ?? '', r.max_value ?? '', r.notuse]),
      );
      return;
    }
    const data = await api('/api/measurements?limit=200&page=1');
    downloadCsv(
      'scdfe_d-measurement-mappings.csv',
      ['设备表', '索引', '设备名称', '量测属性', '类型', 'RTU', '点号', '权重', '下限', '上限', '禁用'],
      (data.items || []).map(r => [r.model_table, r.model_idx, r.device_name, r.metric, r.kind, r.rtu_id, r.pnt_no, r.weight, r.min_value ?? '', r.max_value ?? '', r.notuse]),
    );
  }

  async function loadControlCatalog() {
    catalog = await api('/api/device-catalog');
    devices = catalog.devices || [];
    tables = catalog.tables || [];
    const cats = catalog.category_labels || {};
    $('#ctrl-category').innerHTML = option('', '全部类型')
      + Object.entries(cats).map(([k, v]) => option(k, v)).join('');
    $('#ctrl-table').innerHTML = option('', '全部设备表')
      + tables.map(t => option(t.table_name, t.table_name + ' (' + t.count + ')')).join('');
    $('#ctrl-edit-device').innerHTML = devices.map(d => option(d.device_id, d.name + ' · ' + (d.tables?.[0]?.table || ''))).join('');
  }

  function renderControlRows(items, total, summaryData) {
    ctrlRows = items;
    if (summaryData) {
      $('#ctrl-summary').textContent = '共 ' + summaryData.total + ' · YK ' + summaryData.yk
        + ' · YT ' + summaryData.yt + (summaryData.pending ? ' · 待回执 ' + summaryData.pending : '');
    }
    if (controlDispatchMode) {
      const headers = ['设备', '指令属性', '类型', '当前设定值', '限值', '状态', '指令 / 回执时间', '操作'];
      $('#control-head').innerHTML = '<tr>' + headers.map(h => '<th scope="col">' + h + '</th>').join('') + '</tr>';
      $('#control-rows').innerHTML = items.map((row, index) => {
        const disabled = Boolean(row.notuse);
        const unitSuffix = row.unit ? ' ' + html(row.unit) : '';
        const draftVal = ctrlDraftValues.has(row.control_id)
          ? ctrlDraftValues.get(row.control_id)
          : (row.value == null ? '' : String(row.value));
        const valCell = row.kind === 'yt'
          ? '<td class="ctrl-input-cell"><input class="ctrl-field" type="number" step="any" data-ctrl-input="' + index
            + '" value="' + html(draftVal) + '" placeholder="' + html(row.unit ? '设定 (' + row.unit + ')' : '输入设定值')
            + '"' + (disabled ? ' disabled' : '') + '></td>'
          : '<td class="quantity-value">' + (row.value == null ? '—' : (Number(row.value) === 1 ? '1 · 合/启' : '0 · 分/停')) + '</td>';
        const opCell = row.kind === 'yk'
          ? '<span class="ctrl-ops"><button type="button" class="btn-on" data-yk="' + index + '" data-val="1"' + (disabled ? ' disabled' : '')
            + '>合/启 (1)</button><button type="button" class="btn-off" data-yk="' + index + '" data-val="0"' + (disabled ? ' disabled' : '')
            + '>分/停 (0)</button></span>'
          : '<span class="ctrl-ops"><button type="button" class="primary" data-yt-send="' + index + '"' + (disabled ? ' disabled' : '')
            + '>下发</button></span>';
        const timeCell = html(time(row.cmd_time_ms))
          + (row.cmd_ack_time_ms ? '<span class="muted">回执 ' + html(time(row.cmd_ack_time_ms)) + '</span>' : '');
        return '<tr><td class="name">' + html(row.device_name) + '<span class="muted">' + html(row.model_table) + ' #' + html(row.model_idx) + '</span></td>'
          + '<td class="quantity-identity"><strong>' + html(row.label || row.control) + '</strong><small><code>' + html(row.control) + '</code>' + unitSuffix + ' · RTU ' + html(row.rtu_id) + '/' + html(row.pnt_no) + '</small></td>'
          + '<td><span class="ctrl-kind-tag ' + html(row.kind) + '">' + html(row.kind.toUpperCase()) + '</span></td>'
          + valCell
          + '<td class="limit-range">' + html(formatLimitRange(row)) + '</td>'
          + '<td><span class="ctrl-status s-' + html(row.cmd_status) + '">' + html(row.status_label) + '</span></td>'
          + '<td>' + timeCell + '</td>'
          + '<td>' + opCell + '</td></tr>';
      }).join('') || emptyRow(headers, '暂无已绑定指令点位');
    } else {
      const headers = ['设备', '指令属性', '类型', '源库 / RTU / 点号', '限值范围', '控制状态', '操作'];
      $('#control-head').innerHTML = '<tr>' + headers.map(h => '<th scope="col">' + h + '</th>').join('') + '</tr>';
      $('#control-rows').innerHTML = items.map((row, index) => '<tr><td class="name">' + html(row.device_name)
        + '<span class="muted">' + html(row.model_table) + ' #' + html(row.model_idx) + '</span></td>'
        + '<td class="quantity-identity"><strong>' + html(row.label || row.control) + '</strong><small><code>' + html(row.control) + '</code></small></td>'
        + '<td><span class="ctrl-kind-tag ' + html(row.kind) + '">' + html(row.kind.toUpperCase()) + '</span></td>'
        + '<td class="path" title="' + html(row.source_database) + '">' + html(row.rtu_id) + ' / ' + html(row.pnt_no)
        + '<span class="muted">' + html(row.source_database || '—') + '</span></td>'
        + '<td class="limit-range">' + html(formatLimitRange(row)) + '</td>'
        + '<td><span class="ctrl-status s-' + (row.notuse ? '5' : '2') + '">' + (row.notuse ? '控制禁用' : '允许控制') + '</span></td>'
        + '<td><span class="ctrl-ops"><button type="button" data-ctrl-edit="' + index + '">编辑</button>'
        + '<button type="button" data-ctrl-delete="' + index + '">删除</button></span></td></tr>').join('')
        || emptyRow(headers, '暂无指令映射，可点击右上角新增');
    }
    const pages = Math.max(1, Math.ceil(total / 100));
    $('#ctrl-page-summary').textContent = '共 ' + total + ' 条指令记录';
    $('#ctrl-page-number').textContent = ctrlPage + ' / ' + pages;
    $('#ctrl-prev').disabled = ctrlPage <= 1;
    $('#ctrl-next').disabled = ctrlPage >= pages;
  }

  async function loadControlHistory() {
    if (!controlDispatchMode) return;
    const data = await api('/api/control-history');
    const items = data.items || [];
    $('#ctrl-history-count').textContent = items.length ? '（最近 ' + items.length + ' 条）' : '（暂无记录）';
    $('#ctrl-history-rows').innerHTML = items.map(row => '<tr><td>' + html(time(row.at))
      + '</td><td>' + html(row.source === 'db_watch' ? '库监视' : 'Web')
      + '</td><td class="name">' + html(row.device_name || row.model_table)
      + '</td><td><code>' + html(row.control) + '</code></td><td><span class="ctrl-kind-tag ' + html(row.kind) + '">' + html((row.kind || '').toUpperCase())
      + '</span></td><td>' + html(row.rtu_id == null ? '—' : row.rtu_id + ' / ' + row.pnt_no)
      + '</td><td class="quantity-value">' + html(number(row.value))
      + '</td><td><span class="ctrl-status s-' + html(row.cmd_status) + '">' + html(row.status_label)
      + '</span></td><td class="wrap">' + html(row.detail || '—') + '</td></tr>').join('')
      || '<tr><td colspan="9" class="empty">暂无指令流水</td></tr>';
  }

  async function loadControls() {
    const req = ++ctrlSeq;
    const params = new URLSearchParams({ page: ctrlPage, limit: 100 });
    if ($('#ctrl-category').value) params.set('category', $('#ctrl-category').value);
    if ($('#ctrl-table').value) params.set('table', $('#ctrl-table').value);
    if (ctrlKind) params.set('kind', ctrlKind);
    if (controlDispatchMode && $('#ctrl-status').value !== '') params.set('status', $('#ctrl-status').value);
    if ($('#ctrl-search').value.trim()) params.set('q', $('#ctrl-search').value.trim());
    const data = await api('/api/controls?' + params);
    if (req !== ctrlSeq) return;
    renderControlRows(data.items || [], data.total || 0, data.summary);
    if (controlDispatchMode) await loadControlHistory();
  }

  async function triggerDispatch(row, targetValue) {
    if (!row) return;
    const isYk = row.kind === 'yk';
    let parsedValue;
    if (isYk) {
      parsedValue = Number(targetValue);
    } else {
      const raw = String(targetValue ?? '').trim();
      if (!raw) throw new Error('请输入有效的遥调设定值');
      parsedValue = Number(raw);
      if (!Number.isFinite(parsedValue)) throw new Error('遥调设定值必须为有限数值');
      if (row.max_value != null && row.min_value != null && Number(row.max_value) > Number(row.min_value)) {
        if (parsedValue > Number(row.max_value) || parsedValue < Number(row.min_value)) {
          throw new Error('设定值 ' + parsedValue + ' 超出限值范围 [' + row.min_value + ', ' + row.max_value + ']');
        }
      }
    }
    const actionDesc = isYk
      ? (parsedValue === 1 ? '合 / 启 (1)' : '分 / 停 (0)')
      : (parsedValue + (row.unit ? ' ' + row.unit : ''));
    const ok = await confirmDanger(
      isYk ? '确认下发遥控指令' : '确认下发遥调指令',
      '目标设备：' + row.device_name + '（' + row.model_table + ' #' + row.model_idx + '）\n'
        + '指令属性：' + (row.label || row.control) + ' [' + row.control + '] · RTU ' + row.rtu_id + ' / 点号 ' + row.pnt_no + '\n'
        + '下发目标值：' + actionDesc + '\n'
        + '确认立即写入 scdfe_c 并下发？',
      '确认下发',
    );
    if (!ok) return;
    await api('/api/controls/' + row.control_id + '/dispatch', 'POST', { value: parsedValue });
    ctrlDraftValues.delete(row.control_id);
    await loadControls();
  }

  function openControlEditor(row) {
    ctrlEditing = row || null;
    error('#ctrl-editor-error', '');
    $('#control-form').reset();
    $('#ctrl-editor-title').textContent = ctrlEditing ? '编辑指令映射' : '新增指令映射';
    $('#ctrl-edit-device').value = row?.device_id || devices[0]?.device_id || '';
    $('#ctrl-edit-attr').value = row?.control || '';
    $('#ctrl-edit-kind').value = row?.kind || ctrlKind || 'yk';
    $('#ctrl-edit-min').value = row?.min_value ?? '';
    $('#ctrl-edit-max').value = row?.max_value ?? '';
    $('#ctrl-edit-notuse').checked = Boolean(row?.notuse);
    $('#ctrl-edit-source').value = row?.source_database || catalog?.sources?.[0] || '';
    $('#ctrl-edit-rtu').value = row?.rtu_id ?? '';
    $('#ctrl-edit-point').value = row?.pnt_no ?? '';
    $('#ctrl-source-rows').replaceChildren();
    $('#ctrl-source-page').textContent = '';
    $('#ctrl-source-message').textContent = '';
    $('#ctrl-source-prev').disabled = true;
    $('#ctrl-source-next').disabled = true;
    ctrlSourcePage = 1;
    ctrlSourceSeq += 1;
    $('#control-editor').showModal();
  }

  async function findControlSource() {
    const req = ++ctrlSourceSeq;
    error('#ctrl-editor-error', '');
    const kindVal = $('#ctrl-edit-kind').value;
    const query = new URLSearchParams({
      source_database: $('#ctrl-edit-source').value.trim(),
      kind: kindVal,
      q: $('#ctrl-source-search').value.trim(),
      page: ctrlSourcePage,
    });
    try {
      const data = await api('/api/source-points?' + query);
      if (req !== ctrlSourceSeq) return;
      $('#ctrl-source-rows').innerHTML = (data.items || []).map((row, index) => '<tr><td>' + html(row.rtu_id)
        + ' / ' + html(row.pnt_no) + '</td><td class="name">' + html(row.name)
        + '</td><td><button type="button" data-ctrl-src="' + index + '">选择</button></td></tr>').join('');
      for (const btn of $('#ctrl-source-rows').querySelectorAll('[data-ctrl-src]')) {
        btn.addEventListener('click', () => {
          const item = data.items[Number(btn.dataset.ctrlSrc)];
          $('#ctrl-edit-rtu').value = item.rtu_id;
          $('#ctrl-edit-point').value = item.pnt_no;
          if (!$('#ctrl-edit-attr').value && item.name && item.name.includes('.')) {
            const guess = item.name.split('.').pop();
            if (/^[a-z][a-z0-9_]{0,47}$/.test(guess)) $('#ctrl-edit-attr').value = guess;
          }
          if (kindVal === 'yt') {
            if ($('#ctrl-edit-min').value === '' && item.eng_min != null) $('#ctrl-edit-min').value = item.eng_min;
            if ($('#ctrl-edit-max').value === '' && item.eng_max != null) $('#ctrl-edit-max').value = item.eng_max;
          }
          $('#ctrl-source-message').textContent = '已选：' + item.name + ' · RTU ' + item.rtu_id + ' / 点号 ' + item.pnt_no;
        });
      }
      $('#ctrl-source-message').textContent = '找到 ' + data.total + ' 个 ' + kindVal.toUpperCase() + ' 指令点';
      $('#ctrl-source-page').textContent = ctrlSourcePage + ' / ' + Math.max(1, Math.ceil(data.total / 50));
      $('#ctrl-source-prev').disabled = ctrlSourcePage <= 1;
      $('#ctrl-source-next').disabled = ctrlSourcePage * 50 >= data.total;
    } catch (exception) {
      if (req === ctrlSourceSeq) error('#ctrl-editor-error', exception.message);
    }
  }

  async function loadLogFiles() {
    const data = await api('/api/logs/files');
    const files = data.items || [];
    const current = $('#log-file').value;
    $('#log-file').innerHTML = option('', '全部小时文件 (' + files.length + ')')
      + files.map(f => option(f.name, f.process + ' · ' + f.hour + ' (' + Math.max(1, Math.round(f.size / 1024)) + ' KB)')).join('');
    if (files.some(f => f.name === current)) $('#log-file').value = current;
  }

  function renderLogRows() {
    $('#log-rows').innerHTML = logItems.map(item => {
      const proc = (item.filename || '').split('/').pop().replace(/-\d{8}-\d{2}\.log$/, '');
      const lvl = item.level || 'INFO';
      return '<tr><td>' + html(time(item.time))
        + '</td><td><span class="log-lvl ' + html(lvl) + '">' + html(lvl) + '</span></td>'
        + '<td>' + html(proc) + '</td>'
        + '<td class="log-text">' + html(item.text) + '</td></tr>';
    }).join('') || '<tr><td colspan="4" class="empty">暂无匹配的运行日志</td></tr>';
  }

  async function loadLogs({ append = false } = {}) {
    const req = ++logSeq;
    const params = new URLSearchParams({ limit: 300 });
    if ($('#log-process').value) params.set('process', $('#log-process').value);
    if ($('#log-level').value) params.set('level', $('#log-level').value);
    if ($('#log-file').value) params.set('filename', $('#log-file').value);
    if ($('#log-search').value.trim()) params.set('q', $('#log-search').value.trim());
    if (append && logCursor) params.set('cursor', logCursor);
    const data = await api('/api/logs/read?' + params);
    if (req !== logSeq) return;
    if (append) logItems = logItems.concat(data.items || []);
    else logItems = data.items || [];
    logCursor = data.next_cursor || null;
    renderLogRows();
    $('#log-meta').textContent = '已扫描 ' + (data.scanned_files || 0) + ' 个文件';
    $('#log-summary').textContent = '当前展示 ' + logItems.length + ' 条日志';
    $('#log-more').hidden = !data.has_more;
  }

  function exportLogs() {
    if (!logItems.length) return;
    const content = logItems.map(i => '[' + (i.filename || '') + '] ' + (i.text || '')).join('\n');
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'scdfe_d-logs-' + new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19) + '.txt';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function init() {
    $('#overview').hidden = mode !== '/';
    $('#overview-stats').hidden = mode !== '/';
    $('#measure-panel').hidden = !deviceMode;
    $('#history-panel').hidden = mode !== '/history';
    $('#control-panel').hidden = !controlMode;
    $('#logs-panel').hidden = !logsMode;
    $('#definition-actions').hidden = !mappingMode;
    $('#parameter-actions').hidden = !parameterMode;
    $('#parameter-selection').hidden = !parameterMode;
    $('#live-refresh').hidden = !queryMode;
    const viewSwitch = $('#measure-view-switch');
    if (viewSwitch) {
      viewSwitch.hidden = !(queryMode || parameterMode);
      $('#switch-live-view')?.classList.toggle('active', queryMode);
      $('#switch-param-view')?.classList.toggle('active', parameterMode);
    }
    if (controlMode) {
      $('#ctrl-status').hidden = !controlDispatchMode;
      $('#ctrl-live-wrap').hidden = !controlDispatchMode;
      $('#new-control').hidden = !controlMappingMode;
      $('#sync-ctrl-bindings').hidden = !controlMappingMode;
      $('#export-ctrl-csv').hidden = !controlMappingMode;
      $('#control-history-drawer').hidden = !controlDispatchMode;
      await loadControlCatalog();
      const ctrlReq = new URLSearchParams(location.search);
      if (ctrlReq.get('category') && $('#ctrl-category')) $('#ctrl-category').value = ctrlReq.get('category');
      await loadControls();
      ready = true;
      return;
    }
    if (logsMode) {
      await loadLogFiles();
      await loadLogsView({ append: false });
      ready = true;
      return;
    }
    if (!deviceMode) {
      await refresh();
      ready = true;
      return;
    }
    const requested = new URLSearchParams(location.search);
    if (mappingMode && requested.get('metric')) $('#search').value = requested.get('metric');
    await loadDevices(requested);
    await loadRows();
    ready = true;
  }

  for (const action of ['start', 'stop', 'once']) {
    $('#collect-' + action).addEventListener('click', run(() => controlCollector(async () => {
      await api('/api/collector/' + action, 'POST', {});
      await refresh();
    })));
  }
  $('#interval').addEventListener('input', event => {
    if (!overview || collectorActionPending) return;
    intervalDraft = event.target.value;
    intervalNotice = '';
    error('#interval-error', '');
    renderCollectorControls();
  });
  $('#interval-form').addEventListener('submit', saveInterval);
  $('#category').addEventListener('change', run(() => changeScope({ changeTable: true, resetDevice: true })));
  $('#model-table').addEventListener('change', run(() => changeScope({ resetDevice: true })));
  $('#device').addEventListener('change', run(() => changeScope()));
  $('#only-invalid').addEventListener('change', run(refreshRowsForFilter));
  let timer;
  $('#search').addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(run(refreshRowsForFilter), 250);
  });
  for (const type of pointKinds) {
    $('#tab-' + (type || 'all')).addEventListener('click', run(async () => {
      kind = type;
      for (const value of pointKinds) {
        const button = $('#tab-' + (value || 'all'));
        button.classList.toggle('active', value === kind);
        button.setAttribute('aria-pressed', String(value === kind));
      }
      await refreshRowsForFilter();
    }));
  }
  $('#refresh').addEventListener('click', run(async () => {
    await loadDevices();
    await refresh();
  }));
  $('#history-refresh').addEventListener('click', run(loadHistory));
  for (const type of ['refreshes', 'changes', 'controls']) {
    const btn = $('#show-' + type);
    if (!btn) continue;
    btn.addEventListener('click', run(async () => {
      historyMode = type;
      for (const value of ['refreshes', 'changes', 'controls']) {
        const el = $('#show-' + value);
        if (el) el.classList.toggle('active', value === type);
      }
      await loadHistory();
    }));
  }
  $('#previous').addEventListener('click', run(async () => {
    if (page <= 1) return;
    page = Math.max(1, page - 1);
    $('#previous').disabled = true;
    $('#next').disabled = true;
    await loadRows();
  }));
  $('#next').addEventListener('click', run(async () => {
    page += 1;
    $('#previous').disabled = true;
    $('#next').disabled = true;
    await loadRows();
  }));
  $('#save-parameters').addEventListener('click', run(saveParameters));
  $('#discard-parameters').addEventListener('click', run(async () => {
    if (!drafts.size) return;
    if (!(await confirmDanger(
      '确认撤销修改',
      '将丢弃当前 ' + drafts.size + ' 项未保存的参数修改，确认继续？',
      '确认撤销',
    ))) return;
    drafts.clear();
    $('#only-draft').checked = false;
    parameterNotice = '';
    error('#alert', '');
    if (detail?.items) renderRows(filtered(detail.items));
    else updateParameterButtons();
  }));
  // 支持在参数配置页按 Ctrl+S 快捷保存未保存参数
  document.addEventListener('keydown', event => {
    if (!parameterMode || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 's') return;
    event.preventDefault();
    if (!$('#save-parameters').disabled) run(saveParameters)();
  });
  $('#only-draft').addEventListener('change', run(refreshRowsForFilter));
  $('#bulk-parameters').addEventListener('click', openBulkParameters);
  $('#bulk-parameter-cancel').addEventListener('click', () => $('#bulk-parameter-editor').close());
  $('#bulk-parameter-form').addEventListener('submit', applyBulkParameters);
  $('#measure-head').addEventListener('change', event => {
    if (event.target.id !== 'choose-all' || saving) return;
    chosen.clear();
    if (event.target.checked) {
      for (const row of rows) if (configurable(row)) chosen.add(row.measurement_id);
    }
    for (const checkbox of $('#measure-rows').querySelectorAll('[data-choose]')) {
      checkbox.checked = chosen.has(Number(checkbox.dataset.choose));
    }
    updateParameterButtons();
  });
  $('#measure-rows').addEventListener('change', event => {
    const id = event.target.dataset.choose;
    if (id == null || saving) return;
    if (event.target.checked) chosen.add(Number(id));
    else chosen.delete(Number(id));
    updateParameterButtons();
  });
  $('#measure-rows').addEventListener('input', updateDraft);
  window.addEventListener('beforeunload', event => {
    if (drafts.size) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
  $('#measure-rows').addEventListener('click', run(async event => {
    // 扩大参数配置页复选框单元格点击热区：点击单元格空白处同样切换勾选
    if (parameterMode && !saving && event.target.tagName !== 'INPUT') {
      const checkCell = event.target.closest('td.parameter-check');
      const box = checkCell?.querySelector('input[type="checkbox"]:not(:disabled)');
      if (box) {
        box.checked = !box.checked;
        box.dispatchEvent(new Event('change', { bubbles: true }));
        box.dispatchEvent(new Event('input', { bubbles: true }));
        return;
      }
    }
    const edit = event.target.closest('[data-edit]');
    const remove = event.target.closest('[data-delete]');
    if (edit) openEditor(rows[Number(edit.dataset.edit)]);
    if (remove) {
      const row = rows[Number(remove.dataset.delete)];
      if (row && await confirmDanger(
        '确认删除量测映射',
        '删除 ' + (row.device_name || detail?.device.name) + ' 的 ' + row.metric + ' 属性及映射后不可恢复，确认删除？',
        '确认删除',
      )) {
        await api('/api/measurements/' + row.measurement_id, 'DELETE', { version: row.version });
        await refresh();
      }
    }
  }));
  $('#new-measurement').addEventListener('click', () => {
    if (!devices.length) error('#alert', '请先新增设备');
    else openEditor();
  });
  $('#import-model').addEventListener('click', () => {
    browserMode = 'import';
    if ($('#source-browser-title')) $('#source-browser-title').textContent = '选择设备模型数据库';
    $('#source-browser').showModal();
    browse('.');
  });
  for (const id of ['editor-cancel', 'editor-close']) {
    $('#' + id).addEventListener('click', () => $('#mapping-editor').close());
  }
  $('#mapping-editor').addEventListener('close', () => { sourceSequence += 1; });
  $('#mapping-form').addEventListener('submit', async event => {
    event.preventDefault();
    error('#editor-error', '');
    $('#editor-save').disabled = true;
    try {
      const minVal = parseLimitInput($('#edit-min').value);
      const maxVal = parseLimitInput($('#edit-max').value);
      const rangeErr = limitRangeError(maxVal, minVal);
      if (rangeErr) {
        error('#editor-error', rangeErr);
        return;
      }
      const data = {
        device_id: Number($('#edit-device').value), metric: $('#edit-metric').value.trim(), kind: $('#edit-kind').value,
        name: $('#edit-name').value.trim(), unit: $('#edit-unit').value.trim(),
        source_database: $('#edit-source').value.trim(), rtu_id: Number($('#edit-rtu').value),
        pnt_no: Number($('#edit-point').value), scale: Number($('#edit-scale').value),
        offset: Number($('#edit-offset').value), weight: Number($('#edit-weight').value),
        max_value: maxVal, min_value: minVal,
        max_age_seconds: Number($('#edit-age').value), notuse: Number($('#edit-notuse').checked),
        enabled: Number($('#edit-enabled').checked), se_enabled: editing?.se_enabled || 0,
      };
      if (editing) data.version = editing.version;
      await api('/api/measurements' + (editing ? '/' + editing.measurement_id : ''), editing ? 'PUT' : 'POST', data);
      $('#mapping-editor').close();
      await refresh();
    } catch (exception) {
      error($('#mapping-editor').open ? '#editor-error' : '#alert', exception.message);
    } finally {
      $('#editor-save').disabled = false;
    }
  });
  $('#find-source').addEventListener('click', () => { sourcePage = 1; findSource(); });
  $('#edit-kind').addEventListener('change', () => {
    sourcePage = 1;
    sourceSequence += 1;
    $('#source-rows').replaceChildren();
    $('#source-page').textContent = '';
    $('#source-prev').disabled = true;
    $('#source-next').disabled = true;
    $('#edit-rtu').value = '';
    $('#edit-point').value = '';
    if ($('#edit-kind').value === 'yx') {
      $('#edit-scale').value = 1;
      $('#edit-offset').value = 0;
    }
  });
  $('#source-search').addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      sourcePage = 1;
      findSource();
    }
  });
  $('#source-prev').addEventListener('click', () => {
    if (sourcePage <= 1) return;
    sourcePage = Math.max(1, sourcePage - 1);
    $('#source-prev').disabled = true;
    $('#source-next').disabled = true;
    findSource();
  });
  $('#source-next').addEventListener('click', () => {
    sourcePage += 1;
    $('#source-prev').disabled = true;
    $('#source-next').disabled = true;
    findSource();
  });
  $('#browse-source').addEventListener('click', () => {
    browserMode = 'source';
    if ($('#source-browser-title')) $('#source-browser-title').textContent = '选择源数据库';
    $('#source-browser').showModal();
    const path = $('#edit-source').value;
    const slash = path.lastIndexOf('/');
    browse(slash >= 0 ? path.slice(0, slash) : '.');
  });
  $('#source-close').addEventListener('click', () => $('#source-browser').close());
  $('#source-root').addEventListener('click', () => browse('.'));
  $('#source-up').addEventListener('click', () => { if (parentFolder !== null) browse(parentFolder); });
  $('#new-device').addEventListener('click', () => {
    error('#device-error', '');
    $('#device-form').reset();
    $('#device-category').value = $('#category').value || 'generator';
    $('#device-editor').showModal();
  });
  $('#device-cancel').addEventListener('click', () => $('#device-editor').close());
  $('#device-form').addEventListener('submit', async event => {
    event.preventDefault();
    const submitBtn = $('#device-form button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
      await api('/api/devices', 'POST', { name: $('#device-name').value, category: $('#device-category').value });
      $('#device-editor').close();
      await loadDevices();
      await refresh();
    } catch (exception) {
      error($('#device-editor').open ? '#device-error' : '#alert', exception.message);
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });

  // 控调下发与控调定义事件绑定
  const resetCtrlAndLoad = run(async () => { ctrlPage = 1; await loadControls(); });
  $('#ctrl-category').addEventListener('change', resetCtrlAndLoad);
  $('#ctrl-table').addEventListener('change', resetCtrlAndLoad);
  $('#ctrl-status').addEventListener('change', resetCtrlAndLoad);
  let ctrlTimer;
  $('#ctrl-search').addEventListener('input', () => {
    clearTimeout(ctrlTimer);
    ctrlTimer = setTimeout(resetCtrlAndLoad, 250);
  });
  for (const type of ['', 'yk', 'yt']) {
    $('#ctrl-tab-' + (type || 'all')).addEventListener('click', run(async () => {
      ctrlKind = type;
      for (const v of ['', 'yk', 'yt']) {
        const b = $('#ctrl-tab-' + (v || 'all'));
        b.classList.toggle('active', v === ctrlKind);
        b.setAttribute('aria-pressed', String(v === ctrlKind));
      }
      ctrlPage = 1;
      await loadControls();
    }));
  }
  $('#ctrl-refresh').addEventListener('click', run(loadControls));
  $('#ctrl-prev').addEventListener('click', run(async () => {
    if (ctrlPage <= 1) return;
    ctrlPage -= 1;
    await loadControls();
  }));
  $('#ctrl-next').addEventListener('click', run(async () => {
    ctrlPage += 1;
    await loadControls();
  }));
  $('#new-control').addEventListener('click', () => {
    if (!devices.length) error('#alert', '请先新增设备或导入模型');
    else openControlEditor();
  });
  $('#control-rows').addEventListener('input', event => {
    const input = event.target.closest('[data-ctrl-input]');
    if (!input) return;
    const row = ctrlRows[Number(input.dataset.ctrlInput)];
    if (!row) return;
    ctrlDraftValues.set(row.control_id, input.value);
    const val = parseLimitInput(input.value);
    const bad = Number.isNaN(val) || (
      val !== null && row.max_value != null && row.min_value != null
      && Number(row.max_value) > Number(row.min_value)
      && (val > Number(row.max_value) || val < Number(row.min_value))
    );
    input.classList.toggle('limit-invalid', bad);
  });
  $('#control-rows').addEventListener('keydown', run(async event => {
    if (event.key !== 'Enter') return;
    const input = event.target.closest('[data-ctrl-input]');
    if (!input) return;
    event.preventDefault();
    const row = ctrlRows[Number(input.dataset.ctrlInput)];
    await triggerDispatch(row, input.value);
  }));
  $('#control-rows').addEventListener('click', run(async event => {
    const ykBtn = event.target.closest('[data-yk]');
    if (ykBtn) {
      const row = ctrlRows[Number(ykBtn.dataset.yk)];
      await triggerDispatch(row, Number(ykBtn.dataset.val));
      return;
    }
    const ytBtn = event.target.closest('[data-yt-send]');
    if (ytBtn) {
      const idx = Number(ytBtn.dataset.ytSend);
      const row = ctrlRows[idx];
      const input = $('#control-rows').querySelector('[data-ctrl-input="' + idx + '"]');
      await triggerDispatch(row, input ? input.value : row?.value);
      return;
    }
    const editBtn = event.target.closest('[data-ctrl-edit]');
    if (editBtn) {
      openControlEditor(ctrlRows[Number(editBtn.dataset.ctrlEdit)]);
      return;
    }
    const delBtn = event.target.closest('[data-ctrl-delete]');
    if (delBtn) {
      const row = ctrlRows[Number(delBtn.dataset.ctrlDelete)];
      if (row && await confirmDanger(
        '确认删除指令映射',
        '删除 ' + row.device_name + ' 的 ' + row.control + '（' + row.kind.toUpperCase() + '）指令映射后不可恢复，确认删除？',
        '确认删除',
      )) {
        await api('/api/controls/' + row.control_id, 'DELETE', { version: row.version });
        await loadControls();
      }
    }
  }));
  for (const id of ['ctrl-editor-cancel', 'ctrl-editor-close']) {
    $('#' + id).addEventListener('click', () => $('#control-editor').close());
  }
  $('#control-editor').addEventListener('close', () => { ctrlSourceSeq += 1; });
  $('#ctrl-find-source').addEventListener('click', () => { ctrlSourcePage = 1; findControlSource(); });
  $('#ctrl-edit-kind').addEventListener('change', () => {
    ctrlSourcePage = 1;
    ctrlSourceSeq += 1;
    $('#ctrl-source-rows').replaceChildren();
    $('#ctrl-source-page').textContent = '';
    $('#ctrl-edit-rtu').value = '';
    $('#ctrl-edit-point').value = '';
  });
  $('#ctrl-source-search').addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      ctrlSourcePage = 1;
      findControlSource();
    }
  });
  $('#ctrl-source-prev').addEventListener('click', () => {
    if (ctrlSourcePage <= 1) return;
    ctrlSourcePage -= 1;
    findControlSource();
  });
  $('#ctrl-source-next').addEventListener('click', () => {
    ctrlSourcePage += 1;
    findControlSource();
  });
  $('#ctrl-browse-source').addEventListener('click', () => {
    browserMode = 'ctrl-source';
    if ($('#source-browser-title')) $('#source-browser-title').textContent = '选择目标 scdfe_c 数据库';
    $('#source-browser').showModal();
    const path = $('#ctrl-edit-source').value;
    const slash = path.lastIndexOf('/');
    browse(slash >= 0 ? path.slice(0, slash) : '.');
  });
  $('#control-form').addEventListener('submit', async event => {
    event.preventDefault();
    error('#ctrl-editor-error', '');
    $('#ctrl-editor-save').disabled = true;
    try {
      const minVal = parseLimitInput($('#ctrl-edit-min').value);
      const maxVal = parseLimitInput($('#ctrl-edit-max').value);
      const rangeErr = limitRangeError(maxVal, minVal);
      if (rangeErr) {
        error('#ctrl-editor-error', rangeErr);
        return;
      }
      const data = {
        device_id: Number($('#ctrl-edit-device').value),
        control: $('#ctrl-edit-attr').value.trim(),
        kind: $('#ctrl-edit-kind').value,
        source_database: $('#ctrl-edit-source').value.trim(),
        rtu_id: Number($('#ctrl-edit-rtu').value),
        pnt_no: Number($('#ctrl-edit-point').value),
        max_value: maxVal,
        min_value: minVal,
        notuse: Number($('#ctrl-edit-notuse').checked),
      };
      if (ctrlEditing) data.version = ctrlEditing.version;
      await api('/api/controls' + (ctrlEditing ? '/' + ctrlEditing.control_id : ''), ctrlEditing ? 'PUT' : 'POST', data);
      $('#control-editor').close();
      await loadControls();
    } catch (exception) {
      error($('#control-editor').open ? '#ctrl-editor-error' : '#alert', exception.message);
    } finally {
      $('#ctrl-editor-save').disabled = false;
    }
  });

  // 量测查询 <-> 批量调参 无缝互切（保留当前设备上下文）
  $('#switch-live-view')?.addEventListener('click', () => {
    if (!queryMode) location.href = '/measurements' + location.search;
  });
  $('#switch-param-view')?.addEventListener('click', () => {
    if (!parameterMode) location.href = '/parameters' + location.search;
  });
  $('#sync-meas-bindings')?.addEventListener('click', run(() => runSyncBindings('measurements')));
  $('#export-meas-csv')?.addEventListener('click', run(() => exportMappingsCsv(false)));
  $('#sync-ctrl-bindings')?.addEventListener('click', run(() => runSyncBindings('controls')));
  $('#export-ctrl-csv')?.addEventListener('click', run(() => exportMappingsCsv(true)));

  // 运行日志与记录 4 分段切换事件绑定
  for (const btn of document.querySelectorAll('#logs-section-tabs [data-log-tab]')) {
    btn.addEventListener('click', run(async () => {
      logTab = btn.dataset.logTab || 'files';
      for (const b of document.querySelectorAll('#logs-section-tabs [data-log-tab]')) {
        b.classList.toggle('active', b === btn);
      }
      await loadLogsView({ append: false });
    }));
  }
  const reloadLogs = run(async () => { await loadLogsView({ append: false }); });
  $('#log-process').addEventListener('change', reloadLogs);
  $('#log-level').addEventListener('change', reloadLogs);
  $('#log-file').addEventListener('change', reloadLogs);
  let logTimer;
  $('#log-search').addEventListener('input', () => {
    clearTimeout(logTimer);
    logTimer = setTimeout(reloadLogs, 250);
  });
  $('#log-refresh').addEventListener('click', run(async () => {
    if (logTab === 'files') await loadLogFiles();
    await loadLogsView({ append: false });
  }));
  $('#log-more').addEventListener('click', run(async () => {
    await loadLogs({ append: true });
  }));
  $('#log-export').addEventListener('click', exportLogs);

  run(init)();
  const update = run(refresh);
  setInterval(async () => {
    if (!ready || polling || collectorActionPending || document.hidden || document.querySelector('dialog[open]')) return;
    if (controlDispatchMode && document.activeElement?.matches('.ctrl-field')) return;
    const autoOn = mode === '/'
      || (queryMode && $('#auto-refresh').checked)
      || (controlDispatchMode && $('#ctrl-auto-refresh').checked)
      || (logsMode && $('#log-auto-refresh').checked);
    if (!autoOn) return;
    polling = true;
    try {
      await update();
    } finally {
      polling = false;
    }
  }, 5000);
})();

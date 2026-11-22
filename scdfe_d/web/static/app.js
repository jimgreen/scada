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
  const deviceMode = mappingMode || queryMode || parameterMode;
  const pointTrendHost = $('#point-trend');
  const pointTrend = queryMode && pointTrendHost ? window.scadaPointTrend?.mount(pointTrendHost) : null;
  const liveColumnOffset = pointTrend ? 1 : 0;
  if (pointTrend) pointTrendHost.hidden = false;
  const pointKinds = ['', 'yc', 'yx'];
  const reasons = {
    bad_quality: '源质量不合格', stale: '数据超时', missing_point: '测点缺失',
    source_error: '源库不可用', disabled: '映射停用', notuse: '量测禁用',
    invalid_value: '数据无效', missing_time: '无源时标', future_time: '源时标超前',
    uncollected: '尚未采集', definition_changed: '定义已修改，等待采集',
    unmapped: '未绑定', unbound: '未绑定', no_mapping: '未绑定',
  };

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
  const parameters = ['enabled', 'notuse', 'weight', 'max_age_seconds'];
  const scopeFields = ['category', 'table', 'idx', 'device_id'];

  const number = value => value == null ? '—'
    : Number.isFinite(Number(value)) ? String(Number(Number(value).toPrecision(10))) : String(value);
  const time = value => {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
  };
  const option = (value, label) => '<option value="' + html(value) + '">' + html(label) + '</option>';
  const kindLabel = value => value === 'yx' ? '遥信 YX' : value === 'yc' ? '遥测 YC' : '—';
  const attached = row => row.measurement_id != null;
  const bound = row => Boolean(row.source_database && row.rtu_id != null && row.pnt_no != null);
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
    if (body !== undefined) {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) {
      const failure = new Error(data.error?.message || '请求失败');
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
    renderCollectorControls();
    const last = overview.last_refresh;
    $('#last-refresh').textContent = last
      ? '最近批次 ' + last.refresh_id + ' · ' + time(last.finished_at) + ' · ' + last.total_count
        + ' 个属性 · ' + last.valid_count + ' 个可用 · ' + (last.total_count - last.valid_count) + ' 个不可用'
      : '尚未执行采集';
    $('#category-rows').innerHTML = overview.categories.map(row => '<tr><td>'
      + html(overview.category_labels[row.category] || row.category) + '</td><td>' + html(row.count)
      + '</td><td><a href="/measurements?category=' + encodeURIComponent(row.category)
      + '">查看量测 →</a></td></tr>').join('') || '<tr><td colspan="3" class="empty">暂无设备。</td></tr>';
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
    const valid = Boolean(row.valid && row.enabled && !row.notuse);
    const label = !mapped ? '未绑定' : !row.enabled ? '映射停用' : row.notuse ? '量测禁用'
      : valid ? '可用' : (reasons[row.reason] || row.reason || '不可用');
    const color = !mapped ? 'unbound' : valid ? 'good' : 'bad';
    return '<span class="measure-status ' + color + '">' + html(label) + '</span>';
  }

  function quantityMarkup(row, includeDevice) {
    const quantity = html(row.name || row.metric);
    const target = includeDevice
      ? html(row.model_table || detail?.device.table) + '[' + html(row.model_idx ?? detail?.device.idx) + '].' : '';
    return '<td class="quantity-identity"><strong>' + quantity + '</strong><small><code>' + target
      + html(row.metric) + '</code>' + (includeDevice ? ' · ' + html(row.device_name || detail?.device.name) : '') + '</small></td>';
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
    return '<tr>' + (pointTrend ? '<td class="point-trend-cell"></td>' : '')
      + quantityMarkup(row, !detail) + '<td>' + kindLabel(row.kind) + '</td>'
      + '<td class="quantity-value">' + html(number(row.value)) + (row.unit ? ' ' + html(row.unit) : '') + '</td>'
      + '<td>' + html(time(row.source_time_ms)) + '</td><td>' + html(time(row.collected_at)) + '</td>'
      + '<td>' + html(number(row.quality)) + '</td><td>' + statusMarkup(row) + '</td>'
      + '<td>' + html(number(row.previous_value)) + '</td>' + sourceMarkup(row) + '</tr>';
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
      if (row.source_time_ms !== prior.source_time_ms) cells[3 + liveColumnOffset].textContent = time(row.source_time_ms);
      if (row.collected_at !== prior.collected_at) cells[4 + liveColumnOffset].textContent = time(row.collected_at);
      if (row.quality !== prior.quality) cells[5 + liveColumnOffset].textContent = number(row.quality);
      if (['enabled', 'notuse', 'valid', 'reason'].some(field => row[field] !== prior[field])
          || bound(row) !== bound(prior)) cells[6 + liveColumnOffset].innerHTML = statusMarkup(row);
      if (row.previous_value !== prior.previous_value) cells[7 + liveColumnOffset].textContent = number(row.previous_value);
      if (row.rtu_id !== prior.rtu_id || row.pnt_no !== prior.pnt_no || bound(row) !== bound(prior)) {
        cells[8 + liveColumnOffset].textContent = bound(row) ? row.rtu_id + ' / ' + row.pnt_no : '—';
      }
    });
    return true;
  }

  function configRow(row) {
    const identity = quantityMarkup(row, false) + '<td>' + kindLabel(row.kind) + '</td>' + sourceMarkup(row);
    if (!configurable(row)) {
      const destination = new URLSearchParams({ table: detail.device.table, idx: detail.device.idx, metric: row.metric });
      return '<tr><td></td>' + identity + '<td colspan="4"><a href="/mappings?' + html(destination.toString())
        + '">绑定测点后配置</a></td></tr>';
    }
    const label = html(row.name || row.metric);
    const checkbox = (field, heading) => '<td class="parameter-check"><input ' + attrs(row, field)
      + ' type="checkbox" aria-label="' + label + ' · ' + heading + '"'
      + (parameterValue(row, field) ? ' checked' : '') + (saving ? ' disabled' : '') + '></td>';
    const numberField = (field, heading, min) => '<td><input ' + attrs(row, field)
      + ' class="parameter-field" type="number" step="any" min="' + min + '" required aria-label="'
      + label + ' · ' + heading + '" value="' + html(parameterValue(row, field)) + '"'
      + (saving ? ' disabled' : '') + '></td>';
    const selection = '<td class="parameter-check"><input type="checkbox" data-choose="' + html(row.measurement_id)
      + '" aria-label="选择 ' + label + '"' + (chosen.has(row.measurement_id) ? ' checked' : '')
      + (saving ? ' disabled' : '') + '></td>';
    return '<tr' + (drafts.has(row.measurement_id) ? ' class="changed"' : '') + '>' + selection + identity
      + checkbox('enabled', '采集映射启用') + checkbox('notuse', '禁用量测')
      + numberField('weight', '权重', '0') + numberField('max_age_seconds', '超时阈值（秒）', '0') + '</tr>';
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
        ? ['<input id="choose-all" type="checkbox" aria-label="选择当前显示的可配置量测" title="全选当前显示的可配置量测">', '量测属性', '类型', 'RTU / 点号', '启用采集映射', '禁用量测', '量测权重', '超时阈值（秒）']
        : [...(pointTrend ? ['曲线'] : []), '量测属性', '类型', '实时值', '源时间', '采集时间', '质量', '状态', '前次值', 'RTU / 点号'];
    const renderer = mappingMode ? mappingRow : parameterMode ? configRow : liveRow;
    if (!$('#measure-head').firstElementChild) {
      $('#measure-head').innerHTML = '<tr>' + headers.map(label => '<th scope="col"'
        + (label === '曲线' ? ' class="point-trend-cell"' : '') + '>' + label + '</th>').join('') + '</tr>';
    }
    if (!queryMode || !updateLiveRows(previous, items)) {
      const body = $('#measure-rows');
      body.innerHTML = rows.map(renderer).join('')
        || emptyRow(headers, detail || mappingMode ? '没有匹配的量测' : '请选择设备表和设备');
      if (pointTrend) rows.forEach((row, index) => {
        const element = body.rows[index];
        const cell = element.cells[0];
        if (!attached(row)) {
          cell.textContent = '—';
          cell.title = '绑定量测后可绘制曲线';
          return;
        }
        const quantity = row.name || row.metric;
        const devName = detail?.device?.name || row.device_name || '';
        const name = devName ? `${devName} · ${quantity}` : quantity;
        const definition = {measurement_id: row.measurement_id, kind: row.kind, name, unit: row.unit};
        cell.append(pointTrend.checkbox(definition));
        pointTrend.bindRow?.(element, definition, element.cells[liveColumnOffset]);
      });
    }
    const count = total ?? rows.length;
    $('#table-hint').textContent = detail?.device
      ? detail.device.name + ' · ' + detail.device.table + '[' + detail.device.idx + ']'
      : queryMode
        ? [$('#category').selectedOptions[0]?.textContent, $('#model-table').value || '全部设备表', '全部设备'].filter(Boolean).join(' · ')
        : '';
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

  function updateParameterButtons() {
    $('#save-parameters').disabled = saving || !drafts.size;
    $('#discard-parameters').disabled = saving || !drafts.size;
    for (const selector of ['#category', '#model-table', '#device', '#refresh']) $(selector).disabled = saving || !!drafts.size;
    $('#parameter-status').textContent = saving ? '正在保存…'
      : drafts.size ? drafts.size + ' 项未保存 · 保存或撤销后可切换设备' : parameterNotice;
    $('#draft-filter').hidden = !drafts.size;
    const selectable = rows.filter(configurable).length;
    const all = $('#choose-all');
    all.checked = selectable > 0 && chosen.size === selectable;
    all.indeterminate = chosen.size > 0 && chosen.size < selectable;
    all.disabled = saving || !selectable;
    $('#bulk-parameters').disabled = saving || !chosen.size;
    $('#parameter-selection-count').textContent = '已选 ' + chosen.size + ' 项 · 当前显示 '
      + selectable + ' 项可配置量测';
  }

  function setDraft(row, changes) {
    const next = { ...drafts.get(row.measurement_id) };
    for (const [field, value] of Object.entries(changes)) {
      if (value !== '' && Number(value) === Number(row[field])) delete next[field];
      else next[field] = value;
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
        if (value == null || value === '' || !Number.isFinite(Number(value))
            || (field === 'weight' && Number(value) <= 0)
            || (field === 'max_age_seconds' && Number(value) <= 0)) {
          throw new Error((row.name || row.metric) + '：权重和超时阈值须为正数');
        }
        values[field] = Number(value);
      }
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
    const changes = historyMode === 'changes';
    const headers = changes ? ['时间', '对象', '编号', '修改内容']
      : ['批次', '开始时间', '完成时间', '属性数', '可用数', '异常'];
    $('#history-head').innerHTML = '<tr>' + headers.map(heading => '<th scope="col">' + heading + '</th>').join('') + '</tr>';
    $('#history-rows').innerHTML = data[historyMode].map(row => changes
      ? '<tr><td>' + html(time(row.changed_at)) + '</td><td>' + html(row.entity) + '</td><td>' + html(number(row.entity_id))
        + '</td><td class="wrap">' + html(row.after_json) + '</td></tr>'
      : '<tr><td>' + html(row.refresh_id) + '</td><td>' + html(time(row.started_at)) + '</td><td>'
        + html(time(row.finished_at)) + '</td><td>' + html(row.total_count) + '</td><td>' + html(row.valid_count)
        + '</td><td class="wrap">' + html(row.error || '—') + '</td></tr>').join('')
      || emptyRow(headers, '暂无记录');
  }

  async function refresh() {
    if (deviceMode) await loadRows();
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
      ['weight', 'weight', 1], ['scale', 'scale', 1], ['offset', 'offset', 0], ['age', 'max_age_seconds', 120],
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
      return Promise.resolve(confirm(message));
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
      $('#source-files').innerHTML = data.entries.map((row, index) => '<button data-file="' + index
        + '">' + (row.directory ? '▸ 📁 ' : '▤ ') + html(row.name) + '</button>').join('')
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

  async function init() {
    $('#overview').hidden = mode !== '/';
    $('#overview-stats').hidden = mode !== '/';
    $('#measure-panel').hidden = !deviceMode;
    $('#history-panel').hidden = mode !== '/history';
    $('#definition-actions').hidden = !mappingMode;
    $('#parameter-actions').hidden = !parameterMode;
    $('#parameter-selection').hidden = !parameterMode;
    $('#live-refresh').hidden = !queryMode;
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
  for (const type of ['refreshes', 'changes']) {
    $('#show-' + type).addEventListener('click', run(async () => {
      historyMode = type;
      for (const value of ['refreshes', 'changes']) $('#show-' + value).classList.toggle('active', value === type);
      await loadHistory();
    }));
  }
  $('#previous').addEventListener('click', run(async () => { page -= 1; await loadRows(); }));
  $('#next').addEventListener('click', run(async () => { page += 1; await loadRows(); }));
  $('#save-parameters').addEventListener('click', run(saveParameters));
  $('#discard-parameters').addEventListener('click', () => {
    drafts.clear();
    $('#only-draft').checked = false;
    parameterNotice = '';
    error('#alert', '');
    renderRows(filtered(detail.items));
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
      const data = {
        device_id: Number($('#edit-device').value), metric: $('#edit-metric').value.trim(), kind: $('#edit-kind').value,
        name: $('#edit-name').value.trim(), unit: $('#edit-unit').value.trim(),
        source_database: $('#edit-source').value.trim(), rtu_id: Number($('#edit-rtu').value),
        pnt_no: Number($('#edit-point').value), scale: Number($('#edit-scale').value),
        offset: Number($('#edit-offset').value), weight: Number($('#edit-weight').value),
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
  $('#source-prev').addEventListener('click', () => { sourcePage -= 1; findSource(); });
  $('#source-next').addEventListener('click', () => { sourcePage += 1; findSource(); });
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
    try {
      await api('/api/devices', 'POST', { name: $('#device-name').value, category: $('#device-category').value });
      $('#device-editor').close();
      await loadDevices();
      await refresh();
    } catch (exception) {
      error($('#device-editor').open ? '#device-error' : '#alert', exception.message);
    }
  });

  run(init)();
  const update = run(refresh);
  setInterval(async () => {
    if (!ready || polling || collectorActionPending || document.hidden || document.querySelector('dialog[open]')) return;
    if (mode !== '/' && !(queryMode && $('#auto-refresh').checked)) return;
    polling = true;
    try {
      await update();
    } finally {
      polling = false;
    }
  }, 5000);
})();

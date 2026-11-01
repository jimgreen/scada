"use strict";

const $ = selector => document.querySelector(selector);
const state = {
  schemas: new Map(), resource: 'rtus', rtu: '', view: 'common', offset: 0,
  limit: 25, items: [], total: 0, editing: null, request: 0, saving: false, loading: false,
};
const resources = {
  rtus: ['fes_rtu_define', 'RTU 与通道', '一个 RTU 一条记录，包含通道地址、端口和通道类型'],
  yc: ['fes_yc_define', '遥测点表', '模拟量定义与采集数据'],
  yx: ['fes_yx_define', '遥信点表', '开关量定义与采集数据'],
  yt: ['fes_yt_define', '遥调点表', '设定值定义与记录'],
  yk: ['fes_yk_define', '遥控点表', '控制量定义与记录'],
};
const pointKinds = ['yc', 'yx', 'yt', 'yk'];
const common = {
  rtus: ['rtu_id', 'name', 'code', 'channel_type', 'chan_addr', 'chan_port', 'status'],
  yc: ['rtu_id', 'pnt_no', 'name', 'code', 'raw_value', 'value', 'status', 'refresh_time'],
  yx: ['rtu_id', 'pnt_no', 'name', 'name_0', 'name_1', 'value', 'status', 'refresh_time'],
  yt: ['rtu_id', 'pnt_no', 'name', 'raw_value', 'value', 'status', 'raw_scale', 'refresh_time'],
  yk: ['rtu_id', 'pnt_no', 'name', 'value', 'status', 'refresh_time'],
};

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}
function channelTypeLabel(value) {
  return ({
    'iec104': 'IEC104', 'modbus-rtu': 'Modbus-RTU', 'modbus-tcp': 'Modbus-TCP',
    'can': 'CAN', 'bacnet': 'BACnet', 'opc': 'OPC', 'custom': '自定义',
  })[value] || value;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const data = await response.json();
  if (!response.ok) {
    let message = data.error?.message || '请求失败';
    if (data.error?.code === 'referenced') {
      message += '：' + Object.entries(data.error.details).map(([name, n]) =>
        (state.schemas.get(name)?.head.chn_name || name) + ' ' + n + ' 条').join('、');
    }
    throw new Error(message);
  }
  return data;
}

function toast(message, error = false) {
  const box = $('#toast');
  box.textContent = message;
  box.className = 'toast show' + (error ? ' error' : '');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { box.className = 'toast'; }, 4000);
}
function metadata(resource = state.resource) {
  return resources[resource] || [resource, state.schemas.get(resource)?.head.chn_name || resource, '协议与辅助表配置'];
}
function schemaFor(resource = state.resource) {
  return state.schemas.get(metadata(resource)[0]);
}
function keyOf(item, schema) {
  return schema.primary_keys.map(key => String(item[key])).join(',');
}
function context() {
  const resource = state.resource;
  const schema = schemaFor(resource);
  const hasRtu = resource !== 'rtus' && schema.columns.some(c => c.eng_name === 'rtu_id');
  return { resource, schema, rtu: hasRtu ? state.rtu : '' };
}
function pathFor(ctx, row = null) {
  let url;
  if (pointKinds.includes(ctx.resource)) {
    url = ctx.rtu ? '/api/rtus/' + encodeURIComponent(ctx.rtu) + '/points/' + ctx.resource : '/api/points/' + ctx.resource;
  } else if (ctx.resource === 'rtus') {
    url = '/api/rtus';
  } else {
    url = '/api/tables/' + encodeURIComponent(ctx.schema.head.eng_name) + '/rows';
  }
  if (row) {
    const key = pointKinds.includes(ctx.resource) && ctx.rtu ? String(row.pnt_no) : keyOf(row, ctx.schema);
    url += '/' + encodeURIComponent(key);
  }
  return url;
}

async function refreshSummary() {
  const data = await api('/api/overview');
  const names = [['fes_rtu_define', 'RTU/通道'], ['fes_yc_define', '遥测'],
    ['fes_yx_define', '遥信'], ['fes_yt_define', '遥调'], ['fes_yk_define', '遥控']];
  $('#summary').innerHTML = names.map(([name, label]) =>
    '<div class="metric"><strong>' + esc(data.counts[name] || 0) + '</strong><span>' + label + '记录</span></div>').join('');
  $('#database-path').textContent = data.database;
}
async function loadRtuOptions() {
  const data = await api('/api/rtus?limit=500&sort=rtu_id');
  // Suggestions are bounded; the field also accepts any existing RTU number.
  $('#rtu-options').innerHTML = data.items.map(row =>
    '<option value="' + esc(row.rtu_id) + '">' + esc(row.name || row.code || '未命名') + '</option>').join('');
}
function showView() {
  const [, title, description] = metadata();
  const schema = schemaFor();
  const isPoint = pointKinds.includes(state.resource);
  $('#section-kicker').textContent = isPoint && state.rtu ? 'RTU ' + state.rtu : 'DEFINITIONS';
  $('#section-title').textContent = title;
  $('#hint').textContent = description + (isPoint ? ' · 四遥数据编辑仅更新数据库记录' : '');
  $('#rtu-picker').hidden = state.resource === 'rtus' || !schema.columns.some(c => c.eng_name === 'rtu_id');
  $('#view-live').hidden = !isPoint;
  for (const view of ['common', 'live', 'all']) {
    $('#view-' + view).classList.toggle('selected', state.view === view);
  }
}
async function loadRows() {
  if (!state.schemas.size) return;
  const sequence = ++state.request;
  state.loading = true;
  const ctx = context();
  showView();
  $('#table-error').hidden = true;
  $('#add').disabled = true;
  const params = new URLSearchParams({
    limit: String(state.limit), offset: String(state.offset), q: $('#search').value,
  });
  if (ctx.rtu && !pointKinds.includes(ctx.resource)) params.set('rtu_id', ctx.rtu);
  try {
    const data = await api(pathFor(ctx) + '?' + params);
    if (sequence !== state.request) return;
    if (state.offset && state.offset >= data.total) {
      state.offset = Math.max(0, Math.floor((data.total - 1) / state.limit) * state.limit);
      return loadRows();
    }
    state.items = data.items;
    state.total = data.total;
    renderTable();
    $('#add').disabled = false;
    $('#health').className = 'health ok';
    $('#health span:last-child').textContent = '服务正常';
  } catch (error) {
    if (sequence !== state.request) return;
    state.items = [];
    state.total = 0;
    renderTable();
    $('#table-error').textContent = error.message;
    $('#table-error').hidden = false;
  } finally {
    if (sequence === state.request) state.loading = false;
  }
}
function visibleColumns() {
  const schema = schemaFor();
  if (state.view === 'all') return schema.columns;
  let names = common[state.resource] || schema.columns.slice(0, 8).map(c => c.eng_name);
  if (state.view === 'live' && pointKinds.includes(state.resource)) {
    names = ['rtu_id', 'pnt_no', 'name', 'value', 'status', 'refresh_time', 'raw_value', 'raw_status',
      'refresh_time1', 'refresh_time2', 'refresh_time3'];
  }
  return names.map(n => schema.columns.find(c => c.eng_name === n)).filter(Boolean);
}
function renderTable() {
  const columns = visibleColumns();
  const schema = schemaFor();
  $('#table-head').innerHTML = '<tr>' + columns.map(column =>
    '<th scope="col">' + esc(column.chn_name) + '<small>' + esc(column.eng_name) + '</small></th>').join('') +
    '<th scope="col" class="sticky-actions">操作</th></tr>';
  $('#table-body').innerHTML = state.items.length ? state.items.map((item, index) =>
    '<tr>' + columns.map(column =>
      '<td class="' + (column.primary_key ? 'key' : '') + '" title="' + esc(item[column.eng_name]) + '">' +
      esc(item[column.eng_name] ?? '—') + '</td>').join('') +
      '<td class="actions-cell sticky-actions"><button class="row-button" data-edit="' + index +
      '">编辑</button><button class="row-button delete" data-delete="' + index + '">删除</button>' +
      (state.resource === 'rtus' ? '<button class="row-button" data-points="' + index + '">四遥</button>' : '') +
      '</td></tr>').join('') :
    '<tr><td class="empty" colspan="' + (columns.length + 1) + '">' +
    '暂无记录<br><small>先创建 RTU，再配置所属通道和四遥点表。</small></td></tr>';
  $('#table-body').querySelectorAll('[data-edit]').forEach(button => {
    button.onclick = () => openEditor(state.items[Number(button.dataset.edit)]);
  });
  $('#table-body').querySelectorAll('[data-delete]').forEach(button => {
    button.onclick = () => removeRow(state.items[Number(button.dataset.delete)]);
  });
  $('#table-body').querySelectorAll('[data-points]').forEach(button => {
    button.onclick = () => {
      state.rtu = String(state.items[Number(button.dataset.points)].rtu_id);
      $('#rtu-filter').value = state.rtu;
      state.view = 'live';
      changeResource('yc');
    };
  });
  $('#result-count').textContent = '共 ' + state.total + ' 条 · ' + schema.columns.length + ' 个字段';
  $('#page').textContent = (Math.floor(state.offset / state.limit) + 1) + ' / ' + Math.max(1, Math.ceil(state.total / state.limit));
  $('#prev').disabled = state.offset === 0;
  $('#next').disabled = state.offset + state.limit >= state.total;
}

async function openEditor(row = null) {
  const ctx = context();
  try {
    // Always edit the latest record. A later concurrent edit is protected by If-Match.
    if (row) row = (await api(pathFor(ctx, row))).item;
    if (ctx.resource !== state.resource || ctx.rtu !== context().rtu) return;
    state.editing = { ...ctx, row };
    $('#form-title').textContent = (row ? '编辑 · ' : '新增 · ') + ctx.schema.head.chn_name;
    $('#form-kicker').textContent = row ? 'EDIT RECORD' : 'NEW RECORD';
    $('#form-error').textContent = '';
    $('#form-fields').innerHTML = ctx.schema.columns.map(column => {
      const name = column.eng_name;
      const scoped = name === 'rtu_id' && !!ctx.rtu;
      const disabled = !!row && column.primary_key;
      const required = column.primary_key || column.required === true || name === 'rtu_id';
      const value = row?.[name] ?? (scoped ? ctx.rtu : '');
      const attrs = (disabled ? ' disabled' : '') + (scoped ? ' readonly' : '') + (required ? ' required' : '');
      const numeric = column.data_type !== 'str';
      const type = column.data_type === 'float' ? 'number' : 'text';
      const list = name === 'rtu_id' && ctx.resource !== 'rtus' ? ' list="rtu-options"' : '';
      const options = Array.isArray(column.allowed_values) ? column.allowed_values : null;
      const control = options ? '<select id="f-' + esc(name) + '" data-field="' + esc(name) + '"' + attrs + '>' +
        options.map(option => '<option value="' + esc(option) + '"' + (String(value) === option ? ' selected' : '') + '>' +
          esc(name === 'channel_type' ? channelTypeLabel(option) : option) + '</option>').join('') + '</select>' :
        '<input id="f-' + esc(name) + '" data-field="' + esc(name) + '" type="' + type +
        '" step="any"' + (numeric ? ' inputmode="decimal"' : '') + list + attrs +
        ' value="' + esc(value) + '">';
      return '<div class="field"><label for="f-' + esc(name) + '">' + esc(column.chn_name) +
        (required ? ' *' : '') + '<small>' + esc(name) + (column.primary_key ? ' · 主键' : '') + '</small></label>' +
      control +
        '<small class="field-help">' + esc(column.disp_type) +
        (column.disp_menu ? ' · 菜单：' + esc(column.disp_menu) : '') + '</small></div>';
    }).join('');
    $('#editor').showModal();
    const preferred = state.view === 'live' && row ? $('#f-value') : $('#form-fields input:not(:disabled):not([readonly])');
    preferred?.focus();
  } catch (error) {
    toast(error.message, true);
  }
}

function typedValue(input, column) {
  const value = column.data_type === 'str' ? input.value : input.value.trim();
  if (column.data_type === 'str') return value;
  if (value === '') return null;
  if (column.data_type === 'int') {
    if (!/^[+-]?[0-9]+$/.test(value)) throw new Error(column.chn_name + ' 必须是整数');
    const number = BigInt(value);
    if (number < -9223372036854775808n || number > 9223372036854775807n) throw new Error(column.chn_name + ' 超出整数范围');
    return String(number); // Preserve every bit of an int64 in JSON.
  }
  const number = Number(value);
  if (!Number.isFinite(number)) throw new Error(column.chn_name + ' 必须是有限数字');
  return number;
}
async function saveRow(event) {
  event.preventDefault();
  if (state.saving) return;
  const ctx = state.editing;
  if (!ctx || !$('#editor-form').reportValidity()) return;
  const body = {};
  try {
    for (const column of ctx.schema.columns) {
      const input = $('#f-' + column.eng_name);
      if (input.disabled) continue;
      const original = ctx.row?.[column.eng_name];
      // An unchanged empty control keeps NULL as NULL.
      if (ctx.row && input.value === String(original ?? '')) continue;
      if (!ctx.row && input.value === '' && !column.primary_key && column.eng_name !== 'rtu_id') continue;
      body[column.eng_name] = typedValue(input, column);
    }
    if (ctx.row && !Object.keys(body).length) {
      $('#editor').close();
      return toast('没有需要保存的修改');
    }
    state.saving = true;
    $('#save').disabled = true;
    const headers = ctx.row ? { 'If-Match': String(ctx.row._version) } : {};
    await api(pathFor(ctx, ctx.row), {
      method: ctx.row ? 'PATCH' : 'POST', headers, body: JSON.stringify(body),
    });
    $('#editor').close();
    toast(ctx.row ? '记录已更新' : '记录已创建');
    await loadRows();
    await Promise.all([refreshSummary(), loadRtuOptions()]);
  } catch (error) {
    $('#form-error').textContent = error.message;
  } finally {
    state.saving = false;
    $('#save').disabled = false;
  }
}
async function removeRow(row) {
  const ctx = context();
  const label = ctx.schema.head.chn_name + ' [' + keyOf(row, ctx.schema) + '] ' + (row.name || '');
  if (!confirm('确认删除 ' + label + '？')) return;
  try {
    await api(pathFor(ctx, row), { method: 'DELETE', headers: { 'If-Match': String(row._version) } });
    toast('记录已删除');
    await loadRows();
    await Promise.all([refreshSummary(), loadRtuOptions()]);
  } catch (error) {
    toast(error.message, true);
  }
}
function changeResource(resource) {
  state.resource = resource;
  const isScdfe = resource === 'scdfe';
  $('.workspace').hidden = isScdfe;
  $('#scdfe-panel').hidden = !isScdfe;
  if (isScdfe) { loadScdfe(); return; }
  state.offset = 0;
  $('#search').value = '';
  if (!pointKinds.includes(resource) && state.view === 'live') state.view = 'common';
  document.querySelectorAll('.nav').forEach(button => button.classList.toggle('active', button.dataset.resource === resource));
  $('#extra-table').value = resources[resource] ? '' : resource;
  loadRows();
}

function fmtTime(value) {
  return value ? new Date(Number(value)).toLocaleString() : '—';
}
function statusLabel(value) {
  return ({0:'停止',1:'启动中',2:'运行中',3:'停止中',4:'重启中',5:'错误'})[value] || String(value ?? '—');
}
async function loadScdfe() {
  try {
    const status = await api('/api/scdfe_c/status');
    const item = status.item;
    $('#scdfe-worker-delay').value = item.worker_start_delay_ms ?? 250;
    $('#scdfe-retention-hours').value = item.log_retention_hours ?? 720;
    $('#scdfe-status').innerHTML = '<div class="metric"><strong>' + esc(statusLabel(item.process_status)) + '</strong><span>主进程状态</span></div><div class="metric"><strong>' + esc(item.process_pid || 0) + '</strong><span>主进程 PID</span></div><div class="metric"><strong>' + esc(item.process_restart_count || 0) + '</strong><span>重启次数</span></div><div class="metric"><strong>' + esc(fmtTime(item.process_heartbeat_time)) + '</strong><span>最近心跳</span></div><div class="metric"><strong>' + esc(item.process_error || '—') + '</strong><span>最近错误</span></div>';
    $('#scdfe-rtus').innerHTML = status.rtus.map(row => '<tr><td>' + esc(row.rtu_id) + '</td><td>' + esc(row.channel_id) + '</td><td>' + esc(channelTypeLabel(row.channel_type)) + '</td><td>' + esc(statusLabel(row.process_status)) + '</td><td>' + esc(row.thread_id || 0) + '</td><td>' + esc(fmtTime(row.process_heartbeat_time)) + '</td><td>' + esc(row.process_error || '—') + '</td></tr>').join('') || '<tr><td colspan="7">暂无 RTU 通信线程</td></tr>';
    await loadScdfeLogs();
  } catch (error) { toast(error.message, true); }
}
async function loadScdfeLogs() {
  const params = new URLSearchParams();
  if ($('#log-process').value) params.set('process', $('#log-process').value);
  const data = await api('/api/scdfe_c/logs?' + params);
  const processes = [...new Set(data.items.map(item => item.process))];
  const selected = $('#log-process').value;
  $('#log-process').innerHTML = '<option value="">全部进程</option>' + processes.map(item => '<option value="' + esc(item) + '">' + esc(item) + '</option>').join('');
  $('#log-process').value = selected;
  $('#scdfe-logs').innerHTML = data.items.map((item, index) => '<tr><td>' + esc(item.name) + '</td><td>' + esc(item.process) + '</td><td>' + esc(item.hour) + '</td><td>' + esc(item.size) + '</td><td>' + esc(fmtTime(item.modified_at)) + '</td><td><button class="row-button" data-log-index="' + index + '">查看</button></td></tr>').join('') || '<tr><td colspan="6">暂无日志文件</td></tr>';
  $('#scdfe-logs').querySelectorAll('[data-log-index]').forEach(button => { button.onclick = async () => { const item = data.items[Number(button.dataset.logIndex)]; const content = await api('/api/scdfe_c/logs/' + encodeURIComponent(item.name) + '?tail=300'); $('#scdfe-log-content').textContent = content.item.content; $('#scdfe-log-content').hidden = false; }; });
}
async function controlScdfe(values) { try { await api('/api/scdfe_c/control', {method:'PATCH', body: JSON.stringify(values)}); toast('控制命令已写入'); await loadScdfe(); } catch (error) { toast(error.message, true); } }
function closeEditor() {
  if (!state.saving) $('#editor').close();
}
async function boot() {
  try {
    await api('/api/health');
    const { items } = await api('/api/schemas');
    items.forEach(schema => state.schemas.set(schema.head.eng_name, schema));
    const primary = Object.values(resources).map(item => item[0]);
    $('#extra-table').innerHTML = '<option value="">选择表…</option>' + items.filter(schema => !primary.includes(schema.head.eng_name) && schema.head.eng_name !== 'fes_scdfe_c_define')
      .map(schema => '<option value="' + esc(schema.head.eng_name) + '">' + esc(schema.head.chn_name) + '</option>').join('');
    await Promise.all([refreshSummary(), loadRtuOptions()]);
    await loadRows();
  } catch (error) {
    $('#health').className = 'health error';
    $('#health span:last-child').textContent = '连接失败';
    $('#table-error').textContent = error.message;
    $('#table-error').hidden = false;
  }
}

document.querySelectorAll('.nav').forEach(button => { button.onclick = () => changeResource(button.dataset.resource); });
$('#extra-table').onchange = event => { if (event.target.value) changeResource(event.target.value); };
$('#scdfe-start').onclick = () => controlScdfe({process_enable: 1});
$('#scdfe-stop').onclick = () => controlScdfe({process_enable: 0});
$('#scdfe-restart').onclick = () => controlScdfe({process_enable: 1, process_restart: 1});
$('#scdfe-refresh').onclick = loadScdfe;
$('#scdfe-save-delay').onclick = () => {
  const input = $('#scdfe-worker-delay');
  const value = Number(input.value);
  if (!input.value.trim() || !Number.isInteger(value) || value < 0 || value > 60000) {
    toast('启动间隔必须是 0 到 60000 的整数毫秒', true);
    input.focus();
    return;
  }
  controlScdfe({worker_start_delay_ms: value});
};
$('#scdfe-save-retention').onclick = () => {
  const input = $('#scdfe-retention-hours');
  const value = Number(input.value);
  if (!input.value.trim() || !Number.isInteger(value) || value < 1 || value > 87600) {
    toast('日志保留时间必须是 1 到 87600 的整数小时', true);
    input.focus();
    return;
  }
  controlScdfe({log_retention_hours: value});
};
$('#log-process').onchange = loadScdfeLogs;
$('#refresh').onclick = async () => {
  await loadRows();
  try { await Promise.all([refreshSummary(), loadRtuOptions()]); } catch (error) { toast(error.message, true); }
};
$('#add').onclick = () => openEditor();
$('#editor-form').onsubmit = saveRow;
$('#close-editor').onclick = $('#cancel-editor').onclick = closeEditor;
$('#editor').addEventListener('cancel', event => { if (state.saving) event.preventDefault(); });
$('#search').oninput = () => {
  state.offset = 0;
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(loadRows, 250);
};
$('#rtu-filter').oninput = event => {
  clearTimeout(state.rtuTimer);
  state.rtu = event.target.value.trim();
  state.offset = 0;
  ++state.request;
  $('#add').disabled = true;
  state.rtuTimer = setTimeout(() => {
    loadRows();
  }, 300);
};
for (const view of ['common', 'live', 'all']) {
  $('#view-' + view).onclick = () => { state.view = view; showView(); renderTable(); };
}
$('#prev').onclick = () => { state.offset = Math.max(0, state.offset - state.limit); loadRows(); };
$('#next').onclick = () => { state.offset += state.limit; loadRows(); };
$('#show-schema').onclick = () => {
  const schema = schemaFor();
  if (!schema) return;
  $('#schema-title').textContent = schema.head.chn_name + ' · ' + schema.columns.length + ' 字段';
  $('#schema-json').textContent = JSON.stringify(schema, null, 2);
  $('#schema-dialog').showModal();
};
$('#close-schema').onclick = () => $('#schema-dialog').close();
setInterval(() => {
  if ($('#auto-refresh').checked && !document.hidden && !$('#editor').open && !$('#schema-dialog').open && !state.saving && !state.loading) loadRows();
}, 5000);
boot();

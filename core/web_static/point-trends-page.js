(() => {
  'use strict';

  const container = document.querySelector('#trend-management-chart');
  const tbody = document.querySelector('#trend-point-rows');
  const count = document.querySelector('#trend-point-count');
  const status = document.querySelector('#trend-list-status');
  const error = document.querySelector('#trend-list-error');
  const form = document.querySelector('#trend-retention-form');
  const retentionInput = document.querySelector('#trend-retention');
  const save = document.querySelector('#trend-retention-save');
  const types = {yc:'遥测', yx:'遥信', yt:'遥调', yk:'遥控'};
  const formatTime = new Intl.DateTimeFormat(undefined, {month:'2-digit', day:'2-digit', hour:'2-digit',
    minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  const formatValue = new Intl.NumberFormat(undefined, {maximumSignificantDigits: 7});
  const rows = new Map();
  const empty = document.createElement('tr');
  const emptyCell = empty.insertCell();
  emptyCell.colSpan = 7;
  emptyCell.className = 'trend-list-empty';

  let state = {ready:false, saving:false, retentionMinutes:60, series:[]};
  let retentionDirty = false;

  function action(label, name, handler, className = '') {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = label;
    button.setAttribute('aria-label', label + ' · ' + name);
    button.addEventListener('click', handler);
    return button;
  }

  function makeRow(item) {
    const element = document.createElement('tr');
    const name = element.insertCell();
    name.className = 'trend-list-name';
    const type = element.insertCell();
    const id = element.insertCell();
    const value = element.insertCell();
    value.className = 'trend-list-value';
    const time = element.insertCell();
    const visibility = element.insertCell();
    const actions = document.createElement('div');
    actions.className = 'trend-list-actions';
    const view = action('查看曲线', item.name, () => trend.showSeries(item.key));
    const remove = action('移除', item.name, async () => {
      if (typeof window.scadaConfirm === 'function' && !(await window.scadaConfirm('确认移除该实时曲线采样点？', { title: '确认移除', confirmText: '确认移除', danger: true }))) return;
      trend.removeSeries(item.key);
    }, 'trend-remove');
    actions.append(view, remove);
    element.insertCell().append(actions);
    return {element, name, type, id, value, time, visibility, view, remove};
  }

  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  function pointId(point) {
    if (point.measurement_id != null) return '量测 ' + point.measurement_id;
    const owner = point.channel_id != null ? '通道 ' + point.channel_id : 'RTU ' + point.rtu_id;
    return owner + ' / 点 ' + point.pnt_no;
  }

  function updateControls() {
    const disabled = !state.ready || state.saving;
    retentionInput.disabled = disabled;
    if (!retentionDirty) retentionInput.value = state.retentionMinutes;
    save.disabled = disabled || !retentionInput.validity.valid ||
      retentionInput.valueAsNumber === state.retentionMinutes;
  }

  function render(next) {
    state = next;
    updateControls();
    setText(error, state.error || '');
    error.hidden = !state.error;
    setText(count, state.ready ? '共 ' + state.series.length + ' 个' : '');
    setText(status, state.saving ? '正在保存…' : state.series.length ? '后台采样周期 ' + state.intervalSeconds + ' 秒' : '');
    const active = new Set(state.series.map(item => item.key));
    for (const [key, row] of rows) {
      if (active.has(key)) continue;
      row.element.remove();
      rows.delete(key);
    }
    if (!state.series.length) {
      setText(emptyCell, state.ready ? '暂无采样点。可在测点表格中右键加入实时曲线。'
        : state.error ? '采样点列表暂不可用。' : '正在读取采样点…');
      if (!empty.isConnected) tbody.append(empty);
      return;
    }
    empty.remove();
    state.series.forEach((item, index) => {
      const row = rows.get(item.key) || makeRow(item);
      rows.set(item.key, row);
      setText(row.name, item.name);
      setText(row.type, types[item.kind] || '量测');
      setText(row.id, pointId(item.point));
      setText(row.value, item.value == null ? '—' : formatValue.format(item.value) +
        (item.unit ? ' ' + item.unit : '') + (item.invalid ? '（无效）' : ''));
      row.value.classList.toggle('trend-list-invalid', item.invalid);
      setText(row.time, item.timeMs == null ? '—' : formatTime.format(item.timeMs));
      row.time.title = item.sourceTimeMs == null ? '' : '测点刷新 ' + formatTime.format(item.sourceTimeMs);
      setText(row.visibility, item.visible ? (item.axis === 'right' ? '右轴' : '左轴') : '已隐藏');
      row.view.disabled = row.remove.disabled = !state.ready || state.saving;
      if (tbody.children[index] !== row.element) tbody.insertBefore(row.element, tbody.children[index] || null);
    });
  }

  render(state);
  const trend = window.scadaPointTrend.mount(container, {management:true, onChange:render});
  retentionInput.addEventListener('input', () => {
    retentionDirty = true;
    updateControls();
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (save.disabled || !form.reportValidity()) return;
    if (trend.setRetention(retentionInput.valueAsNumber)) {
      retentionDirty = false;
      updateControls();
    }
  });
})();

(() => {
  'use strict';

  const container = document.querySelector('#trend-management-chart');
  const tbody = document.querySelector('#trend-point-rows');
  const count = document.querySelector('#trend-point-count');
  const status = document.querySelector('#trend-list-status');
  const error = document.querySelector('#trend-list-error');
  const types = {yc:'遥测', yx:'遥信', yt:'遥调', yk:'遥控'};
  const formatTime = new Intl.DateTimeFormat(undefined, {month:'2-digit', day:'2-digit', hour:'2-digit',
    minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  const formatValue = new Intl.NumberFormat(undefined, {maximumSignificantDigits: 7});
  const rows = new Map();
  const empty = document.createElement('tr');
  const emptyCell = empty.insertCell();
  emptyCell.colSpan = 9;
  emptyCell.className = 'trend-list-empty';

  let state = {ready:false, saving:false, minIntervalSeconds:1, series:[]};
  let activeSelect = null;
  let pendingState = null;

  function setDisabled(node, value) {
    if (node.disabled !== value) node.disabled = value;
  }

  function setTitle(node, value) {
    if (node.title !== value) node.title = value;
  }

  function releaseSelectInteraction() {
    if (!activeSelect) return;
    activeSelect = null;
    if (pendingState) {
      const next = pendingState;
      pendingState = null;
      render(next);
    }
  }

  function action(label, name, handler, className = '') {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = label;
    button.setAttribute('aria-label', label + ' · ' + name);
    button.addEventListener('click', handler);
    return button;
  }

  function numberCellInput({min, max, value, ariaLabel, className, onCommit}) {
    const input = document.createElement('input');
    input.type = 'number';
    input.min = String(min);
    input.max = String(max);
    input.step = '1';
    input.value = String(value);
    input.className = className;
    input.setAttribute('aria-label', ariaLabel);
    input.addEventListener('change', () => onCommit(input));
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        input.blur();
      }
    });
    return input;
  }

  function makeRow(item) {
    const element = document.createElement('tr');
    const name = element.insertCell();
    name.className = 'trend-list-name';
    const type = element.insertCell();
    const id = element.insertCell();

    const intervalCell = element.insertCell();
    const intervalInput = numberCellInput({
      min: 1, max: 3600, value: item.intervalSeconds ?? 1,
      ariaLabel: '采样周期（秒） · ' + item.name,
      className: 'trend-cell-input trend-interval-input',
      onCommit: input => {
        const current = state.series.find(entry => entry.key === item.key);
        const parsed = input.valueAsNumber;
        if (!current || !input.validity.valid || !Number.isInteger(parsed)) {
          input.value = String(current?.intervalSeconds ?? 1);
          return;
        }
        if (!trend.updateSeriesConfig(item.key, {intervalSeconds: parsed})) {
          input.value = String(current.intervalSeconds ?? 1);
        }
      },
    });
    intervalCell.append(intervalInput);

    const retentionCell = element.insertCell();
    const retentionInput = numberCellInput({
      min: 1, max: 100000, value: item.retentionPoints ?? 10000,
      ariaLabel: '后台留存（点数） · ' + item.name,
      className: 'trend-cell-input trend-retention-input',
      onCommit: input => {
        const current = state.series.find(entry => entry.key === item.key);
        const parsed = input.valueAsNumber;
        if (!current || !input.validity.valid || !Number.isInteger(parsed)) {
          input.value = String(current?.retentionPoints ?? 10000);
          return;
        }
        if (!trend.updateSeriesConfig(item.key, {retentionPoints: parsed})) {
          input.value = String(current.retentionPoints ?? 10000);
        }
      },
    });
    retentionCell.append(retentionInput);

    const value = element.insertCell();
    value.className = 'trend-list-value';
    const time = element.insertCell();
    time.className = 'trend-list-time';
    const stateCell = element.insertCell();
    const stateSelect = document.createElement('select');
    stateSelect.className = 'trend-cell-select';
    stateSelect.setAttribute('aria-label', '曲线状态 · ' + item.name);
    for (const [val, text] of [['hidden', '隐藏'], ['left', '左轴显示'], ['right', '右轴显示']]) {
      stateSelect.add(new Option(text, val));
    }
    stateSelect.value = item.displayState || (item.visible ? (item.axis === 'right' ? 'right' : 'left') : 'hidden');
    // 打开下拉框期间冻结表格 DOM 更新，避免每秒刷新触发重排导致原生下拉菜单自动收起。
    const beginSelect = () => { activeSelect = stateSelect; };
    stateSelect.addEventListener('pointerdown', beginSelect);
    stateSelect.addEventListener('mousedown', beginSelect);
    stateSelect.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        releaseSelectInteraction();
      } else if ([' ', 'Enter', 'ArrowDown', 'ArrowUp'].includes(event.key)) {
        beginSelect();
      }
    });
    stateSelect.addEventListener('blur', releaseSelectInteraction);
    stateSelect.addEventListener('change', () => {
      const nextValue = stateSelect.value;
      activeSelect = null;
      const current = state.series.find(entry => entry.key === item.key);
      if (!trend.updateSeriesConfig(item.key, {displayState: nextValue}) && current) {
        stateSelect.value = current.displayState || (current.visible ? (current.axis === 'right' ? 'right' : 'left') : 'hidden');
      }
      if (pendingState) {
        const queued = pendingState;
        pendingState = null;
        render(queued);
      }
    });
    stateCell.append(stateSelect);
    const actions = document.createElement('div');
    actions.className = 'trend-list-actions';
    const view = action('查看曲线', item.name, () => trend.showSeries(item.key));
    const remove = action('移除', item.name, async () => {
      if (typeof window.scadaConfirm === 'function' && !(await window.scadaConfirm('确认移除该实时曲线采样点？', { title: '确认移除', confirmText: '确认移除', danger: true }))) return;
      trend.removeSeries(item.key);
    }, 'trend-remove');
    actions.append(view, remove);
    element.insertCell().append(actions);
    return {element, name, type, id, intervalInput, retentionInput, value, time, stateSelect, view, remove};
  }

  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  function pointId(point) {
    if (point.measurement_id != null) return '量测 ' + point.measurement_id;
    const owner = point.channel_id != null ? '通道 ' + point.channel_id : 'RTU ' + point.rtu_id;
    return owner + ' / 点 ' + point.pnt_no;
  }

  function render(next) {
    if (activeSelect && activeSelect.isConnected) {
      pendingState = next;
      return;
    }
    state = next;
    setText(error, state.error || '');
    error.hidden = !state.error;
    setText(count, state.ready ? '共 ' + state.series.length + ' 个' : '');
    setText(
      status,
      state.saving ? '正在保存…'
        : state.series.length ? '后台独立采样 · 最快 ' + state.minIntervalSeconds + ' 秒' : '',
    );
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
    if (empty.isConnected) empty.remove();
    const disabled = !state.ready || state.saving;
    state.series.forEach((item, index) => {
      const row = rows.get(item.key) || makeRow(item);
      rows.set(item.key, row);
      setText(row.name, item.name);
      setText(row.type, types[item.kind] || '量测');
      setText(row.id, pointId(item.point));
      setDisabled(row.intervalInput, disabled);
      if (document.activeElement !== row.intervalInput) {
        const iv = String(item.intervalSeconds ?? 1);
        if (row.intervalInput.value !== iv) row.intervalInput.value = iv;
      }
      setDisabled(row.retentionInput, disabled);
      if (document.activeElement !== row.retentionInput) {
        const rv = String(item.retentionPoints ?? 10000);
        if (row.retentionInput.value !== rv) row.retentionInput.value = rv;
      }
      setText(row.value, item.value == null ? '—' : formatValue.format(item.value) +
        (item.unit ? ' ' + item.unit : '') + (item.invalid ? '（无效）' : ''));
      row.value.classList.toggle('trend-list-invalid', item.invalid);
      setText(row.time, item.timeMs == null ? '—' : formatTime.format(item.timeMs));
      setTitle(row.time, item.sourceTimeMs == null ? '' : '测点刷新 ' + formatTime.format(item.sourceTimeMs));
      setDisabled(row.stateSelect, disabled);
      const nextDisplayState = item.displayState || (item.visible ? (item.axis === 'right' ? 'right' : 'left') : 'hidden');
      if (document.activeElement !== row.stateSelect && row.stateSelect.value !== nextDisplayState) {
        row.stateSelect.value = nextDisplayState;
      }
      setDisabled(row.view, disabled);
      setDisabled(row.remove, disabled);
      if (tbody.children[index] !== row.element) tbody.insertBefore(row.element, tbody.children[index] || null);
    });
  }

  // 点击下拉框外部取消时兜底释放交互锁定，确保实时数据恢复更新。
  document.addEventListener('pointerdown', event => {
    if (activeSelect && event.target !== activeSelect) releaseSelectInteraction();
  }, true);

  render(state);
  const trend = window.scadaPointTrend.mount(container, {management:true, onChange:render});
})();

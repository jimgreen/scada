'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const MAX_ROWS = 500;
  let after = 0;
  let busy = false;
  let rows = [];
  let generation = 0;
  let initial = true;
  let cursorSignature = '';
  let lastComponent = '';

  function formatUtc(ms) {
    return ms == null ? '无源时标' : new Date(ms).toISOString().replace('T', ' ').replace('Z', '');
  }

  function buildRow(values) {
    const tr = document.createElement('tr');
    for (const value of values) {
      const td = document.createElement('td');
      if (value instanceof Node) td.append(value);
      else td.textContent = value;
      tr.append(td);
    }
    return tr;
  }

  function makeEventRow(r) {
    const flags = [
      r.time_invalid ? '无效' : '',
      r.time_substituted ? '替代' : '',
      r.summer ? '夏令时' : '',
    ].filter(Boolean).join(' / ') || '正常';
    const cp56 = r.cp56_hex || (r.origin === 'S' && [30, 31].includes(r.type_id) ? '按记录时刻编码' : '无 CP56 标签');
    const rtuCell = document.createElement('span');
    rtuCell.textContent = `${r.rtu_id} / ${r.channel_id}`;
    if (r.channel_id && lastComponent) {
      const link = document.createElement('a');
      link.href = lastComponent === 'c'
        ? `/#logs`
        : `/logs${lastComponent === 't' ? '.html' : ''}?channel_id=${encodeURIComponent(r.channel_id)}`;
      link.textContent = ' 日志↗';
      link.title = '查看通道日志';
      rtuCell.append(link);
    }
    return buildRow([
      r.event_id,
      rtuCell,
      r.pnt_no,
      `${r.common_address} / ${r.io_address}`,
      `${r.type_id} / ${r.cot}${r.is_test ? ' 测试' : ''}`,
      r.raw_state,
      '0x' + Number(r.quality || 0).toString(16).padStart(2, '0'),
      formatUtc(r.event_time_ms),
      formatUtc(r.received_time_ms),
      flags,
      cp56,
    ]);
  }

  function renderEmpty() {
    const body = $('events');
    const tr = buildRow(['暂无 SOE 事件']);
    tr.firstChild.colSpan = 11;
    tr.className = 'empty';
    body.replaceChildren(tr);
  }

  function appendEvents(added, resetAll = false) {
    const body = $('events');
    if (resetAll) {
      if (!rows.length) {
        renderEmpty();
        return;
      }
      const frag = document.createDocumentFragment();
      for (const r of rows) frag.append(makeEventRow(r));
      body.replaceChildren(frag);
      return;
    }
    if (!added.length) {
      if (!rows.length && !body.childElementCount) renderEmpty();
      return;
    }
    const emptyRow = body.querySelector('tr.empty');
    if (emptyRow) emptyRow.remove();
    const frag = document.createDocumentFragment();
    for (const r of added) frag.append(makeEventRow(r));
    body.append(frag);
    while (body.childElementCount > MAX_ROWS) {
      body.firstElementChild.remove();
    }
  }

  function renderCursors(cursors) {
    const sig = JSON.stringify(cursors || []);
    if (sig === cursorSignature) return;
    cursorSignature = sig;
    const target = $('cursors');
    if (!cursors || !cursors.length) {
      target.replaceChildren(buildRow(['尚无已确认批次', '—', '—']));
      return;
    }
    const frag = document.createDocumentFragment();
    for (const r of cursors) {
      frag.append(buildRow([r.consumer, r.last_event_id, formatUtc(r.ack_time_ms)]));
    }
    target.replaceChildren(frag);
  }

  async function loadRtuOptions() {
    const select = $('rtu');
    if (!select || select.tagName !== 'SELECT') return;
    try {
      const response = await fetch('/api/command-options', { cache: 'no-store' });
      if (!response.ok) return;
      const data = await response.json();
      const current = select.value;
      const frag = document.createDocumentFragment();
      const all = document.createElement('option');
      all.value = '';
      all.textContent = '全部 RTU';
      frag.append(all);
      for (const item of data.items || []) {
        const opt = document.createElement('option');
        opt.value = String(item.rtu_id);
        opt.textContent = `RTU ${item.rtu_id}${item.name ? ' · ' + item.name : ''}`;
        frag.append(opt);
      }
      select.replaceChildren(frag);
      if ([...select.options].some(o => o.value === current)) select.value = current;
    } catch (_) {
      // 选项加载失败时保留默认“全部 RTU”
    }
  }

  let activeAbort = null;

  async function load(tail = initial) {
    if (busy) return;
    busy = true;
    const version = generation;
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    activeAbort = controller;
    const timer = controller ? setTimeout(() => controller.abort(), 12000) : null;
    try {
      const q = new URLSearchParams({ limit: '100' });
      if ($('rtu').value) q.set('rtu_id', $('rtu').value);
      if (tail) q.set('tail', '1');
      else q.set('after_id', String(after));
      const response = await fetch('/api/soe?' + q, { cache: 'no-store', signal: controller?.signal });
      const data = await response.json();
      if (!response.ok) throw Error(data.error?.message || '读取失败');
      if (version !== generation) return;
      const wasInitial = initial;
      initial = false;
      lastComponent = data.component || '';
      after = data.next_after_id;
      const added = data.items || [];
      if (wasInitial) {
        rows = added.slice(-MAX_ROWS);
        appendEvents(rows, true);
      } else if (added.length) {
        rows = rows.concat(added).slice(-MAX_ROWS);
        appendEvents(added, false);
      } else if (!rows.length) {
        appendEvents([], false);
      }
      $('component').textContent = `scdfe_${data.component} · ${data.component === 's' ? '独立模拟库' : 'C/T 共享业务库'}`;
      $('status').className = '';
      $('status').textContent = `已显示 ${rows.length} 条 · 游标 ${after}${data.has_more ? ' · 还有后续事件' : ''} · 更新于 ${new Date().toLocaleTimeString()}`;
      renderCursors(data.cursors);
    } catch (e) {
      if (version === generation && e?.name !== 'AbortError') {
        $('status').className = 'error';
        $('status').textContent = e.message;
      }
    } finally {
      if (timer) clearTimeout(timer);
      if (activeAbort === controller) activeAbort = null;
      busy = false;
      if (version !== generation) queueMicrotask(() => load(initial));
    }
  }

  function reset(tail) {
    generation++;
    activeAbort?.abort();
    after = 0;
    rows = [];
    initial = tail;
    renderEmpty();
    if (!busy) load(tail);
  }

  $('latest').onclick = () => reset(true);
  $('history').onclick = () => reset(false);
  $('rtu').onchange = () => reset(true);
  $('next').onclick = () => load();
  setInterval(() => {
    if ($('auto').checked && !document.hidden) load();
  }, 1000);
  loadRtuOptions();
  load();
})();

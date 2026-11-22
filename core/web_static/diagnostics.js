(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '—').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const names = {
    c_sample_submit_ms: 'C 样本处理与提交', c_queue_ms: 'C 入队至写库开始',
    c_write_ms: 'C 写库事务（含重试）', c_queue_commit_ms: 'C 入队至提交',
    s_sync_ms: 'S 数据库同步与提交发送', s_source_read_ms: 'S 变化点读取',
    t_source_read_ms: 'T 源点读取', t_refresh_ms: 'T 读取、发布与状态保存',
    d_collect_ms: 'D 设备量测采集刷新',
    c_db_lock_wait_ms: 'C 获取写锁（单次尝试）', c_db_apply_ms: 'C SQL 更新',
    c_db_commit_ms: 'C 提交事务', c_receipt_log_ms: 'C 写库回执日志',
    t_runtime_lock_ms: 'T 显示状态获取写锁', t_runtime_apply_ms: 'T 显示状态 SQL 更新',
    t_runtime_commit_ms: 'T 显示状态提交', t_runtime_flush_ms: 'T 显示状态写库总耗时',
  };
  const commandStages = {
    connection_ms: '连接获取', setup_ms: '事务参数设置', lock_wait_ms: '获取写锁',
    validation_ms: '读取与校验', apply_ms: '更新及回执读取', commit_ms: 'FULL 提交',
    cleanup_ms: '参数恢复', total_ms: '入库调用总耗时',
  };
  for (const [key, name] of Object.entries(commandStages)) names['command_' + key] = '指令 ' + name;

  const fragments = new Map();
  function setHTML(id, html) {
    const el = $(id);
    if (el && fragments.get(id) !== html) {
      el.innerHTML = html;
      fragments.set(id, html);
    }
  }
  function setBadge(id, count) {
    const el = $(id);
    if (!el) return;
    el.textContent = String(count);
    el.classList.toggle('has-items', count > 0);
  }

  // ==================== 明细选项卡 ====================
  const tabButtons = Array.from(document.querySelectorAll('[data-diag-tab]'));
  const tabPanels = Array.from(document.querySelectorAll('[data-diag-panel]'));
  const TAB_KEY = 'scada_diag_tab_v1:' + location.pathname;
  function selectTab(name, persist = true) {
    if (!tabButtons.some(b => b.dataset.diagTab === name)) name = 'command';
    for (const btn of tabButtons) {
      const active = btn.dataset.diagTab === name;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-selected', String(active));
    }
    for (const panel of tabPanels) {
      panel.hidden = panel.dataset.diagPanel !== name;
    }
    if (persist) {
      try { localStorage.setItem(TAB_KEY, name); } catch (_) { /* ignore */ }
    }
  }
  tabButtons.forEach(btn => btn.addEventListener('click', () => selectTab(btn.dataset.diagTab)));
  try {
    const savedTab = localStorage.getItem(TAB_KEY);
    if (savedTab) selectTab(savedTab, false);
  } catch (_) { /* ignore */ }

  // ==================== 横向/纵向可拖拽分隔条 ====================
  const LAYOUT_KEY = 'scada_diag_layout_v1:' + location.pathname;
  const layout = { asideWidth: 360, asideHidden: false, detailHeight: 230, yMode: 'split' };
  try {
    const saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || 'null');
    if (saved && typeof saved === 'object') {
      if (Number.isFinite(saved.asideWidth)) layout.asideWidth = Math.max(220, Math.min(720, saved.asideWidth));
      if (typeof saved.asideHidden === 'boolean') layout.asideHidden = saved.asideHidden;
      if (Number.isFinite(saved.detailHeight)) layout.detailHeight = Math.max(120, Math.min(600, saved.detailHeight));
      if (['split', 'timings-hidden', 'details-hidden'].includes(saved.yMode)) layout.yMode = saved.yMode;
    }
  } catch (_) { /* ignore */ }
  const saveLayout = () => {
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch (_) { /* ignore */ }
  };

  const aside = $('diag-aside');
  const splitX = $('diag-split-x');
  const btnXToggle = $('diag-x-toggle');
  const timingsPane = $('diag-pane-timings');
  const detailsPane = $('diag-pane-details');
  const splitY = $('diag-split-y');
  const btnYUp = $('diag-y-up');
  const btnYDown = $('diag-y-down');

  function applyLayout(persist = false) {
    if (aside && splitX) {
      aside.classList.toggle('diag-pane-hidden', layout.asideHidden);
      splitX.classList.toggle('is-collapsed', layout.asideHidden);
      if (!layout.asideHidden) aside.style.width = `${Math.round(layout.asideWidth)}px`;
      if (btnXToggle) {
        btnXToggle.classList.toggle('is-active', layout.asideHidden);
        btnXToggle.title = layout.asideHidden ? '展开右侧状态栏' : '折叠右侧状态栏';
      }
    }
    if (timingsPane && detailsPane && splitY) {
      timingsPane.classList.toggle('diag-pane-hidden', layout.yMode === 'timings-hidden');
      detailsPane.classList.toggle('diag-pane-hidden', layout.yMode === 'details-hidden');
      detailsPane.classList.toggle('is-expanded', layout.yMode === 'timings-hidden');
      if (layout.yMode === 'split') detailsPane.style.height = `${Math.round(layout.detailHeight)}px`;
      if (btnYUp) btnYUp.classList.toggle('is-active', layout.yMode === 'timings-hidden');
      if (btnYDown) btnYDown.classList.toggle('is-active', layout.yMode === 'details-hidden');
    }
    if (persist) saveLayout();
  }

  if (splitX && aside) {
    btnXToggle?.addEventListener('click', event => {
      event.stopPropagation();
      layout.asideHidden = !layout.asideHidden;
      applyLayout(true);
    });
    splitX.addEventListener('dblclick', event => {
      if (event.target.closest('button')) return;
      layout.asideHidden = false;
      layout.asideWidth = 360;
      applyLayout(true);
    });
    let dragX = null;
    splitX.addEventListener('pointerdown', event => {
      if (event.button !== 0 || event.target.closest('button')) return;
      dragX = { x: event.clientX, width: layout.asideHidden ? 0 : aside.getBoundingClientRect().width, hidden: layout.asideHidden };
      splitX.classList.add('is-dragging');
      document.body.classList.add('diag-resizing-x');
      splitX.setPointerCapture?.(event.pointerId);
      event.preventDefault();
    });
    splitX.addEventListener('pointermove', event => {
      if (!dragX) return;
      const dx = event.clientX - dragX.x;
      const wsWidth = $('diag-workspace')?.getBoundingClientRect().width || window.innerWidth;
      const maxW = Math.max(240, wsWidth - 320);
      if (dragX.hidden) {
        if (dx < -14) {
          dragX.hidden = layout.asideHidden = false;
          layout.asideWidth = Math.max(220, Math.min(maxW, -dx));
          applyLayout(false);
        }
        return;
      }
      const nextW = dragX.width - dx;
      if (nextW < 90) {
        layout.asideHidden = true;
        applyLayout(false);
        return;
      }
      layout.asideHidden = false;
      layout.asideWidth = Math.max(220, Math.min(maxW, nextW));
      applyLayout(false);
    });
    const endX = () => {
      if (!dragX) return;
      dragX = null;
      splitX.classList.remove('is-dragging');
      document.body.classList.remove('diag-resizing-x');
      saveLayout();
    };
    splitX.addEventListener('pointerup', endX);
    splitX.addEventListener('pointercancel', endX);
  }

  if (splitY && timingsPane && detailsPane) {
    btnYUp?.addEventListener('click', event => {
      event.stopPropagation();
      layout.yMode = layout.yMode === 'details-hidden' ? 'split' : layout.yMode === 'timings-hidden' ? 'split' : 'timings-hidden';
      applyLayout(true);
    });
    btnYDown?.addEventListener('click', event => {
      event.stopPropagation();
      layout.yMode = layout.yMode === 'timings-hidden' ? 'split' : layout.yMode === 'details-hidden' ? 'split' : 'details-hidden';
      applyLayout(true);
    });
    splitY.addEventListener('dblclick', event => {
      if (event.target.closest('button')) return;
      layout.yMode = 'split';
      layout.detailHeight = 230;
      applyLayout(true);
    });
    let dragY = null;
    splitY.addEventListener('pointerdown', event => {
      if (event.button !== 0 || event.target.closest('button')) return;
      dragY = {
        y: event.clientY,
        mode: layout.yMode,
        timingsH: timingsPane.getBoundingClientRect().height,
        detailsH: detailsPane.getBoundingClientRect().height || layout.detailHeight,
      };
      splitY.classList.add('is-dragging');
      document.body.classList.add('diag-resizing-y');
      splitY.setPointerCapture?.(event.pointerId);
      event.preventDefault();
    });
    splitY.addEventListener('pointermove', event => {
      if (!dragY) return;
      const dy = event.clientY - dragY.y;
      const totalH = $('diag-primary')?.getBoundingClientRect().height || 600;
      const maxD = Math.max(160, totalH - 120);
      if (dragY.mode === 'details-hidden') {
        if (dy < -12) {
          dragY.mode = layout.yMode = 'split';
          layout.detailHeight = Math.max(120, Math.min(maxD, -dy));
          applyLayout(false);
        }
        return;
      }
      if (dragY.mode === 'timings-hidden') {
        if (dy > 12) {
          dragY.mode = layout.yMode = 'split';
          layout.detailHeight = Math.max(120, Math.min(maxD, totalH - dy));
          applyLayout(false);
        }
        return;
      }
      const nextTimingsH = dragY.timingsH + dy;
      const nextDetailsH = dragY.detailsH - dy;
      if (nextTimingsH < 64) {
        layout.yMode = 'timings-hidden';
        applyLayout(false);
        return;
      }
      if (nextDetailsH < 64) {
        layout.yMode = 'details-hidden';
        applyLayout(false);
        return;
      }
      layout.yMode = 'split';
      layout.detailHeight = Math.max(110, Math.min(maxD, nextDetailsH));
      applyLayout(false);
    });
    const endY = () => {
      if (!dragY) return;
      dragY = null;
      splitY.classList.remove('is-dragging');
      document.body.classList.remove('diag-resizing-y');
      saveLayout();
    };
    splitY.addEventListener('pointerup', endY);
    splitY.addEventListener('pointercancel', endY);
  }
  applyLayout(false);

  // ==================== 数据拉取与渲染 ====================
  let busy = false, markup = '', pendingCheck = false;
  async function load(checks = false) {
    if (busy) { pendingCheck ||= checks; return; }
    busy = true;
    $('refresh').disabled = true;
    try {
      const response = await fetch('/api/diagnostics' + (checks ? '?checks=1' : ''), {
        cache: 'no-store',
        signal: AbortSignal.timeout(12000),
      });
      const data = await response.json();
      if (!response.ok) throw Error(data.error?.message || `HTTP ${response.status}`);
      $('diag-title').textContent = `scdfe_${data.component} · 分段耗时与配置检查`;
      $('note').textContent = data.note;
      const diagLogLink = (comp, row) => {
        if (!row.channel_id || row.service.endsWith('_writer')) return '';
        if (comp === 's') return ` <a href="/logs?channel_id=${encodeURIComponent(row.channel_id)}" title="查看通道日志">日志↗</a>`;
        if (comp === 't') return ` <a href="/logs.html?channel_id=${encodeURIComponent(row.channel_id)}" title="查看通道日志">日志↗</a>`;
        return ` <a href="/?log_channel=${encodeURIComponent(row.channel_id)}#logs" title="查看运行日志">日志↗</a>`;
      };
      const html = data.items.flatMap(row => Object.entries(row.latency).map(([stage, x]) =>
        `<tr><td>${esc(row.name || row.service)}<small> · ${esc(row.service.endsWith('_writer') ? '全局写库' : row.channel_id)}</small>${diagLogLink(data.component, row)}</td>` +
        `<td>${esc(names[stage] || stage)}</td>` +
        `<td class="col-num">${x.count} / ${x.window_count}</td>` +
        `<td class="col-num">${esc(x.last_ms)}</td>` +
        `<td class="col-num">${esc(x.p50_ms)}</td>` +
        `<td class="col-num">${esc(x.p95_ms)}</td>` +
        `<td class="col-num">${esc(x.p99_ms)}</td>` +
        `<td class="${row.stale ? 'stale' : ''}">${row.stale ? '快照已过期' : row.running ? '运行中' : '已停止'} · ${esc(new Date(row.sampled_at).toLocaleTimeString())}</td></tr>`
      )).join('') || '<tr><td colspan="8">暂无阶段样本；启动对应通信进程并产生数据后显示。</td></tr>';
      if (markup !== html) { $('timings').innerHTML = html; markup = html; }

      $('delta').textContent = data.items.filter(x => x.source_delta?.polls).map(x =>
        `${x.name || x.channel_id}：扫描 ${x.source_delta.polls} 次，读取 ${x.source_delta.rows_read} 行，无变化 ${x.source_delta.unchanged_polls} 次`
      ).join('；');

      $('database-wait').textContent = data.items.filter(x => x.database_wait).map(x =>
        `${x.name || x.service}：锁冲突 ${x.database_wait.lock_conflicts} 次，累计退避 ${x.database_wait.retry_backoff_ms} ms，WAL ${(x.database_wait.wal_bytes / 1048576).toFixed(2)} MiB，自动检查点阈值 ${x.database_wait.wal_autocheckpoint_pages} 页，待写 ${x.pending_messages ?? 0} 条消息`
      ).join('\n') || '暂无写库等待样本';

      const slowItems = data.items.flatMap(x => (x.slow_batches?.items || []).slice().reverse());
      setBadge('badge-slow', slowItems.length);
      setHTML('slow-batches', slowItems.map(b =>
        `<tr><td>${esc(new Date(b.sampled_at).toLocaleTimeString())}<small> · ${esc(b.rtus.join('、') || '未标识')}</small></td>` +
        `<td>${esc(b.messages)} / ${esc(b.updates)}</td>` +
        `<td class="col-num">${esc(b.queue_wait_ms)}</td>` +
        `<td class="col-num">${esc(b.lock_wait_ms)} / ${esc(b.backoff_ms)}</td>` +
        `<td class="col-num">${esc(b.apply_ms)} / ${esc(b.commit_ms)}</td>` +
        `<td class="col-num">${esc(b.queue_commit_ms)}</td>` +
        `<td class="col-num">${esc(b.receipt_log_ms)}</td></tr>`
      ).join('') || '<tr><td colspan="7">暂无慢批次记录</td></tr>');

      $('command-summary').textContent = data.items.filter(x => x.command_write?.count).map(x =>
        `${x.name || x.channel_id}：累计 ${x.command_write.count} 次，未提交 ${x.command_write.failed} 次；慢请求阈值 ${x.command_write.threshold_ms} ms`
      ).join('；') || '暂无指令入库样本';

      const runtimeItems = data.items.filter(x => x.point_runtime);
      setBadge('badge-runtime', runtimeItems.length);
      setHTML('point-runtime', runtimeItems.map(x => {
        const b = x.point_runtime;
        return `<tr><td>${esc(x.name || x.channel_id)}<small> · ${x.stale ? '快照已过期' : x.running ? '运行中' : '已停止'}</small></td>` +
          `<td class="col-num">${esc(b.pending_points)} / ${esc(b.pending_age_ms)}</td>` +
          `<td class="col-num">${esc(b.batches)} / ${esc(b.rows)}</td>` +
          `<td class="col-num">${esc(b.failures)} / ${esc(b.lock_conflicts)}</td>` +
          `<td class="col-num">${esc(b.log_unconfirmed)}</td>` +
          `<td>${esc(b.last_error || '—')}</td></tr>`;
      }).join('') || '<tr><td colspan="6">暂无转发显示状态写库样本</td></tr>');

      $('modbus-summary').textContent = data.items.filter(x => x.modbus_requests).map(x => {
        const m = x.modbus_requests;
        return `${x.name || x.channel_id}：已处理 ${m.completed}，待处理 ${m.pending}/${m.limit}，峰值 ${m.peak}，忙拒绝 ${m.busy}，断开或停止时放弃未执行 ${m.abandoned}`;
      }).join('；') || '暂无 Modbus 请求样本';

      const modbusItems = data.items.flatMap(x => (x.modbus_requests?.recent || []).slice().reverse().map(b => [x, b]));
      setBadge('badge-modbus', modbusItems.length);
      setHTML('modbus-requests', modbusItems.map(([x, b]) =>
        `<tr><td>${esc(x.name || x.channel_id)}<small> · ${x.stale ? '快照已过期' : x.running ? '运行中' : '已停止'}</small></td>` +
        `<td>${esc(b.transaction_id)} / ${esc(b.unit_id)} / ${esc(b.function_code)}</td>` +
        `<td>${esc(b.exception_code || '无')} / ${b.peer_disconnected ? '客户端已断开' : b.response_submitted ? '响应已提交' : '未提交响应'}</td>` +
        `<td class="col-num">${esc(b.queue_ms)}</td>` +
        `<td class="col-num">${esc(b.execution_ms)}</td>` +
        `<td class="col-num">${esc(b.response_submit_ms)}</td>` +
        `<td class="col-num">${esc(b.total_ms)}</td></tr>`
      ).join('') || '<tr><td colspan="7">暂无 Modbus 写请求或异常记录</td></tr>');

      const commandItems = data.items.flatMap(x => (x.command_write?.recent || []).slice().reverse().map(b => [x, b]));
      setBadge('badge-command', commandItems.length);
      setHTML('command-writes', commandItems.map(([x, b]) =>
        `<tr><td>${esc(new Date(b.sampled_at).toLocaleTimeString())}<small> · ${esc(x.name || x.channel_id)} · ${esc(b.point_count)} 点${b.points?.length ? ' · ' + esc(b.points.map(p => `RTU ${p.rtu_id}/${p.kind}/${p.pnt_no} @${p.cmd_time ?? '未生成'}`).join('；')) : ''}</small></td>` +
        `<td>${b.committed ? '已提交待处理' : '未提交'}<small> · ${esc(commandStages[b.failed_stage] || b.failed_stage || '—')}${b.error ? ' · ' + esc(b.error) : ''}</small></td>` +
        `<td class="col-num">${esc(b.connection_ms)}</td>` +
        `<td class="col-num">${esc(b.lock_wait_ms)}</td>` +
        `<td class="col-num">${esc(b.validation_ms)} / ${esc(b.apply_ms)}</td>` +
        `<td class="col-num">${esc(b.commit_ms)}</td>` +
        `<td class="col-num">${esc(b.total_ms)}</td></tr>`
      ).join('') || '<tr><td colspan="7">暂无慢请求或失败请求</td></tr>');

      $('soe-progress').textContent = data.items.filter(x => x.soe_progress).map(x =>
        `${x.name || x.channel_id}：持久化游标 ${x.soe_progress.durable_cursor}；` +
        `${x.soe_progress.disconnected_floor != null ? '保留断开连接进度 ' + x.soe_progress.disconnected_floor + '；' : ''}` +
        `${x.soe_progress.connections.map(p => `连接 ${p.connection_id} ${p.address}，已确认至 ${p.acknowledged}，待确认至 ${p.pending ?? '—'}`).join('；') || '无活动连接'}`
      ).join('\n') || '暂无 SOE 转发样本';

      if (data.checks) {
        setHTML('checks', data.checks.map(x =>
          `<div class="diag-issue ${x.severity === 'error' ? 'error' : ''}"><strong>${esc(x.name || '通道 ' + (x.channels || []).join('、'))}</strong> · ${esc(x.message)}</div>`
        ).join('') || '<p>未发现本次检查可识别的问题；对端可达性及规约通信仍需实际验证。</p>');
        $('checked-at').textContent = '检查于 ' + new Date(data.checked_at).toLocaleString();
      }
      $('message').dataset.error = 'false';
      $('message').textContent = '更新于 ' + new Date().toLocaleTimeString();
    } catch (e) {
      $('message').dataset.error = 'true';
      $('message').textContent = e.message;
    } finally {
      busy = false;
      $('refresh').disabled = false;
      if (pendingCheck) { pendingCheck = false; load(true); }
    }
  }
  $('refresh').onclick = () => load(true);
  setInterval(() => { if (!document.hidden && $('auto').checked) load(); }, 5000);
  load(true);
})();

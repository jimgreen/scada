async page => {
  const base = 'http://127.0.0.1:8881', passed = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.unroute('**/static/app.js');
  await page.goto(base + '/?monitor-regression=' + Date.now() + '#scdfe');
  await page.waitForFunction(() => monitor.rows.length === 50 && !monitor.loading);
  await page.locator('#auto-refresh').uncheck();
  const fixture = await page.evaluate(() => api('/api/scdfe_c/status'));
  const checks = await page.evaluate(async fixture => {
    const passed = [], check = (ok, name) => { if (!ok) throw Error(name); passed.push(name); };
    const savedFetch = window.fetch, savedConfirm = window.confirm;
    let payload = structuredClone(fixture), failRefresh = false, failPatch = false, lastPatch;
    const body = document.querySelector('#scdfe-rtus');
    window.confirm = () => true;
    window.fetch = (url, options = {}) => {
      if (String(url).startsWith('/api/scdfe_c/status?')) return Promise.resolve(new Response(JSON.stringify(failRefresh ? { error: { message: '测试刷新失败' } } : payload), { status: failRefresh ? 503 : 200 }));
      if (String(url).startsWith('/api/rtus/') && options.method === 'PATCH') {
        lastPatch = { url, headers: options.headers, body: JSON.parse(options.body) };
        if (failPatch) return Promise.resolve(new Response(JSON.stringify({ error: { message: '测试控制失败' } }), { status: 503 }));
        Object.assign(payload.rtus[0], lastPatch.body); payload.rtus[0]._version++;
        return Promise.resolve(new Response(JSON.stringify({ item: payload.rtus[0] })));
      }
      if (String(url) === '/api/rtus/' + payload.rtus[0].rtu_id) return Promise.resolve(new Response(JSON.stringify({ item: { ...payload.rtus[0], code: '最新记录' } })));
      return savedFetch(url, options);
    };
    try {
      await loadScdfe(); let nodes = [...body.rows];
      await loadScdfe(); check(nodes.every((row, i) => row === body.rows[i]), '通信监控稳定刷新保留全部 50 行');
      const buttons = nodes.map(row => row.querySelector('[data-thread-enable]'));
      const cells = nodes.map(row => [...row.cells]);
      payload.rtus.forEach(row => { row.process_heartbeat_time = Number(row.process_heartbeat_time || 1793600000000) + 1000; });
      await loadScdfe();
      check(nodes.every((row, i) => row === body.rows[i]), '50 行心跳同时变化仍保留行节点');
      check(buttons.every((button, i) => button === body.rows[i].querySelector('[data-thread-enable]')), '心跳变化保留全部启停按钮');
      check(cells.every((row, i) => row.every((cell, c) => c === 5 ? cell !== body.rows[i].cells[c] : cell === body.rows[i].cells[c])), '心跳批量刷新仅替换第 6 列');
      payload.rtus[0].name = '<img src=x onerror=alert(1)> &'; await loadScdfe();
      check(body.rows[0].textContent.includes(payload.rtus[0].name) && !body.querySelector('img'), '通信表差分更新保持 HTML 转义');
      const first = body.rows[0]; payload.rtus[0]._version += 17; await loadScdfe();
      check(first === body.rows[0], '只更新版本不重建通信行');
      const detail = body.querySelector('[data-thread-detail="0"]'); detail.innerHTML = '<span>详情</span>';
      await body.onclick({ target: detail.firstElementChild });
      check(document.querySelector('#detail-dialog').open && document.querySelector('#detail-fields').textContent.includes('最新记录'), '委托详情支持嵌套点击并请求最新记录');
      document.querySelector('#detail-dialog').close();
      let button = body.querySelector('[data-thread-enable="0"]');
      const version = payload.rtus[0]._version, enable = payload.rtus[0].process_enable;
      await body.onclick({ target: button });
      check(lastPatch.headers['If-Match'] === String(version) && lastPatch.body.process_enable === (enable === 1 ? 0 : 1), '启停委托使用最新版本与期望标志');
      check(!body.querySelector('[data-thread-enable="0"]').disabled, '启停完成后按钮可用');
      failPatch = true; button = body.querySelector('[data-thread-enable="0"]'); await body.onclick({ target: button });
      check(!button.disabled && document.querySelector('#toast').textContent.includes('测试控制失败'), '控制失败恢复按钮并显示错误');
      failPatch = false; failRefresh = true; button = body.querySelector('[data-thread-enable="0"]'); await body.onclick({ target: button });
      check(!button.disabled && !document.querySelector('#scdfe-error').hidden, '控制成功但刷新失败时按钮仍可重试');
      failRefresh = false; await loadScdfe(); check(document.querySelector('#scdfe-error').hidden, '下一次成功刷新清除通信错误');
      payload.rtus = []; payload.total = 0; await loadScdfe(); check(body.textContent.includes('没有匹配'), '通信监控空结果正常');
      payload = structuredClone(fixture); await loadScdfe(); check(body.rows.length === 50 && body.querySelectorAll('[data-thread-enable]').length === 50, '通信监控空结果恢复');
      // Process actions remain mocked; no runtime process is launched by these checks.
      state.resource = 'processes'; document.querySelector('#scdfe-panel').hidden = true; document.querySelector('#processes-panel').hidden = false;
      const processes = Array.from({ length: 60 }, (_, i) => ({ process_key: 'debug:' + (i + 1), log_process: 'scdfe_c-debug-rtu-' + (i + 1),
        kind: 'debug', process_enable: 0, alive: false, process_status: 0, include_in_all: 1, process_pid: i + 1, parent_pid: 0,
        process_start_time: 0, process_stop_time: 0, process_heartbeat_time: 1793609000000 + i * 1000, process_error: '', process_exit_code: 0 }));
      processMonitor.rows = structuredClone(processes); processMonitor.busy = false; processMonitor.offset = 0; renderProcesses();
      const root = document.querySelector('#process-rows'), processNodes = [...root.rows], pidCell = root.rows[0].cells[2];
      renderProcesses(); check(processNodes.every((row, i) => row === root.rows[i]), '进程列表稳定刷新保留可见 50 行');
      processMonitor.rows.forEach(row => { row.process_heartbeat_time += 1000; }); renderProcesses();
      check(processNodes.every((row, i) => row === root.rows[i]) && pidCell === root.rows[0].cells[2], '多行进程心跳变化保留其他单元格');
      processMonitor.busy = true; renderProcesses();
      check([...root.querySelectorAll('[data-process-action],input')].every(input => input.disabled) && document.querySelector('#process-all-start').disabled, '忙状态禁用进程启停与选择控件');
      processMonitor.busy = false; renderProcesses(); check(!root.querySelector('[data-process-action="start"]').disabled, '忙状态结束恢复允许的操作');
      document.querySelector('#process-search').value = 'debug:60'; renderProcesses(); check(root.rows.length === 1 && root.textContent.includes('RTU 60'), '进程搜索与计数正常');
      document.querySelector('#process-search').value = 'NO_MATCH'; renderProcesses(); check(root.textContent.includes('没有匹配'), '进程搜索空结果');
      document.querySelector('#process-search').value = ''; processMonitor.offset = 50; renderProcesses(); check(root.rows.length === 10, '进程翻页保留最后 10 行');
      let control;
      window.fetch = (url, options = {}) => {
        if (url === '/api/processes/control') { control = JSON.parse(options.body); return Promise.resolve(new Response('{}')); }
        if (url === '/api/processes') return Promise.resolve(new Response(JSON.stringify({ items: processes, busy: false, operation: null })));
        return savedFetch(url, options);
      };
      root.querySelector('[data-process-action="start"]').click();
      for (let i = 0; i < 12; i++) await Promise.resolve();
      check(control?.target === 'debug:51' && control?.action === 'start', '翻页后进程启动事件定位正确');
      await loadProcesses();
    } finally { window.fetch = savedFetch; window.confirm = savedConfirm; clearTimeout(processMonitor.timer); }
    return passed;
  }, fixture);
  passed.push(...checks);
  await page.goto(base + '/?log-tree-regression=' + Date.now() + '#logs');
  await page.waitForFunction(() => logs.files.length > 50 && !logs.loading && !logs.contentLoading);
  await page.locator('#auto-refresh').uncheck();
  const treeChecks = await page.evaluate(async () => {
    const passed = [], check = (ok, name) => { if (!ok) throw Error(name); passed.push(name); };
    const root = document.querySelector('#scdfe-logs'), original = root.firstElementChild;
    original.open = false; const button = root.querySelector('[data-log-name]');
    renderLogFiles(); check(root.firstElementChild === original && !original.open && button === root.querySelector('[data-log-name]'), '日志稳定刷新保留目录折叠状态和文件按钮');
    logs.files = structuredClone(logs.files); logs.filtered = structuredClone(logs.filtered); renderLogFiles();
    check(root.firstElementChild === original, '相同文件的新响应对象仍命中可见页缓存');
    const file = logs.filtered[0], oldSize = file.size; file.size += 10240; renderLogFiles();
    check(root.firstElementChild !== original && root.textContent.includes(fmtSize(file.size)), '同对象文件大小变化立即显示');
    file.size = oldSize; renderLogFiles();
    logs.offset = 50; renderLogFiles(); check(root.querySelector('[data-log-name]').dataset.logName === logs.filtered[50].name, '日志文件下一页正确');
    const selected = root.querySelectorAll('[data-log-name]')[1]; selected.innerHTML = '<span>选择日志</span>';
    root.onclick({ target: selected.firstElementChild });
    check(logs.selected === selected.dataset.logName && document.querySelector('#log-scope').value === 'file', '文件树嵌套点击委托选择正确文件');
    return passed;
  });
  passed.push(...treeChecks);
  await page.waitForFunction(() => !logs.contentLoading);
  if (errors.length) throw Error(errors.join('\n'));
  return { passed, count: passed.length, errors, native_confirm: 'test substitute; no actual process starts' };
}

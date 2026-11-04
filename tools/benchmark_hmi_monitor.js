async page => {
  const base = 'http://127.0.0.1:8881';
  const before = await (await page.request.get(base + '/static/app.before.js')).text();
  const after = await (await page.request.get(base + '/static/app.js')).text();
  const errors = [], results = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  for (let repeat = 1; repeat <= 7; repeat++) {
    for (const variant of repeat % 2 ? ['before', 'after'] : ['after', 'before']) {
      await page.unroute('**/static/app.js');
      await page.route('**/static/app.js', route => route.fulfill({
        contentType: 'application/javascript', body: variant === 'before' ? before : after,
      }));
      await page.goto(base + '/?monitor-benchmark=' + variant + repeat + '#processes');
      await page.waitForFunction(() => state.schemas.size && !processMonitor.loading && processMonitor.rows.length && document.querySelector('#database-path').textContent !== '读取中…');
      await page.locator('#auto-refresh').uncheck();
      const samples = await page.evaluate(async ({ repeat, variant }) => {
        const results = [], cycles = 10;
        const savedFetch = window.fetch;
        const show = kind => {
          for (const [panel, value] of [['data-panel', 'data'], ['processes-panel', 'processes'], ['scdfe-panel', 'scdfe'], ['logs-panel', 'logs']]) document.getElementById(panel).hidden = value !== kind;
          document.querySelector('#summary').hidden = true;
          state.resource = kind;
        };
        const digest = async html => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(html))), n => n.toString(16).padStart(2, '0')).join('');
        const measure = async (name, selector, render, update) => {
          const root = document.querySelector(selector);
          await render(); void root.offsetHeight;
          if (root.offsetHeight <= 0) throw Error('benchmark target hidden: ' + name);
          const nodes = [...root.children];
          let mutationCount = 0;
          const observer = new MutationObserver(records => { mutationCount += records.length; });
          observer.observe(root, { childList: true, subtree: true });
          let elapsed = 0;
          for (let i = 1; i <= cycles; i++) {
            update(i);
            const start = performance.now();
            await render(); void root.offsetHeight;
            elapsed += performance.now() - start;
          }
          mutationCount += observer.takeRecords().length; observer.disconnect();
          results.push({ case: name, variant, repeat, cycles, elapsed_ms: elapsed, ms_per_operation: elapsed / cycles,
            visible_nodes: root.children.length, retained_nodes: [...root.children].filter((n, i) => n === nodes[i]).length,
            mutation_records: mutationCount, dom_sha256: await digest(root.innerHTML) });
        };
        try {
          show('processes');
          const processBase = Array.from({ length: 500 }, (_, i) => ({
            process_key: 'debug:' + (i + 1), kind: 'debug', log_process: 'scdfe_c-debug-rtu-' + (i + 1),
            process_status: i % 6, process_enable: i % 2, process_pid: 20000 + i, parent_pid: 100,
            process_start_time: 1793600000000 + i * 1000, process_stop_time: 0, process_heartbeat_time: 1793609000000 + i * 1000,
            process_exit_code: 0, process_error: i % 17 === 0 ? '<错误> & RTU ' + i : '', alive: !!(i % 2), include_in_all: 1,
          }));
          for (const [mode, count] of [['stable', 0], ['sparse', 5], ['heartbeat', 50], ['dense', 50]]) {
            processMonitor.rows = structuredClone(processBase); processMonitor.offset = 0; processMonitor.busy = false;
            document.querySelector('#process-search').value = '';
            await measure('process_' + mode + '_50_of_500', '#process-rows', renderProcesses, cycle => {
              processMonitor.rows = processBase.map((row, i) => i >= count ? { ...row }
                : mode === 'heartbeat' ? { ...row, process_heartbeat_time: row.process_heartbeat_time + cycle * 1000 }
                : mode === 'dense' ? { ...row, process_status: (row.process_status + cycle) % 6, process_pid: row.process_pid + cycle,
                  log_process: 'scdfe_c-debug-rtu-' + (i + cycle + 1000), process_error: '错误 ' + cycle,
                  process_start_time: row.process_start_time + cycle * 1000, process_exit_code: cycle }
                : { ...row, process_status: (row.process_status + cycle) % 6, process_pid: row.process_pid + cycle });
            });
          }
          show('scdfe');
          const threadBase = Array.from({ length: 500 }, (_, i) => ({ rtu_id: i + 1, name: 'RTU <' + (i + 1) + '> &', channel_id: i + 1,
            channel_type: ['iec104', 'modbus-tcp', 'modbus-rtu', 'can', 'bacnet', 'opc'][i % 6], process_enable: i % 2,
            process_status: i % 6, thread_id: 2000 + i, process_heartbeat_time: 1793609000000 + i * 1000,
            process_error: i % 17 ? '' : '<错误> & RTU ' + i, _version: 1 }));
          const payload = { item: { process_status: 2, process_pid: 100, process_start_time: 1793600000000, process_stop_time: 0,
            process_heartbeat_time: 1793609000000, process_exit_code: 0, process_error: '', worker_start_delay_ms: 100, log_retention_hours: 24 },
            counts: { total: 500, running: 250, errors: 20, automatic: 250 }, total: 500, rtus: [] };
          let encoded;
          window.fetch = (url, options) => String(url).startsWith('/api/scdfe_c/status?')
            ? Promise.resolve(new Response(encoded, { headers: { 'Content-Type': 'application/json' } })) : savedFetch(url, options);
          for (const count of [50, 500]) {
            monitor.offset = 0; monitor.limit = count;
            for (const [mode, changed] of [['stable', 0], ['sparse', count === 50 ? 1 : 5], ['heartbeat', count], ['dense', count]]) {
              payload.rtus = structuredClone(threadBase.slice(0, count)); encoded = JSON.stringify(payload);
              await measure('thread_' + mode + '_' + count, '#scdfe-rtus', loadScdfe, cycle => {
                payload.rtus = threadBase.slice(0, count).map((row, i) => i >= changed ? { ...row }
                  : mode === 'heartbeat' ? { ...row, process_heartbeat_time: row.process_heartbeat_time + cycle * 1000 }
                  : mode === 'dense' ? { ...row, name: '站 ' + cycle, channel_id: row.channel_id + cycle,
                    process_status: (row.process_status + cycle) % 6, thread_id: row.thread_id + cycle,
                    process_heartbeat_time: row.process_heartbeat_time + cycle * 1000, process_enable: (row.process_enable + cycle) % 2, process_error: '轮次 ' + cycle }
                  : { ...row, process_status: (row.process_status + cycle) % 6, process_error: '轮次 ' + cycle });
                encoded = JSON.stringify(payload);
              });
            }
          }
          window.fetch = savedFetch;
          show('logs');
          const files = Array.from({ length: 10000 }, (_, i) => {
            const hour = String(Math.floor(i / 60) % 24).padStart(2, '0'), day = String(1 + Math.floor(i / 1440)).padStart(2, '0');
            return { name: '2026/11/' + day + '/' + hour + '/scdfe_c-rtu-' + (1 + i % 60) + '-202611' + day + '-' + hour + '.log', process: 'scdfe_c-rtu-' + (1 + i % 60), size: 1000 + i };
          });
          for (const mode of ['stable', 'page-change']) {
            logs.files = structuredClone(files); logs.filtered = logs.files; logs.offset = 0; logs.selected = files[0].name;
            await measure('log_tree_' + mode + '_50_of_10000', '#scdfe-logs', renderLogFiles, cycle => {
              logs.offset = mode === 'stable' ? 0 : cycle % 2 * 50;
              logs.files = structuredClone(files); logs.filtered = logs.files;
            });
          }
        } finally { window.fetch = savedFetch; }
        return results;
      }, { repeat, variant });
      results.push(...samples);
    }
  }
  await page.unroute('**/static/app.js');
  const hashes = await page.evaluate(async sources => {
    const out = {};
    for (const [key, value] of Object.entries(sources)) out[key] = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))), n => n.toString(16).padStart(2, '0')).join('');
    return out;
  }, { before, after });
  return { browser: await page.context().browser().version(), viewport: { width: 1440, height: 900 }, source_sha256: hashes,
    method: '7 alternating repeats; 10 operations per sample; visible panels; forced layout; thread includes Response JSON parsing with preencoded synthetic data; data preparation and network/server excluded', results, errors };
}

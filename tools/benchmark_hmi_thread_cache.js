async page => {
  const base = 'http://127.0.0.1:8881';
  const sources = {
    before: await (await page.request.get(base + '/static/app.before.js')).text(),
    after: await (await page.request.get(base + '/static/app.js')).text(),
  };
  const results = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  for (let repeat = 1; repeat <= 5; repeat++) {
    for (const variant of repeat % 2 ? ['before', 'after'] : ['after', 'before']) {
      await page.unroute('**/static/app.js');
      await page.route('**/static/app.js', route => route.fulfill({
        contentType: 'application/javascript', body: sources[variant],
      }));
      await page.goto(base + '/?thread-cache-benchmark=' + variant + repeat + '#scdfe');
      await page.waitForFunction(() => monitor.rows.length && !monitor.loading);
      await page.locator('#auto-refresh').uncheck();
      const samples = await page.evaluate(async ({ repeat, variant }) => {
        const payload = {
          item: { process_status: 2, process_pid: 100, process_start_time: 1793600000000,
            process_stop_time: 0, process_heartbeat_time: 1793609000000,
            process_exit_code: 0, process_error: '', worker_start_delay_ms: 100,
            log_retention_hours: 24 },
          counts: { total: 500, running: 250, errors: 20, automatic: 250 },
          total: 500, rtus: [],
        };
        const baseRows = Array.from({ length: 500 }, (_, i) => ({
          rtu_id: i + 1, name: 'RTU <' + (i + 1) + '> &', channel_id: i + 1,
          channel_type: ['iec104', 'modbus-tcp', 'modbus-rtu', 'can', 'bacnet', 'opc'][i % 6],
          process_enable: i % 2, process_status: i % 6, thread_id: 2000 + i,
          process_heartbeat_time: 1793609000000 + i * 1000,
          process_error: i % 17 ? '' : '<错误> & RTU ' + i, _version: 1,
        }));
        const actual = window.fetch, cases = [];
        window.fetch = (url, options) => String(url).startsWith('/api/scdfe_c/status?')
          ? Promise.resolve(new Response(JSON.stringify(payload), { headers: { 'Content-Type': 'application/json' } }))
          : actual(url, options);
        const measure = async (name, update) => {
          monitor.limit = 500; monitor.offset = 0; payload.rtus = structuredClone(baseRows);
          await loadScdfe();
          const body = document.querySelector('#scdfe-rtus');
          const original = [...body.rows];
          let elapsed = 0;
          for (let cycle = 1; cycle <= 8; cycle++) {
            update(cycle);
            const start = performance.now();
            await loadScdfe();
            void body.offsetHeight;
            elapsed += performance.now() - start;
          }
          cases.push({ case: name, repeat, variant, cycles: 8,
            ms_per_operation: elapsed / 8,
            retained_rows: original.filter((row, index) => row === body.rows[index]).length,
            dom_sha256: await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body.innerHTML))
              .then(bytes => Array.from(new Uint8Array(bytes), n => n.toString(16).padStart(2, '0')).join('')) });
        };
        try {
          await measure('thread_stable_500', cycle => {
            payload.rtus = baseRows.map(row => ({ ...row }));
          });
          await measure('thread_sparse_500', cycle => {
            payload.rtus = baseRows.map((row, index) => index < 5
              ? { ...row, process_error: '轮次 ' + cycle } : { ...row });
          });
          await measure('thread_heartbeat_500', cycle => {
            payload.rtus = baseRows.map(row => ({ ...row,
              process_heartbeat_time: row.process_heartbeat_time + cycle * 1000 }));
          });
        } finally {
          window.fetch = actual;
        }
        return cases;
      }, { repeat, variant });
      results.push(...samples);
    }
  }
  await page.unroute('**/static/app.js');
  const hashes = await page.evaluate(async sources => {
    const values = {};
    for (const [key, value] of Object.entries(sources)) {
      const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
      values[key] = Array.from(new Uint8Array(bytes), n => n.toString(16).padStart(2, '0')).join('');
    }
    return values;
  }, sources);
  return { browser: await page.context().browser().version(), viewport: { width: 1440, height: 900 },
    source_sha256: hashes, results, errors };
}

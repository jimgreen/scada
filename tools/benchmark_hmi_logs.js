async page => {
  const base = 'http://127.0.0.1:8881';
  const sources = {
    before: await (await page.request.get(base + '/static/app.before.js')).text(),
    after: await (await page.request.get(base + '/static/app.js')).text(),
  };
  const results = [], requests = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  for (let repeat = 1; repeat <= 7; repeat++) {
    for (const variant of repeat % 2 ? ['before', 'after'] : ['after', 'before']) {
      await page.unroute('**/static/app.js');
      await page.route('**/static/app.js', route => route.fulfill({ contentType: 'application/javascript', body: sources[variant] }));
      await page.goto(base + '/?log-benchmark=' + variant + repeat + '#logs');
      await page.waitForFunction(() => logs.files.length > 0 && !logs.loading && !logs.contentLoading);
      await page.locator('#auto-refresh').uncheck();
      const samples = await page.evaluate(async ({ repeat, variant }) => {
        const results = [], cycles = 6, content = document.querySelector('#scdfe-log-content');
        document.querySelector('#log-follow').checked = false;
        document.querySelector('#log-wrap').checked = true;
        const fixture = Array.from({ length: 5000 }, (_, i) => ({ filename: '2026/11/02/10/scdfe_c-rtu-1-20261102-10.log',
          text: '10:00:' + String(i % 60).padStart(2, '0') + ' [日志] 测点 ' + i + ' 采集完成 raw=' + (i + .25) + ' value=' + (i * .01) + ' <status> &',
          level: ['INFO','WARNING','ERROR','PROCESS'][i % 4], time: 1793584800000 + i * 1000 }));
        const digest = async text => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))), n => n.toString(16).padStart(2, '0')).join('');
        const measure = async (name, render, update) => {
          await render(); void content.offsetHeight;
          if (content.offsetHeight <= 0) throw Error('Hidden log benchmark');
          const nodes = [...content.children];
          let mutations = 0, elapsed = 0;
          const observer = new MutationObserver(records => { mutations += records.length; });
          observer.observe(content, { childList: true, subtree: true });
          for (let i = 1; i <= cycles; i++) {
            update(i);
            const start = performance.now(); await render(); void content.offsetHeight; elapsed += performance.now() - start;
          }
          mutations += observer.takeRecords().length; observer.disconnect();
          results.push({ case: name, repeat, variant, cycles, elapsed_ms: elapsed, ms_per_operation: elapsed / cycles,
            lines: content.children.length, retained_lines: [...content.children].filter((node, i) => node === nodes[i]).length,
            mutations, dom_sha256: await digest(content.innerHTML) });
        };
        for (const [name, count, changed, scope, keyword] of [
          ['render_stable_200',200,0,'file',''], ['render_stable_1000',1000,0,'file',''],
          ['render_stable_5000',5000,0,'file',''], ['render_sparse_5000',5000,5,'file',''],
          ['render_dense_5000',5000,5000,'file',''], ['render_highlight_allfiles_1000',1000,0,'all','采集'],
        ]) {
          document.querySelector('#log-scope').value = scope;
          logs.entries = structuredClone(fixture.slice(0,count)); logs.meta = { keyword, skipped_lines: 0 }; logs.nextCursor = null;
          await measure(name, renderLogContent, cycle => {
            logs.entries = fixture.slice(0,count).map((entry,i) => i < changed ? { ...entry, text: entry.text + ' cycle=' + cycle } : { ...entry });
          });
        }
        const actual = window.fetch;
        let searchCalls = 0;
        const payload = JSON.stringify({ items: fixture.slice().reverse(), next_cursor: null, scanned_files: 1, skipped_lines: 0, keyword: '' });
        window.fetch = (url, options) => String(url).startsWith('/api/scdfe_c/logs/search?')
          ? (searchCalls++, Promise.resolve(new Response(payload))) : actual(url, options);
        try {
          document.querySelector('#log-scope').value = 'file'; document.querySelector('#log-process').value = '';
          document.querySelector('#log-tail').value = '5000'; document.querySelector('#log-search').value = '';
          document.querySelector('#log-level').value = ''; document.querySelector('#log-start').value = ''; document.querySelector('#log-end').value = '';
          logs.selected = fixture[0].filename;
          await measure('read_stable_5000', () => readLog(logs.selected), () => {});
        } finally { window.fetch = actual; }
        return { results, read_search_requests: searchCalls };
      }, { repeat, variant });
      results.push(...samples.results);
      const counts = await page.evaluate(async () => {
        const actual = window.fetch, calls = [];
        window.fetch = (url, options) => { calls.push(String(url)); return actual(url, options); };
        try {
          openProcessLogs('rtu:1');
          while (logs.loading || logs.contentLoading) await new Promise(resolve => setTimeout(resolve, 5));
          const openCalls = calls.splice(0);
          const search = document.querySelector('#log-search'); search.value = '采集'; search.oninput(); search.onkeydown({ key: 'Enter' });
          await new Promise(resolve => setTimeout(resolve, 420));
          while (logs.contentLoading) await new Promise(resolve => setTimeout(resolve,5));
          return { open_rtu_calls: openCalls, enter_calls: calls.slice() };
        } finally { window.fetch = actual; }
      });
      requests.push({ repeat, variant, read_search_requests_including_warmup: samples.read_search_requests, ...counts });
    }
  }
  await page.unroute('**/static/app.js');
  const hashes = await page.evaluate(async sources => {
    const hashes = {};
    for (const [key, source] of Object.entries(sources)) hashes[key] = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(source))), n => n.toString(16).padStart(2,'0')).join('');
    return hashes;
  }, sources);
  return { source_sha256: hashes, browser: await page.context().browser().version(), viewport: { width:1440, height:900 },
    method: '7 alternating repeats; 6 operations per sample; visible panel; forced layout; read includes simulated Response JSON parsing, excludes network/server; direct rendering excludes fixture preparation; profiler off', results, requests, errors };
}

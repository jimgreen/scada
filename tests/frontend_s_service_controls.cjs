/* Browser regression: all HTTP requests are intercepted; no live service writes. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const report = path.join(root, 'output/s_service_stop_20261114');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function fixture(browser, kind) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const requests = [], errors = [], running = new Map([[8, false], [9, false]]);
  let delay = 0, fail = false;
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.__monitorPolls = [];
    window.setInterval = callback => { window.__monitorPolls.push(callback); return window.__monitorPolls.length; };
  });
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    assert.equal(url.hostname, 's-controls.test');
    if (url.pathname.startsWith('/api/')) {
      if (request.method() !== 'GET') {
        requests.push(url.pathname);
        const match = /iec104-rtu-(\d+)\/(start|stop)$/.exec(url.pathname);
        if (!match) throw Error('unexpected mutation: ' + url.pathname);
        if (delay) await sleep(delay);
        if (fail) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({error:{message:'测试控制失败'}}) });
        running.set(Number(match[1]), match[2] === 'start');
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify({item:{running:match[2] === 'start'}}) });
      }
      let data = {};
      if (url.pathname === '/api/health') data = {status:'ok', database:'isolated fixture'};
      if (url.pathname === '/api/rtus') data = {items:[8,9].map(id=>({rtu_id:id, channel_id:id-1, channel_type:'iec104', name:'测试站'+id, process_enable:1})), total:2};
      if (/\/api\/monitor\/(communications|processes)$/.test(url.pathname)) data = {items:[8,9].map(id=>({name:'iec104-rtu-'+id, rtu_id:id, running:running.get(id), status:{started_at:1}, point_count:2}))};
      return route.fulfill({ contentType:'application/json', body:JSON.stringify(data) });
    }
    if (url.pathname.startsWith('/static/')) {
      const name = path.basename(url.pathname), file = path.join(root, 'scdfe_s/web/static', name);
      return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':'text/css', body:fs.readFileSync(file)});
    }
    return route.fulfill({contentType:'text/html', body:fs.readFileSync(path.join(root, 'scdfe_s/web/static', kind+'.html'))});
  });
  await page.goto('http://s-controls.test/' + kind);
  await page.locator('[data-service="iec104-rtu-8"][data-action="start"]').waitFor();
  return { page, requests, running, errors, context, setDelay: value=>delay=value, setFail:value=>fail=value };
}

(async () => {
  fs.mkdirSync(report, {recursive:true});
  const browser = await chromium.launch({headless:true}), results = [];
  try {
    for (const kind of ['processes','communication']) {
      const f = await fixture(browser, kind);
      try {
        const start = f.page.locator('[data-service="iec104-rtu-8"][data-action="start"]');
        const box = await start.boundingBox();
        // Two clicks at the original START position. With a toggle button,
        // the second click reaches STOP after the first response refreshes it.
        await f.page.mouse.click(box.x + box.width/2, box.y + box.height/2);
        await f.page.waitForFunction(() => !!document.querySelector('[data-service="iec104-rtu-8"][data-action="stop"]:not(:disabled)'));
        await f.page.mouse.click(box.x + box.width/2, box.y + box.height/2);
        await sleep(200);
        const observed = {page:kind, startPositionClicks:[...f.requests], stillRunning:f.running.get(8)};
        results.push(observed);
        if (process.argv.includes('--observe-before')) continue;
        assert.deepEqual(f.requests, ['/api/monitor/communications/iec104-rtu-8/start']);
        assert(f.running.get(8), 'repeating START must not send STOP');
        assert(await start.isDisabled());
        // A genuine STOP remains available at its separate fixed position.
        await f.page.locator('[data-service="iec104-rtu-8"][data-action="stop"]').click();
        await f.page.waitForFunction(() => !!document.querySelector('[data-service="iec104-rtu-8"][data-action="start"]:not(:disabled)'));
        assert.equal(f.requests.at(-1), '/api/monitor/communications/iec104-rtu-8/stop');
        // While a start is pending, a monitor refresh must not enable a
        // replacement button and admit a duplicate start/stop.
        f.setDelay(700);
        await start.click();
        await f.page.locator('#refresh').click();
        await sleep(100);
        assert(await start.isDisabled());
        assert(await f.page.locator('[data-service="iec104-rtu-8"][data-action="stop"]').isDisabled());
        // Different RTUs can still be controlled independently.
        assert(await f.page.locator('[data-service="iec104-rtu-9"][data-action="start"]').isEnabled());
        await f.page.waitForFunction(() => !!document.querySelector('[data-service="iec104-rtu-8"][data-action="stop"]:not(:disabled)'));
        f.setDelay(0); f.setFail(true);
        await f.page.locator('[data-service="iec104-rtu-8"][data-action="stop"]').click();
        await f.page.waitForFunction(() => document.querySelector('#message').textContent.includes('测试控制失败'));
        assert(await f.page.locator('[data-service="iec104-rtu-8"][data-action="stop"]').isEnabled());
        f.setFail(false);
        const mutations = f.requests.length;
        await f.page.evaluate(() => window.__monitorPolls.forEach(callback=>callback()));
        await sleep(150);
        assert.equal(f.requests.length, mutations, 'refresh must be read-only');
        assert.deepEqual(f.errors, []);
        await f.page.screenshot({path:path.join(report,kind+'-fixed-controls.png'),fullPage:true});
        observed.pendingGuard=true; observed.independentRtu=true; observed.readOnlyRefresh=true;
      } finally { await f.context.close(); }
    }
    const name = process.argv.includes('--observe-before') ? 'frontend-before.json' : 'frontend-after.json';
    fs.writeFileSync(path.join(report,name),JSON.stringify(results,null,2));
    console.log(JSON.stringify(results,null,2));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});

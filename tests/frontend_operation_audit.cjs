/* Intercept every request: tests never write to a running SCADA installation. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    const sent = [], errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body:
        '<script src="/audit.js"></script><button id="save">保存</button><input value="private-value"><script>' +
        'document.querySelector("button").onclick=()=>fetch("/api/change",{method:"PATCH",headers:{"Content-Type":"application/json"},body:"{}"});</script>'});
      if (url.pathname === '/audit.js') return route.fulfill({contentType: 'application/javascript',
        body: fs.readFileSync(path.join(root, 'core/web_static/audit-client.js'))});
      sent.push({url: request.url(), method: request.method(), headers: request.headers(), body: request.postData()});
      return route.fulfill({contentType: 'application/json', body: '{}', headers: {'Access-Control-Allow-Origin': '*'}});
    });
    await page.goto('http://127.0.0.1:18001/');
    await page.click('#save');
    await page.waitForFunction(() => true);
    await page.evaluate(async () => {
      await fetch('/api/health');
      await fetch(new Request('/api/request-object', {method: 'POST', body: 'test', headers: {'X-Original': 'yes'}}));
      await fetch('http://localhost:18002/outside', {method: 'POST', body: 'outside'});
      await fetch('/api/second-write', {method: 'DELETE'});
    });
    const writes = sent.filter(r => !['GET', 'OPTIONS'].includes(r.method) && new URL(r.url).host === '127.0.0.1:18001');
    assert.equal(writes.length, 3);
    assert.equal(new Set(writes.map(r => r.headers['x-scada-tab'])).size, 1);
    assert.equal(new Set(writes.map(r => r.headers['x-scada-request'])).size, 3);
    assert.equal(decodeURIComponent(writes[0].headers['x-scada-action']), 'save');
    assert.equal(writes[1].headers['x-original'], 'yes');
    assert.equal(writes[1].body, 'test');
    for (const row of sent.filter(r => r.method === 'GET' || new URL(r.url).hostname === 'localhost')) {
      assert.equal(row.headers['x-scada-tab'], undefined);
      assert.equal(row.headers['x-scada-action'], undefined);
    }
    assert(!JSON.stringify(sent).includes('private-value'));
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({status:'passed', assertions:11, requests:sent.length}));
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});

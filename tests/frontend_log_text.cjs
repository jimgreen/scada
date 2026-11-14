/* All requests are intercepted. Display/export must preserve stored log text. */
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const python = process.env.SCADA_PYTHON || 'D:/anaconda3/python.exe';
const output = path.join(root, 'output/playwright/log-text');
fs.mkdirSync(output, {recursive: true});
const lines = [
  '22:00:00 [日志] 日志上下文 @example.1 进程编号=123 RTU编号=9 通道编号=8',
  '22:00:01 [日志] @example.1 业务事件 实时变化 点号=1 值=1.2.*[x] 质量=0',
  '22:00:02 [日志] @example.1 业务事件 详细原因 pid=17  RTU=9\t\t<unsafe>&"  ',
  '22:00:03 [日志] @example.1 业务事件 指令失败 原因=first\\r\\nsecond',
];
fs.writeFileSync(path.join(output,'stored.log'),lines.join('\n')+'\n');

(async () => {
  const browser = await chromium.launch({headless: true});
  const results = [];
  try {
    for (const family of ['s','t']) {
      const page = await browser.newPage({viewport: {width:1440,height:960}});
      const errors = [];
      page.on('pageerror',error => errors.push(error.message));
      await page.addInitScript(() => {
        const Native = RegExp;
        window.regexConstructions = 0;
        window.RegExp = new Proxy(Native, {construct(target,args) {
          window.regexConstructions++;
          return Reflect.construct(target,args);
        }});
      });
      const process = `scdfe_${family}-channel-8`, filename = `2026/11/14/22/${process}-20261114-22.log`;
      const template = fs.readFileSync(path.join(root,'scdfe_s/web/static/logs.html'),'utf8')
        .replace('data-log-module="s"',`data-log-module="${family}"`);
      const html = cp.execFileSync(python,['-c',
        'import sys; from core.web_workbench import render_workbench; sys.stdout.buffer.write(render_workbench(sys.stdin.buffer.read(),sys.argv[1],"logs.html"))',family],
        {cwd:root,input:template,encoding:'utf8'});
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/') return route.fulfill({contentType:'text/html',body:html});
        if (url.pathname.startsWith('/static/')) return route.fulfill({
          contentType: url.pathname.endsWith('.js') ? 'application/javascript' : 'text/css',
          body:fs.readFileSync(path.join(root,'core/web_static',path.basename(url.pathname)))});
        let data;
        if (url.pathname === '/api/health') data = {status:'ok',database:'isolated/test.sqlite3'};
        else if (url.pathname === '/api/log-files') data = {directory:'isolated/logs',items:[{
          name:filename,process,hour:'20261114-22',size:Buffer.byteLength(lines.join('\n')),channel_id:8,modified_at:'2026-11-14T22:00:00Z'}]};
        else if (url.pathname === '/api/rtus') data = {items:[{rtu_id:9,channel_id:8,name:'日志验证通道'}]};
        else if (url.pathname === '/api/log-files/search') {
          const keyword = url.searchParams.get('q') || '';
          data = {items:lines.filter(line=>line.toLowerCase().includes(keyword.toLowerCase())).slice().reverse()
            .map(text=>({filename,text,level:'INFO'})),keyword,next_cursor:null,scanned_files:1,scanned_bytes:500};
        } else throw new Error('Unexpected request: '+url.pathname);
        return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
      });
      await page.goto('http://127.0.0.1:18001/');
      await page.waitForFunction(() => document.querySelectorAll('.lb-line-text').length === 4);
      const readerBox = await page.locator('.lb-reader').boundingBox();
      assert(readerBox.width>500 && readerBox.height>300,'production workbench layout is required');
      assert.deepEqual(await page.locator('.lb-line-text').allTextContents(),lines);
      assert.equal(await page.locator('.lb-line-text unsafe').count(),0);
      await page.locator('#lb-toggle-filters').click();
      await page.locator('#lb-keyword').fill('值=1.2.*[x]');
      await page.locator('#lb-apply').click();
      await page.waitForFunction(() => document.querySelectorAll('.lb-line-text').length === 1);
      assert.equal(await page.locator('.lb-line-text').textContent(),lines[1]);
      assert.equal(await page.locator('mark').textContent(),'值=1.2.*[x]');
      await page.locator('#lb-clear').click();
      await page.waitForFunction(() => document.querySelectorAll('.lb-line-text').length === 4);
      assert.deepEqual(await page.locator('.lb-line-text').allTextContents(),lines);
      const pendingDownload = page.waitForEvent('download');
      await page.locator('#lb-export').click();
      const download = await pendingDownload;
      const target = path.join(output,`${family}-export.log`);
      await download.saveAs(target);
      // The existing export adds a UTF-8 BOM for Windows editors; its body
      // must still be exactly the displayed text, including tabs/spaces.
      assert.equal(fs.readFileSync(target,'utf8'),'\ufeff'+lines.join('\r\n')+'\r\n');
      assert.deepEqual(errors,[]);
      if (family === 's') await page.screenshot({path:path.join(output,'log-page.png'),fullPage:true});
      results.push({family,rows:lines.length,display_and_export_equal:true,html_escaped:true,
                    literal_keyword_highlight:true,regex_constructions:await page.evaluate(()=>window.regexConstructions)});
      await page.close();
    }
  } finally { await browser.close(); }
  fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify({results},null,2));
  console.log(JSON.stringify({results}));
})().catch(error=>{console.error(error);process.exitCode=1;});

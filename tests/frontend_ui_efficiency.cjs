/* Browser regression/benchmark. No server or database is accessed.
 * PLAYWRIGHT_MODULE may point at an existing playwright package directory.
 * node tests/frontend_ui_efficiency.cjs [--compare] [--output path.json]
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {createHash} = require('node:crypto');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const baseline = process.env.FRONTEND_BASELINE || 'b35268b4ad3a082319cd80f07f40de00babeeab3';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const checks = [], measurements = [];
function check(condition, name) { assert.ok(condition, name); checks.push(name); }
async function until(test) {
  for (let i = 0; i < 600; i++) { if (await test()) return; await sleep(10); }
  throw Error('Timed out waiting for fixture request');
}
function source(file, variant) {
  return variant === 'before' ? execFileSync('git', ['show', `${baseline}:${file}`], {cwd:root, encoding:'utf8'}) : fs.readFileSync(path.join(root, file), 'utf8');
}
async function fixture(browser, module, pageName, variant, logOnly = false) {
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage(), calls = [], errors = [], held = [];
  const mock = {delay:0, hold:null, fail:null, active:0};
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.__intervals = [];
    window.setInterval = (fn, ms) => (__intervals.push({fn,ms}), __intervals.length);
    window.__tick = ms => __intervals.filter(x => x.ms === ms).forEach(x => x.fn());
  });
  const file = (hour,channel=1) => `2026/11/12/${hour}/scdfe_${module}-channel-${channel}-20261112-${hour}.log`;
  function payload(url) {
    const p = url.pathname, q = url.searchParams;
    if (p === '/api/health') return {status:'ok',database:'mock-only'};
    if (p === '/api/schemas') return {items:[]};
    if (p === '/api/overview') return {counts:{},database:'mock-only'};
    if (p === '/api/log-files' || p === '/api/scdfe_c/logs') return {directory:`fixture/${q.get('record')}`,items:[1,2].flatMap(channel=>['11','10'].map(hour => ({name:file(hour,channel),filename:file(hour,channel),process:`scdfe_${module}-channel-${channel}`,channel_id:channel,hour:`20261112-${hour}`,size:80,mtime:1794477600000}))),total:4};
    if (p === '/api/log-files/search' || p === '/api/scdfe_c/logs/search') return {items:[{filename:q.get('filename')||file('11'),text:(q.get('cursor') ? 'older fixture line' : `latest ${q.get('q') || ''} fixture line`)+` ${q.get('record')}`,level:'INFO'}],next_cursor:q.get('cursor') ? null : 'older',keyword:q.get('q') || '',scanned_files:1};
    if (p === '/api/rtus') return {items:logOnly ? [{rtu_id:1,channel_id:1,name:'模拟站一'},{rtu_id:2,channel_id:2,name:'模拟站二'}] : [],total:logOnly ? 2 : 0};
    if (p === '/api/trans/channels' || p === '/api/trans/status') return {items:[{channel_id:1,protocol:'iec104',name:'模拟转发通道'}],total:1};
    if (p === '/api/trans/source-catalog') return {kind:q.get('kind'),rtus:[{rtu_id:1,name:'源模拟站',channel_type:'iec104',point_count:1}],items:[{pnt_no:1,name:'源测点',value:1}]};
    if (/\/api\/trans\/channels\/\d+\/points$/.test(p)) return {items:[{channel_id:Number(p.split('/')[4]),kind:q.get('kind'),pnt_no:1,name:`limit-${q.get('limit')}`,value:1}],total:200};
    return {items:[],total:0};
  }
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    assert.equal(url.hostname, 'frontend.test', 'all traffic stays inside the mock origin');
    if (url.pathname.startsWith('/api/')) {
      assert.equal(route.request().method(), 'GET', 'benchmark is read-only');
      const call = {path:url.pathname+url.search}; calls.push(call); mock.active++;
      const body = payload(url);
      try {
        if (mock.hold?.(url)) await new Promise(resolve => held.push({url,resolve,body,call}));
        if (mock.delay) await sleep(mock.delay);
        const fail = mock.fail?.(url);
        await route.fulfill({status:fail ? 503 : 200,contentType:'application/json',body:JSON.stringify(fail ? {error:{message:fail}} : body)});
      } finally { mock.active--; }
      return;
    }
    if (url.pathname.startsWith('/static/')) {
      const name = path.basename(url.pathname);
      const file = fs.existsSync(path.join(root, `scdfe_${module}/web/static/${name}`)) ? `scdfe_${module}/web/static/${name}` : `core/web_static/${name}`;
      if (!fs.existsSync(path.join(root,file))) return route.fulfill({status:404,body:''});
      return route.fulfill({contentType:name.endsWith('.js') ? 'application/javascript' : 'text/css',body:source(file,variant)});
    }
    let html = fs.readFileSync(path.join(root, `scdfe_${module}/web/static/${pageName}.html`),'utf8');
    // The Python page wrapper supplies these header nodes at serving time.
    // Reproduce only that chrome; all tested controls come from the real HTML.
    if(module!=='c')html=html.replace('</header>','<span id="connection"></span><span id="workbench-database"></span></header>');
    html = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, tag => {
      const keep = logOnly ? /\/log-browser\.js/.test(tag) : /\/app\.js/.test(tag);
      return keep ? tag : '';
    });
    await route.fulfill({contentType:'text/html',body:html});
  });
  await page.goto('http://frontend.test/');
  const settle = async () => {
    let previous=-1, quietSince=Date.now();
    for(let i=0;i<300;i++){
      await sleep(20);
      // route.fulfill resolves before the browser consumes JSON and queues
      // chained directory/content requests. Wait for a quiet request window.
      if(mock.active || calls.length!==previous)quietSince=Date.now();
      previous=calls.length;
      if(!mock.active && Date.now()-quietSince>=350)return;
    }
    throw Error('Fixture requests did not settle');
  };
  await settle();
  return {page,calls,errors,mock,held,settle,close:async()=>{await context.close();check(errors.length===0,`${module}/${pageName}/${variant}: no browser errors`);}};
}
async function cRefresh(browser, variant) {
  const f=await fixture(browser,'c','index',variant), {page,mock,calls}=f;
  mock.delay=30;
  const start=calls.length;
  await page.evaluate(async()=>{const b=document.querySelector('#refresh');await Promise.all([b.onclick(),b.onclick(),b.onclick()]);});
  await f.settle();
  const burst=calls.length-start;
  measurements.push({scenario:'C: three overlapping manual refreshes',variant,requests:burst});
  if(variant==='after')check(burst===3,'C: one rows/summary/RTU batch for three clicks');
  check(await page.locator('#refresh').isEnabled(),'C: refresh is re-enabled');
  mock.fail=url=>url.pathname==='/api/overview'?'fixture summary failure':null;
  await page.evaluate(()=>document.querySelector('#refresh').onclick());
  check(await page.locator('#refresh').isEnabled(),'C: failure releases refresh guard');
  mock.fail=null;
  const retry=calls.length;
  await page.evaluate(()=>document.querySelector('#refresh').onclick());
  check(calls.length-retry===3,'C: explicit retry starts a fresh batch');
  // C has its own reader: test the equivalent latest-query guard separately.
  await page.evaluate(()=>{document.querySelector('#log-scope').value='all';});
  for(const failOld of [false,true]){
    f.held.length=0;mock.hold=url=>url.pathname==='/api/scdfe_c/logs/search';
    mock.fail=url=>failOld&&url.searchParams.get('q')==='old'?'stale C error':null;
    await page.evaluate(()=>{document.querySelector('#log-search').value='old';window.__old=readLog('fixture.log',true);});
    await until(()=>f.held.length===1);
    await page.evaluate(()=>{document.querySelector('#log-search').value='new';window.__new=readLog('fixture.log',true);});
    await until(()=>f.held.length===2);
    f.held[1].resolve();await page.evaluate(()=>window.__new);
    f.held[0].resolve();await page.evaluate(()=>window.__old);
    check((await page.locator('#scdfe-log-content').textContent()).includes('latest new'),`C: stale ${failOld?'failure':'success'} cannot overwrite new query`);
  }
  mock.hold=null;mock.fail=null;
  await f.close();
}
async function logs(browser,module,pageName,variant) {
  const f=await fixture(browser,module,pageName,variant,true),{page,mock,calls}=f;
  const refresh=()=>page.evaluate(()=>document.querySelector('#lb-refresh').click());
  const hidden=pageName==='test';
  if(hidden){
    for(let i=0;i<3;i++){await page.evaluate(()=>__tick(5000));await f.settle();}
    measurements.push({scenario:'S: hidden log panel startup plus three polls',variant,requests:calls.length});
    if(variant==='after')check(calls.length===0,'S: hidden log panel makes zero requests');
    await page.evaluate(()=>document.querySelector('[data-tab-panel="logs"]').hidden=false);
    if(variant==='before')await page.evaluate(()=>__tick(5000));
    await f.settle();
    if(variant==='after')await page.waitForFunction(()=>document.querySelector('#lb-content').textContent.includes('fixture line'));
    if(variant==='after')check((await page.locator('#lb-content').textContent()).includes('fixture line'),'S: revealing panel loads log content with an input channel filter');
    else check((await page.locator('#lb-status').textContent()).includes('options'),'S baseline: embedded input channel filter exposes existing select.options error');
    await f.close();return;
  }
  // Identical RTU notifications must not reset file or cursor state.
  const notify=()=>page.evaluate(()=>document.dispatchEvent(new CustomEvent('scada:log-process',{detail:{process:'scdfe_s-channel-1',channel_id:1}})));
  if(module==='s'){
    await notify();await f.settle();
    const start=calls.length;
    for(let i=0;i<3;i++){await notify();await f.settle();}
    measurements.push({scenario:'S: three unchanged RTU status events',variant,requests:calls.length-start});
    if(variant==='after')check(calls.length===start,'S: repeated RTU notification does not re-search');
    await page.locator('#lb-older').click();await f.settle();
    await notify();await f.settle();
    if(variant==='after')check((await page.locator('#lb-content').textContent()).includes('older fixture line'),'S: repeated RTU event preserves older cursor');
  }
  const start=calls.length;mock.delay=30;
  await page.evaluate(()=>{const b=document.querySelector('#lb-refresh');for(let i=0;i<3;i++)b.click();});
  await f.settle();
  measurements.push({scenario:`${module.toUpperCase()}: three overlapping log refreshes`,variant,requests:calls.length-start});
  if(variant==='after')check(calls.length-start===(module==='s'?3:2),`${module}: manual log refresh has one directory/content batch`);
  mock.delay=0;
  const applyStart=calls.length;
  await page.evaluate(()=>{const q=document.querySelector('#lb-keyword');q.value='needle';q.dispatchEvent(new Event('input'));document.querySelector('#lb-apply').click();});
  await sleep(320);await f.settle();
  measurements.push({scenario:`${module.toUpperCase()}: apply while search debounce pending`,variant,requests:calls.length-applyStart});
  if(variant==='after')check(calls.length-applyStart===1,`${module}: explicit search consumes debounce`);
  // Delayed old success and old failure both lose to the newest query.
  for(const failOld of [false,true]){
    f.held.length=0;mock.hold=url=>url.pathname==='/api/log-files/search';
    mock.fail=url=>failOld&&url.searchParams.get('q')==='old'?'stale error':null;
    await page.evaluate(()=>{document.querySelector('#lb-keyword').value='old';document.querySelector('#lb-apply').click();});
    await until(()=>f.held.length===1);
    await page.evaluate(()=>{document.querySelector('#lb-keyword').value='new';document.querySelector('#lb-apply').click();});
    await until(()=>f.held.length===2);
    f.held[1].resolve();await until(async()=> (await page.locator('#lb-content').textContent()).includes('latest new'));
    f.held[0].resolve();await f.settle();
    check((await page.locator('#lb-content').textContent()).includes('latest new'),`${module}: stale ${failOld?'failure':'success'} cannot overwrite new log query`);
  }
  mock.hold=null;mock.fail=null;
  if(variant==='after'){
    // A global (unfiltered) directory response must use the latest process.
    f.held.length=0;mock.hold=url=>url.pathname==='/api/log-files';
    await refresh();await until(()=>f.held.length===1);
    await page.evaluate(m=>{const s=document.querySelector('#lb-process');s.value=`scdfe_${m}-channel-2`;s.dispatchEvent(new Event('change'));},module);
    f.held[0].resolve();await f.settle();mock.hold=null;
    check((await page.locator('#lb-name').textContent()).includes('channel-2'),`${module}: in-flight directory keeps latest process selection`);
    if(module==='s'){
      f.held.length=0;mock.hold=url=>url.pathname==='/api/log-files';
      await refresh();await until(()=>f.held.length===1);
      await page.evaluate(()=>document.dispatchEvent(new CustomEvent('scada:log-process',{detail:{process:'scdfe_s-channel-1',channel_id:1}})));
      // This equals the last auto event but the user's manual process wins.
      f.held[0].resolve();await f.settle();mock.hold=null;
      check((await page.locator('#lb-name').textContent()).includes('channel-2'),'S: repeated RTU event preserves manual process during directory refresh');
      f.held.length=0;mock.hold=url=>url.pathname==='/api/log-files';
      await refresh();await until(()=>f.held.length===1);
      await page.evaluate(()=>document.dispatchEvent(new CustomEvent('scada:log-process',{detail:{process:'scdfe_s-channel-2',channel_id:2}})));
      f.held[0].resolve();await f.settle();mock.hold=null;
      check((await page.locator('#lb-process').inputValue())==='scdfe_s-channel-2','S: changed RTU remains selected after old directory completes');
      f.held.length=0;mock.hold=url=>url.pathname==='/api/log-files';
      await refresh();await until(()=>f.held.length===1);
      await page.evaluate(()=>document.dispatchEvent(new CustomEvent('scada:log-process',{detail:{process:'scdfe_s-channel-1',channel_id:1}})));
      f.held[0].resolve();await f.settle();mock.hold=null;
      check((await page.locator('#lb-name').textContent()).includes('channel-1'),'S: switching RTU during old directory request renders the new channel file');
    }
    f.held.length=0;mock.hold=url=>url.pathname==='/api/log-files';
    await refresh();await until(()=>f.held.length===1);
    await page.locator('[data-record="packet"]').click();await until(()=>f.held.length===2);
    check(f.held[1].url.searchParams.get('record')==='packet',`${module}: record change starts a new directory read without waiting`);
    f.held[1].resolve();await until(async()=>(await page.locator('#lb-directory').textContent()).includes('fixture/packet'));
    f.held[0].resolve();await f.settle();mock.hold=null;
    check((await page.locator('#lb-directory').textContent()).includes('fixture/packet'),`${module}: late old directory cannot replace new record`);
    check((await page.locator('#lb-content').textContent()).includes('packet'),`${module}: record switch does not lose the new content read`);
    await page.evaluate(()=>document.querySelector('.log-browser').hidden=true);
    const n=calls.length;
    await page.evaluate(()=>{__tick(5000);document.querySelector('#lb-keyword').dispatchEvent(new Event('input'));});
    await sleep(300);await f.settle();
    check(calls.length===n,`${module}: hidden reader suppresses polling and debounced search`);
    await page.evaluate(()=>document.querySelector('.log-browser').hidden=false);await f.settle();
    check(calls.length>n,`${module}: reader reveal resumes without a timer tick`);
    mock.fail=url=>url.pathname==='/api/log-files'?'directory failure':null;
    await refresh();await f.settle();
    check((await page.locator('#lb-status').textContent()).includes('directory failure'),`${module}: directory failure is visible`);
    mock.fail=null;await refresh();await f.settle();
    check(!(await page.locator('#lb-status').textContent()).includes('failure'),`${module}: directory failure is retryable`);
  }
  await f.close();
}
async function tPoints(browser,variant){
  const f=await fixture(browser,'t','points',variant),{page,calls,mock}=f;
  await page.evaluate(()=>document.querySelector('#point-live-refresh').checked=true);
  mock.hold=url=>url.pathname.endsWith('/points');
  const start=calls.length;
  await page.evaluate(()=>{window.__read=loadPoints();__tick(2000);__tick(2000);__tick(2000);});
  await until(()=>f.held.length===1);
  mock.hold=null;f.held[0].resolve();await page.evaluate(()=>window.__read);await f.settle();
  measurements.push({scenario:'T: slow point read with three background ticks',variant,requests:calls.length-start});
  if(variant==='after')check(calls.length-start===1,'T: background ticks do not queue duplicate point reads');
  // Page-size changes must invalidate success as well as failure responses.
  for(const failOld of [false,true]){
    f.held.length=0;mock.hold=url=>url.pathname.endsWith('/points');
    mock.fail=url=>failOld&&url.searchParams.get('limit')==='100'?'stale page-size error':null;
    await page.evaluate(()=>{state.pointLimit=100;window.__renders=[];window.__renderOriginal=renderPoints;renderPoints=function(){__renders.push(state.points.map(p=>p.name));return __renderOriginal();};window.__read=loadPoints().catch(e=>toast(e.message));});
    await until(()=>f.held.length===1);
    await page.evaluate(()=>{state.pointLimit=50;loadPoints();});
    f.held[0].resolve();await page.evaluate(()=>window.__read);await until(()=>f.held.length===2);
    if(variant==='after'){
      check(!(await page.evaluate(()=>__renders.flat())).includes('limit-100'),'T: outdated page-size success never renders');
      check(!(await page.locator('#toast').textContent()).includes('stale page-size'),'T: outdated page-size failure is ignored');
    }
    mock.hold=null;f.held[1].resolve();await f.settle();
    check((await page.locator('#points').textContent()).includes('limit-50'),'T: explicit change keeps trailing read for latest page size');
    await page.evaluate(()=>renderPoints=__renderOriginal);
  }
  mock.fail=null;
  await f.close();
}
async function tProtocolEditor(browser){
  const f=await fixture(browser,'t','points','after'),{page}=f;
  await page.evaluate(()=>openEditor('channel'));
  for(const name of ['k','w','t1','t2','t3','max_asdu_length']){
    check(await page.locator(`#editor-fields [name="${name}"]`).isVisible(),`T: IEC104 editor exposes ${name}`);
  }
  await page.locator('#editor-fields [name="protocol"]').selectOption('modbus-tcp');
  check(!(await page.locator('#editor-fields [name="k"]').isVisible()),'T: Modbus editor hides IEC104 windows');
  check(await page.locator('#editor-fields [name="unit_id"]').isVisible(),'T: Modbus editor exposes unit ID');
  await page.locator('#cancel-editor').click();
  await page.evaluate(()=>openEditor('point'));
  check(await page.locator('#editor-fields [name="time_tag"]').isVisible(),'T: point editor exposes time tag');
  await page.locator('#editor-fields [name="time_tag"]').selectOption('CP56Time2a');
  const values=await page.evaluate(()=>readEditorValues(editorFields('point')));
  check((await page.locator('#editor-error').textContent())==='','T: point editor loads source catalogue without errors');
  check(values.time_tag==='CP56Time2a','T: time tag is included in point payload');
  check(!('address_base' in values),'T: hidden Modbus address base is omitted from IEC104 payload');
  const output=path.join(root,'output/playwright/protocol-interface-20261112');fs.mkdirSync(output,{recursive:true});
  await page.screenshot({path:path.join(output,'t-point-editor.png'),fullPage:true});
  await f.close();
}
(async()=>{
  const browser=await chromium.launch({headless:true});
  try{
    for(const variant of process.argv.includes('--compare')?['before','after']:['after']){
      console.log(`Running ${variant}: C refresh/log races, S/T logs, T points`);
      await cRefresh(browser,variant);
      await logs(browser,'s','test',variant);
      await logs(browser,'s','logs',variant);
      await logs(browser,'t','logs',variant);
      await tPoints(browser,variant);
    }
    await tProtocolEditor(browser);
    const changed=['core/web_static/log-browser.js','scdfe_c/web/static/app.js','scdfe_t/web/static/app.js'];
    const report={baseline,generated_at:new Date().toISOString(),node:process.version,browser:browser.version(),mode:'real Chromium, repository HTML/JS, fully intercepted API fixtures; no database or running service',source_sha256:Object.fromEntries(changed.map(file=>[file,createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex')])),checks,measurements};
    const output=process.argv.indexOf('--output');
    if(output>=0){const target=path.resolve(process.argv[output+1]);fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,JSON.stringify(report,null,2)+'\n');}
    console.log(JSON.stringify(report,null,2));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});

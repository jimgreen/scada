/* Point snapshot diagnostics with populated, stale, unchanged and empty data. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({headless:true}),checks=[],errors=[];
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.setInterval=fn=>{window.tick=fn;return 1}});
  let empty=false,fail=false,stale=false;
  await page.route('**/*',route=>{
   const url=new URL(route.request().url());
   if(!url.pathname.startsWith('/api/')){
    const name=url.pathname.startsWith('/static/')?path.basename(url.pathname):'diagnostics.html';
    return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(root,'core/web_static',name),'utf8')});
   }
   if(fail)return route.fulfill({status:503,contentType:'application/json',body:'{"error":{"message":"测试中断"}}'});
   return route.fulfill({contentType:'application/json',body:JSON.stringify({component:'t',note:'隔离测试',items:empty?[]:[{name:'转发测试站',service:'scdfe_t',channel_id:2,running:true,stale,sampled_at:Date.now(),latency:{},point_runtime:{pending_points:12,pending_age_ms:150,batches:10,rows:100,failures:3,lock_conflicts:2,log_unconfirmed:1,last_error:'<img src=x onerror=alert(1)>'}}]})});
  });
  const check=(v,label)=>{assert.ok(v,label);checks.push(label)};
  const ready=()=>page.waitForFunction(()=>!document.getElementById('refresh').disabled);
  await page.goto('http://round16.test/diagnostics');await ready();
  check((await page.locator('#point-runtime').textContent()).includes('12 / 150'),'pending count and age');
  check((await page.locator('#point-runtime').textContent()).includes('10 / 100'),'saved batches and rows');
  check((await page.locator('#point-runtime').textContent()).includes('3 / 2'),'errors and lock conflicts');
  check(await page.locator('#point-runtime img').count()===0,'error text escaped');
  await page.evaluate(()=>{window.saved=document.querySelector('#point-runtime tr');tick()});await ready();
  check(await page.evaluate(()=>saved===document.querySelector('#point-runtime tr')),'unchanged rows retain DOM');
  fail=true;await page.locator('#refresh').click();await ready();
  check(await page.evaluate(()=>saved===document.querySelector('#point-runtime tr')),'failure retains previous diagnostics');
  fail=false;stale=true;await page.locator('#refresh').click();await ready();
  check((await page.locator('#point-runtime').textContent()).includes('快照已过期'),'stale snapshot explicit');
  const out=path.join(root,'output/playwright/efficiency-round16');fs.mkdirSync(out,{recursive:true});
  await page.locator('#point-runtime').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'snapshot.png')});
  empty=true;await page.locator('#refresh').click();await ready();
  check((await page.locator('#point-runtime').textContent()).includes('暂无转发显示状态'),'empty data clears stale rows');
  check(errors.length===0,'no browser errors');
  fs.writeFileSync(path.join(out,'fixture-results.json'),JSON.stringify({passed:true,checks,errors},null,2));
  console.log(JSON.stringify({passed:true,checks}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});

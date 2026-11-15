/* Isolated Chromium fixture for Modbus request diagnostics. */
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
   if(fail)return route.fulfill({status:503,contentType:'application/json',body:'{"error":{"message":"测试连接中断"}}'});
   return route.fulfill({contentType:'application/json',body:JSON.stringify({component:'s',note:'隔离测试',items:empty?[]:[{name:'<img src=x onerror=alert(1)>',service:'scdfe_s',channel_id:2,running:true,stale,sampled_at:Date.now(),latency:{},modbus_requests:{pending:2,limit:256,peak:7,busy:3,completed:100,abandoned:1,recent:[{transaction_id:102,unit_id:2,function_code:5,exception_code:4,queue_ms:2,execution_ms:120,response_submit_ms:.1,total_ms:122.1,response_submitted:false,peer_disconnected:true}]}}]})});
  });
  const check=(v,label)=>{assert.ok(v,label);checks.push(label)};
  const ready=()=>page.waitForFunction(()=>!document.getElementById('refresh').disabled);
  await page.goto('http://round15.test/diagnostics');await ready();
  check((await page.locator('#modbus-summary').textContent()).includes('待处理 2/256'),'queue count and limit');
  check((await page.locator('#modbus-summary').textContent()).includes('忙拒绝 3'),'busy rejection count');
  check((await page.locator('#modbus-requests').textContent()).includes('102 / 2 / 5'),'transaction, unit and function');
  check((await page.locator('#modbus-requests').textContent()).includes('客户端已断开'),'disconnect not mistaken for client receipt');
  check((await page.locator('#modbus-requests').textContent()).includes('120'),'execution timing');
  check(await page.locator('#modbus-requests img').count()===0,'names escaped');
  await page.evaluate(()=>{window.saved=document.querySelector('#modbus-requests tr');tick()});await ready();
  check(await page.evaluate(()=>saved===document.querySelector('#modbus-requests tr')),'unchanged request rows retain DOM');
  fail=true;await page.locator('#refresh').click();await ready();
  check((await page.locator('#message').textContent()).includes('测试连接中断'),'error visible');
  check(await page.evaluate(()=>saved===document.querySelector('#modbus-requests tr')),'failed refresh retains evidence');
  fail=false;stale=true;await page.locator('#refresh').click();await ready();
  check((await page.locator('#modbus-requests').textContent()).includes('快照已过期'),'stale persisted snapshot labelled');
  const out=path.join(root,'output/playwright/efficiency-round15');fs.mkdirSync(out,{recursive:true});
  await page.locator('#modbus-requests').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'modbus.png')});
  empty=true;await page.locator('#refresh').click();await ready();
  check((await page.locator('#modbus-summary').textContent()).includes('暂无 Modbus'),'empty counter');
  check((await page.locator('#modbus-requests').textContent()).includes('暂无 Modbus'),'empty table clears old rows');
  check(errors.length===0,'no browser errors');
  fs.writeFileSync(path.join(out,'fixture-results.json'),JSON.stringify({passed:true,checks,errors},null,2));
  console.log(JSON.stringify({passed:true,checks}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});

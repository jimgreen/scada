/* Diagnostic screen behaviours using real Chromium and isolated HTTP fixtures. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),checks=[];
function check(value,label){assert.ok(value,label);checks.push(label)}
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),requests=[],errors=[];
  let fail=false,empty=false,hold=false,release;
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.setInterval=fn=>{window.tick=fn;return 1}});
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(!url.pathname.startsWith('/api/')){
    const name=url.pathname.startsWith('/static/')?path.basename(url.pathname):'diagnostics.html';
    return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(root,'core/web_static',name),'utf8')});
   }
   requests.push(url.search);
   if(hold){hold=false;await new Promise(resolve=>{release=resolve})}
   if(fail){fail=false;return route.fulfill({status:503,contentType:'application/json',body:'{"error":{"message":"临时不可用"}}'})}
   const data={component:'t',note:'阶段分位数不能相加',items:empty?[]:[{service:'scdfe_t',channel_id:1,name:'<img src=x onerror=alert(1)>',running:true,sampled_at:1794710000000,stale:false,source_delta:{polls:5,rows_read:2,unchanged_polls:3},latency:{t_source_read_ms:{count:5,window_count:5,last_ms:2,p50_ms:1,p95_ms:3,p99_ms:3}}}]};
   if(!empty){data.items[0].command_write={count:3,failed:1,threshold_ms:100,recent:[{sampled_at:1794710000000,point_count:2,points:[{rtu_id:1,kind:'yk',pnt_no:1,cmd_time:1794710000000}],committed:false,failed_stage:'lock_wait_ms',error:'<img src=x onerror=alert(1)>',connection_ms:1,lock_wait_ms:120,validation_ms:2,apply_ms:3,commit_ms:1,total_ms:130}]};data.items[0].slow_batches={items:[{sampled_at:1794710000000,rtus:[1],messages:2,updates:3,queue_wait_ms:110,lock_wait_ms:1,backoff_ms:0,apply_ms:2,commit_ms:1,queue_commit_ms:114,receipt_log_ms:1}]}}
   if(url.searchParams.has('checks')){data.checks=[{name:'测试通道',severity:'error',message:'端口 <2404> 已占用'}];data.checked_at=1794710000000}
   return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  });
  const ready=()=>page.waitForFunction(()=>!document.getElementById('refresh').disabled);
  await page.goto('http://round10.test/diagnostics');await ready();
  check(requests[0]==='?checks=1','first request includes configuration checks');
  check(await page.locator('#timings tr').count()===1,'timing row renders');
  check(await page.locator('#timings img').count()===0,'service name is escaped');
  check((await page.locator('#checks').textContent()).includes('<2404>'),'check message remains literal text');
  check((await page.locator('#command-summary').textContent()).includes('未提交 1 次'),'uncommitted count is explicit');
  check((await page.locator('#command-writes').textContent()).includes('获取写锁'),'failed stage has a readable name');
  check((await page.locator('#command-writes').textContent()).includes('RTU 1/yk/1 @1794710000000'),'command token links diagnostic evidence');
  check(await page.locator('#command-writes img').count()===0,'command errors are escaped');
  await page.evaluate(()=>{window.savedCommand=document.querySelector('#command-writes tr');window.savedBatch=document.querySelector('#slow-batches tr');window.savedCheck=document.querySelector('#checks div')});
  await page.evaluate(()=>{window.saved=document.querySelector('#timings tr');tick()});await ready();
  check(requests.at(-1)==='','automatic refresh excludes port checks');
  check(await page.evaluate(()=>saved===document.querySelector('#timings tr')),'unchanged timing rows retain DOM');
  check(await page.evaluate(()=>savedCommand===document.querySelector('#command-writes tr')&&savedBatch===document.querySelector('#slow-batches tr')),'unchanged command and batch rows retain DOM');
  await page.locator('#auto').uncheck();const stopped=requests.length;
  await page.evaluate(()=>tick());check(requests.length===stopped,'disabled automatic refresh sends no request');
  await page.locator('#auto').check();
  await page.evaluate(()=>Object.defineProperty(document,'hidden',{configurable:true,value:true}));
  await page.evaluate(()=>tick());check(requests.length===stopped,'hidden page pauses automatic refresh');
  await page.evaluate(()=>Object.defineProperty(document,'hidden',{configurable:true,value:false}));
  hold=true;await page.evaluate(()=>tick());await page.waitForFunction(()=>document.getElementById('refresh').disabled);
  await page.evaluate(()=>{tick();tick()});check(requests.length===stopped+1,'concurrent refresh does not overlap');
  release();await ready();
  fail=true;await page.locator('#refresh').click();await ready();
  check((await page.locator('#message').textContent()).includes('临时不可用'),'HTTP failure is displayed');
  check(await page.evaluate(()=>savedCommand===document.querySelector('#command-writes tr')),'HTTP failure retains command diagnostics');
  check(await page.locator('#timings tr').count()===1,'failure retains last timing snapshot');
  empty=true;await page.locator('#refresh').click();await ready();
  check((await page.locator('#timings').textContent()).includes('暂无阶段样本'),'empty state is clear');
  check((await page.locator('#command-writes').textContent()).includes('暂无慢请求'),'empty command state clears old evidence');
  check(errors.length===0,'no browser JavaScript errors');
  const out=path.join(root,'output/playwright/efficiency-round14');fs.mkdirSync(out,{recursive:true});
  await page.screenshot({path:path.join(out,'fixture.png'),fullPage:true});
  fs.writeFileSync(path.join(out,'fixture-results.json'),JSON.stringify({passed:true,checks,errors},null,2));
  console.log(JSON.stringify({passed:true,checks}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});

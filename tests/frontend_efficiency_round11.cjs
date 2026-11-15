/* Command history: isolated fixtures, real Chromium, no operational commands. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),checks=[];
function check(value,label){assert.ok(value,label);checks.push(label)}
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1600,height:1000}}),requests=[],errors=[];
  let fail=false,empty=false,hold=false,release;
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.setInterval=fn=>{window.tick=fn;return 1}});
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(!url.pathname.startsWith('/api/')){
    const name=url.pathname.startsWith('/static/')?path.basename(url.pathname):'command-traces.html';
    return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(root,'core/web_static',name),'utf8')});
   }
   requests.push(url.search);
   if(hold){hold=false;await new Promise(resolve=>{release=resolve})}
   if(fail){fail=false;return route.fulfill({status:503,contentType:'application/json',body:'{"error":{"message":"临时不可用"}}'})}
   const data={items:empty?[]:[{command_id:42,rtu_id:7,rtu_name:'<img src=x onerror=alert(1)>',kind:'yk',pnt_no:1,value:1,stage:'protocol_confirmed',confirmation_scope:'iec104_activation_confirmation',cmd_time:1794710000000,dispatch_time:1794710000010,select_time:1794710000020,select_ack_time:1794710000030,execute_time:1794710000040,finish_time:1794710000050,feedback_state:'未关联回采验证',error:''}],has_more:!empty&&!url.searchParams.has('before'),next_before:42};
   return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  });
  const ready=()=>page.waitForFunction(()=>!document.getElementById('refresh').disabled);
  await page.goto('http://round11.test/command-traces?rtu_id=7&kind=yk&pnt_no=1');await ready();
  check(requests[0].includes('rtu_id=7'),'point context survives request');
  check(await page.locator('#commands tr').count()===1,'history row renders');
  check(await page.locator('#commands img').count()===0,'RTU name is escaped');
  check((await page.locator('#commands').textContent()).includes('未关联回采验证'),'physical feedback is explicitly unknown');
  await page.evaluate(()=>{window.saved=document.querySelector('#commands tr');tick()});await ready();
  check(await page.evaluate(()=>saved===document.querySelector('#commands tr')),'unchanged rows retain DOM');
  await page.locator('#older').click();await ready();
  check(requests.at(-1).includes('before=42'),'older page uses command cursor');
  let count=requests.length;await page.evaluate(()=>tick());
  check(requests.length===count,'history page pauses auto refresh');
  await page.locator('#stage').selectOption('protocol_confirmed');await ready();
  check(requests.at(-1).includes('stage=protocol_confirmed')&&!requests.at(-1).includes('before='),'phase filter returns to latest page');
  await page.evaluate(()=>Object.defineProperty(document,'hidden',{configurable:true,value:true}));
  count=requests.length;await page.evaluate(()=>tick());
  check(requests.length===count,'hidden page pauses requests');
  await page.evaluate(()=>Object.defineProperty(document,'hidden',{configurable:true,value:false}));
  hold=true;await page.evaluate(()=>tick());await page.waitForFunction(()=>document.getElementById('refresh').disabled);
  await page.evaluate(()=>{tick();tick()});check(requests.length===count+1,'requests do not overlap');release();await ready();
  fail=true;await page.locator('#refresh').click();await ready();
  check((await page.locator('#message').textContent()).includes('临时不可用'),'HTTP error is visible');
  check((await page.locator('#commands').textContent()).includes('规约已确认'),'error retains previous snapshot');
  const out=path.join(root,'output/playwright/efficiency-round11');fs.mkdirSync(out,{recursive:true});
  await page.screenshot({path:path.join(out,'command-fixture.png'),fullPage:true});
  empty=true;await page.locator('#refresh').click();await ready();
  check((await page.locator('#commands').textContent()).includes('暂无指令记录'),'empty state is clear');
  check(errors.length===0,'no browser JavaScript errors');
  fs.writeFileSync(path.join(out,'fixture-results.json'),JSON.stringify({passed:true,checks,errors},null,2));
  console.log(JSON.stringify({passed:true,checks}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});

/* Isolated Chromium fixtures for command rules/review/ETag, no device actions. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),checks=[];
function check(value,label){assert.ok(value,label);checks.push(label)}
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1600,height:1000}}),requests=[],writes=[],errors=[];
  let fail=false,empty=false,hold=false,release,unchanged=false,rev=1,details=0,closed=false,uncertain=false;
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.setInterval=fn=>{window.tick=fn;return 1}});
  const row={command_id:42,rtu_id:7,rtu_name:'<img src=x onerror=alert(1)>',channel_id:2,point_name:'开关反馈测试',kind:'yk',pnt_no:1,value:1,stage:'protocol_confirmed',confirmation_scope:'iec104_activation_confirmation',cmd_time:1794710000000,dispatch_time:1794710000010,select_time:1794710000020,select_ack_time:1794710000030,execute_time:1794710000040,finish_time:1794710000050,feedback_state:'matched',feedback_label:'新回采符合预期',feedback_rtu_id:7,feedback_kind:'yx',feedback_pnt_no:1,feedback_rtu_name:'测试站',feedback_point_name:'开关位置',feedback_expected:1,feedback_tolerance:0,feedback_value:1,feedback_quality:0,feedback_received_time:1794710000060,feedback_deadline:1794710005040,error:''};
  await page.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url());
   if(!url.pathname.startsWith('/api/')){
    const name=url.pathname.startsWith('/static/')?path.basename(url.pathname):'command-traces.html';
    return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(root,'core/web_static',name),'utf8')});
   }
   const send=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
   if(request.method()!=='GET'){
    writes.push({path:url.pathname,method:request.method(),body:request.postDataJSON()});rev++;
    if(url.pathname.endsWith('/resolve'))closed=true;
    return send({item:{version:1,review_time:1794710000100}});
   }
   if(url.pathname==='/api/command-options')return send({component:'c',items:url.searchParams.has('kind')?[{pnt_no:1,name:'开关位置'}]:[{rtu_id:7,name:'测试站'}],has_more:false});
   if(url.pathname==='/api/command-feedback')return send({item:null});
   if(/^\/api\/command-traces\/\d+$/.test(url.pathname)){
    details++;
    return send({component:'c',item:{...row,stage:uncertain?'execute_sent':row.stage,resolution_time:closed?1794710000101:null,resolution_actor:closed?'值班员':null,resolution_note:closed?'已核实现场并停止原采集进程':null,resolution_blocker:null}});
   }
   requests.push({query:url.search,headers:request.headers()});
   if(hold){hold=false;await new Promise(resolve=>{release=resolve})}
   if(fail){fail=false;return route.fulfill({status:503,contentType:'application/json',body:'{"error":{"message":"临时不可用"}}'})}
   if(unchanged&&request.headers()['if-none-match']==='"v'+rev+'"')return route.fulfill({status:304,headers:{ETag:'"v'+rev+'"'},body:''});
   const compact=Object.fromEntries(Object.entries(row).filter(([key])=>!['feedback_expected','feedback_tolerance','feedback_value','feedback_quality','feedback_received_time','feedback_deadline','review_note','select_time','select_ack_time','execute_time'].includes(key)));
   return route.fulfill({contentType:'application/json',headers:{ETag:'"v'+rev+'"'},body:JSON.stringify({component:'c',items:empty?[]:[compact],has_more:!empty&&!url.searchParams.has('before'),next_before:42})});
  });
  const ready=()=>page.waitForFunction(()=>!document.getElementById('refresh').disabled);
  const ruleReady=()=>page.waitForFunction(()=>!document.getElementById('rule-save').disabled);
  await page.goto('http://round13.test/command-traces?rtu_id=7&kind=yk&pnt_no=1');await ready();
  await page.waitForFunction(()=>!document.getElementById('configure').disabled);
  check((await page.locator('#rtu').textContent()).includes('测试站'),'RTU selector displays name');
  check(requests[0].query.includes('rtu_id=7')&&requests[0].query.includes('pnt_no=1'),'incoming point context is preserved');
  check(await page.locator('#commands img').count()===0,'RTU label is escaped');
  check((await page.locator('#commands').textContent()).includes('新回采符合预期'),'feedback label and point render');
  check(requests[0].query.includes('view=summary')&&details===0,'list loads summary without fetching details');
  await page.locator('[data-detail="42"]').click();await page.waitForFunction(()=>document.getElementById('detail-content').textContent.includes('回采点'));
  check(details===1&&(await page.locator('#detail-content').textContent()).includes('开关位置'),'details fetched on demand');
  check(await page.locator('#detail-content img').count()===0,'detail metadata is escaped');
  check(!await page.locator('#resolve-form').isVisible(),'confirmed commands cannot be closed as uncertain');
  await page.locator('#detail-close').click();
  check((await page.locator('#commands a').getAttribute('href')).includes('log_rtu=7'),'log link targets source RTU');
  await page.evaluate(()=>window.saved=document.querySelector('#commands tr'));unchanged=true;
  await page.evaluate(()=>tick());await ready();
  check(requests.at(-1).headers['if-none-match']==='"v1"','repeat request sends ETag');
  check(await page.evaluate(()=>saved===document.querySelector('#commands tr')),'304 preserves DOM');
  await page.locator('#older').click();await ready();
  let count=requests.length;await page.evaluate(()=>tick());check(requests.length===count,'history cursor freezes auto refresh');
  await page.locator('#stage').selectOption('protocol_confirmed');await ready();
  check(!requests.at(-1).query.includes('before='),'phase change returns to latest page');
  await page.locator('[data-rule="42"]').click();await ruleReady();
  check((await page.locator('#rule-point').textContent()).includes('开关位置'),'rule uses named command point');
  count=requests.length;await page.evaluate(()=>tick());check(requests.length===count,'modal pauses history polling');
  await page.locator('#feedback-point').selectOption('1');await page.locator('#rule-save').click();
  await page.waitForFunction(()=>!document.getElementById('rule-dialog').open);await ready();
  check(writes[0].path==='/api/command-feedback'&&writes[0].method==='PUT','rule is saved through database configuration API');
  check(writes[0].body.feedback_rtu_id===7&&writes[0].body.version===0,'rule uses explicit target and version');
  await page.locator('[data-review="42"]').click();
  await page.locator('#review-actor').fill('值班员');await page.locator('#review-note').fill('已核对报文，等待现场确认');
  await page.locator('#review-save').click();await page.waitForFunction(()=>!document.getElementById('review-dialog').open);await ready();
  check(writes[1].path==='/api/command-traces/42/review'&&writes[1].body.conclusion==='unknown','manual review records uncertainty without execution');
  check(writes.every(x=>!x.path.includes('/commands')),'UI never sends device commands');
  uncertain=true;await page.locator('[data-detail="42"]').click();await page.waitForFunction(()=>!document.getElementById('resolve-form').hidden);
  await page.locator('#resolve-actor').fill('值班员');await page.locator('#resolve-note').fill('已核实现场并停止原采集进程');
  await page.locator('#resolve-save').click();check(writes.length===2,'resolution requires checkbox confirmation');
  await page.locator('#resolve-confirm').check();await page.locator('#resolve-save').click();
  await page.waitForFunction(()=>document.getElementById('detail-message').textContent.includes('已结案'));
  await page.waitForFunction(()=>document.getElementById('detail-content').textContent.includes('旧命令未重发'));
  check(writes[2].path==='/api/command-traces/42/resolve'&&writes[2].body.confirm_no_replay===true,'explicit close uses separate endpoint and keeps original token');
  check(!await page.locator('#resolve-form').isVisible(),'closed command cannot be resolved again');
  await page.locator('#detail-close').click();await ready();
  await page.locator('#keyword').fill('测试');await page.locator('#filter').click();await ready();
  check(new URLSearchParams(requests.at(-1).query).get('q')==='测试','name filter is passed');
  hold=true;await page.evaluate(()=>tick());await page.waitForFunction(()=>document.getElementById('refresh').disabled);
  count=requests.length;await page.evaluate(()=>{tick();tick()});check(requests.length===count,'in-flight refresh does not overlap');release();await ready();
  fail=true;await page.locator('#refresh').click();await ready();
  check((await page.locator('#message').textContent()).includes('临时不可用'),'HTTP errors are shown');
  check((await page.locator('#commands').textContent()).includes('新回采符合预期'),'errors retain last results');
  const out=path.join(root,'output/playwright/efficiency-round13');fs.mkdirSync(out,{recursive:true});
  await page.screenshot({path:path.join(out,'command-fixture.png'),fullPage:true});
  empty=true;unchanged=false;await page.locator('#refresh').click();await ready();
  check((await page.locator('#commands').textContent()).includes('暂无指令记录'),'empty state renders');
  check(errors.length===0,'no JavaScript errors');
  fs.writeFileSync(path.join(out,'fixture-results.json'),JSON.stringify({passed:true,checks,errors,writes},null,2));console.log(JSON.stringify({passed:true,checks}));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});

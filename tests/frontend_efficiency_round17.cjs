/* Live S monitor counters must take precedence over sampled database rows. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({headless:true}),checks=[],errors=[];
 try{
  const page=await browser.newPage({viewport:{width:1600,height:1000}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.setInterval=fn=>{window.tick=fn;return 1}});
  let mode='live',fail=false;
  await page.route('**/*',route=>{
   const url=new URL(route.request().url());
   if(!url.pathname.startsWith('/api/')){
    const name=url.pathname.startsWith('/static/')?path.basename(url.pathname):'communication.html';
    return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(root,'scdfe_s/web/static',name),'utf8')});
   }
   assert.equal(route.request().method(),'GET');
   if(fail)return route.fulfill({status:503,contentType:'application/json',body:'{"error":{"message":"测试读取失败"}}'});
   let data={};
   if(url.pathname==='/api/rtus')data={items:[{rtu_id:1,channel_id:1,channel_type:'iec104',name:'实时测试站',process_enable:1}],total:1};
   if(url.pathname==='/api/monitor/communications'){
    const runtime={state:'running',rx_count:10,tx_count:20,error_count:1,reconnect_count:2,last_rx_time:1000,last_tx_time:2000};
    const live=mode==='fallback'?{}:mode==='zero'?{rx_count:0,tx_count:0,error_count:0,reconnect_count:0,last_rx_time:0,last_tx_time:0}:{rx_count:123,tx_count:456,error_count:3,reconnect_count:4,last_rx_time:1700000000000,last_tx_time:1700000001000};
    data={items:[{name:'iec104-rtu-1',rtu_id:1,running:true,point_count:24,open_connections:1,runtime_status:runtime,...live}]};
   }
   return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  });
  const check=(v,label)=>{assert.ok(v,label);checks.push(label)};
  const row=page.locator('#communication-rows tr');
  const refresh=async()=>{await page.locator('#refresh').click()};
  await page.goto('http://round17.test/communication');
  await page.waitForFunction(()=>document.querySelector('#communication-rows').textContent.includes('123 / 456 / 3 / 4'));
  check((await row.textContent()).includes('123 / 456 / 3 / 4'),'live counters override stored snapshot');
  check((await row.locator('td').nth(9).textContent()).includes('2023'),'live traffic timestamps override stored snapshot');
  mode='zero';await refresh();await page.waitForFunction(()=>document.querySelector('#communication-rows').textContent.includes('0 / 0 / 0 / 0'));
  check((await row.locator('td').nth(9).textContent()).includes('— / —'),'live zero timestamps do not resurrect old traffic');
  mode='fallback';await refresh();await page.waitForFunction(()=>document.querySelector('#communication-rows').textContent.includes('10 / 20 / 1 / 2'));
  check((await row.textContent()).includes('10 / 20 / 1 / 2'),'missing live counters fall back to stored snapshot');
  fail=true;await refresh();await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('测试读取失败'));
  check((await row.textContent()).includes('10 / 20 / 1 / 2'),'read failure retains previous rows');
  check(errors.length===0,'no browser errors');
  const out=path.join(root,'output/playwright/efficiency-round17');fs.mkdirSync(out,{recursive:true});
  await page.screenshot({path:path.join(out,'monitor.png')});
  fs.writeFileSync(path.join(out,'fixture-results.json'),JSON.stringify({passed:true,checks,errors},null,2));
  console.log(JSON.stringify({passed:true,checks}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});

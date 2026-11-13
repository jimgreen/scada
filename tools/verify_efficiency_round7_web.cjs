/* Read-only browser acceptance against the local C/S/T Web applications. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict'),{pathToFileURL}=require('url');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),report=path.join(root,'reports/scdfe_efficiency_20261113_round7');
(async()=>{const browser=await chromium.launch({headless:true}),results=[];try{
 async function makePage(){
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('response',r=>{if(r.status()>=400)errors.push(`${r.status()} ${r.url()}`)});
  await page.route('**/*',route=>['GET','HEAD'].includes(route.request().method())?route.continue():route.abort());
  return {page,errors};
 }
 for(const [role,url,body] of [['c','http://127.0.0.1:8877/#processes','#process-rows'],['s','http://127.0.0.1:8878/processes','#rows'],['s-communication','http://127.0.0.1:8878/communication','#communication-rows'],['t','http://127.0.0.1:8879/processes.html','#processes']]){
  const {page,errors}=await makePage();
  await page.goto(url);await page.waitForLoadState('networkidle');await page.waitForSelector(body,{state:'visible'});
  await page.locator(role==='c'?'#process-refresh':'#refresh').click();await page.waitForLoadState('networkidle');
  assert.equal(errors.length,0,errors.join('\n'));
  results.push({role,url,rows:await page.locator(body+' tr').count(),errors});
  await page.screenshot({path:path.join(report,`live-${role}.png`),fullPage:true});await page.close();
 }
 {
  const {page,errors}=await makePage();
  await page.goto('http://127.0.0.1:8878/test');await page.waitForLoadState('networkidle');
  const select=page.locator('#rtu-select');await select.waitFor();
  const options=await select.locator('option').evaluateAll(nodes=>nodes.map(n=>n.value).filter(Boolean));
  assert.ok(options.length,'Expected RTU definitions in S database');
  const chosen=options.find(value=>value!==undefined&&value!==options[0])||options[0];
  await select.selectOption(chosen);await page.waitForLoadState('networkidle');
  const pending=page.waitForResponse(r=>{const u=new URL(r.url());return u.pathname==='/api/monitor/communications'&&u.searchParams.get('rtu_id')===chosen;});
  await page.locator('#test-refresh').click();const response=await pending;const data=await response.json();
  await page.waitForFunction(()=>document.querySelector('#test-refresh-status').textContent.startsWith('已刷新'));
  assert.equal(response.status(),200);assert.equal(data.total,1);assert.equal(String(data.items[0].rtu_id),chosen);
  assert.ok(data.items[0].runtime_status,'This refresh publishes a diagnostic snapshot');
  assert.equal(await select.inputValue(),chosen);assert.equal(errors.length,0,errors.join('\n'));
  results.push({role:'s-test',rtu_id:chosen,selected_rtu_preserved:true,diagnostic_available:true,errors});
  await page.screenshot({path:path.join(report,'live-s-test.png'),fullPage:true});await page.close();
 }
 for(const file of ['scdfe_efficiency_round7_20261113.html','protocol_interface_updates.html'])for(const width of [1440,390]){
  const page=await browser.newPage({viewport:{width,height:1000}});await page.goto(pathToFileURL(path.join(root,'docs',file)).href);
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);assert.ok(!overflow,`${file}/${width}`);
  results.push({file,width,overflow});await page.screenshot({path:path.join(report,`${file}-${width}.png`),fullPage:true});await page.close();
 }
 fs.writeFileSync(path.join(report,'browser_live.json'),JSON.stringify(results,null,2)+'\n');console.log(JSON.stringify(results,null,2));
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exitCode=1});

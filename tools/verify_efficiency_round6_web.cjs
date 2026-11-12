/* Inspect actual restarted services using GET only; never operate channels. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict'),{pathToFileURL}=require('url');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),report=path.join(root,'reports/scdfe_efficiency_20261113_round6');
(async()=>{const browser=await chromium.launch({headless:true}),results=[];try{
 for(const [role,url,body] of [['c','http://127.0.0.1:8877/#processes','#process-rows'],['s','http://127.0.0.1:8878/processes','#rows'],['s-communication','http://127.0.0.1:8878/communication','#communication-rows'],['t','http://127.0.0.1:8879/processes.html','#processes']]){
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('response',r=>{if(r.status()>=400)errors.push(`${r.status()} ${r.url()}`)});
  await page.route('**/*',route=>['GET','HEAD'].includes(route.request().method())?route.continue():route.abort());
  await page.goto(url);await page.waitForLoadState('networkidle');await page.waitForSelector(body,{state:'visible'});
  await page.locator(role==='c'?'#process-refresh':'#refresh').click();await page.waitForLoadState('networkidle');
  assert.equal(errors.length,0,errors.join('\n'));results.push({role,url,rows:await page.locator(body+' tr').count(),errors});
  await page.screenshot({path:path.join(report,`live-${role}.png`),fullPage:true});await page.close();
 }
 for(const file of ['scdfe_efficiency_round6_20261113.html','protocol_interface_updates.html'])for(const width of [1440,390]){
  const page=await browser.newPage({viewport:{width,height:1000}});await page.goto(pathToFileURL(path.join(root,'docs',file)).href);
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);assert.ok(!overflow,`${file}/${width}`);
  results.push({file,width,overflow});await page.screenshot({path:path.join(report,`${file}-${width}.png`),fullPage:true});await page.close();
 }
 fs.writeFileSync(path.join(report,'browser_live.json'),JSON.stringify(results,null,2)+'\n');console.log(JSON.stringify(results,null,2));
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exitCode=1});

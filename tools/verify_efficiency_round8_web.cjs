/* Verify actual T pages and offline reports without operating channels. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict'),{pathToFileURL}=require('url');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),report=path.join(root,'reports/scdfe_efficiency_20261113_round8');
(async()=>{const browser=await chromium.launch({headless:true}),results=[];try{
 for(const [view,body] of [['channels','#channels'],['communication','#communication-rows']]){
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('response',r=>{if(r.status()>=400)errors.push(`${r.status()} ${r.url()}`)});
  await page.route('**/*',route=>['GET','HEAD'].includes(route.request().method())?route.continue():route.abort());
  await page.goto(`http://127.0.0.1:8879/${view}.html`);await page.waitForLoadState('networkidle');
  await page.locator(body).waitFor();await page.locator('#refresh').click();await page.waitForLoadState('networkidle');
  if(view==='communication'){
   const text=await page.locator(body).textContent();assert.match(text,/0\.0\.0\.0:2404/);assert.match(text,/200 ms/);
  }else{
   await page.locator('#selected-channel').selectOption('2');
   assert.equal(await page.locator('#channels tr.selected').getAttribute('data-select'),'2');
   assert.match(await page.locator('#channel-detail').textContent(),/2405/);
  }
  assert.deepEqual(errors,[]);results.push({view,rows:await page.locator(body+' tr').count(),config_visible:true,errors});
  await page.screenshot({path:path.join(report,'live-'+view+'.png'),fullPage:true});await page.close();
 }
 for(const file of ['scdfe_efficiency_round8_20261113.html','protocol_interface_updates.html'])for(const width of [1440,390]){
  const page=await browser.newPage({viewport:{width,height:1000}});await page.goto(pathToFileURL(path.join(root,'docs',file)).href);
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);assert.ok(!overflow,`${file}/${width}`);
  results.push({file,width,overflow});await page.screenshot({path:path.join(report,`${file}-${width}.png`),fullPage:true});await page.close();
 }
 fs.writeFileSync(path.join(report,'browser_live.json'),JSON.stringify(results,null,2)+'\n');console.log(JSON.stringify(results,null,2));
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exitCode=1});

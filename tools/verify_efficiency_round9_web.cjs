/* Read-only verification of deployed S/T pages and standalone HTML reports. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict'),{pathToFileURL}=require('url');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),out=path.join(root,'output/playwright/efficiency-round9');
(async()=>{
 fs.mkdirSync(out,{recursive:true});const browser=await chromium.launch({headless:true}),results=[];
 try {
  for(const [name,port] of [['s',8878],['t',8879]]) {
   const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],eventQueries=[];
   page.on('pageerror',e=>errors.push(e.message));
   page.on('response',r=>{if(r.status()>=400)errors.push(`${r.status()} ${r.url()}`)});
   await page.route('**/*',route=>['GET','HEAD'].includes(route.request().method())?route.continue():route.abort());
   await page.goto(`http://127.0.0.1:${port}/connections`);await page.waitForLoadState('networkidle');
   await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
   assert.ok(!(await page.locator('#target').isDisabled()));
   const links=page.locator('#rows a');
   results.push({name,list_rows:await page.locator('#rows tr').count(),options:await page.locator('#target option').count()});
   await page.screenshot({path:path.join(out,`live-${name}-connections.png`),fullPage:false});
   if(await links.count()) {
    await links.first().click();await page.waitForLoadState('networkidle');
    await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
    page.on('request',r=>{if(r.url().includes('/events?'))eventQueries.push(r.url())});
    await page.locator('#auto').uncheck();
    await page.locator('#refresh').click();await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
    assert.ok(eventQueries.some(url=>new URL(url).searchParams.has('after')));
    assert.ok((await page.locator('#summary').textContent()).includes('客户端 IP：源端口'));
    results.push({name,detail:true,incremental:true,events:await page.locator('.conn-event').count()});
    await page.screenshot({path:path.join(out,`live-${name}-detail.png`),fullPage:false});
   }
   assert.deepEqual(errors,[]);await page.close();
  }
  for(const file of ['scdfe_efficiency_20261115_round9.html','scdfe_usage_guide.html']) {
   const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   await page.goto(pathToFileURL(path.join(root,'docs',file)).href+(file.includes('usage')?'#current-performance':''));
   await page.waitForLoadState('load');
   assert.ok((await page.locator('body').textContent()).includes('99.45%'));
   assert.deepEqual(errors,[]);
   await page.screenshot({path:path.join(out,file+'.png'),fullPage:false});
   results.push({file,script_errors:errors});await page.close();
  }
  fs.writeFileSync(path.join(out,'live.json'),JSON.stringify(results,null,2));console.log(JSON.stringify(results,null,2));
 } finally {await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});

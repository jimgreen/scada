/* Read-only browser smoke checks after the Web restart; also verify HTML layout. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {pathToFileURL}=require('url');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),report=path.join(root,'reports/scdfe_efficiency_20261112_round4');
(async()=>{const browser=await chromium.launch({headless:true}),results=[];try{
 for(const [role,url] of [['c','http://127.0.0.1:8877/#yc'],['s','http://127.0.0.1:8878/points'],['t','http://127.0.0.1:8879/points.html']]){
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('response',r=>{if(r.status()>=400)errors.push(`${r.status()} ${r.url()}`)});
  await page.route('**/*',route=>['GET','HEAD'].includes(route.request().method())?route.continue():route.abort());
  await page.goto(url);await page.waitForLoadState('networkidle');
  const body=role==='c'?'#table-body':'[data-point-page="yc"] [data-points-body]';
  await page.waitForSelector(body,{state:'attached'});
  if(role==='c'){
   await page.locator('#view-all').click();
   await page.waitForFunction(()=>state.view==='all'&&!state.loading&&document.querySelectorAll('#table-head th').length===visibleColumns().length+1);
  }
  assert.equal(errors.length,0,errors.join('\n'));
  results.push({role,url,errors,rows:await page.locator(body+' tr').count()});
  await page.screenshot({path:path.join(report,`live-${role}.png`),fullPage:true});await page.close();
 }
 for(const file of ['scdfe_efficiency_round4_20261112.html','protocol_interface_updates.html']){
  for(const width of [1440,390]){
   const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.goto(pathToFileURL(path.join(root,'docs',file)).href);
   const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
   assert.ok(!overflow,`${file}/${width} overflow`);assert.equal(errors.length,0);
   results.push({file,width,overflow,errors});
   await page.screenshot({path:path.join(report,`${file}-${width}.png`),fullPage:true});await page.close();
  }
 }
 fs.writeFileSync(path.join(report,'browser_live.json'),JSON.stringify(results,null,2)+'\n');console.log(JSON.stringify(results,null,2));
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exitCode=1});

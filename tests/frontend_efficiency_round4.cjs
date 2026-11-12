/* Live Chromium DOM checks with intercepted data; no field-device operations. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {fixture}=require('./frontend_efficiency_round3.cjs');
const root=path.resolve(__dirname,'..'),report=path.join(root,'reports/scdfe_efficiency_20261112_round4');
const baseline='a9f0978',checks=[],measurements=[];
const check=(ok,label)=>{assert.ok(ok,label);checks.push(label)};
(async()=>{fs.mkdirSync(report,{recursive:true});const browser=await chromium.launch({headless:true});try{
 for(const variant of ['before','after'])for(const role of ['c','s','t']){
  const f=await fixture(browser,role,variant,{baseline,count:50}),{page,mock}=f;
  const selector=role==='c'?'#table-body':'[data-point-page="yc"] [data-points-body]';
  await page.evaluate(selector=>{
   const body=document.querySelector(selector);window.savedRows=Array.from(body.rows);
   window.savedButton=body.querySelector('button');savedButton.focus();
   window.changes=[];window.observer=new MutationObserver(records=>changes.push(...records));
   observer.observe(body.closest('table'),{childList:true,subtree:true});
  },selector);
  mock.value=77;mock.version=2;
  await page.evaluate(role=>role==='c'?loadRows({fresh:true}):role==='s'?__loadPoints({fresh:true}):loadPoints(),role);
  await f.settle();
  const metrics=await page.evaluate(selector=>{
   const body=document.querySelector(selector);observer.disconnect();
   return {preserved_rows:savedRows.filter((row,i)=>row===body.rows[i]).length,
    focus_preserved:document.activeElement===savedButton,
    added_rows:changes.reduce((n,r)=>n+Array.from(r.addedNodes).filter(x=>x.tagName==='TR').length,0),
    added_headers:changes.reduce((n,r)=>n+Array.from(r.addedNodes).filter(x=>x.tagName==='TH').length,0)};
  },selector);
  measurements.push({role,variant,...metrics});
  if(variant==='after'){
   check(metrics.preserved_rows===50,role+' value changes preserve 50 DOM rows');
   check(metrics.focus_preserved,role+' updates preserve button focus');
   check((await page.locator(selector).textContent()).includes('77'),role+' updated value is visible');
   if(role==='s'){
    const edit=page.locator(selector+' [data-point-edit]').first();
    const current=JSON.parse(await edit.getAttribute('data-point-edit'));
    check(current.value===77&&current._version===2,'S editor data carries latest value and version');
    check(await page.locator(selector+' [data-point-delete]').first().getAttribute('data-version')==='2','S delete uses latest version');
    await edit.click();check(await page.locator('#point-value').inputValue()==='77','S modal opens with refreshed value');
    await page.keyboard.press('Escape');
   }
   if(role==='t')check(metrics.added_headers===0,'T unchanged headers are retained');
   await page.screenshot({path:path.join(report,`fixture-${role}.png`),fullPage:true});
  }
  await f.close();
 }
 fs.writeFileSync(path.join(report,'frontend.json'),JSON.stringify({baseline,checks,measurements,scope:'Chromium fixture; 50 visible rows with all values changed'},null,2)+'\n');
 console.log(JSON.stringify({checks:checks.length,measurements},null,2));
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exitCode=1});

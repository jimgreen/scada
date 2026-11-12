const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {fixture}=require('./frontend_efficiency_round3.cjs');
const root=path.resolve(__dirname,'..'),report=path.join(root,'reports/scdfe_efficiency_20261112_round5');
const checks=[],measurements=[],baseline='c1e9509';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn){for(let n=0;n<500;n++){if(await fn())return;await sleep(10)}throw Error('fixture timeout')}
function check(ok,label){assert.ok(ok,label);checks.push(label)}
(async()=>{fs.mkdirSync(report,{recursive:true});const browser=await chromium.launch({headless:true});try{
 for(const variant of ['before','after']){
  for(const role of ['c','s']){
   const f=await fixture(browser,role,variant,{baseline}),{page}=f;
   const selector=role==='c'?'#rtu-filter':'#point-rtu';
   await page.locator(selector).evaluate(el=>window.savedOptions=Array.from(el.options));
   let count=0,held=[],hold=true,name='站1';
   if(role==='c')await page.route('**/api/rtus?*',async route=>{
    count++;const body=JSON.stringify({items:[{rtu_id:1,name},{rtu_id:2,name:'站2'}],total:2});
    if(hold)await new Promise(resolve=>held.push(resolve));
    await route.fulfill({contentType:'application/json',body});
   });
   if(role==='c'){
    await page.evaluate(()=>{window.reads=Promise.all(Array.from({length:5},()=>loadRtuOptions()))});
    await until(()=>held.length>0);await sleep(80);
    measurements.push({role,variant,overlapping_queries:count});
    if(variant==='after')check(count===1,'C coalesces five overlapping RTU catalogs');
    hold=false;held.splice(0).forEach(r=>r());await page.evaluate(()=>reads);
   }else{await page.locator('#reload').click();await f.settle()}
   const kept=await page.locator(selector).evaluate(el=>savedOptions.filter((x,i)=>x===el.options[i]).length);
   measurements.push({role,variant,preserved_options:kept});
   if(variant==='after')check(kept===3,role+' retains unchanged RTU options');
   if(variant==='after'&&role==='c'){
    hold=true;await page.evaluate(()=>{window.oldRead=loadRtuOptions()});await until(()=>held.length===1);
    name='已保存的新名称';await page.evaluate(()=>{window.freshRead=loadRtuOptions({fresh:true})});
    hold=false;held.splice(0).forEach(r=>r());await page.evaluate(()=>freshRead);
    check((await page.locator(selector).textContent()).includes(name),'C saved catalog is read after in-flight old query');
   }
   await f.close();
  }
  const f=await fixture(browser,'t',variant,{baseline}),{page}=f;
  let held=[],hold=true;const queries=[];
  await page.route('**/api/trans/source-catalog?*',async route=>{
   const q=new URL(route.request().url()).searchParams,rtu=q.get('rtu_id');queries.push(q.toString());
   let status=200;
   if(rtu&&hold)status=await new Promise(resolve=>held.push({rtu,resolve}));
   await route.fulfill({status,contentType:'application/json',body:JSON.stringify(status===200?{
    kind:q.get('kind'),rtu_id:Number(rtu),rtus:[{rtu_id:1,name:'站1',point_count:2},{rtu_id:2,name:'站2',point_count:2}],
    items:rtu?[{pnt_no:10,name:`站${rtu}点10`,value:rtu},{pnt_no:11,name:`站${rtu}点11`,value:rtu}]:[]
   }:{error:{message:'旧 RTU 请求失败'}})});
  });
  await page.evaluate(()=>openEditor('point',null,true));
  const rtu=page.locator('[name=source_rtu_id]'),point=page.locator('[name=source_pnt_no]');
  await rtu.selectOption('1');await until(()=>held.length===1);
  await rtu.selectOption('2');await until(()=>held.length===2);
  if(variant==='after')check(await point.isDisabled(),'T disables stale source points while loading');
  held.find(x=>x.rtu==='2').resolve(200);await until(async()=> (await point.textContent()).includes('站2点10'));
  held.find(x=>x.rtu==='1').resolve(200);held=[];await sleep(150);
  const latest=(await point.textContent()).includes('站2点10');
  measurements.push({role:'t',variant,latest_source_retained:latest});
  if(variant==='after'){
   check(latest,'T late RTU response cannot replace current source points');
   check(queries.filter(q=>q.includes('rtu_id=')).every(q=>q.includes('include_rtus=0')),'T source-detail reads omit global RTU counts');
   await rtu.selectOption('1');await until(()=>held.length===1);
   await rtu.selectOption('2');await until(()=>held.length===2);
   held.find(x=>x.rtu==='2').resolve(200);await until(async()=> (await point.textContent()).includes('站2点10'));
   held.find(x=>x.rtu==='1').resolve(500);held=[];await sleep(150);
   check(await page.locator('#editor-error').textContent()==='','T ignores errors from an obsolete RTU request');
   await rtu.selectOption('1');await until(()=>held.length===1);
   await page.locator('#cancel-editor').click();
   await page.evaluate(()=>openEditor('point',null,true));
   held[0].resolve(200);held=[];await sleep(150);
   check(!(await point.textContent()).includes('站1点10'),'T old editor request cannot populate a reopened editor');
   hold=false;await rtu.selectOption('2');await until(async()=> (await point.textContent()).includes('站2点10'));
   await point.selectOption(['10','11']);
   const post=page.waitForRequest(r=>r.method()==='POST'&&r.url().endsWith('/points/batch'));
   await page.locator('#editor-form button[type=submit]').click();
   const payload=(await post).postDataJSON();
   check(payload.items.length===2&&payload.items.every(p=>String(p.source_rtu_id)==='2'),'T batch save uses the current RTU and selected points');
   await f.settle();
  }
  await f.close();
 }
 fs.writeFileSync(path.join(report,'frontend.json'),JSON.stringify({baseline,checks,measurements},null,2)+'\n');
 console.log(JSON.stringify({checks:checks.length,measurements},null,2));
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exitCode=1});

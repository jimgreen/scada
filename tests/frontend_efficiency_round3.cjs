/* Chromium fixtures: no live station reads or writes. */
const fs=require('fs'), path=require('path'), assert=require('assert/strict'), {execFileSync}=require('child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'), report=path.join(root,'reports/scdfe_efficiency_20261112_round3');
const baseline='1d21c03', checks=[], measurements=[], sleep=ms=>new Promise(r=>setTimeout(r,ms));
function check(ok,label){assert.ok(ok,label);checks.push(label)}
async function until(fn){for(let i=0;i<600;i++){if(await fn())return;await sleep(10)}throw Error('fixture timeout')}
const schemas=JSON.parse(execFileSync('D:/anaconda3/python.exe',['-c',"import json;from scdfe_c.web.service import SchemaCatalog;c=SchemaCatalog();print(json.dumps([c.public(t) for t in ('fes_yc_define','fes_rtu_define')]))"],{cwd:root,encoding:'utf8'}));
const ycSchema=schemas.find(s=>s.head.eng_name==='fes_yc_define');
async function fixture(browser,role,variant){
 const context=await browser.newContext({viewport:{width:1600,height:1000}}),page=await context.newPage(),calls=[],held=[],errors=[],mock={active:0,hold:false,value:10};
 page.on('pageerror',e=>errors.push(e.message));await page.addInitScript(()=>{window.setInterval=()=>0});
 const filename=role==='c'?'index.html':'points.html', js=role==='s'?'channels.js':'app.js';
 function payload(url,method){
  const p=url.pathname,q=url.searchParams;
  if(method!=='GET'){mock.value=77;return {item:{value:77}}}
  if(p==='/api/health')return {status:'ok'};
  if(p==='/api/schemas')return {items:schemas};
  if(p==='/api/overview')return {counts:{}};
  if(p==='/api/rtus'||p==='/api/channels')return {items:[{rtu_id:1,channel_id:1,channel_type:'iec104',name:'站1'},{rtu_id:2,channel_id:2,channel_type:'iec104',name:'站2'}],total:2};
  if(p==='/api/trans/channels'||p==='/api/trans/status')return {items:[{channel_id:1,name:'通道1',protocol:'iec104'},{channel_id:2,name:'通道2',protocol:'iec104'}],total:2};
  if(p.includes('/points')){
   const id=p.includes('/2/')?2:1;
   let row={rtu_id:id,channel_id:id,kind:'yc',pnt_no:1,name:'点'+id,value:mock.value,status:0,refresh_time:1000,_version:1};
   if(role==='c')for(const c of ycSchema.columns)if(!(c.eng_name in row))row[c.eng_name]=c.data_type==='str'?'完整字段':19357;
   if(q.has('fields'))row=Object.fromEntries(['rtu_id','pnt_no','_version',...q.get('fields').split(',')].map(k=>[k,row[k]]));
   return {items:[row],total:1};
  }
  return {items:[],total:0,item:{}};
 }
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.pathname.startsWith('/api/')){
   const method=route.request().method(),body=JSON.stringify(payload(u,method));calls.push({path:u.pathname+u.search,method});mock.active++;
   try{if(mock.hold&&method==='GET'&&(u.pathname.includes('/points')||u.pathname==='/api/channels'||u.pathname.startsWith('/api/trans/')))await new Promise(resolve=>held.push(resolve));
    await route.fulfill({contentType:'application/json',body});}finally{mock.active--}return;
  }
  if(u.pathname.startsWith('/static/')){
   const name=path.basename(u.pathname),relative=`scdfe_${role}/web/static/${name}`;
   const local=fs.existsSync(path.join(root,relative))?path.join(root,relative):path.join(root,'core/web_static',name);
   if(!fs.existsSync(local))return route.fulfill({status:404,body:''});
   let source=name===js&&variant==='before'?execFileSync('git',['show',`${baseline}:${relative}`],{cwd:root,encoding:'utf8'}):fs.readFileSync(local,'utf8');
   if(role==='s'&&name===js)source=source.replace(/\}\)\(\);\s*$/, 'window.__loadPoints=loadPoints; window.__api=api;})();');
   return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':'text/css',body:source});
  }
  let html=fs.readFileSync(path.join(root,`scdfe_${role}/web/static/${filename}`),'utf8');
  html=html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,tag=>tag.includes('/'+js)||tag.includes('/layout-resizer.js')?tag:'');
  if(role==='t')html=html.replace('</header>','<span id="connection"></span></header>');
  await route.fulfill({contentType:'text/html',body:html});
 });
 async function settle(){await until(()=>mock.active===0);await sleep(80);await until(()=>mock.active===0)}
 await page.goto('http://round3.test/'+(role==='c'?'#yc':''));await settle();
 return {page,calls,held,mock,settle,close:async()=>{await context.close();check(!errors.length,role+'/'+variant+' no browser errors')}};
}
(async()=>{fs.mkdirSync(report,{recursive:true});const browser=await chromium.launch({headless:true});try{
 for(const variant of ['before','after'])for(const role of ['c','s','t']){
  const f=await fixture(browser,role,variant),{page,calls,held,mock}=f;
  const start=calls.length;mock.hold=true;
  await page.evaluate(role=>{for(let i=0;i<5;i++){
   if(role==='c')loadRows();else document.querySelector(role==='s'?'#reload':'#refresh').click();
  }},role);
  await until(()=>held.length>0);await sleep(100);
  const overlapping=calls.length-start;mock.hold=false;held.splice(0).forEach(r=>r());await f.settle();
  measurements.push({role,variant,overlapping_requests:overlapping});
  if(variant==='after')check(overlapping===1,role+' merges five identical reads');
  if(role==='c'){
   const before=calls.length;await page.locator('#view-all').click({timeout:5000}).catch(async e=>{await page.screenshot({path:path.join(report,'click-failure.png')});throw e});await f.settle();
   const count=calls.length-before;measurements.push({role,variant,all_fields_queries:count});
   if(variant==='after'){check(count===1,'C view switch reloads projection');
    const body=await page.locator('#table-body').textContent();
    if(!body.replaceAll(',','').includes('19357'))console.log(JSON.stringify({body,error:await page.locator('#table-error').textContent(),calls:calls.slice(-2)},null,2));
    check(body.replaceAll(',','').includes('19357'),'C renders formerly unqueried values');
    check(await page.evaluate(()=>Object.hasOwn(state.items[0], 'raw_base') && document.querySelector('#table-head').textContent.includes('raw_base')),'C all-fields response includes a formerly hidden field')}
  }
  if(variant==='after'){
   mock.hold=true;
   await page.evaluate(role=>{window.__oldRead=role==='c'?loadRows():role==='s'?__loadPoints():loadChannels({loadSelectedPoints:true})},role);
   await until(()=>held.length>0);
   // Simulate a committed write while the older response is in flight.
   mock.value=77;
   await page.evaluate(role=>{window.__freshRead=role==='c'?loadRows({fresh:true}):role==='s'?__loadPoints({fresh:true}):loadChannels({loadSelectedPoints:true,fresh:true})},role);
   mock.hold=false;held.splice(0).forEach(r=>r());await page.evaluate(()=>window.__freshRead);await f.settle();
   const text=await page.locator(role==='c'?'#table-body':role==='s'?'[data-point-page="yc"] [data-points-body]':'[data-point-page="yc"] [data-points-body]').textContent();
   check(text.includes('77'),role+' fresh read after write is not lost');
   if(role==='s'){
    await page.locator('[data-point-page="yc"] [data-points-body]').evaluate(el=>window.__row=el.firstElementChild);
    await page.evaluate(()=>__loadPoints());await f.settle();
    check(await page.evaluate(()=>window.__row===document.querySelector('[data-point-page="yc"] [data-points-body]').firstElementChild),'S unchanged read preserves DOM row');
   }
   mock.hold=true;
   await page.evaluate(role=>{window.__oldScope=role==='c'?loadRows():role==='s'?__loadPoints():loadPoints()},role);
   await until(()=>held.length>0);
   await page.locator(role==='c'?'#rtu-filter':role==='s'?'#point-rtu':'#selected-channel').selectOption('2');
   await sleep(350);
   mock.hold=false;held.splice(0).reverse().forEach(r=>r());await f.settle();
   await until(async()=> (await page.locator(role==='c'?'#table-body':'[data-point-page="yc"] [data-points-body]').textContent()).includes('点2'));
   check(true,role+' scope change discards older point response');
  }
  await f.close();
 }
 const result={baseline,checks,measurements,scope:'Chromium with intercepted APIs; no real station traffic'};
 fs.writeFileSync(path.join(report,'frontend.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result,null,2));
 }finally{await browser.close()}})().catch(e=>{console.error(e);process.exitCode=1});

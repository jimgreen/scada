/* Chromium checks against repository pages and controlled, isolated APIs. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict'),{execFileSync}=require('child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),report=path.join(root,'reports/scdfe_efficiency_20261113_round8');
const baseline='3e36657',checks=[],measurements=[];
function check(value,label){assert.ok(value,label);checks.push(label)}
async function fixture(browser,view,variant){
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],mock={tick:0,count:50,reverse:false,failure:false,deletes:[]};
 const js=view==='channels'?'app.js':'communication.js';
 const rows=()=>{const values=Array.from({length:mock.count},(_,i)=>({channel_id:i+1,name:'通道'+(i+1),protocol:i%2?'modbus-tcp':'iec104',
  bind_host:'127.0.0.1',bind_port:2404+i,scan_interval_ms:200,common_address:7,unit_id:3,report_interval_ms:1000,_version:mock.tick+1,
  runtime_status:{state:'running',running:1,rx_count:mock.tick,tx_count:mock.tick+1,connection_state:'connected',sampled_at:1794542400000+mock.tick*1000,
    process_heartbeat_time:1794542400000+mock.tick*1000,diagnostic:{tick:mock.tick},last_error:mock.tick?'new diagnostic':''}}));return mock.reverse?values.reverse():values};
 page.on('pageerror',e=>errors.push(e.message));await page.addInitScript(()=>{window.setInterval=()=>0});
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.pathname.startsWith('/api/')){
   if(request.method()==='DELETE')mock.deletes.push({path:url.pathname,version:request.headers()['if-match']});
   const status=mock.failure?503:200;
   const body=status!==200?{error:{message:'fixture unavailable'}}:url.pathname==='/api/health'?{status:'ok'}:
    url.pathname.match(/\/channels\/\d+$/)?{item:rows().find(x=>String(x.channel_id)===url.pathname.split('/').at(-1))}:{items:rows(),total:mock.count};
   return route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  }
  if(url.pathname.startsWith('/static/')){
   const name=path.basename(url.pathname),relative='scdfe_t/web/static/'+name;
   let source=variant==='before'&&name===js?execFileSync('git',['show',baseline+':'+relative],{cwd:root,encoding:'utf8'}):fs.readFileSync(path.join(root,relative),'utf8');
   if(name==='communication.js')source=source.replace(/\}\)\(\);\s*$/,'window.__refresh=load;})();');
   return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':'text/css',body:source});
  }
  let html=fs.readFileSync(path.join(root,`scdfe_t/web/static/${view}.html`),'utf8').replace('</header>','<span id="connection"></span></header>');
  return route.fulfill({contentType:'text/html',body:html});
 });
 await page.goto('http://round8.test/');await page.waitForLoadState('networkidle');
 return {page,mock,errors,body:view==='channels'?'#channels':'#communication-rows',load:()=>page.evaluate(view=>view==='channels'?loadChannels():__refresh(),view)};
}
(async()=>{fs.mkdirSync(report,{recursive:true});const browser=await chromium.launch({headless:true});try{
 for(const view of ['channels','communication'])for(const variant of ['before','after']){
  const f=await fixture(browser,view,variant),{page,mock,body}=f;
  await page.evaluate(body=>{
   const b=document.querySelector(body);window.savedRows=[...b.rows];window.savedAction=b.querySelector('button,a');savedAction.focus();
   window.mutations=[];window.observer=new MutationObserver(r=>mutations.push(...r));observer.observe(b,{childList:true,subtree:true});
  },body);
  mock.tick=1;await f.load();
  const result=await page.evaluate(body=>{
   observer.disconnect();const b=document.querySelector(body);
   return {preserved_rows:savedRows.filter((row,i)=>row===b.rows[i]).length,focus_preserved:document.activeElement===savedAction,
    removed_rows:mutations.reduce((n,r)=>n+[...r.removedNodes].filter(x=>x.tagName==='TR').length,0)};
  },body);
  measurements.push({view,variant,...result});
  if(variant==='after'){
   check(result.preserved_rows===50,view+' preserves all 50 rows when counters/heartbeat change');
   check(result.focus_preserved,view+' preserves action focus');
   check((await page.locator(body).textContent()).includes('new diagnostic'),view+' refreshes diagnostic text');
   if(view==='channels'){
    check(await page.locator('[data-delete="1"]').getAttribute('data-version')==='2','Channel delete uses refreshed optimistic version');
    await page.locator('#selected-channel').selectOption('2');
    check(await page.locator('#channels tr.selected').getAttribute('data-select')==='2','Selection highlight changes with selector');
    check((await page.locator('#channel-detail').textContent()).includes('2405'),'Selected channel detail shows listener port');
   }
   mock.reverse=true;mock.tick=2;await f.load();
   check((await page.locator(body+' tr').first().textContent()).includes('通道50'),view+' reorder binds the correct channel');
   if(view==='channels'){
    page.on('dialog',d=>d.accept());await page.locator('[data-delete="50"]').click();await page.waitForLoadState('networkidle');
    check(mock.deletes.length===1&&mock.deletes[0].path.endsWith('/50')&&mock.deletes[0].version==='3','Reordered delete dispatches correct id and latest version');
   }
   mock.failure=true;await f.load().catch(()=>{});mock.failure=false;mock.count=0;await f.load();
   check((await page.locator(body).textContent()).includes('暂无'),view+' empty state after recoverable request failure');
   mock.count=2;await f.load();check(await page.locator(body+' tr').count()===2,view+' restores rows after empty result');
   await page.screenshot({path:path.join(report,'fixture-'+view+'.png'),fullPage:true});
  }
  check(f.errors.length===0,view+'/'+variant+' no script errors');await page.close();
 }
 const data={baseline,checks,measurements,scope:'Actual repository HTML/JavaScript, mocked HTTP data in Chromium. No field commands.'};
 fs.writeFileSync(path.join(report,'frontend.json'),JSON.stringify(data,null,2)+'\n');console.log(JSON.stringify(data,null,2));
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exitCode=1});

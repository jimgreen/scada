/* Real repository pages with controlled API responses; no station traffic. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict'),{execFileSync}=require('child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {fixture:cFixture}=require('./frontend_efficiency_round3.cjs');
const root=path.resolve(__dirname,'..'),report=path.join(root,'reports/scdfe_efficiency_20261113_round6');
const baseline='4f9aed9',checks=[],measurements=[],sleep=ms=>new Promise(r=>setTimeout(r,ms));
function check(ok,label){assert.ok(ok,label);checks.push(label)}
async function until(fn){for(let n=0;n<600;n++){if(await fn())return;await sleep(10)}throw Error('fixture timeout')}
function rows(tick=0,count=50){return Array.from({length:count},(_,i)=>({channel_id:i+1,rtu_id:i+1,name:'站'+(i+1),channel_type:'modbus-tcp',protocol:'modbus-tcp',
 process_key:'rtu-'+(i+1),log_process:'scdfe_c-channel-'+(i+1),kind:'debug',include_in_all:true,
 process_pid:100+i,process_status:2,process_enable:1,alive:true,process_start_time:1794477600000,process_heartbeat_time:1794477600000+tick*1000,process_error:''}))}
async function fixture(browser,role,variant,view='processes'){
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],state={tick:0,hold:false,active:0,count:0,held:[],failure:false,reverse:false,size:50,posts:[]};
 const js=role==='s'?'monitor.js':'processes.js';
 page.on('pageerror',error=>errors.push(error.message));
 await page.addInitScript(()=>{window.setInterval=()=>0;window.clock=1794477600000;Date.now=()=>window.clock;});
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url()),method=route.request().method();
  if(url.pathname.startsWith('/api/')){
   state.active++;state.count++;
   const source=rows(state.tick,state.size);if(state.reverse)source.reverse();
   let result={items:source},status=state.failure?503:200;
   if(method==='POST'){state.posts.push(url.pathname);state.tick++;state.failure=false;status=200;result={item:{}}}
   else if(url.pathname==='/api/monitor/processes')result={items:source.map(x=>({name:'rtu-'+x.rtu_id,rtu_id:x.rtu_id,running:true,status:{started_at:x.process_start_time,rtu_id:x.rtu_id,last_error:'generation-'+state.tick}}))};
   else if(url.pathname==='/api/monitor/communications')result={items:source.map(x=>({name:'rtu-'+x.rtu_id,rtu_id:x.rtu_id,running:true,rx_count:state.tick,last_error:'generation-'+state.tick}))};
   const body=JSON.stringify(status===200?result:{error:{message:'old failure'}});
   try{if(state.hold&&method==='GET')await new Promise(resolve=>state.held.push(resolve));await route.fulfill({status,contentType:'application/json',body});}finally{state.active--}return;
  }
  if(url.pathname.startsWith('/static/')){
   const name=path.basename(url.pathname),relative=`scdfe_${role}/web/static/${name}`,local=path.join(root,relative);
   let text=variant==='before'&&name===js?execFileSync('git',['show',`${baseline}:${relative}`],{cwd:root,encoding:'utf8'}):fs.readFileSync(local,'utf8');
   if(role==='s'&&name===js)text=text.replace(/\}\)\(\);\s*$/,'window.__load=loadProcesses;window.__communications=loadCommunications;})();');
   return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':'text/css',body:text});
  }
  return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,`scdfe_${role}/web/static/${view}.html`),'utf8')});
 });
 await page.goto('http://frontend.test/');await page.waitForLoadState('networkidle');
 const load=()=>page.evaluate(({role,view})=>role==='s'?(view==='communication'?window.__communications():window.__load()):load(),{role,view});
 const settle=async()=>{await until(()=>state.active===0);await sleep(60)};
 return{page,state,load,settle,close:async()=>{await page.close();check(!errors.length,`${role}/${variant}: no browser errors`)}};
}
(async()=>{fs.mkdirSync(report,{recursive:true});const browser=await chromium.launch({headless:true});try{
 for(const variant of ['before','after']){
  const c=await cFixture(browser,'c',variant,{baseline});
  let tick=0;
  await c.page.route('**/api/processes',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({items:rows(tick),busy:false})}));
  await c.page.evaluate(()=>changeResource('processes'));await c.settle();
  await c.page.evaluate(()=>{window.metrics=Array.from(document.querySelector('#process-summary').children)});tick++;
  await c.page.evaluate(()=>loadProcesses());
  const kept=await c.page.evaluate(()=>metrics.filter((x,i)=>x===document.querySelector('#process-summary').children[i]).length);
  measurements.push({role:'c',variant,summary_nodes_preserved:kept});if(variant==='after')check(kept===4,'C preserves summary metric nodes on heartbeat');await c.close();
  for(const role of ['s','t']){
   const f=await fixture(browser,role,variant),{page,state}=f,body=role==='s'?'#rows':'#processes';
   await page.evaluate(selector=>{window.saved=Array.from(document.querySelector(selector).querySelectorAll('button'));saved[0].focus();},body);
   state.tick++;await page.evaluate(()=>window.clock+=1000);await f.load();await f.settle();
   const preserved=await page.evaluate(selector=>{const buttons=Array.from(document.querySelector(selector).querySelectorAll('button'));return {count:saved.filter((x,i)=>x===buttons[i]).length,focus:document.activeElement===saved[0],total:saved.length}},body);
   measurements.push({role,variant,...preserved});if(variant==='after'){check(preserved.count===preserved.total,role+' retains action buttons during heartbeat');check(preserved.focus,role+' preserves keyboard focus')}
   if(role==='s'){
    state.count=0;state.hold=true;
    await page.evaluate(()=>{window.loads=Promise.all(Array.from({length:5},()=>window.__load()))});await until(()=>state.held.length>0);await sleep(80);
    measurements.push({role,variant,overlapping_requests:state.count});if(variant==='after')check(state.count===2,'S merges five refreshes into one pair of API calls');
    state.hold=false;state.held.splice(0).forEach(release=>release());await page.evaluate(()=>loads);await f.settle();
    if(variant==='after'){
     state.hold=true;await page.evaluate(()=>{window.oldRead=window.__load()});await until(()=>state.held.length===2);
     await page.locator('button[data-service]').first().click();await until(()=>state.tick===2);
     state.hold=false;state.held.splice(0).forEach(release=>release());await f.settle();
     await until(async()=>(await page.locator('#rows').textContent()).includes('generation-2'));
     check(true,'S control completion reads fresh state after an older request');
     // A failed request also releases the merged slot so the next refresh works.
     state.failure=true;await f.load();check((await page.locator('#message').textContent()).includes('old failure'),'S reports current request errors');
     state.failure=false;await f.load();check(!(await page.locator('#message').textContent()).includes('old failure'),'S recovers after failed refresh');
    }
   }
   if(variant==='after'){
    state.reverse=true;await f.load();await f.settle();
    const button=page.locator(body+' button').first();
    check(await button.getAttribute(role==='s'?'data-service':'data-id')===(role==='s'?'rtu-50':'50'),role+' controls follow reordered channel rows');
    state.size=0;await f.load();check((await page.locator(body).textContent()).includes('暂无'),role+' renders empty result after channel removal');
    state.size=50;await f.load();check(await page.locator(body+' tr').count()===50,role+' restores rows after empty result');
    await page.screenshot({path:path.join(report,role+'-processes.png'),fullPage:true});
   }
   await f.close();
  }
 }
 const comm=await fixture(browser,'s','after','communication');
 await comm.page.evaluate(()=>window.button=document.querySelector('#communication-rows button'));
 comm.state.tick++;await comm.load();
 check(await comm.page.evaluate(()=>button===document.querySelector('#communication-rows button')),'S communication counters preserve action button');
 check((await comm.page.locator('#communication-rows').textContent()).includes('generation-1'),'S communication diagnostics show new data');
 await comm.close();
 fs.writeFileSync(path.join(report,'frontend.json'),JSON.stringify({baseline,checks,measurements},null,2)+'\n');console.log(JSON.stringify({passed:checks.length,measurements},null,2));
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exitCode=1});

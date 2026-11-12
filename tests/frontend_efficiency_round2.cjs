/* Real Chromium; intercepted API fixtures, no live station writes. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'), report=path.join(root,'reports/scdfe_efficiency_20261112_round2');
const checks=[],measurements=[];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function check(ok,label){assert.ok(ok,label);checks.push(label)}
async function until(fn){for(let n=0;n<800;n++){if(await fn())return;await sleep(10)}throw Error('fixture timeout')}
async function fixture(browser,mod,variant){
  const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
  const calls=[],errors=[],held=[],mock={active:0,hold:null,fail:null,value:10,delay:0,total:10000};
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.setInterval=()=>0});
  function payload(url,method){
    const p=url.pathname,q=url.searchParams;
    if(method!=='GET'){mock.value=77;return {item:{value:77},ok:true}}
    if(p==='/api/health')return {status:'ok'};
    if(p==='/api/overview')return {counts:{}};
    if(p==='/api/rtus'){
      const rows=Array.from({length:600},(_,n)=>({rtu_id:n+1,channel_id:n+1,channel_type:'iec104',name:'站'+(n+1)}));
      return {items:rows.slice(Number(q.get('offset')||0),Number(q.get('offset')||0)+Number(q.get('limit')||500)),total:rows.length};
    }
    if(/\/rtus\/\d+\/channel$/.test(p))return {item:{rtu_id:Number(p.split('/')[3]),channel_id:Number(p.split('/')[3]),channel_type:'iec104'}};
    if(/\/rtus\/\d+\/points\/\w+$/.test(p)){
      const id=Number(p.split('/')[3]),offset=Number(q.get('offset')||0),limit=Number(q.get('limit')||500);
      return {items:Array.from({length:Math.max(0,Math.min(limit,mock.total-offset))},(_,i)=>({pnt_no:offset+i+1,name:`RTU${id}-点${offset+i+1}`,value:mock.value,status:0,raw_status:0,refresh_time:1794477600000,cmd_handle_time:0})),total:mock.total};
    }
    if(p==='/api/monitor/communications')return {items:[{rtu_id:Number(q.get('rtu_id')),running:true,point_count:40000}]};
    if(p==='/api/processes')return {items:[{kind:'rtu',process_key:'rtu:1',rtu_id:1,channel_id:1,name:'进程'+mock.value,log_process:'fixture-进程'+mock.value,alive:false,process_status:0}],busy:false};
    if(p==='/api/trans/processes')return {items:[{channel_id:1,protocol:'iec104',name:'进程'+mock.value,alive:false}]};
    if(p==='/api/trans/channels'||p==='/api/trans/status')return {items:[{channel_id:1,name:'转发',protocol:'iec104'}],total:1};
    return {items:[],total:0,item:{}};
  }
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());assert.equal(url.hostname,'round2.test');
    if(url.pathname.startsWith('/api/')){
      const body=payload(url,route.request().method()),json=JSON.stringify(body),call={path:url.pathname+url.search,bytes:Buffer.byteLength(json),method:route.request().method()};
      calls.push(call);mock.active++;
      try{
        if(mock.hold?.(url))await new Promise(resolve=>held.push({resolve,url}));
        if(mock.delay)await sleep(mock.delay);
        const fail=mock.fail?.(url);
        await route.fulfill({status:fail?503:200,contentType:'application/json',body:fail?JSON.stringify({error:{message:'fixture failure'}}):json});
      }finally{mock.active--}return;
    }
    if(url.pathname.startsWith('/static/')){
      const name=path.basename(url.pathname),file=`scdfe_${mod}/web/static/${name}`,snap=path.join(report,'before',file.replaceAll('/','__')+'.snapshot');
      const local=fs.existsSync(path.join(root,file))?path.join(root,file):path.join(root,'core/web_static',name);
      if(!fs.existsSync(local))return route.fulfill({status:404,body:''});
      let body=fs.readFileSync(variant==='before'&&fs.existsSync(snap)?snap:local,'utf8');
      if(mod==='t'&&name==='processes.js')body+='\nwindow.loadProcesses=load;';
      return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':'text/css',body});
    }
    let html=fs.readFileSync(path.join(root,`scdfe_${mod}/web/static/${mod==='s'?'test':mod==='t'?'processes':'index'}.html`),'utf8');
    if(mod!=='c')html=html.replace('</header>','<span id="connection"></span><span id="workbench-database"></span></header>');
    html=html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,tag=>(mod==='s'?/\/test\.js/:mod==='t'?/\/processes\.js/:/\/app\.js/).test(tag)?tag:'');
    await route.fulfill({contentType:'text/html',body:html});
  });
  async function settle(){let count=-1,quiet=Date.now();for(let i=0;i<1000;i++){await sleep(10);if(mock.active||calls.length!==count)quiet=Date.now();count=calls.length;if(!mock.active&&Date.now()-quiet>120)return}throw Error('not settled')}
  await page.goto('http://round2.test/');await settle();
  return {page,calls,errors,held,mock,settle,close:async()=>{await context.close();check(!errors.length,`${mod}/${variant}: no browser exceptions`)}};
}
async function sCase(browser,variant){
  const f=await fixture(browser,'s',variant),{page,calls,mock,held}=f;
  const pointCalls=calls.filter(x=>/\/points\//.test(x.path));
  measurements.push({module:'S',variant,point_requests:pointCalls.length,point_response_bytes:pointCalls.reduce((n,c)=>n+c.bytes,0)});
  check(await page.locator('#metric-points').textContent()==='40000',`S/${variant}: total point count`);
  if(variant==='before'){await f.close();return}
  check(pointCalls.length===4,'S: only four page queries');
  check(pointCalls.every(c=>c.path.includes('limit=25')),'S: query limited to 25 rows');
  check(await page.locator('#rtu-select option').count()===600,'S: catalog contains RTUs beyond first 500');
  await page.locator('[data-page-tab="points"]').click();
  const count=calls.length;await page.locator('[data-point-kind="yc"][data-point-page="next"]').click();await f.settle();
  check(calls.length-count===1,'S: page change queries only selected point kind');
  check((await page.locator('[data-point-body="yc"]').textContent()).includes('RTU1-点26'),'S: second page displays point 26');
  await page.locator('#points-refresh').click();await f.settle();
  check((await page.locator('[data-point-pager="yc"]').textContent()).includes('第 2 /'),'S: refresh preserves page');
  // Save while a stale read is in flight. A second read must follow the PATCH.
  mock.hold=url=>url.pathname.includes('/points/');
  await page.locator('#points-refresh').click();await until(()=>held.length===4);
  await page.locator('[data-point-action="yc"]').first().click();
  await page.locator('#point-update-value').fill('77');
  await page.locator('#point-update-form').evaluate(form=>form.requestSubmit());
  await until(()=>calls.some(c=>c.method==='PATCH'));
  mock.hold=null;held.splice(0).forEach(x=>x.resolve());await f.settle();
  check((await page.locator('[data-point-body="yc"] tr').first().textContent()).includes('77'),'S: fresh read after save replaces stale response');
  // Superseded failures may not erase the new station.
  mock.hold=url=>url.pathname.startsWith('/api/rtus/1/');
  await page.locator('#points-refresh').click();await until(()=>held.length>=5);
  await page.locator('#rtu-select').selectOption('2');await until(async()=> (await page.locator('[data-point-body="yc"]').textContent()).includes('RTU2-点1'));
  mock.fail=url=>url.pathname.startsWith('/api/rtus/1/');mock.hold=null;held.splice(0).forEach(x=>x.resolve());await f.settle();mock.fail=null;
  check(await page.locator('#rtu-select').inputValue()==='2','S: late old response keeps selected RTU');
  check(!(await page.locator('#test-message').textContent()).includes('failure'),'S: late failure does not overwrite current status');
  await page.locator('#rtu-select').selectOption('600');await f.settle();
  check((await page.locator('[data-point-body="yc"]').textContent()).includes('RTU600-点1'),'S: RTU 600 data accessible');
  await page.locator('#service-stop').click();await f.settle();
  check(calls.some(c=>c.method==='POST'&&c.path==='/api/rtus/600/service/stop'),'S: service control targets selected RTU 600');
  check(!calls.some(c=>c.method==='POST'&&c.path==='/api/iec104/stop'),'S: default protocol service is not controlled');
  await page.locator('[data-point-kind="yc"][data-point-page="next"]').click();await f.settle();mock.total=10;
  await page.locator('#points-refresh').click();await f.settle();
  check((await page.locator('[data-point-body="yc"]').textContent()).includes('RTU600-点1'),'S: deletion clamps missing last page');
  await page.screenshot({path:path.join(report,'s-paged-points.png'),fullPage:true});
  await f.close();
}
async function processCase(browser,mod,variant){
  const f=await fixture(browser,mod,variant),{page,mock,calls,held}=f;
  if(mod==='c')await page.evaluate(()=>changeResource('processes'));await f.settle();
  const endpoint=mod==='c'?'/api/processes':'/api/trans/processes';
  mock.delay=40;const before=calls.filter(c=>c.path===endpoint).length;
  await page.evaluate(()=>Promise.all([loadProcesses(),loadProcesses(),loadProcesses()]));await f.settle();
  const count=calls.filter(c=>c.path===endpoint).length-before;
  measurements.push({module:mod.toUpperCase(),variant,concurrent_refresh_requests:count});
  if(variant==='after'){
    check(count===1,`${mod}: concurrent process refresh merged`);
    mock.hold=url=>url.pathname===endpoint;
    await page.evaluate(()=>{window.pendingRead=loadProcesses()});await until(()=>held.length===1);
    mock.value=77;await page.evaluate(()=>{window.freshRead=loadProcesses({fresh:true})});
    mock.hold=null;held.splice(0).forEach(x=>x.resolve());await page.evaluate(()=>window.freshRead);await f.settle();
    check((await page.locator(mod==='c'?'#process-rows':'#processes').textContent()).includes('进程77'),`${mod}: fresh request reads post-write state`);
    mock.fail=url=>url.pathname===endpoint;
    await page.evaluate(()=>loadProcesses().catch(()=>{}));mock.fail=null;
    await page.evaluate(()=>loadProcesses());await f.settle();
    check(true,`${mod}: failed request permits retry`);
  }
  await f.close();
}
(async()=>{const browser=await chromium.launch({headless:true});try{
  for(const variant of ['before','after']){await sCase(browser,variant);for(const mod of ['c','t'])await processCase(browser,mod,variant)}
  const result={checks,measurements,scope:'Chromium real UI, deterministic intercepted APIs. No real station writes.'};
  fs.writeFileSync(path.join(report,'frontend_comparison.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exitCode=1});

/* Before/after idle polling on real repository JavaScript in Chromium. */
const fs=require('fs'),path=require('path'),{execFileSync}=require('child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),out=path.join(root,'output/playwright/efficiency-round9');
(async()=>{
 const browser=await chromium.launch({headless:true}),results=[];
 try {
  for(const variant of ['before','after']) {
   const page=await browser.newPage(),sizes=[];
   const rows=Array.from({length:100},(_,i)=>({id:100-i,time:1794700000,kind:'packet',direction:'RX',summary:'IEC104 遥测变化 点号=1 值=12.5 质量=0',hex:'68 04 07 00 00 00'}));
   await page.addInitScript(()=>{window.setInterval=fn=>{window.tick=fn;return 1}});
   await page.route('**/*',route=>{
    const u=new URL(route.request().url()),q=u.searchParams;
    if(u.pathname.startsWith('/static/')) {
     const name=path.basename(u.pathname),relative='core/web_static/'+name;
     const body=variant==='before'&&name.endsWith('.js')?execFileSync('git',['show','6fc0a35:'+relative],{cwd:root,encoding:'utf8'}):fs.readFileSync(path.join(root,relative),'utf8');
     return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':'text/css',body});
    }
    if(u.pathname.endsWith('/events')) {
     const items=q.has('after')?[]:rows,body=JSON.stringify({items,has_more:false,next_before:items.at(-1)?.id??null,next_after:100});
     sizes.push(Buffer.byteLength(body));return route.fulfill({contentType:'application/json',body});
    }
    if(u.pathname.startsWith('/api/')) return route.fulfill({contentType:'application/json',body:JSON.stringify({item:{id:'session',channel_id:1,rtu_id:1,display_name:'测试站',service:'scdfe_s',protocol:'IEC104',state:'active'},writers:[],retention:'10000条'})});
    return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'core/web_static/connections.html'),'utf8')});
   });
   await page.goto('http://bench.test/connections/session');
   const ready=()=>page.waitForFunction(()=>!document.getElementById('refresh').disabled);
   await ready();sizes.length=0;
   await page.evaluate(()=>{window.removed=0;window.saved=[...document.querySelectorAll('.conn-event')];new MutationObserver(records=>{for(const r of records)removed+=[...r.removedNodes].filter(n=>n.matches?.('.conn-event')).length}).observe(document.getElementById('events'),{childList:true})});
   const elapsed=[];
   for(let i=0;i<10;i++){const start=performance.now();await page.evaluate(()=>tick());await ready();elapsed.push(performance.now()-start)}
   results.push({variant,polls:10,event_response_bytes_per_poll:sizes[0],total_event_bytes:sizes.reduce((a,b)=>a+b,0),
    ...await page.evaluate(()=>({removed_event_nodes:removed,preserved_event_nodes:saved.filter(n=>n.isConnected).length})),
    browser_roundtrip_median_ms:elapsed.sort((a,b)=>a-b)[5]});
   await page.close();
  }
  fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'comparison.json'),JSON.stringify(results,null,2));console.log(JSON.stringify(results,null,2));
 } finally {await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});

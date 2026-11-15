/* Repository connection page with isolated HTTP data in real Chromium. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),checks=[];
const report=path.join(root,'output/playwright/efficiency-round9');
function check(value,label){assert.ok(value,label);checks.push(label)}
(async()=>{
 fs.mkdirSync(report,{recursive:true});const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[];
  let count=120,hold=null,release=null,fail=false;
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.setInterval=fn=>{window.tick=fn;return 1}});
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.pathname.startsWith('/static/')) {
    const name=path.basename(url.pathname);
    return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':'text/css',body:fs.readFileSync(path.join(root,'core/web_static',name),'utf8')});
   }
   if(!url.pathname.startsWith('/api/')) return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'core/web_static/connections.html'),'utf8')});
   if(url.pathname.endsWith('/events')) {
    requests.push(url.search);
    const q=url.searchParams,after=Number(q.get('after')||0),before=Number(q.get('before')||1e9),limit=Number(q.get('limit'));
    let rows=Array.from({length:count},(_,i)=>({id:i+1,time:1794700000+i,kind:i%2?'packet':'log',summary:`内容 ${i+1} <script>invalid()</script>`,direction:'RX',hex:i%2?'68 04':''}));
    rows=rows.filter(x=>x.id>after&&x.id<before&&(!q.get('kind')||q.get('kind')===x.kind));
    if(!q.has('after')) rows.reverse();
    const has_more=rows.length>limit;rows=rows.slice(0,limit);if(q.has('after')) rows.reverse();
    const body=JSON.stringify({items:rows,has_more,next_before:rows.at(-1)?.id??null,next_after:rows[0]?.id??after});
    if(hold){release=hold;hold=null;await new Promise(resolve=>{release.resolve=resolve})}
    if(fail){fail=false;return route.fulfill({status:503,contentType:'application/json',body:'{"error":{"message":"temporary failure"}}'})}
    return route.fulfill({contentType:'application/json',body});
   }
   return route.fulfill({contentType:'application/json',body:JSON.stringify({item:{id:'session',channel_id:1,rtu_id:2,display_name:'IEC104测试站',service:'scdfe_s',protocol:'IEC104',peer:'127.0.0.1:6738',local:'127.0.0.1:2404',state:'active',opened:1794700000,updated:1794700010,rx_count:10,tx_count:20,rx_bytes:200,tx_bytes:300},writers:[],retention:'每连接最近10000条记录'})});
  });
  await page.goto('http://round9.test/connections/session');
  const ready=()=>page.waitForFunction(()=>!document.getElementById('refresh').disabled);
  await ready();check(await page.locator('.conn-event').count()===100,'initial page has newest 100');
  await page.evaluate(()=>{window.saved=document.querySelector('[data-event-id="120"]');window.summarySaved=document.querySelector('#summary').firstElementChild});
  await page.evaluate(()=>tick());await ready();
  check(requests.at(-1).includes('after=120'),'idle refresh uses forward cursor');
  check(await page.evaluate(()=>saved===document.querySelector('[data-event-id="120"]')&&summarySaved===document.querySelector('#summary').firstElementChild),'idle refresh preserves event and summary nodes');
  count=370;await page.evaluate(()=>tick());await ready();
  let ids=await page.locator('.conn-event').evaluateAll(nodes=>nodes.map(n=>Number(n.dataset.eventId)));
  check(ids.length===350&&new Set(ids).size===350&&ids.every((id,i)=>id===370-i),'250-event burst drains all pages in order without duplicates');
  check(await page.evaluate(()=>saved===document.querySelector('[data-event-id="120"]')),'new events preserve previous event nodes');
  check(await page.locator('#events script').count()===0,'log text remains escaped');
  await page.locator('#older').click();await ready();
  check(await page.locator('.conn-event').count()===370,'older pagination preserves live records');
  const frozen=requests.length;count=380;await page.evaluate(()=>tick());await ready();
  check(requests.length===frozen,'history browsing freezes automatic polling');
  await page.locator('#latest').click();await ready();check(await page.locator('.conn-event').count()===100,'return to latest resets window');
  hold={};count=390;await page.evaluate(()=>tick());
  while(!release) await new Promise(resolve=>setTimeout(resolve,10));
  await page.locator('#kind').selectOption('log');release.resolve();await ready();
  check((await page.locator('.conn-event').evaluateAll(nodes=>nodes.map(n=>Number(n.dataset.eventId)))).every(id=>id%2===1),'filter change rejects obsolete in-flight page');
  count=400;fail=true;await page.evaluate(()=>tick());await ready();
  check((await page.locator('#message').textContent()).includes('temporary failure'),'failed poll is visible');
  await page.evaluate(()=>tick());await ready();
  check((await page.locator('.conn-event').first().getAttribute('data-event-id'))==='399','failed poll retries without cursor loss');
  await page.locator('#kind').selectOption('');await ready();
  count=2800;for(let n=0;n<5;n++){await page.evaluate(()=>tick());await ready();}
  ids=await page.locator('.conn-event').evaluateAll(nodes=>nodes.map(n=>Number(n.dataset.eventId)));
  check(ids.length===2000&&ids[0]===2800&&ids.at(-1)===801&&new Set(ids).size===2000,'large burst resumes across polls and bounds DOM to latest 2000');
  check(!(await page.locator('#older').isDisabled()),'evicted older rows remain pageable');
  check(errors.length===0,'no browser script errors');
  await page.screenshot({path:path.join(report,'connections.png'),fullPage:false});
  fs.writeFileSync(path.join(report,'frontend.json'),JSON.stringify({checks,requests},null,2));
  console.log(JSON.stringify({checks},null,2));
 } finally {await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});

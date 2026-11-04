async page => {
 const base='http://127.0.0.1:8881';
 await page.unroute('**/static/app.js');
 const sources={before:await(await page.request.get(base+'/static/app.before.js')).text(),after:await(await page.request.get(base+'/static/app.js')).text()};
 await page.addInitScript(()=>{
  if (window.__hmiNetwork) return;
  window.__hmiNetwork=[]; const fetchOriginal=window.fetch;
  window.fetch=async(...args)=>{const r={path:String(args[0]),start:performance.now()};window.__hmiNetwork.push(r);try{return await fetchOriginal(...args);}finally{r.end=performance.now();}};
  const observer=new MutationObserver(()=>{if(document.querySelector('#table-body tr[data-row]')){window.__firstRowsMs=performance.now();observer.disconnect();}});
  observer.observe(document,{childList:true,subtree:true});
 });
 const results=[];
 for(const delay of [0,120]){
  for(let repeat=1;repeat<=5;repeat++)for(const variant of repeat%2?['before','after']:['after','before']){
   await page.route('**/static/app.js',route=>route.fulfill({contentType:'application/javascript',body:sources[variant]}));
   await page.route('**/api/**',async route=>{if(delay)await new Promise(r=>setTimeout(r,delay));await route.continue();});
   await page.goto(base+'/?boot='+delay+'-'+repeat+'-'+variant+'#rtus');
   await page.waitForFunction(()=>window.__firstRowsMs&&document.querySelector('#rtu-options option'));
   await page.locator('#auto-refresh').uncheck();
   const result=await page.evaluate(()=>({elapsed_ms:window.__firstRowsMs,requests:window.__hmiNetwork}));
   results.push({repeat,variant,added_request_delay_ms:delay,...result});
   await page.unroute('**/static/app.js');await page.unroute('**/api/**');
  }
 }
 return {results};
}

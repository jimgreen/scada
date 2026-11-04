async page => {
 const base='http://127.0.0.1:8881';
 const before=await (await page.request.get(base+'/static/app.before.js')).text();
 const after=await (await page.request.get(base+'/static/app.js')).text();
 const fixture=await (await page.request.get(base+'/api/points/yc?limit=500')).json();
 const results=[],errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.setViewportSize({width:1440,height:900});
 for(let repeat=1;repeat<=5;repeat++) {
  for(const variant of repeat%2?['before','after']:['after','before']) {
   await page.unroute('**/static/app.js');
   await page.route('**/static/app.js',route=>route.fulfill({contentType:'application/javascript',body:variant==='before'?before:after}));
   await page.goto(base+'/?benchmark='+variant+'-'+repeat+'#yc');
   await page.waitForFunction(()=>state.items.length===50&&!state.loading);
   await page.locator('#auto-refresh').uncheck();
   const cases=await page.evaluate(async ({fixture,repeat,variant})=>{
    const out=[];
    for(const [name,view,count,changed] of [['common_stable_50','common',50,0],['all_stable_500','all',500,0],['all_sparse_500','all',500,5],['all_dense_500','all',500,500]]) {
     state.resource='yc';state.rtu='';state.view=view;state.offset=0;state.sort='';state.order='asc';state.limit=count;state.total=fixture.total;
     state.items=fixture.items.slice(0,count).map(x=>({...x}));showView();renderTable();
     const body=document.querySelector('#table-body'),head=document.querySelector('#table-head');void body.offsetHeight;
     const original=[...body.rows]; const observer=new MutationObserver(()=>{});observer.observe(body,{childList:true,subtree:true});
     let ms=0;
     for(let cycle=1;cycle<=6;cycle++){
      state.items=state.items.map((row,index)=>index<changed?{...row,value:row.value+1,raw_value:row.raw_value+1,status:cycle%4}:{...row});
      const start=performance.now();renderTable();void body.offsetHeight;ms+=performance.now()-start;
     }
     const mutations=observer.takeRecords().length;observer.disconnect();
     const sameRows=[...body.rows].filter((row,index)=>row===original[index]).length;
     const bytes=new TextEncoder().encode(head.innerHTML+body.innerHTML);
     const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),n=>n.toString(16).padStart(2,'0')).join('');
     out.push({repeat,variant,case:name,cycles:6,elapsed_ms:ms,retained_rows:sameRows,mutation_records:mutations,dom_sha256:digest});
    }
    document.querySelector('#data-panel').hidden=true;document.querySelector('#logs-panel').hidden=false;
    logs.entries=Array.from({length:5000},(_,i)=>({level:['INFO','WARNING','ERROR','PROCESS'][i%4],text:'10:00:00 [日志] 测点 '+i+' '+('详细采集记录 '.repeat(24)),filename:'2026/11/02/10/scdfe_c-rtu-1-20261102-10.log'}));
    logs.meta={keyword:'',skipped_lines:0};logs.nextCursor=null;
    document.querySelector('#log-follow').checked=false;document.querySelector('#log-scope').value='file';renderLogContent();
    const content=document.querySelector('#scdfe-log-content');void content.offsetHeight;const node=content.firstChild;
    let ms=0;
    for(let n=0;n<6;n++){const start=performance.now();document.querySelector('#log-wrap').checked=n%2===0;document.querySelector('#log-wrap').onchange();void content.scrollHeight;ms+=performance.now()-start;}
    out.push({repeat,variant,case:'log_wrap_5000',cycles:6,elapsed_ms:ms,retained_first_line:node===content.firstChild,lines:content.children.length});
    return out;
   },{fixture,repeat,variant});
   results.push(...cases);
   await page.unroute('**/static/app.js');
  }
 }
 const hashes=await page.evaluate(async ({before,after})=>{const out={};for(const [k,v] of Object.entries({before,after})){out[k]=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(v))),n=>n.toString(16).padStart(2,'0')).join('');}return out;},{before,after});
 return {source_sha256:hashes,browser:await page.context().browser().version(),viewport:{width:1440,height:900},fixture:{rtus:60,yc:60000,yx:60000,yk:12000,yt:12000},results,errors};
}

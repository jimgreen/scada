(()=>{
'use strict';
const $=id=>document.getElementById(id);
const esc=x=>String(x??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=x=>x?new Date(Number(x)).toLocaleString():'—';
const labels={queued:'已入库待派发',dispatching:'已开始派发，结果待确认',select_sent:'选择请求已提交',select_confirmed:'选择已确认',execute_sent:'执行请求已提交，结果待确认',protocol_confirmed:'规约已确认',driver_completed:'驱动调用完成',failed:'失败或未确认',received:'S 数据库已接收'};
const scopes={database_commit:'数据库提交',iec104_activation_confirmation:'IEC104 激活确认',modbus_response:'Modbus 响应',driver_return:'驱动返回',unconfirmed:'未确认',simulator_database:'模拟数据库'};
const conclusions={unknown:'仍待核对',observed_expected:'现场状态符合预期',observed_not_expected:'现场状态不符合预期'};
let before=null,next=null,busy=false,markup='',etag='',lastQuery='',rows=new Map(),component='c',rtus=[],ruleVersion=0,ruleGeneration=0,ruleBusy=false,reviewRow=null,detailRow=null,detailBusy=false,openingReview=false;
const base=new URLSearchParams(location.search);
for(const key of [...base.keys()])if(!['rtu_id','kind','pnt_no'].includes(key))base.delete(key);
if(base.has('rtu_id'))$('rtu').add(new Option('RTU '+base.get('rtu_id'),base.get('rtu_id')));
$('rtu').value=base.get('rtu_id')||'';$('kind').value=base.get('kind')||'';
const controls=['refresh','older','stage','rtu','kind','keyword','from','to','filter','clear'];
async function json(url,options={}){
 const response=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(12000),...options});
 const data=await response.json();if(!response.ok)throw Error(data.error?.message||'HTTP '+response.status);return data;
}
function parameters(){
 const q=new URLSearchParams({view:'summary'});
 for(const [id,key] of [['rtu','rtu_id'],['kind','kind'],['stage','stage'],['keyword','q']])if($(id).value.trim())q.set(key,$(id).value.trim());
 if(base.has('pnt_no'))q.set('pnt_no',base.get('pnt_no'));
 for(const id of ['from','to'])if($(id).value){const n=new Date($(id).value).getTime();if(!Number.isFinite(n))throw Error('请输入有效时间');q.set(id,n)}
 if(q.has('from')&&q.has('to')&&Number(q.get('from'))>Number(q.get('to')))throw Error('起始时刻晚于结束时刻');
 if(before)q.set('before',before);return q.toString();
}
function logLink(x){
 if(component==='c')return '/?'+new URLSearchParams({log_rtu:x.rtu_id,log_query:x.cmd_time})+'#logs';
 if(component==='s'){
  const q=new URLSearchParams();
  if(x.channel_id)q.set('channel_id',String(x.channel_id));
  if(x.cmd_time)q.set('q',String(x.cmd_time));
  return '/logs'+(q.size?'?'+q:'');
 }
 const tq=new URLSearchParams();
 if(x.channel_id)tq.set('channel_id',String(x.channel_id));
 if(x.cmd_time)tq.set('q',String(x.cmd_time));
 return '/logs.html'+(tq.size?'?'+tq:'');
}
async function load(){
 if(busy)return;busy=true;controls.forEach(id=>$(id).disabled=true);
 try{
  const query=parameters();const headers=query===lastQuery&&etag?{'If-None-Match':etag}:{};
  const response=await fetch('/api/command-traces?'+query,{cache:'no-store',headers,signal:AbortSignal.timeout(12000)});
  if(response.status===304){$('message').textContent='已核对，无新增变化 · '+new Date().toLocaleTimeString();return}
  const data=await response.json();if(!response.ok)throw Error(data.error?.message||'HTTP '+response.status);
  component=data.component||component;rows=new Map(data.items.map(x=>[String(x.command_id),x]));
  const html=data.items.map(x=>'<tr><td>'+esc(x.command_id)+'</td><td>'+esc(x.rtu_name||'RTU '+x.rtu_id)+'<small>'+esc(x.point_name||x.kind.toUpperCase()+' / '+x.pnt_no)+'</small></td><td>'+esc(x.value)+'</td><td>'+esc(labels[x.stage]||x.stage)+'<small>'+esc(scopes[x.confirmation_scope]||x.confirmation_scope)+'</small></td><td>'+fmt(x.cmd_time)+'</td><td>'+fmt(x.finish_time)+'</td><td class="command-notes">'+esc(x.feedback_label||x.feedback_state||'未关联回采验证')+
   (x.has_error?' · 有错误，详见记录':'')+(x.review_time?' · 已有人工核对':'')+(x.resolution_time?' · 已人工结案（未重发）':'')+'</td><td class="actions-cell"><div class="process-actions"><button data-detail="'+esc(x.command_id)+'">详情 / 结案</button><a href="'+esc(logLink(x))+'">'+(component==='t'?'转发日志':'相关日志')+'</a><button data-review="'+esc(x.command_id)+'">人工核对</button>'+(component!=='s'?'<button data-rule="'+esc(x.command_id)+'">回采关联</button>':'')+'</div></td></tr>').join('')||'<tr><td colspan="8">暂无指令记录</td></tr>';
  if(markup!==html){$('commands').innerHTML=html;markup=html}
  etag=response.headers.get('ETag')||'';lastQuery=query;next=data.has_more?data.next_before:null;
  $('message').textContent='更新于 '+new Date().toLocaleTimeString();
 }catch(error){$('message').textContent=error.message}
 finally{busy=false;controls.forEach(id=>$(id).disabled=false);$('older').disabled=!next}
}
function latest(){if(!busy){before=null;load()}}
$('refresh').onclick=$('filter').onclick=latest;
$('stage').onchange=latest;
for(const id of ['rtu','kind'])$(id).onchange=()=>{base.delete('pnt_no');latest()};
$('keyword').onkeydown=event=>{if(event.key==='Enter')latest()};
$('clear').onclick=()=>{if(!busy){base.delete('pnt_no');for(const id of ['rtu','kind','stage','keyword','from','to'])$(id).value='';latest()}};
$('older').onclick=()=>{if(!busy&&next){before=next;load()}};
function rtuOptions(select,empty=false){
 const previous=select.value;select.replaceChildren();
 if(empty)select.add(new Option('全部 RTU',''));else select.add(new Option('请选择 RTU',''));
 for(const row of rtus)select.add(new Option(row.name||'未命名 RTU '+row.rtu_id,String(row.rtu_id)));
 select.value=previous;
}
async function initOptions(){
 try{
  const data=await json('/api/command-options');rtus=data.items;component=data.component||component;
  for(const id of ['rtu','rule-rtu','feedback-rtu'])rtuOptions($(id),id==='rtu');
  $('configure').hidden=component==='s';$('configure').disabled=component==='s';
 }catch(error){$('message').textContent='名称列表读取失败：'+error.message}
}
async function pointOptions(target,rid,kind,keyword='',selected=''){
 const select=$(target);select.replaceChildren(new Option('请选择测点',''));
 if(!rid)return;
 const data=await json('/api/command-options?'+new URLSearchParams({rtu_id:rid,kind,q:keyword}));
 for(const row of data.items)select.add(new Option((row.name||'未命名测点')+' · '+row.pnt_no,String(row.pnt_no)));
 select.value=String(selected);
 if(data.has_more)$('rule-message').textContent='点数较多，请按名称或点号查找后选择。';
}
async function loadFeedbackPoints(selected=''){
 await pointOptions('feedback-point',$('feedback-rtu').value,$('rule-kind').value==='yk'?'yx':'yc',$('feedback-query').value,selected);
}
async function loadRule(){
 ruleVersion=0;
 for(const [id,value] of [['gain',1],['offset',0],['tolerance',0],['timeout-ms',5000]])$(id).value=value;
 $('rule-enabled').checked=true;$('feedback-query').value='';
 $('feedback-rtu').value=$('rule-rtu').value;
 if($('rule-point').value){
  const data=await json('/api/command-feedback?'+new URLSearchParams({rtu_id:$('rule-rtu').value,kind:$('rule-kind').value,pnt_no:$('rule-point').value}));
  if(data.item){
   const r=data.item;ruleVersion=r.version;
   $('feedback-rtu').value=String(r.feedback_rtu_id);$('gain').value=r.gain;$('offset').value=r.offset;
   $('tolerance').value=r.tolerance;$('timeout-ms').value=r.timeout_ms;$('rule-enabled').checked=!!r.enabled;
   // Exact point lookup also finds selected targets beyond the first 1000.
   $('feedback-query').value=String(r.feedback_pnt_no);await loadFeedbackPoints(r.feedback_pnt_no);return;
  }
 }
 await loadFeedbackPoints();
}
async function ruleWork(task){
 if(ruleBusy)return;ruleBusy=true;
 const generation=++ruleGeneration;$('rule-message').textContent='';
 const controls=[...$('rule-form').elements];controls.forEach(x=>x.disabled=true);
 try{await task()}catch(error){if(generation===ruleGeneration)$('rule-message').textContent=error.message}
 finally{if(generation===ruleGeneration)controls.forEach(x=>x.disabled=false);ruleBusy=false}
}
async function openRule(row=null){
 if(component==='s'||ruleBusy)return;
 $('rule-dialog').showModal();
 await ruleWork(async()=>{
  $('rule-query').value=row?String(row.pnt_no):'';
  $('rule-rtu').value=row?String(row.rtu_id):$('rtu').value;$('rule-kind').value=row?row.kind:($('kind').value||'yk');
  await pointOptions('rule-point',$('rule-rtu').value,$('rule-kind').value,$('rule-query').value,row?.pnt_no||'');
  await loadRule();
 });
}
$('configure').onclick=()=>openRule();
for(const id of ['rule-rtu','rule-kind'])$(id).onchange=()=>ruleWork(async()=>{
 $('rule-query').value='';await pointOptions('rule-point',$('rule-rtu').value,$('rule-kind').value);await loadRule();
});
$('rule-find').onclick=()=>ruleWork(async()=>{await pointOptions('rule-point',$('rule-rtu').value,$('rule-kind').value,$('rule-query').value);await loadRule()});
$('rule-point').onchange=()=>ruleWork(loadRule);
$('feedback-rtu').onchange=()=>ruleWork(async()=>{$('feedback-query').value='';await loadFeedbackPoints()});
$('feedback-find').onclick=()=>ruleWork(()=>loadFeedbackPoints());
$('rule-close').onclick=()=>{$('rule-dialog').close()};
$('rule-dialog').oncancel=event=>{if(ruleBusy)event.preventDefault()};
$('rule-form').onsubmit=event=>{
 event.preventDefault();ruleWork(async()=>{
  const body={rtu_id:Number($('rule-rtu').value),kind:$('rule-kind').value,pnt_no:Number($('rule-point').value),
   feedback_rtu_id:Number($('feedback-rtu').value),feedback_pnt_no:Number($('feedback-point').value),
   gain:Number($('gain').value),offset:Number($('offset').value),tolerance:Number($('tolerance').value),
   timeout_ms:Number($('timeout-ms').value),enabled:$('rule-enabled').checked,version:ruleVersion};
  await json('/api/command-feedback',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  $('rule-dialog').close();latest();
 });
};
$('commands').onclick=async event=>{
 const review=event.target.closest('[data-review]'),rule=event.target.closest('[data-rule]'),detail=event.target.closest('[data-detail]');
 if(detail){openDetail(rows.get(detail.dataset.detail));return}
 if(rule){openRule(rows.get(rule.dataset.rule));return}
 if(!review||openingReview||$('review-save').disabled)return;
 openingReview=true;
 try{reviewRow=(await json('/api/command-traces/'+encodeURIComponent(review.dataset.review))).item}
 catch(error){$('message').textContent=error.message;return}finally{openingReview=false}
 $('review-target').textContent='命令 '+reviewRow.command_id+' · '+(reviewRow.rtu_name||reviewRow.rtu_id)+' · '+(reviewRow.point_name||reviewRow.kind);
 $('review-conclusion').value=reviewRow.review_conclusion||'unknown';$('review-actor').value=reviewRow.review_actor||'';
 $('review-note').value=reviewRow.review_note||'';$('review-message').textContent='';$('review-dialog').showModal();
};
$('review-close').onclick=()=>{if(!$('review-save').disabled)$('review-dialog').close()};
$('review-dialog').oncancel=event=>{if($('review-save').disabled)event.preventDefault()};
$('review-form').onsubmit=async event=>{
 event.preventDefault();$('review-save').disabled=true;
 try{
  const body={stage:reviewRow.stage,review_time:reviewRow.review_time||0,conclusion:$('review-conclusion').value,note:$('review-note').value,actor:$('review-actor').value};
  await json('/api/command-traces/'+encodeURIComponent(reviewRow.command_id)+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  $('review-dialog').close();latest();
 }catch(error){$('review-message').textContent=error.message}finally{$('review-save').disabled=false}
};
function renderDetail(x){
 const fields=[['命令',x.command_id],['RTU / 点',`${x.rtu_name||x.rtu_id} / ${x.point_name||x.pnt_no}`],['命令值',x.value],['规约阶段',labels[x.stage]||x.stage],['确认范围',scopes[x.confirmation_scope]||x.confirmation_scope],['入库 / 接收',fmt(x.cmd_time)],['派发',fmt(x.dispatch_time)],['选择 / 确认',`${fmt(x.select_time)} / ${fmt(x.select_ack_time)}`],['执行请求',fmt(x.execute_time)],['完成',fmt(x.finish_time)],['错误',x.error||'—'],['回采状态',x.feedback_label],['回采点',x.feedback_kind?`${x.feedback_rtu_name||x.feedback_rtu_id} / ${x.feedback_point_name||x.feedback_pnt_no}`:'—'],['预期 / 偏差',x.feedback_kind?`${x.feedback_expected} ± ${x.feedback_tolerance}`:'—'],['回采 / 质量',x.feedback_kind?`${x.feedback_value??'—'} / ${x.feedback_quality??'—'}`:'—'],['接收 / 截止',`${fmt(x.feedback_received_time)} / ${fmt(x.feedback_deadline)}`],['人工核对',x.review_time?`${conclusions[x.review_conclusion]} · ${x.review_actor} · ${fmt(x.review_time)}\n${x.review_note}`:'—'],['人工结案',x.resolution_time?`${x.resolution_actor} · ${fmt(x.resolution_time)}\n${x.resolution_note}\n旧命令未重发，规约结果未被改写`:'未结案']];
 $('detail-content').innerHTML=fields.map(([name,value])=>'<div><strong>'+esc(name)+'</strong><span>'+esc(value)+'</span></div>').join('');
 $('resolve-form').hidden=component==='s'||!['dispatching','select_sent','select_confirmed','execute_sent'].includes(x.stage)||!!x.resolution_time;
 $('resolve-blocker').textContent=x.resolution_blocker||'原派发进程已退出；提交时会再次核实状态。';
 $('resolve-save').disabled=!!x.resolution_blocker;
}
async function openDetail(row){
 if(detailBusy)return;detailBusy=true;detailRow=null;
 $('detail-dialog').showModal();$('detail-message').textContent='正在读取…';$('detail-content').replaceChildren();$('resolve-form').hidden=true;
 $('resolve-actor').value='';$('resolve-note').value='';$('resolve-confirm').checked=false;
 try{detailRow=(await json('/api/command-traces/'+encodeURIComponent(row.command_id))).item;renderDetail(detailRow);$('detail-message').textContent=''}
 catch(error){$('detail-message').textContent=error.message}finally{detailBusy=false}
}
$('detail-close').onclick=()=>{if(!detailBusy)$('detail-dialog').close()};
$('detail-dialog').oncancel=event=>{if(detailBusy)event.preventDefault()};
$('resolve-form').onsubmit=async event=>{
 event.preventDefault();if(detailBusy||!detailRow)return;detailBusy=true;$('resolve-save').disabled=true;
 try{
  await json('/api/command-traces/'+detailRow.command_id+'/resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stage:detailRow.stage,cmd_time:detailRow.cmd_time,actor:$('resolve-actor').value,note:$('resolve-note').value,confirm_no_replay:$('resolve-confirm').checked})});
  $('resolve-form').hidden=true;$('detail-message').textContent='已结案，旧命令未重发。';
  detailRow=(await json('/api/command-traces/'+detailRow.command_id)).item;renderDetail(detailRow);latest();
 }catch(error){$('detail-message').textContent=error.message;$('resolve-save').disabled=false}finally{detailBusy=false}
};
setInterval(()=>{if(!document.hidden&&!before&&!openingReview&&!document.querySelector('dialog[open]'))load()},5000);
load();initOptions();
})();

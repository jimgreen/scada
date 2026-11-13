"use strict";
const $=s=>document.querySelector(s);
const esc=x=>String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;", "'":"&#39;"}[c]));
const state={channels:[],points:[],selected:new URLSearchParams(location.search).get("channel_id")||null,logs:[],log:null,logRecord:"runtime",page:"channels",edit:null,batch:false,sourceCatalog:null,pointOffset:0,pointLimit:100,pointTotal:0,pointRequest:0,pointLoading:false,pointReloadPending:false};
const kindName={yc:"遥测",yx:"遥信",yk:"遥控",yt:"遥调"};
const pointKinds=Object.keys(kindName);
function activePointKind(){return $("#kind-filter")?.value||"yc"}
function pointTableBody(kind=activePointKind()){
  if(typeof document.querySelectorAll==="function")for(const page of document.querySelectorAll("[data-point-page]"))if(page.dataset.pointPage===kind)return page.querySelector("[data-points-body]");
  return $("#points");
}
function pointTableHead(kind=activePointKind()){
  if(typeof document.querySelectorAll==="function")for(const page of document.querySelectorAll("[data-point-page]"))if(page.dataset.pointPage===kind)return page.querySelector("[data-points-head]");
  return $("#points-head");
}
function setPointTab(kind){
  if(!pointKinds.includes(kind))kind="yc";
  const filter=$("#kind-filter");if(filter)filter.value=kind;
  if(typeof document.querySelectorAll!=="function")return;
  document.querySelectorAll("[data-point-tab]").forEach(tab=>{const active=tab.dataset.pointTab===kind;tab.setAttribute("aria-selected",active?"true":"false");tab.classList.toggle("active",active)});
  document.querySelectorAll("[data-point-page]").forEach(page=>{const active=page.dataset.pointPage===kind;page.hidden=!active;page.classList.toggle("active",active);const body=page.querySelector("[data-points-body]");if(body){if(active)body.id="points";else if(body.id==="points")body.removeAttribute("id")} });
}
const statusName={0:"停止",1:"启动中",2:"运行",3:"停止中",4:"处理中",5:"错误"};
async function api(path,opt={}){const r=await fetch(path,{...opt,headers:{"Content-Type":"application/json",...(opt.headers||{})}});let v;try{v=await r.json()}catch(_){throw Error(`HTTP ${r.status}`)}if(!r.ok)throw Error(v.error?.message||r.statusText);return v}
const versionHeaders=v=>v==null?{}:{"If-Match":String(v)};
const send=(path,method,body,version)=>api(path,{method,headers:versionHeaders(version),body:JSON.stringify(body)});
function toast(m){const e=$("#toast");e.textContent=m;clearTimeout(state.toastTimer);state.toastTimer=setTimeout(()=>e.textContent="",4500)}
function fmt(v,timeOnly=false){if(!v)return "—";const d=new Date(Number(v));if(Number.isNaN(d.valueOf()))return String(v);const p=n=>String(n).padStart(2,"0"),t=`${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;return timeOnly?t:`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${t}`}
function id(r){return String(r.channel_id)}
function selected(){return state.channels.find(x=>id(x)===state.selected)}
function badge(r){const s=Number(r.process_status||0);return `<span class="status-badge ${s===2?"running":s===5?"error":""}">${esc(statusName[s]||s)}</span>`}
function runtimeBadge(r){const names={unknown:"未知",stopped:"停止",starting:"启动中",running:"运行中",stopping:"停止中",error:"错误",degraded:"降级"};const state=String(r?.state||"unknown");return `<span class="status-badge ${state==="running"?"running":state==="error"?"error":""}">${esc(names[state]||state)}</span>`}
function setPage(p){state.page=p;document.querySelectorAll(".page").forEach(x=>x.classList.toggle("active",x.id===`page-${p}`));document.querySelectorAll("nav button[data-page]").forEach(x=>x.classList.toggle("active",x.dataset.page===p));if(p==="processes")loadProcesses().catch(e=>toast(e.message));if(p==="logs")loadLogs().catch(e=>toast(e.message))}
function setOptions(node,markup){if(node._optionsMarkup!==markup){node.innerHTML=markup;node._optionsMarkup=markup;}}
function renderSelects(){const s=$("#selected-channel"),l=$("#log-channel"),old=l?.value||"";if(s)setOptions(s,'<option value="">请选择</option>'+state.channels.map(x=>`<option value="${id(x)}">${id(x)} · ${esc(x.name||"")} · ${esc(x.protocol||"")}</option>`).join(""));if(s)s.value=state.selected||"";if(!l)return;setOptions(l,'<option value="">全部通道</option>'+state.channels.map(x=>`<option value="${id(x)}">${id(x)} · ${esc(x.name||"")}</option>`).join(""));l.value=state.logsChannel||old}
function renderChannels() {
  const body = $("#channels");
  if (!body) { renderSelects(); return; }
  const rows = state.channels;
  $("#channel-count").textContent = `${rows.length} 个通道`;
  $("#summary").textContent = `运行 ${rows.filter(x => x.runtime_status?.state === "running" || Number(x.runtime_status?.running) === 1 || Number(x.process_status) === 2).length} / 总数 ${rows.length}`;
  const markup = rows.length ? rows.map(x => { const r=x.runtime_status||{}; const d=r.diagnostic||{}; return `<tr class="${id(x) === state.selected ? "selected" : ""}" data-select="${id(x)}"><td>${id(x)}</td><td>${esc(x.name)}</td><td>${esc(x.protocol)}</td><td>${runtimeBadge(r)}</td><td>${esc(r.connection_state || "—")}</td><td class="mono">${esc([r.rx_count||0,r.tx_count||0,r.error_count||0,r.reconnect_count||0].join(" / "))}</td><td class="mono">${esc(fmt(r.process_heartbeat_time||x.process_heartbeat_time,true))}<small class="cell-sub">${esc(fmt(r.sampled_at,true))}</small></td><td title="${esc(JSON.stringify(d))}">${esc(Object.keys(d).length ? `${Object.keys(d).length} 项` : "—")}<small class="cell-sub">${esc(r.last_error||x.process_error||"")}</small></td><td class="actions"><a href="/points.html?channel_id=${id(x)}">四遥数据</a><a href="/logs.html?channel_id=${id(x)}">日志报文</a><button data-edit="${id(x)}">编辑</button><button data-version="${esc(x._version)}" data-delete="${id(x)}" class="danger">删除</button></td></tr>`; }).join("") : '<tr><td colspan="9" class="muted">暂无转发通道</td></tr>';
  // Heartbeats and counters change on every poll. Preserve action nodes and
  // their focus while updating cells; a changed row identity rebuilds safely.
  if (body._channelMarkup !== markup) {
    const keys=rows.map(id).join(','), next=markup.match(/<tr\b[\s\S]*?<\/tr>/g)||[];
    const cells=text=>text.match(/<td\b[\s\S]*?<\/td>/g)||[];
    if(rows.length && keys===body._channelKeys && body.rows.length===rows.length){
      next.forEach((row,i)=>{
        if(row===body._channelRows[i])return;
        const old=cells(body._channelRows[i]);
        cells(row).forEach((cell,j)=>{
          if(cell===old[j])return;
          if(j===8 && cell.replace(/ data-version="[^"]*"/g,'')===old[j].replace(/ data-version="[^"]*"/g,''))
            body.rows[i].querySelector('[data-delete]').dataset.version=String(rows[i]._version);
          else body.rows[i].cells[j].outerHTML=cell;
        });
        body.rows[i].classList.toggle('selected',id(rows[i])===state.selected);
      });
    }else body.innerHTML=markup;
    body._channelKeys=keys;body._channelRows=next;body._channelMarkup=markup;
  }
  renderSelects(); renderDetail();
}
function renderDetail(){if(!$("#channel-detail"))return;const x=selected();if(!x){$("#channel-detail").innerHTML='<dt>通道</dt><dd>未选择</dd>';return}const protocol=String(x.protocol||"").toLowerCase(),r=x.runtime_status||{};const common={channel_id:"编号",name:"名称",protocol:"规约",bind_host:"监听地址",bind_port:"监听端口",scan_interval_ms:"读取周期(ms)",log_retention_hours:"日志保留小时",packet_recording:"保存报文",process_enable:"自动运行",process_pid:"进程号",process_start_time:"启动时间",process_stop_time:"停止时间",process_heartbeat_time:"心跳",process_error:"错误"};const specific=protocol==="iec104"?{common_address:"IEC104 公共地址",report_interval_ms:"全量上送周期(ms)"}:{unit_id:"Modbus 从站地址"};const n={...common,...specific};const fields=Object.entries(n).filter(([k])=>k in x).map(([k,v])=>`<dt>${v}</dt><dd>${k.includes("time")?fmt(x[k],k.includes("heartbeat")):k==="process_enable"||k==="packet_recording"?(Number(x[k])?"是":"否"):esc(x[k])||"—"}`).join("");$("#channel-detail").innerHTML=fields+`<dt>运行状态</dt><dd>${runtimeBadge(r)}</dd><dt>连接状态</dt><dd>${esc(r.connection_state||"—")}</dd><dt>收/发/错/重连</dt><dd class="mono">${esc([r.rx_count||0,r.tx_count||0,r.error_count||0,r.reconnect_count||0].join(" / "))}</dd><dt>最近错误</dt><dd>${esc(r.last_error||"—")}</dd><dt>诊断</dt><dd class="mono">${esc(JSON.stringify(r.diagnostic||{}))}</dd>`}
function pno(x){return x.pnt_no??0}
let previousRows = null;
function renderPoints(){
  const r=state.points, kind=activePointKind(), body=pointTableBody(kind), protocol=String(selected()?.protocol||"iec104").toLowerCase();
  if(!body)return;
  const head=pointTableHead(kind);
  if(head){const markup=["类型","转发点号","源 RTU","源点号","名称",protocol==="iec104"?"IEC IOA":"Modbus 区/地址","实时值","状态","刷新时间","指令接收时间","最后转发","启用","操作"].map(x=>`<th>${x}</th>`).join("");if(head._pointMarkup!==markup){head.innerHTML=markup;head._pointMarkup=markup;}}
  const rows=r.map(x=>({owner:String(x.channel_id),kind:x.kind,no:String(pno(x)),version:String(x._version),
    cells:[kindName[x.kind]||x.kind,pno(x),x.source_rtu_id,x.source_pnt_no,x.name,
      protocol==="iec104"?(x.io_address??"—"):(x.register_area??"—")+" / "+(x.address??"—"),
      x.value??"—",x.status??"—",fmt(x.refresh_time),fmt(x.command_receive_time),fmt(x.forward_time),
      Number(x.enabled??1)?"是":"否"].map(v=>String(v??""))}));
  const same=previousRows && rows.length===previousRows.length && rows.every((x,i)=>{
    const old=previousRows[i];return x.owner===old.owner && x.kind===old.kind && x.no===old.no;
  });
  if (!same) {
    body.innerHTML=rows.map(x=>`<tr>${x.cells.map(v=>`<td>${esc(v)}</td>`).join("")}<td class="actions"><button data-point-edit="${esc(x.no)}" data-point-kind="${esc(x.kind)}">编辑</button><button data-owner="${esc(x.owner)}" data-version="${esc(x.version)}" data-point-delete="${esc(x.no)}" data-point-kind="${esc(x.kind)}" class="danger">删除</button></td></tr>`).join("")||'<tr><td colspan="13" class="muted">暂无点映射</td></tr>';
  } else {
    rows.forEach((x,i)=>{
      const old=previousRows[i];
      x.cells.forEach((value,j)=>{if(value!==old.cells[j]) body.rows[i].cells[j].textContent=value;});
      if(x.version!==old.version) body.rows[i].querySelector("[data-point-delete]").dataset.version=x.version;
    });
  }
  previousRows=rows;
  if($("#point-page-summary"))$("#point-page-summary").textContent=`共 ${state.pointTotal} 条 · 当前 ${r.length?state.pointOffset+1:0}–${state.pointOffset+r.length} 条`;if($("#point-prev"))$("#point-prev").disabled=state.pointOffset===0;if($("#point-next"))$("#point-next").disabled=state.pointOffset+r.length>=state.pointTotal}
let channelRequest = 0, pendingChannels = 0, channelRead = null;
function loadChannels({loadSelectedPoints = false, fresh = false} = {}) {
  if (channelRead) {
    if (fresh) return channelRead.promise.catch(() => {}).then(() => loadChannels({loadSelectedPoints}));
    channelRead.loadSelectedPoints ||= loadSelectedPoints;
    return channelRead.promise;
  }
  const pending = {loadSelectedPoints};
  pending.promise = performLoadChannels(pending).finally(() => { if (channelRead === pending) channelRead = null; });
  channelRead = pending;
  return pending.promise;
}
async function performLoadChannels(pending) {
  const request = ++channelRequest;
  pendingChannels++;
  try {
    const v = await api(pointTableBody() && !$("#channels") ? "/api/trans/channels?view=selector" : "/api/trans/status");
    if (request !== channelRequest) return;
    const previous = state.selected;
    state.channels = v.items || [];
    if (state.selected && !state.channels.some(x => id(x) === state.selected)) state.selected = null;
    const requested = new URLSearchParams(location.search).get("channel_id");
    if (!state.selected) state.selected = requested ? (state.channels.some(x => id(x) === requested) ? requested : null) : (state.channels.length ? id(state.channels[0]) : null);
    if (previous !== state.selected) state.pointOffset = 0;
    renderChannels();
    if (pending.loadSelectedPoints || previous !== state.selected) await loadPoints();
  } catch (error) { if (request === channelRequest) throw error; }
  finally { pendingChannels--; }
}
async function loadPoints({background=false}={}){
  // Polls join the current read; explicit edits/navigation still request one
  // trailing read, so a save during an in-flight GET cannot lose its refresh.
  if(state.pointLoading){if(!background)state.pointReloadPending=true;return}
  state.pointLoading=true;
  try{
    const kind=activePointKind(),body=pointTableBody(kind);if(!body)return;setPointTab(kind);
    const request=++state.pointRequest;
    if(!state.selected){state.points=[];state.pointTotal=0;state.pointOffset=0;renderPoints();return}
    const cid=state.selected,offset=state.pointOffset,limit=state.pointLimit,q=new URLSearchParams({limit:String(limit),offset:String(offset),kind});
    const current=()=>request===state.pointRequest&&cid===state.selected&&kind===activePointKind()&&offset===state.pointOffset&&limit===state.pointLimit;
    const scope=cid+"/"+kind;
    if(state.pointScope!==scope){state.pointScope=scope;state.points=[];state.pointTotal=0;renderPoints();previousRows=null;body.innerHTML='<tr><td colspan="13">正在读取点映射…</td></tr>'}
    let page;
    try{page=await api(`/api/trans/channels/${cid}/points?${q}`)}catch(error){if(!current())return;throw error}
    if(!current())return;
    state.pointTotal=page.total??0;
    if(offset>0&&offset>=state.pointTotal){state.pointOffset=Math.max(0,Math.floor((state.pointTotal-1)/state.pointLimit)*state.pointLimit);state.pointReloadPending=true;return}
    state.points=page.items||[];renderPoints()
  }finally{
    state.pointLoading=false;
    if(state.pointReloadPending){state.pointReloadPending=false;queueMicrotask(()=>loadPoints().catch(e=>toast(e.message)))}
  }
}
async function control(cid,a){await api(`/api/trans/channels/${cid}/${a}`,{method:"POST",body:"{}"});toast(a==="start"?"启动请求已发送":"停止请求已发送");await loadChannels({loadSelectedPoints:true,fresh:true});if(state.page==="processes")await loadProcesses({fresh:true})}
let pendingProcesses=null, previousProcessesHtml=null;
function loadProcesses({fresh=false}={}){
  if(pendingProcesses)return fresh?pendingProcesses.catch(()=>{}).then(()=>loadProcesses()):pendingProcesses;
  pendingProcesses=fetchProcesses().finally(()=>{pendingProcesses=null});
  return pendingProcesses;
}
async function fetchProcesses(){const r=(await api("/api/trans/processes")).items||[];$("#process-summary").textContent=`运行 ${r.filter(x=>x.alive).length} / 总数 ${r.length}`;const html=r.length?r.map(x=>`<tr><td>${id(x)} · ${esc(x.name||"")}</td><td>${esc(x.protocol)}</td><td>${badge(x)}</td><td>${x.process_pid||"—"}</td><td>${fmt(x.process_start_time)}</td><td>${fmt(x.process_heartbeat_time,true)}</td><td title="${esc(x.process_error)}">${esc(x.process_error)||"—"}</td><td class="actions"><button data-control="start" data-id="${id(x)}">启动</button><button data-control="stop" data-id="${id(x)}">停止</button><button data-log-channel="${id(x)}">日志</button></td></tr>`).join(""):'<tr><td colspan="8" class="muted">暂无通道</td></tr>';if(html!==previousProcessesHtml){$("#processes").innerHTML=html;previousProcessesHtml=html}}
async function loadLogs(){const c=$("#log-channel")?.value||"";state.logsChannel=c;const q=new URLSearchParams({record:state.logRecord||"runtime"});if(c)q.set("channel_id",c);const v=await api(`/api/trans/logs?${q}`);state.logs=v.items||[];$("#log-count").textContent=`${v.total||0} 个文件`;$("#log-files").innerHTML=state.logs.map(x=>`<button class="log-file ${state.log===x.filename?"active":""}" data-log="${esc(x.filename)}">${esc(x.filename)}<small>${(x.size/1024).toFixed(1)} KB · ${fmt(x.mtime)}</small></button>`).join("")||'<div class="muted">暂无日志</div>';if(state.log&&!state.logs.some(x=>x.filename===state.log))state.log=null;if(!state.log&&state.logs.length)state.log=state.logs[0].filename;if(state.log)await loadLog();else {$("#log-name").textContent="请选择日志文件";$("#log-content").textContent="—"}}
async function loadLog(){if(!state.log)return;const q=new URLSearchParams({filename:state.log,tail:"2000",record:state.logRecord||"runtime"}),v=await api(`/api/trans/log?${q}`),k=$("#log-keyword")?.value.trim().toLowerCase()||"";$("#log-name").textContent=state.log;$("#log-content").textContent=(v.lines||[]).filter(x=>!k||x.toLowerCase().includes(k)).join("\n")||"（没有匹配内容）";document.querySelectorAll(".log-file").forEach(e=>e.classList.toggle("active",e.dataset.log===state.log))}
const channelCommonFields=[["name","名称","text"],["protocol","规约","protocol"],["bind_host","监听地址","text"],["bind_port","监听端口","number"],["scan_interval_ms","读取周期(ms)","number"],["log_retention_hours","日志保留小时","number"],["packet_recording","保存报文","bool"],["process_enable","自动运行","bool"]];
const channelProtocolFields={"iec104":[["common_address","IEC104 公共地址","number"],["report_interval_ms","全量上送周期(ms)","number"],["k","发送窗口 k","number"],["w","接收确认窗口 w","number"],["t1","确认超时 t1(s)","float"],["t2","延迟确认 t2(s)","float"],["t3","空闲测试 t3(s)","float"],["max_asdu_length","ASDU 长度诊断阈值（非组帧限制）","number"]],"modbus-tcp":[["unit_id","Modbus 从站地址","number"]]};
const pointCommonFields=[["pnt_no","转发点号","number"],["name","名称","text"],["kind","转发类型","kind"],["source_rtu_id","源 RTU","number"],["source_pnt_no","源点号（默认同转发点号）","number"],["enabled","启用","bool"]];
const pointProtocolFields={"iec104":[["io_address","IEC IOA","number"],["type_id","IEC 类型号","number"],["time_tag","时间标签","time_tag"],["command_mode","命令方式（确认表示写库受理）","mode"]],"modbus-tcp":[["register_area","Modbus 数据区","area"],["address","Modbus 地址","number"],["address_base","地址基准（0/1/10001/30001/40001）","number"],["data_type","数据类型","dtype"],["byte_order","字节序","order"],["word_order","字序","order"],["scale","比例","float"],["offset","偏移","float"]]};
const defaults={protocol:"iec104",bind_host:"0.0.0.0",bind_port:2404,common_address:1,unit_id:1,scan_interval_ms:200,report_interval_ms:1000,log_retention_hours:720,packet_recording:0,process_enable:0,kind:"yc",enabled:1,io_address:0,type_id:0,command_mode:"DIRECT",register_area:"holding",address:0,data_type:"float32",byte_order:"big",word_order:"big",scale:1,offset:0};
Object.assign(defaults,{k:12,w:8,t1:15,t2:10,t3:20,max_asdu_length:249,time_tag:"NONE",address_base:0});
function timeTagInput(value){const tags=["NONE","CP56Time2a"];if(value&&!tags.includes(value))tags.push(value);return `<select name="time_tag">${tags.map(tag=>`<option value="${esc(tag)}" ${value===tag?"selected":""}>${esc(tag)}${["NONE","CP56Time2a"].includes(tag)?"":"（当前驱动不支持，请重新选择）"}</option>`).join("")}</select>`}
function input(k,t,v,disabled){const d=disabled?" disabled":"";if(t==="protocol")return `<select name="${k}"><option value="iec104" ${v==="iec104"?"selected":""}>IEC104</option><option value="modbus-tcp" ${v==="modbus-tcp"?"selected":""}>Modbus TCP</option></select>`;if(t==="kind")return `<select name="${k}"${d}>${Object.entries(kindName).map(([x,n])=>`<option value="${x}" ${v===x?"selected":""}>${n}</option>`).join("")}</select>`;if(t==="bool")return `<select name="${k}"><option value="1" ${Number(v)?"selected":""}>是</option><option value="0" ${!Number(v)?"selected":""}>否</option></select>`;if(t==="mode")return `<select name="${k}"><option value="DIRECT" ${v==="DIRECT"?"selected":""}>直接执行</option><option value="SELECT_AND_EXECUTE" ${v==="SELECT_AND_EXECUTE"?"selected":""}>选择并执行</option></select>`;if(t==="area")return `<select name="${k}">${["coil","discrete","holding","input"].map(x=>`<option value="${x}" ${v===x?"selected":""}>${x}</option>`).join("")}</select>`;if(t==="dtype")return `<select name="${k}">${["bool","uint16","int16","uint32","int32","float32","float64","uint64","int64"].map(x=>`<option value="${x}" ${v===x?"selected":""}>${x}</option>`).join("")}</select>`;if(t==="order")return `<select name="${k}"><option value="big" ${v==="big"?"selected":""}>big</option><option value="little" ${v==="little"?"selected":""}>little</option></select>`;return `<input name="${k}" type="${t==="float"?"number":t}" step="${t==="float"?"any":"1"}" value="${esc(v)}"${d}>`}
function editorProtocol(){const select=$("#editor-fields [name=\"protocol\"]");return String(select?.value||selected()?.protocol||defaults.protocol).toLowerCase()}
function editorFields(type){const protocol=editorProtocol();return type==="channel"?[...channelCommonFields,...channelProtocolFields.iec104,...channelProtocolFields["modbus-tcp"]]:[...pointCommonFields,...pointProtocolFields.iec104,...pointProtocolFields["modbus-tcp"]]}
function setEditorProtocolVisibility(){const protocol=editorProtocol();if(typeof document.querySelectorAll!=="function")return;document.querySelectorAll("#editor-fields [data-protocol-field]").forEach(label=>{label.hidden=label.dataset.protocolField!==protocol})}
function sourceKind(){return String($("#editor-fields [name=\"kind\"]")?.value||"yc").toLowerCase()}
let sourceCatalogRequest=0, sourcePointRequest=0;
async function loadSourceCatalog(){
  const edit=state.edit;
  if(edit?.type!=="point")return;
  const kind=sourceKind(), request=++sourceCatalogRequest;
  ++sourcePointRequest;
  const rtu=$("#editor-fields [name=\"source_rtu_id\"]"), point=$("#editor-fields [name=\"source_pnt_no\"]");
  if(!rtu||!point)return;
  const currentRtu=rtu.value||String(edit.item?.source_rtu_id||"");
  const current=()=>request===sourceCatalogRequest&&state.edit===edit&&sourceKind()===kind&&rtu.isConnected&&$("#editor").open;
  rtu.disabled=point.disabled=true;
  point.innerHTML='<option value="">正在加载源点…</option>';
  try{
    const response=await api(`/api/trans/source-catalog?kind=${encodeURIComponent(kind)}`);
    if(!current())return;
    state.sourceCatalog=response;
    rtu.innerHTML='<option value="">请选择源 RTU</option>'+response.rtus.map(x=>`<option value="${esc(x.rtu_id)}">${esc(x.rtu_id)} · ${esc(x.name||"")} · ${esc(x.channel_type||"")}（${esc(x.point_count)}点）</option>`).join("");
    rtu.value=response.rtus.some(x=>String(x.rtu_id)===currentRtu)?currentRtu:"";
    rtu.disabled=false;
    const refreshPoints=async(initial=false)=>{
      const selectedRtu=rtu.value, pointRequest=++sourcePointRequest;
      const valid=()=>current()&&pointRequest===sourcePointRequest&&rtu.value===selectedRtu;
      point.disabled=true;point.innerHTML='<option value="">'+(selectedRtu?'正在加载源点…':'请选择源 RTU 后加载源点')+'</option>';
      if(!selectedRtu)return;
      try{
        const detail=await api(`/api/trans/source-catalog?kind=${encodeURIComponent(kind)}&rtu_id=${encodeURIComponent(selectedRtu)}&include_rtus=0`);
        if(!valid())return;
        state.sourceCatalog=detail;
        point.innerHTML=(point.multiple?'':'<option value="">请选择源点</option>')+(detail.items||[]).map(x=>`<option value="${esc(x.pnt_no)}">${esc(x.pnt_no)} · ${esc(x.name||"")}（值 ${esc(x.value??"")}）</option>`).join("");
        if(initial&&String(edit.item?.source_rtu_id)===selectedRtu&&edit.item?.kind===kind)
          Array.from(point.options).forEach(o=>o.selected=o.value===String(edit.item.source_pnt_no));
        point.disabled=false;
        $("#editor-error").textContent="";
      }catch(err){if(valid()){$("#editor-error").textContent=err.message;point.innerHTML='<option value="">源点加载失败，请重新选择 RTU</option>';}}
    };
    rtu.onchange=()=>refreshPoints();
    await refreshPoints(true);
  }catch(err){if(current()){$("#editor-error").textContent=err.message;rtu.disabled=false;}}
}
function setPointSourceControls(batch){
  const rtu=$("#editor-fields [name=\"source_rtu_id\"]"), point=$("#editor-fields [name=\"source_pnt_no\"]");
  if(!rtu||!point)return;
  rtu.outerHTML='<select name="source_rtu_id" required></select>';
  point.outerHTML=`<select name="source_pnt_no" ${batch?'multiple size="8" required':'required'}></select>`;
}
async function openEditor(type,item,batch=false){
  state.edit={type,item,channel:item ? id(item) : state.selected};state.batch=Boolean(batch);state.sourceCatalog=null;
    const protocol=String(item?.protocol||selected()?.protocol||defaults.protocol).toLowerCase();
    const pointKind = item?.kind || activePointKind();
  const fields=type==="channel"?[...channelCommonFields,...channelProtocolFields.iec104,...channelProtocolFields["modbus-tcp"]]:[...pointCommonFields,...pointProtocolFields.iec104,...pointProtocolFields["modbus-tcp"]];
  $("#editor-title").textContent=batch?"批量新增点映射":`${item?"编辑":"新建"}${type==="channel"?"转发通道":"点映射"}`;
  $("#editor-error").textContent="";
  $("#editor-fields").innerHTML=fields.map(([k,l,t])=>{
    const group=type==="channel"?(channelProtocolFields.iec104.some(x=>x[0]===k)?"iec104":channelProtocolFields["modbus-tcp"].some(x=>x[0]===k)?"modbus-tcp":""):(pointProtocolFields.iec104.some(x=>x[0]===k)?"iec104":pointProtocolFields["modbus-tcp"].some(x=>x[0]===k)?"modbus-tcp":"");
    const hidden=batch&&type==="point"&&k==="pnt_no";
    const value = k === "kind" && type === "point" ? pointKind : (item?.[k] ?? defaults[k] ?? "");
    return `<label data-field="${k}"${group?` data-protocol-field="${group}"`:""}${hidden?' hidden':''}>${l}${t==="time_tag"?timeTagInput(value):input(k,t,value,Boolean(item&&type==="point"&&["pnt_no","kind"].includes(k)))}</label>`;
  }).join("");
  if(type==="point")setPointSourceControls(batch);
  $("#editor").classList.toggle("batch-editor", Boolean(batch));
  $("#editor").showModal();
  $("#editor-fields [name=\"protocol\"]")?.addEventListener("change",setEditorProtocolVisibility);
  $("#editor-fields [name=\"kind\"]")?.addEventListener("change",()=>loadSourceCatalog().catch(err=>$("#editor-error").textContent=err.message));
  setEditorProtocolVisibility();
  if(type==="point")try{await loadSourceCatalog()}catch(err){$("#editor-error").textContent=err.message}
}
function readEditorValues(fields){const values={};for(const [k,,t]of fields){const el=$("#editor-fields [name=\""+k+"\"]");if(!el||el.disabled||el.value===""||el.closest("label")?.hidden)continue;values[k]=["source_rtu_id","source_pnt_no","pnt_no"].includes(k)?el.value:["number","float","bool"].includes(t)?Number(el.value):el.value}return values}
async function saveEditor(e){
  e.preventDefault();
  const {type,item,channel}=state.edit,fields=editorFields(type),values=readEditorValues(fields);
  try{
    if(type==="channel"){
      if(item)await send(`/api/trans/channels/${id(item)}`,"PATCH",values,item._version);else await send("/api/trans/channels","POST",values);
      await loadChannels({loadSelectedPoints:true,fresh:true});
    }else{
      if(!channel||channel!==state.selected)throw Error("通道已切换，请重新选择测点");
      if($("#editor-fields [name=\"source_rtu_id\"]")?.disabled||$("#editor-fields [name=\"source_pnt_no\"]")?.disabled)throw Error("请等待源点加载完成后再保存");
      if(state.batch){
        const pointSelect=$("#editor-fields [name=\"source_pnt_no\"]"), rtu=values.source_rtu_id;
        const selectedPoints=Array.from(pointSelect?.selectedOptions||[]).map(option=>option.value).filter(Boolean);
        if(!rtu||!selectedPoints.length)throw Error("请选择源 RTU 和至少一个源点");
        const items=selectedPoints.map(source_pnt_no=>{const item={...values,source_rtu_id:rtu,source_pnt_no};delete item.pnt_no;return item});
        await send(`/api/trans/channels/${channel}/points/batch`,"POST",{items});
      }else if(item)await send(`/api/trans/channels/${channel}/points/${encodeURIComponent(item.kind)}/${pno(item)}`,"PATCH",values,item._version);else await send(`/api/trans/channels/${channel}/points`,"POST",values);
      await loadPoints();
    }
    $("#editor").close();toast("已保存");
  }catch(err){$("#editor-error").textContent=err.message}
}
document.addEventListener("click",async e=>{const b=e.target.closest("button");if(!b)return;try{if(b.dataset.pointTab){state.pointOffset=0;state.pointScope="";previousRows=null;setPointTab(b.dataset.pointTab);await loadPoints({fresh:true});return}if(b.dataset.page)return setPage(b.dataset.page);if(b.dataset.control)return await control(b.dataset.id,b.dataset.control);if(b.dataset.edit){state.selected=b.dataset.edit;renderChannels();return openEditor("channel",selected())}if(b.dataset.delete){if(confirm(`删除转发通道 ${b.dataset.delete} 及全部点映射？`)){await api(`/api/trans/channels/${b.dataset.delete}`,{method:"DELETE",headers:versionHeaders(b.dataset.version)});state.selected=null;await loadChannels({loadSelectedPoints:true,fresh:true})}return}if(b.dataset.pointEdit)return openEditor("point",state.points.find(x=>String(pno(x))===b.dataset.pointEdit&&x.kind===b.dataset.pointKind));if(b.dataset.pointDelete){if(b.dataset.owner!==state.selected)return;if(confirm(`删除点映射 ${b.dataset.pointDelete}？`)){await api(`/api/trans/channels/${b.dataset.owner}/points/${encodeURIComponent(b.dataset.pointKind)}/${b.dataset.pointDelete}`,{method:"DELETE",headers:versionHeaders(b.dataset.version)});await loadPoints()}return}if(b.dataset.log){state.log=b.dataset.log;return loadLog()}if(b.dataset.logChannel){$("#log-channel").value=b.dataset.logChannel;state.log=null;return setPage("logs")}}catch(err){toast(err.message)}});
$("#channels")?.addEventListener("click",async e=>{if(e.target.closest("button"))return;const r=e.target.closest("tr[data-select]");if(r){state.selected=r.dataset.select;state.pointOffset=0;renderChannels();await loadPoints()}});$("#selected-channel")?.addEventListener("change",async e=>{state.selected=e.target.value||null;state.pointOffset=0;const url=new URL(location.href);if(state.selected)url.searchParams.set("channel_id",state.selected);else url.searchParams.delete("channel_id");history.replaceState(null,"",url);renderChannels();await loadPoints()});$("#kind-filter")?.addEventListener("change",()=>{state.pointOffset=0;state.pointScope="";previousRows=null;setPointTab(activePointKind());loadPoints().catch(e=>toast(e.message))});$("#point-limit")?.addEventListener("change",e=>{state.pointLimit=Number(e.target.value);state.pointOffset=0;loadPoints().catch(e=>toast(e.message))});$("#point-prev")?.addEventListener("click",()=>{state.pointOffset=Math.max(0,state.pointOffset-state.pointLimit);loadPoints().catch(e=>toast(e.message))});$("#point-next")?.addEventListener("click",()=>{state.pointOffset+=state.pointLimit;loadPoints().catch(e=>toast(e.message))});$("#log-channel")?.addEventListener("change",()=>{state.log=null;loadLogs().catch(e=>toast(e.message))});$("#log-keyword")?.addEventListener("input",()=>loadLog().catch(e=>toast(e.message)));if($("#new-channel"))$("#new-channel").onclick=()=>openEditor("channel");if($("#edit-channel"))$("#edit-channel").onclick=()=>selected()?openEditor("channel",selected()):toast("请选择通道");if($("#new-point"))$("#new-point").onclick=()=>state.selected?openEditor("point"):toast("请选择通道");if($("#batch-point"))$("#batch-point").onclick=()=>state.selected?openEditor("point",null,true):toast("请选择通道");if($("#refresh"))$("#refresh").onclick=()=>loadChannels({loadSelectedPoints:true}).catch(e=>toast(e.message));if($("#refresh-processes"))$("#refresh-processes").onclick=()=>loadProcesses().catch(e=>toast(e.message));if($("#refresh-logs"))$("#refresh-logs").onclick=()=>loadLogs().catch(e=>toast(e.message));if($("#reload-log"))$("#reload-log").onclick=()=>loadLog().catch(e=>toast(e.message));if($("#close-editor"))$("#close-editor").onclick=$("#cancel-editor").onclick=()=>$("#editor").close();if($("#editor-form"))$("#editor-form").addEventListener("submit",saveEditor);
setPointTab(activePointKind());
$("#point-live-refresh")?.addEventListener("change",()=>loadPoints().catch(e=>toast(e.message)));
async function boot(){try{await api("/api/health");$("#connection").textContent="Web 已连接";$("#connection").classList.add("ok");await loadChannels({loadSelectedPoints:true,fresh:true})}catch(e){$("#connection").textContent="连接失败";$("#connection").classList.add("bad");toast(e.message)}}boot();setInterval(()=>{if(document.hidden||pendingChannels||$("#editor")?.open)return;(state.page==="channels"?loadChannels:state.page==="processes"?loadProcesses:loadLogs)().catch(e=>toast(e.message))},5000);
setInterval(()=>{if(document.hidden||pendingChannels||$("#editor")?.open)return;if(pointTableBody()&&$("#point-live-refresh")?.checked)loadPoints({background:true}).catch(e=>toast(e.message))},2000);

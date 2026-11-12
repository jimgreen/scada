"use strict";
const $=s=>document.querySelector(s); const esc=x=>String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;", "'":"&#39;"}[c]));
const statusName={0:"停止",1:"启动中",2:"运行",3:"停止中",4:"处理中",5:"错误"};
async function api(path,opt={}){const r=await fetch(path,{...opt,headers:{"Content-Type":"application/json",...(opt.headers||{})}});let v;try{v=await r.json()}catch(_){throw Error(`HTTP ${r.status}`)}if(!r.ok)throw Error(v.error?.message||r.statusText);return v}
function toast(s){const e=$("#toast");e.textContent=s;setTimeout(()=>e.textContent="",4000)}
function fmt(v,timeOnly=false){if(!v)return "—";const d=new Date(Number(v));if(Number.isNaN(d.valueOf()))return String(v);const p=n=>String(n).padStart(2,"0"),t=`${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;return timeOnly?t:`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${t}`}
function badge(x){const s=Number(x.process_status||0);return `<span class="status-badge ${s===2?"running":s===5?"error":""}">${esc(statusName[s]??s)}</span>`}
let pending=null,previousHtml=null;
function load({fresh=false}={}){
  if(pending)return fresh?pending.catch(()=>{}).then(()=>load()):pending;
  pending=fetchProcesses().finally(()=>{pending=null});return pending;
}
async function fetchProcesses(){const rows=(await api("/api/trans/processes")).items||[];$("#summary").textContent=`运行 ${rows.filter(x=>x.alive).length} / 总数 ${rows.length}`;$("#connection").textContent="Web 已连接";$("#connection").classList.add("ok");const html=rows.length?rows.map(x=>`<tr><td>${esc(x.channel_id)} · ${esc(x.name||"")}</td><td>${esc(x.protocol)}</td><td>${badge(x)}</td><td>${x.process_pid||"—"}</td><td>${fmt(x.process_start_time)}</td><td>${fmt(x.process_heartbeat_time,true)}</td><td title="${esc(x.process_error)}">${esc(x.process_error)||"—"}</td><td class="actions"><button data-action="start" data-id="${x.channel_id}">启动</button><button data-action="stop" data-id="${x.channel_id}">停止</button><a href="/logs.html?channel_id=${encodeURIComponent(x.channel_id)}">日志报文</a></td></tr>`).join(""):"<tr><td colspan=8 class=muted>暂无转发通道</td></tr>";if(html!==previousHtml){$("#processes").innerHTML=html;previousHtml=html}}
async function control(id,action){await api(`/api/trans/channels/${id}/${action}`,{method:"POST",body:"{}"});toast(action==="start"?"启动请求已发送":"停止请求已发送");await load({fresh:true})}
document.addEventListener("click",e=>{const b=e.target.closest("button[data-action]");if(b)control(b.dataset.id,b.dataset.action).catch(x=>toast(x.message))});$("#refresh").onclick=()=>load().catch(x=>toast(x.message));load().catch(e=>{$("#connection").textContent="连接失败";toast(e.message)});setInterval(()=>{if(!document.hidden)load().catch(()=>{})},5000);

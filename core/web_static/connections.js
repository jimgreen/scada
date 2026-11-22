(() => {
  'use strict';
  const $ = id => document.getElementById(id), params = new URLSearchParams(location.search);
  const ident = location.pathname.split('/')[2] || '';
  let offset=0, busy=false, epoch=0, items=[], before=null, historyMode=false;
  let after=null, hasOlder=false, catchup=false, summaryMarkup='';
  let targets=[], targetsLoaded=false, refreshTargets=false, pendingReload=false;
  const esc = value => String(value??'—').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const stamp = n => n ? new Date(n*1000).toLocaleString('zh-CN',{hour12:false}) : '—';
  const state = row => row.stale ? '状态未知（记录进程心跳超时）' : ({connected:'已接入',active:'传输激活',inactive:'传输停止',closed:'已断开',unknown:'状态待确认'}[row.state]||row.state);
  const message = (text,error=false) => { $('message').textContent=text; $('message').dataset.error=error; };
  async function api(path) {
    const response = await fetch(path,{cache:'no-store',signal:AbortSignal.timeout(12000)});
    const result=await response.json();
    if(!response.ok) throw Error(result.error?.message||`HTTP ${response.status}`);
    return result;
  }
  function warnings(result) {
    const rows=(result.writers||[]).filter(w=>w.dropped||w.error);
    $('warnings').textContent=rows.map(w=>`记录警告：进程累计丢失 ${w.dropped} 条${w.error?'；'+w.error:''}`).join('\n') +
      (result.item?.removed?`\n此连接已清理 ${result.item.removed} 条较早记录。`:'') +
      (result.item?.packet_recording===0?'\n此连接的原始报文保存已关闭；可在所属通道定义中开启。':'');
  }
  function query() {
    const q=new URLSearchParams({limit:50,offset});
    const target=targets.find(item=>item.value===$('target').value);
    if(target) Object.entries(target.query).forEach(([key,value])=>q.set(key,value));
    if($('state').value) q.set('state',$('state').value);
    return q;
  }
  async function loadTargets() {
    if(targetsLoaded&&!refreshTargets) return;
    const result=await api('/api/connections/options');
    let selected=$('target').value;
    let ambiguous=false;
    if(!targetsLoaded && epoch===0) {
      const key=result.component==='s'&&params.has('rtu_id')?'rtu_id':'channel_id';
      const raw=params.get(key);
      if(raw && /^[1-9][0-9]*$/.test(raw)) {
        const matches=result.items.filter(item=>String(item[key])===String(Number(raw)));
        if(matches.length===1) selected=matches[0].value;
        else if(matches.length>1) ambiguous=true;
        else {
          selected=`${key}:${raw}`;
          result.items.push({value:selected,label:`未找到的${key==='rtu_id'?'RTU':'通道'}（${raw}）`,query:{[key]:Number(raw)}});
        }
      }
    }
    const previous=targets.find(item=>item.value===selected);
    if(previous&&!result.items.some(item=>item.value===selected)) result.items.push(previous);
    targets=result.items;
    $('target').innerHTML='<option value="">全部通道 / RTU</option>'+targets.map(item=>`<option value="${esc(item.value)}">${esc(item.label)}</option>`).join('');
    $('target').value=selected;
    $('target').disabled=false;
    targetsLoaded=true;refreshTargets=false;
    if(result.component==='t') {
      $('target-label').textContent='转发通道';
      $('target').setAttribute('aria-label','转发通道名称');
      $('target').options[0].textContent='全部转发通道';
    }
    if(ambiguous) throw Error('此通道编号对应多个规约，请在下拉框中按名称选择。');
  }
  async function list(version) {
    const result=await api('/api/connections?'+query());
    if(version!==epoch) return;
    if(result.component==='t') {
      $('notice').textContent='记录 IEC104 和 Modbus TCP 转发通道实际接入的客户端；历史从升级后新接入的连接开始。';
    }
    warnings(result);
    $('rows').innerHTML=result.items.map(row=>`<tr><td><strong>${esc(row.display_name)}</strong><small>通道 ${esc(row.channel_id)}${row.rtu_id!=null?` / RTU ${esc(row.rtu_id)}`:''}</small></td><td>${esc(row.service)}<small>${esc(row.protocol)}</small></td><td>${esc(row.peer)}</td><td>${esc(row.local)}</td><td>${esc(state(row))}</td><td>${esc(stamp(row.opened))}<small>${esc(stamp(row.closed))}</small></td><td>${row.rx_count} / ${row.tx_count}<small>${row.rx_bytes} / ${row.tx_bytes} B</small></td><td><a href="/connections/${encodeURIComponent(row.id)}">独立详情 ↗</a></td></tr>`).join('') || `<tr><td colspan="8" class="conn-empty">暂无符合条件的连接记录。支持 ${result.component==='t'?'IEC104、Modbus TCP':'IEC104、Modbus TCP、OPC UA'} 实际入站连接。</td></tr>`;
    $('total').textContent=`${result.total} 个连接 · 第 ${Math.floor(offset/50)+1} 页 · ${result.retention}`;
    $('prev').disabled=offset===0; $('next').disabled=offset+50>=result.total;
  }
  const eventMarkup = x => `<article class="conn-event" data-event-id="${x.id}"><div class="meta">#${x.id} · ${esc(stamp(x.time))} · ${esc(({packet:'报文',interaction:'交互',log:'日志'})[x.kind])} · ${esc(x.direction)}</div><pre>${esc(x.summary)}</pre>${x.hex?`<pre>HEX ${esc(x.hex)}</pre>`:''}</article>`;
  function renderEvents(events, mode) {
    const container=$('events'), known=new Set(items.map(x=>x.id));
    const added=mode==='reset'?events.items:events.items.filter(x=>!known.has(x.id));
    if(mode==='reset') {
      items=added;container.innerHTML=added.map(eventMarkup).join('');
    } else if(added.length) {
      container.querySelector('.conn-empty')?.remove();
      if(mode==='older') { items.push(...added);container.insertAdjacentHTML('beforeend',added.map(eventMarkup).join('')); }
      else { items.unshift(...added);container.insertAdjacentHTML('afterbegin',added.map(eventMarkup).join('')); }
    }
    if(mode!=='new') { before=events.next_before;hasOlder=events.has_more; }
    // A page may load new static assets just before its Web backend restarts.
    // Keep the old full-refresh behavior until that backend exposes the cursor.
    if(mode!=='older') after=events.next_after??null;
    // Bound DOM size; retain the end being browsed, and keep its pagination cursor.
    if(items.length>2000) {
      const excess=items.length-2000;
      if(mode==='older') {items.splice(0,excess);for(let n=0;n<excess;n++) container.firstElementChild.remove();}
      else {items.splice(2000);for(let n=0;n<excess;n++) container.lastElementChild.remove();hasOlder=true;}
    }
    if(items.length) before=items[items.length-1].id;
    else if(!container.childElementCount) container.innerHTML='<div class="conn-empty">此连接暂无该类记录</div>';
    $('older').disabled=!hasOlder;
    $('event-count').textContent=`显示 ${items.length} 条（页面最多 2000 条）${historyMode?' · 正在浏览历史':catchup?' · 正在补读新增记录':''}`;
  }
  async function detail(older=false, version=epoch) {
    const q=new URLSearchParams({limit:100});
    if($('kind').value) q.set('kind',$('kind').value);
    if(older && before) q.set('before',before);
    const incremental=!older && after!==null;
    if(incremental) q.set('after',after);
    const [result,events]=await Promise.all([api('/api/connections/'+ident),api('/api/connections/'+ident+'/events?'+q)]);
    if(version!==epoch) return;
    const row=result.item;
    detailClosed = Boolean(row.closed);
    warnings(result);
    const logLink=$('open-logs');
    if(logLink && row.channel_id!=null) {
      const base=result.component==='t'?'/logs.html':'/logs';
      logLink.href=`${base}?channel_id=${encodeURIComponent(row.channel_id)}`;
      logLink.hidden=false;
    }
    const fields={'连接编号':row.id,'通道 / RTU 名称':row.display_name,'关联编号':`通道 ${row.channel_id??'—'}${row.rtu_id!=null?` / RTU ${row.rtu_id}`:''}`,'服务 / 规约':`${row.service} / ${row.protocol}`,'客户端 IP：源端口':row.peer,'服务监听地址':row.local,'状态':state(row),'接入时间':stamp(row.opened),'断开时间':stamp(row.closed),'最近交互':stamp(row.updated),'收 / 发报文':`${row.rx_count} / ${row.tx_count}`,'收 / 发字节':`${row.rx_bytes} / ${row.tx_bytes} B`,'保留范围':result.retention};
    const markup=Object.entries(fields).map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('');
    if(markup!==summaryMarkup) { $('summary').innerHTML=markup;summaryMarkup=markup; }
    catchup=incremental&&events.has_more;
    renderEvents(events,older?'older':incremental?'new':'reset');
    // Drain bursts in bounded pages, preserving the last accepted cursor even
    // if a later request fails. Next poll resumes without skipping records.
    for(let page=1;incremental&&catchup&&page<5;page++) {
      q.set('after',after);
      const more=await api('/api/connections/'+ident+'/events?'+q);
      if(version!==epoch) return;
      catchup=more.has_more;renderEvents(more,'new');
    }
  }
  async function load(older=false) {
    if(busy) return;
    busy=true; $('refresh').disabled=true;
    const version=epoch;
    try { message(''); if(ident) await detail(older,version); else { await loadTargets(); await list(version); } if(version===epoch) message('更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false})); }
    catch(e) { if(version===epoch) message(e.message,true); }
    finally { busy=false; $('refresh').disabled=false; if(pendingReload){pendingReload=false;load();} }
  }
  $('state').value=['open','closed'].includes(params.get('state'))?params.get('state'):'';
  if(ident) {
    ['filters','list','pages'].forEach(id=>$(id).hidden=true);
    $('detail').hidden=false; $('back').hidden=false; $('title').textContent='客户端连接详情';
    $('notice').textContent='每次接入生成独立连接编号；客户端重连生成新页面。旧版通道日志无法补分到各连接。';
  }
  function filterChanged() {
    offset=0;epoch++;
    const q=query();q.delete('limit');q.delete('offset');
    history.replaceState(null,'',location.pathname+(q.size?'?'+q:''));
    if(busy) pendingReload=true;else load();
  }
  $('filters').onsubmit=e=>{e.preventDefault();filterChanged();};
  $('target').onchange=filterChanged;
  $('state').onchange=filterChanged;
  $('clear').onclick=()=>{$('target').value='';$('state').value='';filterChanged();};
  $('prev').onclick=()=>{if(!busy){offset=Math.max(0,offset-50);load();}};
  $('next').onclick=()=>{if(!busy){offset+=50;load();}};
  function latest() {
    epoch++;historyMode=false;after=null;catchup=false;
    if(busy) pendingReload=true;else load();
  }
  $('refresh').onclick=()=>{refreshTargets=true;if(ident&&historyMode) latest();else load();};
  $('latest').onclick=latest;
  $('kind').onchange=latest;
  $('older').onclick=()=>{if(!busy){historyMode=true;load(true);}};
  $('export').onclick=()=>{
    const text=items.map(x=>`${stamp(x.time)} [${x.kind}] ${x.direction} ${x.summary}${x.hex?'\nHEX '+x.hex:''}`).join('\n');
    const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'})), a=document.createElement('a');
    a.href=url;a.download=`connection-${ident}.log`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  let detailClosed = false;
  setInterval(()=>{if($('auto').checked&&!document.hidden&&!historyMode&&!(ident&&detailClosed)) load();},5000);
  load();
})();

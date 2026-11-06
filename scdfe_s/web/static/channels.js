(function () {
  "use strict";
  const $ = s => document.querySelector(s);
  const esc = v => String(v == null ? "" : v).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const names = {yc:"遥测",yx:"遥信",yk:"遥控",yt:"遥调"};
  let selected = null, selectedPoint = null, offset = 0, limit = 100, total = 0, requestId = 0;
  let pointScope = "", channelsRequest = 0;
  const versionHeaders = version => version == null ? {} : {"If-Match":String(version)};
  async function api(path, options) {
    const response = await fetch(path, {...options, headers:{"Content-Type":"application/json", ...options?.headers}});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error && body.error.message || "请求失败");
    return body;
  }
  function notice(id, text, error) {
    const node = $(id);
    if (node) { node.textContent = text || ""; node.className = "message" + (error ? " error" : ""); }
  }
  function showEditor(id) { const editor = $(id); if (editor) editor.open = true; }
  async function loadChannels() {
    const request = ++channelsRequest;
    try {
      const data = await api("/api/channels?limit=500"), rows = data.items || [];
      if (request !== channelsRequest) return;
      if ($("#channels")) {
        $("#channels").innerHTML = rows.map(r => `<tr><td>${esc(r.rtu_id)}</td><td>${esc(r.channel_id)}</td><td>${esc(r.channel_type)}</td><td>${esc(r.name || r.code || "")}</td>${window.PacketRecordingChannels.cell(r.channel_id)}<td>${r.process_enable ? "启用" : "停用"}</td><td><a href="/points?rtu_id=${encodeURIComponent(r.rtu_id)}">四遥数据</a> <button data-edit='${esc(JSON.stringify(r))}'>编辑</button> <button data-version="${esc(r._version)}" data-delete='${esc(r.rtu_id)}'>删除</button></td></tr>`).join("") || '<tr><td colspan="7">暂无通道</td></tr>';
        window.PacketRecordingChannels.mount();
        notice("#channel-message", `共 ${rows.length} 个通道`);
      }
      if ($("#point-rtu")) {
        const previous = selected?.rtu_id;
        const requested = previous ?? Number(new URLSearchParams(location.search).get("rtu_id"));
        selected = rows.find(r => Number(r.rtu_id) === Number(requested)) || rows[0] || null;
        $("#point-rtu").innerHTML = '<option value="">请选择通道</option>' + rows.map(r => `<option value="${esc(r.rtu_id)}">${esc(r.rtu_id)} · ${esc(r.name || r.code || r.channel_type)}</option>`).join("");
        $("#point-rtu").value = selected?.rtu_id || "";
        if (previous !== selected?.rtu_id) { selectedPoint = null; offset = 0; }
        await loadPoints();
      }
    } catch (error) { if (request === channelsRequest) notice($("#points") ? "#point-message" : "#channel-message", error.message, true); }
  }
  function fillChannel(row) {
    selected = row;
    for (const key of ["rtu_id","channel_id","channel_type","code","name","process_enable"]) $("#" + key).value = row[key] == null ? "" : row[key];
    showEditor("#channel-editor");
  }
  async function loadPoints() {
    if (!$("#points")) return;
    const request = ++requestId, rtu = selected?.rtu_id, kind = $("#kind-filter").value || "yc";
    const scope = `${rtu}/${kind}`;
    if (scope !== pointScope) {
      pointScope = scope; selectedPoint = null;
      $("#points").innerHTML = '<tr><td colspan="6">正在读取测点…</td></tr>';
      $("#point-page-summary").textContent = ""; $("#point-prev").disabled = $("#point-next").disabled = true;
    }
    try {
      const data = rtu ? await api(`/api/channels/${rtu}/points/${kind}?limit=${limit}&offset=${offset}`) : {items:[],total:0};
      if (request !== requestId || rtu !== selected?.rtu_id) return;
      total = data.total || 0;
      if (offset > 0 && offset >= total) { offset = Math.max(0, Math.floor((total - 1) / limit) * limit); return loadPoints(); }
      const points = data.items || [];
      $("#points").innerHTML = points.map(p => `<tr><td>${names[kind]}</td><td>${esc(p.pnt_no)}</td><td>${esc(p.code)}</td><td>${esc(p.name)}</td><td>${esc(p.value)}</td><td><button data-point-edit='${esc(JSON.stringify({...p,kind}))}'>编辑</button> <button data-owner="${esc(rtu)}" data-version="${esc(p._version)}" data-point-delete="${kind}" data-point-no="${esc(p.pnt_no)}">删除</button></td></tr>`).join("") || `<tr><td colspan="6">${rtu ? "暂无测点" : "请先选择通道"}</td></tr>`;
      $("#point-page-summary").textContent = `共 ${total} 条 · 当前 ${points.length ? offset + 1 : 0}–${offset + points.length} 条`;
      $("#point-prev").disabled = offset === 0;
      $("#point-next").disabled = offset + points.length >= total;
    } catch (error) { if (request === requestId) notice("#point-message", error.message, true); }
  }
  function fillPoint(point) {
    if (point.rtu_id !== selected?.rtu_id) return;
    selectedPoint = point;
    $("#point-kind").value = point.kind;
    $("#point-no").value = point.pnt_no;
    $("#point-code").value = point.code || "";
    $("#point-name").value = point.name || "";
    $("#point-value").value = point.value == null ? 0 : point.value;
    showEditor("#point-editor");
  }
  const channelBody = () => ({rtu_id:Number($("#rtu_id").value),channel_id:Number($("#channel_id").value),channel_type:$("#channel_type").value,code:$("#code").value,name:$("#name").value,process_enable:Number($("#process_enable").value)});
  const pointBody = () => ({pnt_no:Number($("#point-no").value),code:$("#point-code").value,name:$("#point-name").value,value:Number($("#point-value").value)});
  if ($("#channel-form")) $("#channel-form").onsubmit = async event => {
    event.preventDefault();
    try { await api("/api/channels", {method:"POST",body:JSON.stringify(channelBody())}); await loadChannels(); notice("#channel-message", "通道已新增"); }
    catch (error) { notice("#channel-message", error.message, true); }
  };
  if ($("#update-channel")) $("#update-channel").onclick = async () => {
    if (!selected) return notice("#channel-message", "请先选择通道", true);
    try {
      const body = channelBody();
      if (body.rtu_id !== selected.rtu_id) throw new Error("修改通道时不能变更 RTU 编号；请新建通道");
      delete body.rtu_id;
      selected = (await api(`/api/channels/${selected.rtu_id}`, {method:"PATCH",headers:versionHeaders(selected._version),body:JSON.stringify(body)})).item;
      await loadChannels(); notice("#channel-message", "通道已保存");
    } catch (error) { notice("#channel-message", error.message, true); }
  };
  if ($("#point-form")) $("#point-form").onsubmit = async event => {
    event.preventDefault();
    if (!selected) return notice("#point-message", "请先选择通道", true);
    try {
      const kind = $("#point-kind").value, body = pointBody();
      const owner = selected.rtu_id;
      const result = await api(`/api/channels/${owner}/points/${kind}`, {method:"POST",body:JSON.stringify(body)});
      if (owner !== selected?.rtu_id || $("#point-kind").value !== kind) return;
      selectedPoint = {...result.item,kind}; $("#kind-filter").value = kind; offset = 0;
      await loadPoints(); notice("#point-message", "测点已新增");
    } catch (error) { notice("#point-message", error.message, true); }
  };
  if ($("#update-point")) $("#update-point").onclick = async () => {
    if (!selected || !selectedPoint) return notice("#point-message", "请先选择要修改的测点", true);
    try {
      const body = pointBody(), kind = $("#point-kind").value;
      if (kind !== selectedPoint.kind || body.pnt_no !== selectedPoint.pnt_no) throw new Error("修改测点时不能变更类型或点号；请新增测点");
      delete body.pnt_no;
      const owner = selectedPoint.rtu_id, edited = selectedPoint;
      if (owner !== selected.rtu_id) throw new Error("通道已切换，请重新选择测点");
      const result = await api(`/api/channels/${owner}/points/${kind}/${edited.pnt_no}`, {method:"PATCH",headers:versionHeaders(edited._version),body:JSON.stringify(body)});
      if (selectedPoint !== edited || selected?.rtu_id !== owner) return;
      selectedPoint = {...result.item,kind};
      await loadPoints(); notice("#point-message", "测点已保存");
    } catch (error) { notice("#point-message", error.message, true); }
  };
  if ($("#point-rtu")) $("#point-rtu").onchange = () => {
    selected = $("#point-rtu").value ? {rtu_id:Number($("#point-rtu").value)} : null;
    selectedPoint = null; offset = 0; $("#point-form").reset();
    const url = new URL(location.href); if (selected) url.searchParams.set("rtu_id", selected.rtu_id); else url.searchParams.delete("rtu_id");
    history.replaceState(null, "", url); loadPoints();
  };
  if ($("#kind-filter")) $("#kind-filter").onchange = () => { selectedPoint = null; offset = 0; $("#point-kind").value = $("#kind-filter").value; loadPoints(); };
  if ($("#point-limit")) $("#point-limit").onchange = () => { limit = Number($("#point-limit").value); offset = 0; loadPoints(); };
  if ($("#point-prev")) $("#point-prev").onclick = () => { offset = Math.max(0, offset - limit); loadPoints(); };
  if ($("#point-next")) $("#point-next").onclick = () => { offset += limit; loadPoints(); };
  $("#reload").onclick = loadChannels;
  document.addEventListener("click", async event => {
    const edit = event.target.closest("button[data-edit]"), del = event.target.closest("button[data-delete]"),
      pointEdit = event.target.closest("button[data-point-edit]"), pointDelete = event.target.closest("button[data-point-delete]");
    try {
      if (edit) fillChannel(JSON.parse(edit.dataset.edit));
      if (del && confirm("确定删除该通道及其四遥点？")) { await api(`/api/channels/${del.dataset.delete}`, {method:"DELETE",headers:versionHeaders(del.dataset.version),body:"{}"}); selected = null; await loadChannels(); }
      if (pointEdit) fillPoint(JSON.parse(pointEdit.dataset.pointEdit));
      if (pointDelete && selected && Number(pointDelete.dataset.owner) === selected.rtu_id && confirm("确定删除该测点？")) { await api(`/api/channels/${pointDelete.dataset.owner}/points/${pointDelete.dataset.pointDelete}/${pointDelete.dataset.pointNo}`, {method:"DELETE",headers:versionHeaders(pointDelete.dataset.version),body:"{}"}); selectedPoint = null; await loadPoints(); }
    } catch (error) { notice($("#points") ? "#point-message" : "#channel-message", error.message, true); }
  });
  loadChannels();
})();

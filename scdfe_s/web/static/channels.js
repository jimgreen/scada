(function () {
  "use strict";
  const $ = s => document.querySelector(s);
  const esc = v => String(v == null ? "" : v).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const names = {yc:"遥测",yx:"遥信",yk:"遥控",yt:"遥调"};
  let selected = null, selectedPoint = null, selectedParameter = null, parameterSchema = null;
  let offset = 0, limit = 100, total = 0, requestId = 0, parameterRequest = 0;
  let pointScope = "", channelsRequest = 0, channelRows = [];
  const pointKinds = Object.keys(names);
  function activeKind() { return $("#kind-filter")?.value || "yc"; }
  function pointTableBody(kind = activeKind()) {
    if (typeof document.querySelectorAll === "function") {
      for (const page of document.querySelectorAll("[data-point-page]")) {
        if (page.dataset.pointPage === kind) return page.querySelector("[data-points-body]");
      }
    }
    return $("#points");
  }
  function setPointTab(kind) {
    if (!pointKinds.includes(kind)) kind = "yc";
    const filter = $("#kind-filter");
    if (filter) filter.value = kind;
    if (typeof document.querySelectorAll !== "function") return;
    document.querySelectorAll("[data-point-tab]").forEach(tab => {
      const selected = tab.dataset.pointTab === kind;
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.classList.toggle("active", selected);
    });
    document.querySelectorAll("[data-point-page]").forEach(page => {
      page.hidden = page.dataset.pointPage !== kind;
      page.classList.toggle("active", page.dataset.pointPage === kind);
      const body = page.querySelector("[data-points-body]");
      if (body) {
        if (page.dataset.pointPage === kind) body.id = "points";
        else if (body.id === "points") body.removeAttribute("id");
      }
    });
  }
  const keyValue = raw => Number.isSafeInteger(Number(raw)) ? Number(raw) : raw;
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
  function showEditor(id) {
    const editor = $(id);
    if (!editor) return;
    if (typeof editor.showModal === "function" && !editor.open) editor.showModal();
    else editor.open = true;
  }
  function closeEditor(id) {
    const editor = $(id);
    if (!editor) return;
    if (typeof editor.close === "function" && editor.open) editor.close();
    else editor.open = false;
  }
  function prepareNewChannel() {
    selected = null;
    const form = $("#channel-form");
    if (form && typeof form.reset === "function") form.reset();
    $("#channel-editor-title").textContent = "新建通道";
    $("#create-channel-submit").hidden = false;
    $("#update-channel").hidden = true;
    if ($("#channel-editor-error")) $("#channel-editor-error").textContent = "";
    showEditor("#channel-editor");
  }
  function prepareNewPoint() {
    selectedPoint = null;
    const form = $("#point-form");
    if (form && typeof form.reset === "function") form.reset();
    $("#point-kind").value = activeKind();
    $("#point-editor-title").textContent = `新增${names[activeKind()] || ""}测点`;
    $("#add-point-submit").textContent = "新增测点";
    $("#add-point-submit").hidden = false;
    $("#update-point").hidden = true;
    if ($("#point-editor-error")) $("#point-editor-error").textContent = "";
    showEditor("#point-editor");
  }
  async function loadChannels() {
    const request = ++channelsRequest;
    try {
      const rows = [];
      const fields = $("#channels") ? "rtu_id,channel_id,channel_type,name,packet_recording,process_enable" : "rtu_id,channel_type,name,packet_recording";
      for (;;) {
        const page = await api("/api/channels?limit=500&fields=" + fields + (rows.length ? "&offset=" + rows.length : ""));
        if (request !== channelsRequest) return;
        const items = page.items || [];
        rows.push(...items);
        if (!items.length || rows.length >= (page.total ?? rows.length)) break;
      }
      channelRows = rows;
      if (request !== channelsRequest) return;
      if ($("#channels")) {
        $("#channels").innerHTML = rows.map(r => `<tr data-select="${esc(r.rtu_id)}"><td>${esc(r.rtu_id)}</td><td>${esc(r.channel_id)}</td><td>${esc(r.channel_type)}</td><td>${esc(r.name || "")}</td><td>${r.process_enable ? "启用" : "停用"}</td><td><a href="/points?rtu_id=${encodeURIComponent(r.rtu_id)}">四遥数据</a> <button data-edit='${esc(JSON.stringify(r))}'>编辑</button> <button data-version="${esc(r._version)}" data-delete='${esc(r.rtu_id)}'>删除</button></td></tr>`).join("") || '<tr><td colspan="6">暂无通道</td></tr>';
        notice("#channel-message", `共 ${rows.length} 个通道`);
      }
      if ($("#point-rtu")) {
        const previous = selected?.rtu_id;
        const requested = previous ?? new URLSearchParams(location.search).get("rtu_id");
        selected = (requested ? rows.find(r => String(r.rtu_id) === String(requested)) : rows[0]) || null;
        $("#point-rtu").innerHTML = '<option value="">请选择通道</option>' + rows.map(r => `<option value="${esc(r.rtu_id)}">${esc(r.rtu_id)} · ${esc(r.name || r.channel_type)}</option>`).join("");
        $("#point-rtu").value = selected?.rtu_id || "";
        if (previous !== selected?.rtu_id) { selectedPoint = null; offset = 0; }
        await loadPoints();
      }
    } catch (error) { if (request === channelsRequest) notice(pointTableBody() ? "#point-message" : "#channel-message", error.message, true); }
  }
  function fillChannel(row) {
    selected = row;
    for (const key of ["rtu_id","channel_id","channel_type","name","packet_recording","process_enable"]) $("#" + key).value = row[key] == null ? "" : row[key];
    $("#channel-editor-title").textContent = "编辑通道";
    $("#create-channel-submit").hidden = true;
    $("#update-channel").hidden = false;
    if ($("#channel-editor-error")) $("#channel-editor-error").textContent = "";
    showEditor("#channel-editor");
    loadParameters();
  }
  function parameterInput(column, value) {
    const name = column.eng_name, label = column.chn_name || name;
    if (column.primary_key || name === "rtu_id") return "";
    const typeLabel = String(column.disp_type || (column.data_type === "int" ? "INT" : column.data_type || "")) === "LONG"
      ? "INT"
      : String(column.disp_type || (column.data_type === "int" ? "INT" : column.data_type || ""));
    const fieldLabel = `${esc(label)}<small>${esc(name)}${typeLabel ? ` · ${esc(typeLabel)}` : ""}</small>`;
    const menuName = String(column.disp_menu || "");
    if (menuName.includes("二值")) return `<label>${fieldLabel}<select data-parameter-field="${esc(name)}"><option value="0"${Number(value) === 0 ? " selected" : ""}>关闭 / 0</option><option value="1"${Number(value) === 1 ? " selected" : ""}>开启 / 1</option></select></label>`;
    if (menuName.includes("校验方式")) return `<label>${fieldLabel}<select data-parameter-field="${esc(name)}">${["N","E","O"].map(option => `<option value="${option}"${String(value ?? "N") === option ? " selected" : ""}>${option}</option>`).join("")}</select></label>`;
    const type = column.data_type === "int" || column.data_type === "float" ? "number" : "text";
    const step = column.data_type === "float" ? " step=\"any\"" : "";
    const current = value == null ? (column.default ?? "") : value;
    return `<label>${fieldLabel}<input data-parameter-field="${esc(name)}" type="${type}"${step} value="${esc(current)}"></label>`;
  }
  function renderParameters(info) {
    const editor = $("#parameter-editor"), fields = $("#parameter-fields");
    if (!editor || !fields) return;
    const item = info?.item || null;
    selectedParameter = item;
    parameterSchema = info?.parameter_schema || null;
    const columns = parameterSchema?.columns || [];
    if (!selected || !columns.length) {
      if (editor.open) closeEditor("#parameter-editor");
      fields.replaceChildren();
      $("#edit-parameters") && ($("#edit-parameters").disabled = true);
      return;
    }
    $("#edit-parameters") && ($("#edit-parameters").disabled = false);
    $("#parameter-editor-title").textContent = `${selected.channel_type || "规约"} 通道参数`;
    $("#parameter-help").textContent = item ? "修改后保存到 S 独立数据库；通信测试页面只读取这些参数。" : "当前通道还没有规约参数，请填写后创建。";
    fields.innerHTML = columns.map(column => parameterInput(column, item?.[column.eng_name])).join("");
    $("#delete-parameters").hidden = !item;
  }
  function openParameterEditor() {
    if (!selected || !parameterSchema?.columns?.length) return notice("#channel-message", "请先选择已加载规约参数的通道", true);
    showEditor("#parameter-editor");
  }
  async function loadParameters() {
    const request = ++parameterRequest, rtu = selected?.rtu_id;
    if (!rtu) { renderParameters(null); return; }
    try {
      const data = await api(`/api/channels/${encodeURIComponent(rtu)}/parameters`);
      if (request === parameterRequest && selected?.rtu_id === rtu) renderParameters(data);
    } catch (error) {
      if (request === parameterRequest) { selectedParameter = null; parameterSchema = null; renderParameters(null); notice("#channel-message", error.message, true); }
    }
  }
  function parameterBody() {
    const body = {};
    document.querySelectorAll("[data-parameter-field]").forEach(node => {
      const column = parameterSchema?.columns?.find(item => item.eng_name === node.dataset.parameterField);
      if (!column) return;
      if (node.tagName === "SELECT" || column.data_type === "int") body[column.eng_name] = keyValue(node.value);
      else if (column.data_type === "float") body[column.eng_name] = Number(node.value);
      else body[column.eng_name] = node.value;
    });
    return body;
  }
  async function loadPoints() {
    const kind = activeKind(), body = pointTableBody(kind);
    if (!body) return;
    setPointTab(kind);
    const request = ++requestId, rtu = selected?.rtu_id;
    const scope = `${rtu}/${kind}`;
    if (scope !== pointScope) {
      pointScope = scope; selectedPoint = null;
      body.innerHTML = '<tr><td colspan="6">正在读取测点…</td></tr>';
      $("#point-page-summary").textContent = ""; $("#point-prev").disabled = $("#point-next").disabled = true;
    }
    try {
      const data = rtu ? await api(`/api/channels/${rtu}/points/${kind}?limit=${limit}&offset=${offset}&fields=pnt_no,name,value`) : {items:[],total:0};
      if (request !== requestId || rtu !== selected?.rtu_id) return;
      total = data.total || 0;
      if (offset > 0 && offset >= total) { offset = Math.max(0, Math.floor((total - 1) / limit) * limit); return loadPoints(); }
      const points = data.items || [];
      body.innerHTML = points.map(p => `<tr><td>${names[kind]}</td><td>${esc(p.pnt_no)}</td><td>${esc(p.name)}</td><td>${esc(p.value)}</td><td><button data-point-edit='${esc(JSON.stringify({...p,kind}))}'>编辑</button> <button data-owner="${esc(rtu)}" data-version="${esc(p._version)}" data-point-delete="${kind}" data-point-no="${esc(p.pnt_no)}">删除</button></td></tr>`).join("") || `<tr><td colspan="5">${rtu ? "暂无测点" : "请先选择通道"}</td></tr>`;
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
    $("#point-name").value = point.name || "";
    $("#point-value").value = point.value == null ? 0 : point.value;
    $("#point-editor-title").textContent = `编辑${names[point.kind] || ""}测点`;
    $("#add-point-submit").textContent = "保存测点";
    $("#add-point-submit").hidden = false;
    $("#update-point").hidden = true;
    if ($("#point-editor-error")) $("#point-editor-error").textContent = "";
    showEditor("#point-editor");
  }
  const channelBody = () => ({rtu_id:keyValue($("#rtu_id").value),channel_id:keyValue($("#channel_id").value),channel_type:$("#channel_type").value,name:$("#name").value,packet_recording:Number($("#packet_recording").value),process_enable:Number($("#process_enable").value)});
  const pointBody = () => ({pnt_no:keyValue($("#point-no").value),name:$("#point-name").value,value:Number($("#point-value").value)});
  if ($("#channel-form")) $("#channel-form").onsubmit = async event => {
    event.preventDefault();
    try { await api("/api/channels", {method:"POST",body:JSON.stringify(channelBody())}); closeEditor("#channel-editor"); await loadChannels(); notice("#channel-message", "通道已新增"); }
    catch (error) { if ($("#channel-editor-error")) $("#channel-editor-error").textContent = error.message; notice("#channel-message", error.message, true); }
  };
  if ($("#update-channel")) $("#update-channel").onclick = async () => {
    if (!selected) return notice("#channel-message", "请先选择通道", true);
    try {
      const body = channelBody();
      if (body.rtu_id !== selected.rtu_id) throw new Error("修改通道时不能变更 RTU 编号；请新建通道");
      delete body.rtu_id;
      selected = (await api(`/api/channels/${selected.rtu_id}`, {method:"PATCH",headers:versionHeaders(selected._version),body:JSON.stringify(body)})).item;
      closeEditor("#channel-editor"); await loadChannels(); await loadParameters(); notice("#channel-message", "通道已保存");
    } catch (error) { if ($("#channel-editor-error")) $("#channel-editor-error").textContent = error.message; notice("#channel-message", error.message, true); }
  };
  if ($("#parameter-form")) $("#parameter-form").onsubmit = async event => {
    event.preventDefault();
    if (!selected || !parameterSchema) return notice("#channel-message", "请先选择通道", true);
    try {
      const body = parameterBody();
      const method = selectedParameter ? "PATCH" : "POST";
      const result = await api(`/api/channels/${encodeURIComponent(selected.rtu_id)}/parameters`, {method, headers:versionHeaders(selectedParameter?._version), body:JSON.stringify(body)});
      selectedParameter = result.item;
      renderParameters({item:selectedParameter, parameter_schema:parameterSchema});
      closeEditor("#parameter-editor");
      notice("#channel-message", "规约参数已保存");
    } catch (error) { notice("#channel-message", error.message, true); }
  };
  if ($("#delete-parameters")) $("#delete-parameters").onclick = async () => {
    if (!selected || !selectedParameter || !confirm("确定删除当前通道的规约参数？")) return;
    try {
      await api(`/api/channels/${encodeURIComponent(selected.rtu_id)}/parameters`, {method:"DELETE", headers:versionHeaders(selectedParameter._version), body:"{}"});
      selectedParameter = null; renderParameters({parameter_schema:parameterSchema}); closeEditor("#parameter-editor"); notice("#channel-message", "规约参数已删除");
    } catch (error) { notice("#channel-message", error.message, true); }
  };
  async function savePoint() {
    if (!selected) return notice("#point-message", "请先选择通道", true);
    const kind = $("#point-kind").value, body = pointBody(), owner = selected.rtu_id;
    if (selectedPoint) {
      if (kind !== selectedPoint.kind || body.pnt_no !== selectedPoint.pnt_no) throw new Error("修改测点时不能变更类型或点号；请新增测点");
      delete body.pnt_no;
      const edited = selectedPoint;
      if (edited.rtu_id !== owner) throw new Error("通道已切换，请重新选择测点");
      const result = await api(`/api/channels/${owner}/points/${kind}/${edited.pnt_no}`, {method:"PATCH",headers:versionHeaders(edited._version),body:JSON.stringify(body)});
      if (selectedPoint !== edited || selected?.rtu_id !== owner) return;
      selectedPoint = {...result.item,kind};
      await loadPoints(); closeEditor("#point-editor"); notice("#point-message", "测点已保存");
      return;
    }
    const result = await api(`/api/channels/${owner}/points/${kind}`, {method:"POST",body:JSON.stringify(body)});
    if (owner !== selected?.rtu_id || $("#point-kind").value !== kind) return;
    selectedPoint = {...result.item,kind}; setPointTab(kind); offset = 0;
    await loadPoints(); closeEditor("#point-editor"); notice("#point-message", "测点已新增");
  }
  if ($("#point-form")) $("#point-form").onsubmit = async event => {
    event.preventDefault();
    try { await savePoint(); }
    catch (error) { if ($("#point-editor-error")) $("#point-editor-error").textContent = error.message; notice("#point-message", error.message, true); }
  };
  if ($("#update-point")) $("#update-point").onclick = async () => { try { await savePoint(); } catch (error) { if ($("#point-editor-error")) $("#point-editor-error").textContent = error.message; notice("#point-message", error.message, true); } };
  if ($("#point-rtu")) $("#point-rtu").onchange = () => {
    selected = channelRows.find(row => String(row.rtu_id) === $("#point-rtu").value) || null;
    selectedPoint = null; offset = 0; if ($("#point-form")?.reset) $("#point-form").reset(); $("#point-kind").value = activeKind();
    const url = new URL(location.href); if (selected) url.searchParams.set("rtu_id", selected.rtu_id); else url.searchParams.delete("rtu_id");
    history.replaceState(null, "", url); loadPoints();
  };
  if ($("#kind-filter")) $("#kind-filter").onchange = () => { selectedPoint = null; offset = 0; setPointTab(activeKind()); $("#point-kind").value = activeKind(); loadPoints(); };
  if ($("#point-limit")) $("#point-limit").onchange = () => { limit = Number($("#point-limit").value); offset = 0; loadPoints(); };
  if ($("#point-prev")) $("#point-prev").onclick = () => { offset = Math.max(0, offset - limit); loadPoints(); };
  if ($("#point-next")) $("#point-next").onclick = () => { offset += limit; loadPoints(); };
  $("#reload").onclick = loadChannels;
  $("#channels")?.addEventListener("click", event => {
    if (event.target.closest("a,button")) return;
    const row = event.target.closest("tr[data-select]");
    if (!row) return;
    const item = channelRows.find(value => String(value.rtu_id) === row.dataset.select);
    if (item) { selected = item; loadParameters(); }
  });
  if ($("#new-channel")) $("#new-channel").onclick = prepareNewChannel;
  if ($("#edit-parameters")) $("#edit-parameters").onclick = openParameterEditor;
  if ($("#close-channel-editor")) $("#close-channel-editor").onclick = () => closeEditor("#channel-editor");
  if ($("#cancel-channel-editor")) $("#cancel-channel-editor").onclick = () => closeEditor("#channel-editor");
  if ($("#close-parameter-editor")) $("#close-parameter-editor").onclick = () => closeEditor("#parameter-editor");
  if ($("#cancel-parameter-editor")) $("#cancel-parameter-editor").onclick = () => closeEditor("#parameter-editor");
  document.addEventListener("click", async event => {
    const tab = event.target.closest("button[data-point-tab]"), edit = event.target.closest("button[data-edit]"), del = event.target.closest("button[data-delete]"),
      pointEdit = event.target.closest("button[data-point-edit]"), pointDelete = event.target.closest("button[data-point-delete]");
    try {
      if (tab) { selectedPoint = null; offset = 0; setPointTab(tab.dataset.pointTab); $("#point-kind").value = activeKind(); await loadPoints(); return; }
      if (edit) fillChannel(JSON.parse(edit.dataset.edit));
      if (del && confirm("确定删除该通道及其四遥点？")) { await api(`/api/channels/${del.dataset.delete}`, {method:"DELETE",headers:versionHeaders(del.dataset.version),body:"{}"}); selected = null; renderParameters(null); await loadChannels(); }
      if (pointEdit) fillPoint(JSON.parse(pointEdit.dataset.pointEdit));
      if (pointDelete && selected && pointDelete.dataset.owner === String(selected.rtu_id) && confirm("确定删除该测点？")) { await api(`/api/channels/${pointDelete.dataset.owner}/points/${pointDelete.dataset.pointDelete}/${pointDelete.dataset.pointNo}`, {method:"DELETE",headers:versionHeaders(pointDelete.dataset.version),body:"{}"}); selectedPoint = null; await loadPoints(); }
    } catch (error) { notice(pointTableBody() ? "#point-message" : "#channel-message", error.message, true); }
  });
  if ($("#new-point")) $("#new-point").onclick = prepareNewPoint;
  if ($("#close-point-editor")) $("#close-point-editor").onclick = () => closeEditor("#point-editor");
  if ($("#cancel-point-editor")) $("#cancel-point-editor").onclick = () => closeEditor("#point-editor");
  setPointTab(activeKind());
  loadChannels();
})();

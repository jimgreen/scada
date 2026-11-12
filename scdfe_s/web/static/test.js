(() => {
  "use strict";
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const protocolMap = {
    iec104: {label:"IEC 60870-5-104", service:"iec104"},
    "modbus-tcp": {label:"Modbus TCP", service:"modbus-tcp"},
    "modbus-rtu": {label:"Modbus RTU", service:"modbus-rtu"},
    can: {label:"CAN / CAN-FD", service:"can"},
    bacnet: {label:"BACnet/IP", service:"bacnet-server"},
    opc: {label:"OPC UA", service:"opc-server"},
  };
  const pointLabels = {yc:"遥测 YC", yx:"遥信 YX", yk:"遥控 YK", yt:"遥调 YT"};
  const runtimeKeys = new Set(["_version", "raw_value", "raw_status", "raw_time", "refresh_time", "process_status", "process_pid", "process_start_time", "process_stop_time", "process_heartbeat_time", "process_exit_code", "process_error"]);
  const state = {rtus:[], rtu:null, channel:null, protocol:null, service:null, points:{yc:[],yx:[],yk:[],yt:[]}, pages:{yc:0,yx:0,yk:0,yt:0}, request:0, editingPoint:null, loading:false};
  const PAGE_SIZE = 25;
  state.totals = {yc:0,yx:0,yk:0,yt:0};
  state.pointRequests = {yc:0,yx:0,yk:0,yt:0};
  let pendingRtu = null;
  const commandKinds = new Set(["yk", "yt"]);
  const rawStatusKinds = new Set(["yc", "yt"]);
  async function api(path, options = {}) {
    const response = await fetch(path, {cache:"no-store", ...options, headers:{"Content-Type":"application/json", ...(options.headers || {})}});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "请求失败");
    return data;
  }
  function message(text, error = false) { const node = $("#test-message"); node.textContent = text || ""; node.className = "message" + (error ? " error" : ""); }
  function protocolFor(type) { return protocolMap[type] || {label:type || "未配置", service:null}; }
  function table(items, empty = "暂无数据") {
    if (!items || !items.length) return `<p class="muted">${esc(empty)}</p>`;
    return `<dl class="key-values">${items.map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value === null || value === undefined || value === "" ? "—" : value)}</dd></div>`).join("")}</dl>`;
  }
  function channelRows(item) {
    return [["RTU 编号", item.rtu_id], ["通道号", item.channel_id], ["规约", protocolFor(item.channel_type).label], ["名称", item.name], ["保存报文", Number(item.packet_recording) ? "开启" : "关闭"], ["自动通信", Number(item.process_enable) ? "启用" : "停用"]];
  }
  function parameterRows(parameter, schema) {
    if (!parameter) return [];
    const columns = schema?.columns || Object.keys(parameter).map(eng_name => ({eng_name, chn_name:eng_name}));
    return columns.filter(column => !runtimeKeys.has(column.eng_name) && column.eng_name !== "id").map(column => [column.chn_name || column.eng_name, parameter[column.eng_name]]);
  }
  function formatPointTime(value) {
    if (!value) return "—";
    const date = new Date(Number(value));
    if (Number.isNaN(date.valueOf())) return String(value);
    const pad = part => String(part).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.` +
      String(date.getMilliseconds()).padStart(3, "0");
  }
  function pointFields(kind) {
    return ["pnt_no", "name", "value", "status"]
      .concat(rawStatusKinds.has(kind) ? ["raw_status"] : [])
      .concat(["refresh_time"])
      .concat(commandKinds.has(kind) ? ["cmd_handle_time"] : []);
  }
  function renderSummary() {
    const item = state.channel?.item;
    if (!item) return;
    const protocol = state.protocol;
    $("#metric-protocol").textContent = protocol.label;
    $("#metric-channel").textContent = item.channel_id ?? "—";
    $("#metric-packet").textContent = Number(item.packet_recording) ? "开启" : "关闭";
    $("#metric-points").textContent = Object.values(state.totals).reduce((n, total) => n + total, 0);
    $("#channel-table").innerHTML = table(channelRows(item));
    $("#parameter-table").innerHTML = table(parameterRows(state.channel.parameter, state.channel.parameter_schema), "该规约尚未配置参数");
  }
  function renderPointKind(kind) {
    const rows = state.points[kind] || [];
    const page = state.pages[kind] || 0;
    const slice = rows;
    const total = state.totals[kind];
    const body = $(`[data-point-body="${kind}"]`);
    const command = commandKinds.has(kind);
    const rawStatus = rawStatusKinds.has(kind);
    const html = slice.length ? slice.map(point => {
      const states = [point.status];
      if (rawStatus) states.push(point.raw_status);
      const times = [formatPointTime(point.refresh_time)];
      if (command) times.push(formatPointTime(point.cmd_handle_time));
      return `<tr><td>${esc(point.pnt_no)}</td><td>${esc(point.name || "")}</td><td>${esc(point.value)}</td>` +
        states.map(value => `<td>${esc(value)}</td>`).join("") +
        times.map(value => `<td>${esc(value)}</td>`).join("") +
        `<td><button type="button" data-point-action="${esc(kind)}" data-point-no="${esc(point.pnt_no)}">${command ? "下发" : "更新"}</button></td></tr>`;
    }).join("") : `<tr><td colspan="${6 + (rawStatus ? 1 : 0) + (command ? 1 : 0)}" class="muted">暂无定义</td></tr>`;
    if (body._pointHtml !== html) { body.innerHTML = html; body._pointHtml = html; }
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    $(`[data-point-pager="${kind}"]`).innerHTML = total > PAGE_SIZE ? `<button type="button" data-point-page="prev" data-point-kind="${kind}" ${page === 0 ? "disabled" : ""}>上一页</button><span>第 ${page + 1} / ${pages} 页，共 ${total} 点</span><button type="button" data-point-page="next" data-point-kind="${kind}" ${page + 1 >= pages ? "disabled" : ""}>下一页</button>` : `<span>${total} 点</span>`;
  }
  function renderPoints() { ["yc","yx","yk","yt"].forEach(renderPointKind); renderSummary(); }
  function renderService(status) {
    const item = status || {};
    $("#metric-running").textContent = item.running ? "运行中" : "已停止";
    $("#metric-connections").textContent = item.open_connections ?? item.connections?.length ?? 0;
    $("#service-status").innerHTML = table([["服务", item.label || state.service], ["运行状态", item.running ? "运行中" : "已停止"], ["绑定 RTU", item.rtu_id ?? item.config?.effective_rtu_id ?? "—"], ["连接数", item.open_connections ?? item.connections?.length ?? 0], ["四遥点数", item.point_count ?? "—"], ["最近错误", item.last_error || "无"]]);
    $("#connection-list").innerHTML = (item.connections || []).map(connection => `<div>[${esc(connection.time || "")}] ${esc(connection.ip || connection.peer || connection.client || "连接")} ${connection.accepted === false ? "已断开" : "已连接"}</div>`).join("") || "暂无连接";
    $("#service-start").disabled = !state.service || !!item.running;
    $("#service-stop").disabled = !state.service || !item.running;
    $("#service-poll").disabled = !state.service || !item.running || !["modbus-tcp","modbus-rtu","can"].includes(state.service);
  }
  async function fetchPointPage(rtuId, kind, page) {
    const fields = pointFields(kind).join(",");
    const read = offset => api(`/api/rtus/${encodeURIComponent(rtuId)}/points/${kind}?limit=${PAGE_SIZE}&offset=${offset}&fields=${fields}`);
    let result = await read(page * PAGE_SIZE);
    const last = Math.max(0, Math.ceil((result.total || 0) / PAGE_SIZE) - 1);
    if (page > last) { page = last; result = await read(page * PAGE_SIZE); }
    return {items:result.items || [], total:result.total ?? (result.items || []).length, page};
  }
  function acceptPointPage(kind, result) {
    state.points[kind] = result.items; state.totals[kind] = result.total; state.pages[kind] = result.page;
  }
  async function changePointPage(kind, direction) {
    if (!state.rtu) return;
    const id = state.rtu.rtu_id, scope = state.request, request = ++state.pointRequests[kind];
    const page = Math.max(0, state.pages[kind] + direction);
    try {
      const result = await fetchPointPage(id, kind, page);
      if (scope !== state.request || request !== state.pointRequests[kind]) return;
      acceptPointPage(kind, result); renderPointKind(kind); renderSummary();
    } catch (error) {
      if (scope === state.request && request === state.pointRequests[kind]) message(error.message, true);
    }
  }
  function refreshStatus(text, active = false, error = false) {
    const node = $("#test-refresh-status");
    if (!node) return;
    node.textContent = text;
    node.className = `refresh-status${active ? " is-active" : ""}${error ? " is-error" : ""}`;
  }
  function loadRtu(id, {fresh = false} = {}) {
    if (pendingRtu && String(pendingRtu.id) === String(id)) {
      return fresh ? pendingRtu.promise.then(() => String($("#rtu-select").value) === String(id) ? loadRtu(id) : undefined) : pendingRtu.promise;
    }
    const pending = {id};
    pending.promise = performLoadRtu(id).finally(() => { if (pendingRtu === pending) pendingRtu = null; });
    pendingRtu = pending;
    return pending.promise;
  }
  async function performLoadRtu(id) {
    const request = ++state.request;
    if (String(state.rtu?.rtu_id) !== String(id)) {
      state.pages = {yc:0,yx:0,yk:0,yt:0}; state.points = {yc:[],yx:[],yk:[],yt:[]};
      state.totals = {yc:0,yx:0,yk:0,yt:0}; state.channel = null;
      closePointUpdate(); renderPoints(); renderService({});
    }
    state.rtu = state.rtus.find(item => String(item.rtu_id) === String(id)) || null;
    if (!state.rtu) { state.loading = false; refreshStatus("请选择 RTU"); return; }
    state.loading = true;
    const protocol = protocolFor(state.rtu.channel_type);
    state.protocol = protocol; state.service = protocol.service;
    refreshStatus("正在刷新…", true);
    try {
      const pointRequests = Object.fromEntries(Object.keys(state.points).map(kind => [kind, ++state.pointRequests[kind]]));
      const [channel, points, communications] = await Promise.all([
        api(`/api/rtus/${encodeURIComponent(id)}/channel`),
        Promise.all(Object.keys(state.points).map(async kind => [kind, await fetchPointPage(id, kind, state.pages[kind])])),
        api(`/api/monitor/communications?rtu_id=${encodeURIComponent(id)}`)
      ]);
      if (request !== state.request) return;
      state.channel = channel;
      for (const [kind, result] of points) if (pointRequests[kind] === state.pointRequests[kind]) acceptPointPage(kind, result);
      const match = (communications.items || []).find(item => String(item.rtu_id) === String(id));
      renderPoints(); renderService(match || {rtu_id:id, last_error:"未取得当前 RTU 的运行状态"});
      // Scope the shared log browser to the RTU currently selected above.
      // The protocol process remains a fallback for manager-only logs, while
      // channel files are preferred whenever they exist.
      document.dispatchEvent(new CustomEvent("scada:log-process", {
        detail: {
          process: state.rtu.channel_id != null
            ? `scdfe_s-channel-${state.rtu.channel_id}`
            : (protocol.service ? `scdfe_s-${protocol.service}` : ""),
          // The RTU list already carries the canonical channel_id.  Prefer it
          // so the default is available even if a protocol endpoint omits the
          // field in its channel response.
          channel_id: state.rtu.channel_id ?? state.channel?.item?.channel_id ?? "",
        },
      }));
      message("");
      refreshStatus(`已刷新 ${formatPointTime(Date.now())}`);
    } catch (error) {
      if (request === state.request) { state.channel = null; message(error.message, true); refreshStatus("刷新失败", false, true); }
    } finally {
      if (request === state.request) state.loading = false;
    }
  }
  async function loadRtus() {
    try {
      state.rtus = [];
      for (let offset = 0; ; offset += 500) {
        const data = await api(`/api/rtus?limit=500&offset=${offset}&fields=rtu_id,channel_id,channel_type,name`);
        state.rtus.push(...(data.items || []));
        if (!(data.items || []).length || state.rtus.length >= (data.total ?? state.rtus.length)) break;
      }
      const select = $("#rtu-select");
      select.innerHTML = state.rtus.map(item => `<option value="${esc(item.rtu_id)}">RTU ${esc(item.rtu_id)} · ${esc(item.name || "未命名")} · ${esc(protocolFor(item.channel_type).label)}</option>`).join("");
      if (!state.rtus.length) { select.innerHTML = '<option value="">暂无 RTU</option>'; return; }
      const queryId = new URLSearchParams(location.search).get("rtu_id");
      select.value = state.rtus.some(item => String(item.rtu_id) === queryId) ? queryId : String(state.rtus[0].rtu_id);
      await loadRtu(select.value);
    } catch (error) { message(error.message, true); refreshStatus("刷新失败", false, true); }
  }
  async function serviceAction(action) {
    if (!state.service || !state.rtu) return;
    const id = state.rtu.rtu_id;
    try {
      await api(`/api/rtus/${encodeURIComponent(id)}/service/${action}`, {method:"POST", body:"{}"});
      if (String(state.rtu?.rtu_id) !== String(id)) return;
      await loadRtu(id, {fresh:true});
      if (String(state.rtu?.rtu_id) === String(id)) message(action === "start" ? "规约服务已启动" : "规约服务已停止");
    }
    catch (error) { if (String(state.rtu?.rtu_id) === String(id)) message(error.message, true); }
  }
  async function poll() {
    if (!state.rtu) return;
    const id = state.rtu.rtu_id;
    try {
      await api(`/api/rtus/${encodeURIComponent(id)}/service/poll`, {method:"POST", body:"{}"});
      if (String(state.rtu?.rtu_id) !== String(id)) return;
      await loadRtu(id, {fresh:true});
      if (String(state.rtu?.rtu_id) === String(id)) message("采集已完成");
    }
    catch (error) { if (String(state.rtu?.rtu_id) === String(id)) message(error.message, true); }
  }
  function closePointUpdate() {
    const dialog = $("#point-update-dialog");
    state.editingPoint = null;
    if (dialog?.open && typeof dialog.close === "function") dialog.close();
    if ($("#point-update-error")) $("#point-update-error").textContent = "";
  }
  function openPointUpdate(kind, pointNo) {
    const current = (state.points[kind] || []).find(point => String(point.pnt_no) === String(pointNo)) || {};
    state.editingPoint = {kind, pointNo, current, rtuId:state.rtu.rtu_id};
    $("#point-update-title").textContent = `${pointLabels[kind]} ${pointNo} · ${commandKinds.has(kind) ? "下发" : "更新"}`;
    $("#point-update-value").value = current.value ?? 0;
    $("#point-update-status").value = current.status ?? 0;
    $("#point-update-status-field").hidden = commandKinds.has(kind);
    $("#point-update-error").textContent = "";
    const dialog = $("#point-update-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.open = true;
    $("#point-update-value").focus();
    $("#point-update-value").select();
  }
  async function submitPointUpdate() {
    const edit = state.editingPoint;
    if (!edit) return;
    const {kind, pointNo, rtuId} = edit;
    const number = Number($("#point-update-value").value);
    if (!Number.isFinite(number)) { $("#point-update-error").textContent = "请输入有限数字"; return; }
    const value = kind === "yx" || kind === "yk" ? (number ? 1 : 0) : number;
    let status = Number($("#point-update-status").value);
    if (!commandKinds.has(kind) && (!Number.isInteger(status) || !Number.isFinite(status))) {
      $("#point-update-error").textContent = "状态必须是整数";
      return;
    }
    try {
      const path = commandKinds.has(kind) ? `/api/rtus/${rtuId}/commands/${kind}/${encodeURIComponent(pointNo)}` : `/api/rtus/${rtuId}/points/${kind}/${encodeURIComponent(pointNo)}`;
      await api(path, {method:commandKinds.has(kind) ? "POST" : "PATCH", body:JSON.stringify(commandKinds.has(kind) ? {value} : {value, status})});
      if (String(state.rtu?.rtu_id) === String(rtuId)) {
        closePointUpdate();
        await loadRtu(rtuId, {fresh:true}); message(kind === "yk" || kind === "yt" ? "指令已提交" : "点值已更新");
      }
    } catch (error) { message(error.message, true); }
  }
  function initTabs() {
    $$("[data-page-tab]").forEach(tab => tab.addEventListener("click", () => { $$("[data-page-tab]").forEach(node => node.setAttribute("aria-selected", String(node === tab))); $$("[data-tab-panel]").forEach(panel => panel.hidden = panel.dataset.tabPanel !== tab.dataset.pageTab); }));
    $$("[data-point-tab]").forEach(tab => tab.addEventListener("click", () => { $$("[data-point-tab]").forEach(node => node.setAttribute("aria-selected", String(node === tab))); $$("[data-point-panel]").forEach(panel => panel.hidden = panel.dataset.pointPanel !== tab.dataset.pointTab); }));
  }
  $("#rtu-select").addEventListener("change", event => loadRtu(event.target.value));
  $("#test-refresh").addEventListener("click", () => loadRtu($("#rtu-select").value));
  $("#points-refresh").addEventListener("click", () => loadRtu($("#rtu-select").value));
  $("#service-start").addEventListener("click", () => serviceAction("start"));
  $("#service-stop").addEventListener("click", () => serviceAction("stop"));
  $("#service-poll").addEventListener("click", poll);
  $("#point-panels").addEventListener("click", event => { const action = event.target.closest("[data-point-action]"); if (action) openPointUpdate(action.dataset.pointAction, action.dataset.pointNo); const page = event.target.closest("[data-point-page]"); if (page) changePointPage(page.dataset.pointKind, page.dataset.pointPage === "next" ? 1 : -1); });
  $("#point-update-form").addEventListener("submit", event => { event.preventDefault(); submitPointUpdate(); });
  $("#point-update-close").addEventListener("click", closePointUpdate);
  $("#point-update-cancel").addEventListener("click", closePointUpdate);
  $("#point-update-dialog").addEventListener("cancel", () => { state.editingPoint = null; });
  initTabs(); loadRtus(); setInterval(() => { if (!document.hidden && state.rtu && !state.loading) loadRtu(state.rtu.rtu_id); }, 10000);
})();

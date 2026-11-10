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
  const state = {rtus:[], rtu:null, channel:null, protocol:null, service:null, points:{yc:[],yx:[],yk:[],yt:[]}, pages:{yc:0,yx:0,yk:0,yt:0}, request:0, editingPoint:null};
  const PAGE_SIZE = 25;
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
    $("#metric-points").textContent = Object.values(state.points).reduce((n, rows) => n + rows.length, 0);
    $("#channel-table").innerHTML = table(channelRows(item));
    $("#parameter-table").innerHTML = table(parameterRows(state.channel.parameter, state.channel.parameter_schema), "该规约尚未配置参数");
  }
  function renderPointKind(kind) {
    const rows = state.points[kind] || [];
    const page = state.pages[kind] || 0;
    const slice = rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
    const body = $(`[data-point-body="${kind}"]`);
    const command = commandKinds.has(kind);
    const rawStatus = rawStatusKinds.has(kind);
    body.innerHTML = slice.length ? slice.map(point => {
      const states = [point.status];
      if (rawStatus) states.push(point.raw_status);
      const times = [formatPointTime(point.refresh_time)];
      if (command) times.push(formatPointTime(point.cmd_handle_time));
      return `<tr><td>${esc(point.pnt_no)}</td><td>${esc(point.name || "")}</td><td>${esc(point.value)}</td>` +
        states.map(value => `<td>${esc(value)}</td>`).join("") +
        times.map(value => `<td>${esc(value)}</td>`).join("") +
        `<td><button type="button" data-point-action="${esc(kind)}" data-point-no="${esc(point.pnt_no)}">${command ? "下发" : "更新"}</button></td></tr>`;
    }).join("") : `<tr><td colspan="${6 + (rawStatus ? 1 : 0) + (command ? 1 : 0)}" class="muted">暂无定义</td></tr>`;
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    $(`[data-point-pager="${kind}"]`).innerHTML = rows.length > PAGE_SIZE ? `<button type="button" data-point-page="prev" data-point-kind="${kind}" ${page === 0 ? "disabled" : ""}>上一页</button><span>第 ${page + 1} / ${pages} 页，共 ${rows.length} 点</span><button type="button" data-point-page="next" data-point-kind="${kind}" ${page + 1 >= pages ? "disabled" : ""}>下一页</button>` : `<span>${rows.length} 点</span>`;
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
  async function fetchPoints(rtuId) {
    async function fetchPointKind(kind) {
      const result = [];
      const fields = pointFields(kind).join(",");
      const first = await api(`/api/rtus/${encodeURIComponent(rtuId)}/points/${kind}?limit=500&offset=0&fields=${fields}`);
      result.push(...(first.items || []));
      for (let offset = result.length; offset < (first.total || result.length); offset += 500) {
        const next = await api(`/api/rtus/${encodeURIComponent(rtuId)}/points/${kind}?limit=500&offset=${offset}&fields=${fields}`);
        result.push(...(next.items || []));
        if (!(next.items || []).length) break;
      }
      return [kind, result];
    }
    return Object.fromEntries(await Promise.all(["yc","yx","yk","yt"].map(fetchPointKind)));
  }
  function refreshStatus(text, active = false, error = false) {
    const node = $("#test-refresh-status");
    if (!node) return;
    node.textContent = text;
    node.className = `refresh-status${active ? " is-active" : ""}${error ? " is-error" : ""}`;
  }
  async function loadRtu(id) {
    const request = ++state.request;
    state.rtu = state.rtus.find(item => String(item.rtu_id) === String(id)) || null;
    if (!state.rtu) { refreshStatus("请选择 RTU"); return; }
    const protocol = protocolFor(state.rtu.channel_type);
    state.protocol = protocol; state.service = protocol.service;
    refreshStatus("正在刷新…", true);
    try {
      const [channel, points, communications, status] = await Promise.all([
        api(`/api/rtus/${encodeURIComponent(id)}/channel`), fetchPoints(id), api("/api/monitor/communications"), protocol.service ? api(`/api/${protocol.service}/status`) : Promise.resolve({item:{}})
      ]);
      if (request !== state.request) return;
      state.channel = channel; state.points = points; state.pages = {yc:0,yx:0,yk:0,yt:0};
      const match = (communications.items || []).find(item => item.name === protocol.service);
      renderPoints(); renderService(status.item || match || {});
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
    }
  }
  async function loadRtus() {
    try {
      const data = await api("/api/rtus?limit=500");
      state.rtus = data.items || [];
      const select = $("#rtu-select");
      select.innerHTML = state.rtus.map(item => `<option value="${esc(item.rtu_id)}">RTU ${esc(item.rtu_id)} · ${esc(item.name || "未命名")} · ${esc(protocolFor(item.channel_type).label)}</option>`).join("");
      if (!state.rtus.length) { select.innerHTML = '<option value="">暂无 RTU</option>'; return; }
      const queryId = new URLSearchParams(location.search).get("rtu_id");
      select.value = state.rtus.some(item => String(item.rtu_id) === queryId) ? queryId : String(state.rtus[0].rtu_id);
      await loadRtu(select.value);
    } catch (error) { message(error.message, true); refreshStatus("刷新失败", false, true); }
  }
  async function serviceAction(action) {
    if (!state.service) return;
    try { await api(`/api/${state.service}/${action}`, {method:"POST", body:"{}"}); await loadRtu($("#rtu-select").value); message(action === "start" ? "规约服务已启动" : "规约服务已停止"); }
    catch (error) { message(error.message, true); }
  }
  async function poll() {
    try { await api(`/api/${state.service}/poll`, {method:"POST", body:"{}"}); await loadRtu($("#rtu-select").value); message("采集已完成"); }
    catch (error) { message(error.message, true); }
  }
  function closePointUpdate() {
    const dialog = $("#point-update-dialog");
    state.editingPoint = null;
    if (dialog?.open && typeof dialog.close === "function") dialog.close();
    if ($("#point-update-error")) $("#point-update-error").textContent = "";
  }
  function openPointUpdate(kind, pointNo) {
    const current = (state.points[kind] || []).find(point => String(point.pnt_no) === String(pointNo)) || {};
    state.editingPoint = {kind, pointNo, current};
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
    const {kind, pointNo} = edit;
    const number = Number($("#point-update-value").value);
    if (!Number.isFinite(number)) { $("#point-update-error").textContent = "请输入有限数字"; return; }
    const value = kind === "yx" || kind === "yk" ? (number ? 1 : 0) : number;
    let status = Number($("#point-update-status").value);
    if (!commandKinds.has(kind) && (!Number.isInteger(status) || !Number.isFinite(status))) {
      $("#point-update-error").textContent = "状态必须是整数";
      return;
    }
    try {
      const path = commandKinds.has(kind) ? `/api/rtus/${state.rtu.rtu_id}/commands/${kind}/${encodeURIComponent(pointNo)}` : `/api/rtus/${state.rtu.rtu_id}/points/${kind}/${encodeURIComponent(pointNo)}`;
      await api(path, {method:commandKinds.has(kind) ? "POST" : "PATCH", body:JSON.stringify(commandKinds.has(kind) ? {value} : {value, status})});
      closePointUpdate();
      await loadRtu(state.rtu.rtu_id); message(kind === "yk" || kind === "yt" ? "指令已提交" : "点值已更新");
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
  $("#point-panels").addEventListener("click", event => { const action = event.target.closest("[data-point-action]"); if (action) openPointUpdate(action.dataset.pointAction, action.dataset.pointNo); const page = event.target.closest("[data-point-page]"); if (page) { const kind = page.dataset.pointKind; state.pages[kind] = Math.max(0, state.pages[kind] + (page.dataset.pointPage === "next" ? 1 : -1)); renderPointKind(kind); } });
  $("#point-update-form").addEventListener("submit", event => { event.preventDefault(); submitPointUpdate(); });
  $("#point-update-close").addEventListener("click", closePointUpdate);
  $("#point-update-cancel").addEventListener("click", closePointUpdate);
  $("#point-update-dialog").addEventListener("cancel", () => { state.editingPoint = null; });
  initTabs(); loadRtus(); setInterval(() => { if (!document.hidden && state.rtu) loadRtu(state.rtu.rtu_id); }, 10000);
})();

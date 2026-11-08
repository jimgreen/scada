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
  const state = {rtus:[], rtu:null, channel:null, protocol:null, service:null, points:{yc:[],yx:[],yk:[],yt:[]}, pages:{yc:0,yx:0,yk:0,yt:0}, request:0};
  const PAGE_SIZE = 25;
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
    return [["RTU 编号", item.rtu_id], ["通道号", item.channel_id], ["规约", protocolFor(item.channel_type).label], ["编码", item.code], ["名称", item.name], ["保存报文", Number(item.packet_recording) ? "开启" : "关闭"], ["自动通信", Number(item.process_enable) ? "启用" : "停用"]];
  }
  function parameterRows(parameter, schema) {
    if (!parameter) return [];
    const columns = schema?.columns || Object.keys(parameter).map(eng_name => ({eng_name, chn_name:eng_name}));
    return columns.filter(column => !runtimeKeys.has(column.eng_name) && column.eng_name !== "id").map(column => [column.chn_name || column.eng_name, parameter[column.eng_name]]);
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
    body.innerHTML = slice.length ? slice.map(point => `<tr><td>${esc(point.pnt_no)}</td><td>${esc(point.name || point.code || "")}</td><td>${esc(point.value)}</td><td>${esc(point.status)}</td><td><button type="button" data-point-action="${esc(kind)}" data-point-no="${esc(point.pnt_no)}">${kind === "yk" || kind === "yt" ? "下发" : "更新"}</button></td></tr>`).join("") : '<tr><td colspan="5" class="muted">暂无定义</td></tr>';
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
    const result = {};
    for (const kind of ["yc","yx","yk","yt"]) {
      const first = await api(`/api/rtus/${encodeURIComponent(rtuId)}/points/${kind}?limit=500&offset=0`);
      result[kind] = first.items || [];
      for (let offset = result[kind].length; offset < (first.total || result[kind].length); offset += 500) {
        const next = await api(`/api/rtus/${encodeURIComponent(rtuId)}/points/${kind}?limit=500&offset=${offset}`);
        result[kind].push(...(next.items || []));
        if (!(next.items || []).length) break;
      }
    }
    return result;
  }
  async function loadRtu(id) {
    const request = ++state.request;
    state.rtu = state.rtus.find(item => String(item.rtu_id) === String(id)) || null;
    if (!state.rtu) return;
    const protocol = protocolFor(state.rtu.channel_type);
    state.protocol = protocol; state.service = protocol.service;
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
    } catch (error) { if (request === state.request) { state.channel = null; message(error.message, true); } }
  }
  async function loadRtus() {
    try {
      const data = await api("/api/rtus?limit=500");
      state.rtus = data.items || [];
      const select = $("#rtu-select");
      select.innerHTML = state.rtus.map(item => `<option value="${esc(item.rtu_id)}">RTU ${esc(item.rtu_id)} · ${esc(item.name || item.code || "未命名")} · ${esc(protocolFor(item.channel_type).label)}</option>`).join("");
      if (!state.rtus.length) { select.innerHTML = '<option value="">暂无 RTU</option>'; return; }
      const queryId = new URLSearchParams(location.search).get("rtu_id");
      select.value = state.rtus.some(item => String(item.rtu_id) === queryId) ? queryId : String(state.rtus[0].rtu_id);
      await loadRtu(select.value);
    } catch (error) { message(error.message, true); }
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
  async function updatePoint(kind, pointNo) {
    const current = (state.points[kind] || []).find(point => String(point.pnt_no) === String(pointNo));
    const raw = window.prompt(`${pointLabels[kind]} ${pointNo} 输入${kind === "yk" || kind === "yt" ? "下发" : "更新"}值`, String(current?.value ?? 0));
    if (raw === null) return;
    const number = Number(raw); if (!Number.isFinite(number)) { message("请输入有限数字", true); return; }
    const value = kind === "yx" || kind === "yk" ? (number ? 1 : 0) : number;
    try {
      const path = kind === "yk" || kind === "yt" ? `/api/rtus/${state.rtu.rtu_id}/commands/${kind}/${encodeURIComponent(pointNo)}` : `/api/rtus/${state.rtu.rtu_id}/points/${kind}/${encodeURIComponent(pointNo)}`;
      await api(path, {method:kind === "yk" || kind === "yt" ? "POST" : "PATCH", body:JSON.stringify(kind === "yk" || kind === "yt" ? {value} : {value, status:0})});
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
  $("#point-panels").addEventListener("click", event => { const action = event.target.closest("[data-point-action]"); if (action) updatePoint(action.dataset.pointAction, action.dataset.pointNo); const page = event.target.closest("[data-point-page]"); if (page) { const kind = page.dataset.pointKind; state.pages[kind] = Math.max(0, state.pages[kind] + (page.dataset.pointPage === "next" ? 1 : -1)); renderPointKind(kind); } });
  initTabs(); loadRtus(); setInterval(() => { if (!document.hidden && state.rtu) loadRtu(state.rtu.rtu_id); }, 10000);
})();

(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const services = ["iec104","modbus-tcp","modbus-tcp-server","modbus-rtu","modbus-rtu-server","can","can-server","opc-server","bacnet-server"];
  const protocolLabels = {
    iec104: "IEC 60870-5-104", "modbus-tcp": "Modbus TCP", "modbus-rtu": "Modbus RTU",
    can: "CAN / CAN-FD", bacnet: "BACnet/IP", opc: "OPC UA",
  };
  const serviceByType = {
    iec104: "iec104", "modbus-tcp": "modbus-tcp", "modbus-rtu": "modbus-rtu",
    can: "can", bacnet: "bacnet-server", opc: "opc-server",
  };
  const statusName = {0:"停止", 1:"启动中", 2:"运行", 3:"停止中", 4:"处理中", 5:"错误"};
  async function api(path, options) {
    const r = await fetch(path, {headers: {"Content-Type":"application/json"}, ...(options || {})});
    const body = await r.json();
    if (!r.ok) throw new Error(body.error && body.error.message || "请求失败");
    return body;
  }
  function msg(text, error) { const node = $("#message"); if (node) { node.textContent = text || ""; node.className = "message" + (error ? " error" : ""); } }
  let requestId = 0, pending = 0, lastMarkup = null;
  async function fetchView(path) {
    const id = ++requestId;
    pending++;
    try { const data = await api(path); return id === requestId ? data : null; }
    catch (error) { if (id === requestId) throw error; return null; }
    finally { pending--; }
  }
  function show(selector, markup) {
    // Each standalone monitor has one list. Keep focused buttons and text
    // selections when the next poll returns the same displayed values.
    if (lastMarkup !== markup) { $(selector).innerHTML = markup; lastMarkup = markup; }
  }
  function fmt(value, timeOnly = false) {
    if (!value) return "—";
    const date = new Date(Number(value));
    if (Number.isNaN(date.valueOf())) return String(value);
    const p = n => String(n).padStart(2, "0");
    const clock = `${p(date.getHours())}:${p(date.getMinutes())}:${p(date.getSeconds())}`;
    return timeOnly ? clock : `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())} ${clock}`;
  }
  function channelStatus(row, service) {
    if (service && service.running) return "运行中";
    const value = Number(row.process_status);
    return statusName[value] || (value ? String(value) : "已停止");
  }
  async function loadProcesses() {
    try {
      const [channels, processData] = await Promise.all([
        api("/api/rtus?limit=500&offset=0&sort=rtu_id&order=asc&fields=rtu_id,channel_id,channel_type,code,name,process_status,process_pid,process_start_time,process_stop_time,process_heartbeat_time,process_error,process_enable"),
        api("/api/monitor/processes"),
      ]);
      if (!channels || !processData) return;
      const servicesByName = new Map((processData.items || []).map(item => [item.name, item]));
      const servicesByRtu = new Map((processData.items || []).filter(item => item.rtu_id != null).map(item => [String(item.rtu_id), item]));
      const rows = (channels.items || []).map(channel => {
        const fallbackName = serviceByType[channel.channel_type];
        const service = servicesByRtu.get(String(channel.rtu_id)) || (fallbackName ? servicesByName.get(fallbackName) : null);
        const serviceName = service?.name || fallbackName;
        const status = service?.status || {};
        const effectiveRtu = service?.rtu_id ?? status.rtu_id ?? status.config?.effective_rtu_id;
        const bound = !!service && String(effectiveRtu ?? channel.rtu_id) === String(channel.rtu_id);
        const running = bound && service.running;
        const error = bound ? (status.last_error || channel.process_error || "—") : (channel.process_error || "—");
        const action = bound ? `<button data-service="${esc(serviceName)}" data-action="${running ? "stop" : "start"}">${running ? "停止" : "启动"}</button>` : "";
        const processName = serviceName ? `scdfe_s-${serviceName}` : "—";
        const startedAt = bound ? status.started_at : channel.process_start_time;
        const heartbeatAt = running ? Date.now() : channel.process_heartbeat_time;
        return `<tr><td><strong>RTU ${esc(channel.rtu_id)} · ${esc(channel.name || channel.code || "未命名")}</strong><span class="cell-sub">通道 ${esc(channel.channel_id ?? "—")} · ${esc(channel.code || "")}</span></td><td class="mono">${esc(processName)}</td><td>${esc(protocolLabels[channel.channel_type] || channel.channel_type || "未配置")}</td><td>${esc(channelStatus(channel, bound ? service : null))}</td><td class="mono">${esc(channel.process_pid || "—")}</td><td>${fmt(startedAt)}</td><td>${fmt(heartbeatAt, true)}</td><td title="${esc(error)}">${esc(error)}</td><td>${action} <a href="/communication">通信</a> <a href="/logs?channel_id=${encodeURIComponent(channel.channel_id ?? "")}">日志</a></td></tr>`;
      }).join("");
      show("#rows", rows || `<tr><td colspan="9">暂无通信通道</td></tr>`);
      msg(`共 ${(channels.items || []).length} 个通信通道`);
      msg("");
    } catch (e) { msg(e.message, true); }
  }
  async function loadCommunications() {
    try {
      const channels = await fetchView("/api/rtus?limit=500&offset=0");
      if (!channels) return;
      const data = await fetchView("/api/monitor/communications");
      if (!data) return;
      const serviceStatus = new Map((data.items || []).map(item => [item.name, item]));
      const serviceByRtu = new Map((data.items || []).filter(item => item.rtu_id != null).map(item => [String(item.rtu_id), item]));
      // The aggregate monitor response remains compatible with older Web
      // workers. Refresh each bound service status when available so the
      // table can identify the actual RTU behind a protocol service.
      await Promise.all([...new Set((channels.items || []).map(item => serviceByType[item.channel_type]).filter(Boolean))].map(async name => {
        try {
          const detail = await api(`/api/${encodeURIComponent(name)}/status`);
          if (detail.item) serviceStatus.set(name, {...serviceStatus.get(name), ...detail.item, name});
        } catch (_) { /* the aggregate status remains usable */ }
      }));
      const rows = (channels.items || []).map(channel => {
        const fallbackName = serviceByType[channel.channel_type];
        const service = serviceByRtu.get(String(channel.rtu_id)) || (fallbackName ? serviceStatus.get(fallbackName) : null);
        const serviceName = service?.name || fallbackName;
        const bound = !!service && String(service.rtu_id ?? channel.rtu_id) === String(channel.rtu_id);
        const state = bound ? (service.running ? "运行中" : "已停止") : (service ? "未绑定" : "未配置");
        const connections = bound ? (service.open_connections ?? 0) : "—";
        const pointCount = bound ? (service.point_count ?? 0) : "—";
        const error = bound ? (service.last_error || "无") : (channel.process_error || "—");
        const action = bound
          ? `<button data-service="${esc(service.name)}" data-action="${service.running ? "stop" : "start"}">${service.running ? "停止" : "启动"}</button>`
          : "";
        return `<tr><td><strong>${esc(channel.rtu_id)} · ${esc(channel.name || channel.code || "未命名")}</strong><small class="cell-sub">${esc(channel.code || "")}</small></td><td>${esc(channel.channel_id ?? "—")}</td><td>${esc(protocolLabels[channel.channel_type] || channel.channel_type || "未配置")}</td><td>${Number(channel.process_enable) ? "启用" : "停用"}</td><td><span class="communication-state ${bound && service.running ? "is-running" : ""}">${state}</span></td><td>${esc(connections)}</td><td>${esc(pointCount)}</td><td>${Number(channel.packet_recording) ? "开启" : "关闭"}</td><td class="communication-error">${esc(error)}</td><td class="communication-actions">${action}<a href="/test?rtu_id=${encodeURIComponent(channel.rtu_id)}">查看</a></td></tr>`;
      }).join("") || `<tr><td colspan="10" class="empty">暂无通信通道</td></tr>`;
      show("#communication-rows", rows);
      msg(`共 ${channels.total ?? (channels.items || []).length} 个通道`);
    } catch (e) { msg(e.message, true); }
  }
  async function loadLogs() {
    try {
      const service = $("#service").value, level = $("#level").value, keyword = $("#keyword").value;
      const query = new URLSearchParams({limit:"500"}); if (service) query.set("service", service); if (level) query.set("level", level); if (keyword) query.set("keyword", keyword);
      const data = await fetchView("/api/monitor/logs?" + query.toString());
      if (!data) return;
      show("#log-list", data.items.map(x => `<div class="${esc(x.level)}">[${esc(x.time)}] [${esc(x.service)}] ${esc(x.message)}</div>`).join("") || "暂无日志");
      msg("");
    } catch (e) { msg(e.message, true); }
  }
  async function action(button) {
    button.disabled = true;
    try { await api(`/api/monitor/communications/${encodeURIComponent(button.dataset.service)}/${button.dataset.action}`, {method:"POST", body:"{}"}); await (document.body.dataset.monitor === "processes" ? loadProcesses() : loadCommunications()); msg("操作已完成"); }
    catch (e) { msg(e.message, true); } finally { button.disabled = false; }
  }
  if (document.body.dataset.monitor) {
    const kind = document.body.dataset.monitor;
    if (kind === "processes") { loadProcesses(); $("#refresh").onclick = loadProcesses; }
    if (kind === "communications") { loadCommunications(); $("#refresh").onclick = loadCommunications; }
    if (kind === "logs") {
      const select = $("#service"); services.forEach(x => select.insertAdjacentHTML("beforeend", `<option value="${esc(x)}">${esc(x)}</option>`));
      loadLogs(); $("#refresh").onclick = loadLogs; $("#service").onchange = loadLogs; $("#level").onchange = loadLogs; $("#keyword").oninput = (() => { let t; return () => { clearTimeout(t); t = setTimeout(loadLogs, 250); }; })();
    }
    document.addEventListener("click", e => { const b = e.target.closest("button[data-service]"); if (b) action(b); });
    setInterval(() => { if (!document.hidden && !pending) { if (kind === "processes") loadProcesses(); else if (kind === "communications") loadCommunications(); else loadLogs(); } }, 5000);
  }
})();

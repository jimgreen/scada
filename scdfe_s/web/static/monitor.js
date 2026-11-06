(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const services = ["iec104","modbus-tcp","modbus-tcp-server","modbus-rtu","modbus-rtu-server","can","can-server","opc-server","bacnet-server"];
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
  async function loadProcesses() {
    try {
      const data = await fetchView("/api/monitor/processes");
      if (!data) return;
      show("#rows", data.items.map(x => {
        const s = x.status || {};
        const action = x.running ? "stop" : "start";
        return `<tr><td>${esc(x.label)}</td><td>${x.running ? "运行中" : "已停止"}</td><td>${esc(s.rtu_id || s.config && s.config.effective_rtu_id || "")}</td><td>${esc(s.started_at || "")}</td><td>${esc(s.last_error || "")}</td><td><button data-service="${esc(x.name)}" data-action="${action}">${action === "start" ? "启动" : "停止"}</button> <a href="/communication">通信</a></td></tr>`;
      }).join("") || `<tr><td colspan="6">暂无服务</td></tr>`);
      msg("已更新 " + new Date().toLocaleTimeString());
    } catch (e) { msg(e.message, true); }
  }
  async function loadCommunications() {
    try {
      const data = await fetchView("/api/monitor/communications");
      if (!data) return;
      show("#cards", data.items.map(x => `<section class="card"><h2>${esc(x.label)}</h2><div class="status-grid"><div class="metric"><b>${x.running ? "运行中" : "停止"}</b><span>状态</span></div><div class="metric"><b>${esc(x.open_connections || 0)}</b><span>连接</span></div><div class="metric"><b>${esc(x.point_count || 0)}</b><span>测点</span></div></div><p class="muted">${esc(x.last_error || "无错误")}</p><button data-service="${esc(x.name)}" data-action="${x.running ? "stop" : "start"}">${x.running ? "停止" : "启动"}</button> <a href="/${esc(x.name)}">打开通道页面</a></section>`).join(""));
      msg("已更新 " + new Date().toLocaleTimeString());
    } catch (e) { msg(e.message, true); }
  }
  async function loadLogs() {
    try {
      const service = $("#service").value, level = $("#level").value, keyword = $("#keyword").value;
      const query = new URLSearchParams({limit:"500"}); if (service) query.set("service", service); if (level) query.set("level", level); if (keyword) query.set("keyword", keyword);
      const data = await fetchView("/api/monitor/logs?" + query.toString());
      if (!data) return;
      show("#log-list", data.items.map(x => `<div class="${esc(x.level)}">[${esc(x.time)}] [${esc(x.service)}] ${esc(x.message)}</div>`).join("") || "暂无日志");
      msg("已更新 " + new Date().toLocaleTimeString());
    } catch (e) { msg(e.message, true); }
  }
  async function action(button) {
    button.disabled = true;
    try { await api(`/api/monitor/communications/${encodeURIComponent(button.dataset.service)}/${button.dataset.action}`, {method:"POST", body:"{}"}); msg("操作已完成"); await (document.body.dataset.monitor === "processes" ? loadProcesses() : loadCommunications()); }
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

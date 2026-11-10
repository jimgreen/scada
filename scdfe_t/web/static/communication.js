(function () {
  "use strict";
  const $ = s => document.querySelector(s);
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const statuses = {0:"停止",1:"启动中",2:"运行",3:"停止中",4:"处理中",5:"错误"};
  let loading = false;
  let lastMarkup = "";
  const statusText = (runtime, fallback) => {
    const names = {unknown:"未知", stopped:"已停止", starting:"启动中", running:"运行中", stopping:"停止中", error:"错误", degraded:"降级"};
    return names[runtime.state] || fallback;
  };
  const clock = value => value ? new Date(Number(value)).toLocaleString() : "—";
  async function load() {
    if (loading) return;
    loading = true;
    try {
      // The status projection is read-only and already contains the persisted
      // runtime/diagnostic snapshot. It avoids process lease checks on every
      // five-second HMI refresh while exposing protocol counters.
      const response = await fetch("/api/trans/status");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const rows = (await response.json()).items || [];
      const running = rows.filter(x => {
        const runtime = x.runtime_status || {};
        return Boolean(Number(runtime.running)) || Number(runtime.process_status ?? x.process_status) === 2;
      }).length;
      const errors = rows.filter(x => {
        const runtime = x.runtime_status || {};
        return runtime.state === "error" || Number(runtime.process_status ?? x.process_status) === 5;
      }).length;
      $("#channel-total").textContent = rows.length;
      $("#channel-running").textContent = running;
      $("#channel-errors").textContent = errors;
      const markup = rows.map(x => {
        const runtime = x.runtime_status || {};
        const status = Number(runtime.process_status ?? x.process_status ?? 0);
        const state = statusText(runtime, statuses[status] || String(status));
        const isRunning = Boolean(Number(runtime.running)) || status === 2;
        const isError = runtime.state === "error" || status === 5;
        const cid = encodeURIComponent(x.channel_id);
        const counts = [runtime.rx_count ?? 0, runtime.tx_count ?? 0, runtime.error_count ?? 0].join(" / ");
        const diagnostic = runtime.diagnostic || {};
        const diagnosticText = Object.keys(diagnostic).length ? `诊断 ${Object.keys(diagnostic).length} 项` : "—";
        const error = runtime.last_error || runtime.process_error || x.process_error || "—";
        return `<tr><td>${esc(x.channel_id)} · ${esc(x.name)}</td><td>${esc(x.protocol)}</td><td>${esc(x.bind_host)}:${esc(x.bind_port)}</td><td><span class="status-badge ${isRunning ? "running" : isError ? "error" : ""}">${esc(state)}</span></td><td>${esc(runtime.connection_state || "未知")}</td><td>${esc(x.scan_interval_ms)} ms</td><td class="mono">${esc(counts)}</td><td>${esc(clock(runtime.process_heartbeat_time || x.process_heartbeat_time))}</td><td title="${esc(JSON.stringify(diagnostic))}"><span>${esc(diagnosticText)}</span><small class="cell-sub">${esc(error)}</small></td><td><a href="/points.html?channel_id=${cid}">四遥数据</a> · <a href="/processes.html">进程</a> · <a href="/logs.html?channel_id=${cid}">日志报文</a></td></tr>`;
      }).join("") || '<tr><td colspan="10" class="muted">暂无转发通道，可前往“通道数据”新建。</td></tr>';
      if (lastMarkup !== markup) {
        $("#communication-rows").innerHTML = markup;
        lastMarkup = markup;
      }
      $("#message").textContent = "";
      $("#message").className = "muted";
    } catch (error) { $("#message").textContent = error.message; $("#message").className = "error"; }
    finally { loading = false; }
  }
  $("#refresh").onclick = load;
  load();
  setInterval(() => { if (!document.hidden && !loading) load(); }, 5000);
})();

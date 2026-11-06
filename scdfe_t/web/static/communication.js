(function () {
  "use strict";
  const $ = s => document.querySelector(s);
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const statuses = {0:"停止",1:"启动中",2:"运行",3:"停止中",4:"处理中",5:"错误"};
  async function load() {
    try {
      const response = await fetch("/api/trans/processes");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const rows = (await response.json()).items || [];
      $("#channel-total").textContent = rows.length;
      $("#channel-running").textContent = rows.filter(x => x.alive).length;
      $("#channel-errors").textContent = rows.filter(x => Number(x.process_status) === 5).length;
      $("#communication-rows").innerHTML = rows.map(x => {
        const status = Number(x.process_status || 0), cid = encodeURIComponent(x.channel_id);
        const heartbeat = x.process_heartbeat_time ? new Date(Number(x.process_heartbeat_time)).toLocaleString() : "—";
        return `<tr><td>${esc(x.channel_id)} · ${esc(x.name || x.code)}</td><td>${esc(x.protocol)}</td><td>${esc(x.bind_host)}:${esc(x.bind_port)}</td><td><span class="status-badge ${status === 2 ? "running" : status === 5 ? "error" : ""}">${statuses[status] || status}</span></td><td>${esc(x.scan_interval_ms)} ms</td><td>${esc(heartbeat)}</td><td>${esc(x.process_error) || "—"}</td><td><a href="/points.html?channel_id=${cid}">四遥数据</a> · <a href="/processes.html">进程</a> · <a href="/logs.html?channel_id=${cid}">日志</a></td></tr>`;
      }).join("") || '<tr><td colspan="8" class="muted">暂无转发通道，可前往“通道数据”新建。</td></tr>';
      $("#message").textContent = "已更新 " + new Date().toLocaleTimeString();
      $("#message").className = "muted";
    } catch (error) { $("#message").textContent = error.message; $("#message").className = "error"; }
  }
  $("#refresh").onclick = load;
  load();
  setInterval(() => { if (!document.hidden) load(); }, 5000);
})();

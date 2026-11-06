(function () {
  "use strict";
  const $ = s => document.querySelector(s);
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  async function api(path, options = {}) {
    const response = await fetch(path, {headers:{"Content-Type":"application/json", ...(options.headers || {})}, ...options});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "请求失败");
    return body;
  }
  function message(text, error = false) {
    $("#message").textContent = text || "";
    $("#message").className = "message" + (error ? " error" : "");
  }
  function render(status, points, logs) {
    $("#status-grid").innerHTML = [["运行状态",status.running ? "运行中" : "已停止"], ["RTU",status.rtu_id || "未选择"], ["连接",status.active_connections || 0], ["点数",status.point_count || 0], ["错误",status.last_error || "无"]]
      .map(([label,value]) => `<div class="metric"><b>${esc(value)}</b><span>${esc(label)}</span></div>`).join("");
    $("#status-json").textContent = JSON.stringify(status,null,2);
    $("#points").innerHTML = (points.items || []).map(p => `<tr><td>${esc(p.kind)}</td><td>${esc(p.pnt_no)}</td><td>${esc(p.value)}</td><td>${esc(p.status)}</td></tr>`).join("") || '<tr><td colspan="4">暂无点</td></tr>';
    $("#connections").innerHTML = (status.connections || []).map(c => `<div>[${esc(c.time)}] ${esc(c.ip)}</div>`).join("") || "暂无连接";
    $("#logs").innerHTML = (logs.items || []).map(log => `<div class="${esc(log.level)}">[${esc(log.time)}] ${esc(log.message)}</div>`).join("") || "暂无日志";
    $("#start").disabled = !!status.running;
    $("#stop").disabled = !status.running;
  }
  async function load() {
    try {
      const [status,points,logs] = await Promise.all([api("/api/iec104/status"), api("/api/iec104/points"), api("/api/iec104/logs?limit=100")]);
      render(status.item,points,logs);
      const config = status.item.config || {};
      $("#bind-ip").value = config.bind_ip || "";
      $("#port").value = config.port || "";
      $("#rtu-id").value = config.rtu_id ?? "";
      $("#max-connections").value = config.max_connections ?? 0;
      $("#tick-rate").value = config.tick_rate_ms ?? 100;
      message("已更新 " + new Date().toLocaleTimeString());
    } catch (error) { message(error.message,true); }
  }
  $("#config-form").onsubmit = async event => {
    event.preventDefault();
    try {
      await api("/api/iec104/config", {method:"PATCH", body:JSON.stringify({
        bind_ip:$("#bind-ip").value.trim(), port:Number($("#port").value),
        rtu_id:$("#rtu-id").value ? Number($("#rtu-id").value) : null,
        max_connections:Number($("#max-connections").value), tick_rate_ms:Number($("#tick-rate").value),
      })});
      await load();
      message("配置已保存");
    } catch (error) { message(error.message,true); }
  };
  for (const action of ["start","stop"]) $("#" + action).onclick = async () => {
    try {
      await api("/api/iec104/" + action, {method:"POST", body:"{}"});
      await load();
      message(action === "start" ? "服务已启动" : "服务已停止");
    } catch (error) { message(error.message,true); }
  };
  $("#refresh").onclick = load;
  $("#update-form").onsubmit = async event => {
    event.preventDefault();
    try {
      const kind = $("#kind").value, input = $("#value").value, numeric = Number(input);
      if (!input.trim() || !Number.isFinite(numeric)) throw new Error("请输入有限数字");
      if ((kind === "yx" || kind === "yk") && numeric !== 0 && numeric !== 1) throw new Error("遥信/遥控值只能为 0 或 1");
      const value = kind === "yx" || kind === "yk" ? numeric === 1 : numeric;
      await api("/api/iec104/update/incremental", {method:"POST", body:JSON.stringify({kind,pnt_no:Number($("#point").value),value})});
      await load();
      message("点值已更新");
    } catch (error) { message(error.message,true); }
  };
  $("#full-update").onclick = async () => {
    try {
      await api("/api/iec104/update/full", {method:"POST", body:JSON.stringify({values:{}})});
      await load();
      message("已执行全量更新");
    } catch (error) { message(error.message,true); }
  };
  load();
  setInterval(() => { if (!document.hidden) load(); }, 5000);
})();

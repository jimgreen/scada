(function () {
  "use strict";
  const configs = {
    "modbus-tcp": {slug:"modbus-tcp", fields:["mode","rtu_id","bind_ip","listen_port","remote_host","remote_port","unit_id","response_timeout","connect_timeout","retry_count","keep_alive"]},
    "modbus-rtu": {slug:"modbus-rtu", fields:["mode","rtu_id","serial_port","baudrate","bytesize","parity","stopbits","unit_id","response_timeout","retry_count"]},
    "can": {slug:"can", fields:["rtu_id","interface","channel","bitrate","data_bitrate","fd","receive_own_messages","receive_timeout","is_extended_id"]},
  };
  const cfg = configs[document.body.dataset.protocol];
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  let status = null;
  async function api(path, options = {}) {
    const response = await fetch(path, {headers: {"Content-Type":"application/json", ...(options.headers || {})}, ...options});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "请求失败");
    return body;
  }
  function message(text, error = false) { const el = $("#message"); el.textContent = text || ""; el.className = "message" + (error ? " error" : ""); }
  function field(name, value, type = "text") { const node = $("[data-field='" + name + "']"); if (node) node.value = value ?? ""; }
  function renderStatus(item) {
    status = item;
    $("#status-grid").innerHTML = [
      ["运行状态", item.running ? "运行中" : "已停止"], ["工作模式", item.config.mode], ["RTU", item.rtu_id || "未选择"], ["连接/客户端", item.open_connections || 0], ["点数", item.point_count || 0]
    ].map(([a,b]) => `<div class="metric"><b>${esc(b)}</b><span>${esc(a)}</span></div>`).join("");
    $("#status-json").textContent = JSON.stringify(item, null, 2);
    $("#start").disabled = !!item.running; $("#stop").disabled = !item.running;
  }
  function renderPoints(items) {
    const rows = items.map(p => `<tr><td>${esc(p.kind)}</td><td>${esc(p.pnt_no)}</td><td>${esc(p.name || "")}</td><td>${esc(p.value)}</td><td>${esc(p.status)}</td><td><button data-update="${esc(p.kind)}" data-point="${esc(p.pnt_no)}">更新</button> <button data-delete="${esc(p.kind)}" data-point="${esc(p.pnt_no)}" class="danger">删除</button></td></tr>`).join("");
    $("#points").innerHTML = rows || `<tr><td colspan="6" class="muted">暂无点定义</td></tr>`;
  }
  function renderLogs(items, target) { $(target).innerHTML = items.slice().reverse().map(x => `<div class="${esc(x.level)}">[${esc(x.time)}] ${esc(x.message)}</div>`).join("") || "暂无记录"; }
  async function load() {
    try {
      const [s, p, l, a] = await Promise.all([api(`/api/${cfg.slug}/status`), api(`/api/${cfg.slug}/points`), api(`/api/${cfg.slug}/logs?limit=100`), api(`/api/${cfg.slug}/alerts?limit=100`)]);
      renderStatus(s.item); renderPoints(p.items); renderLogs(l.items, "#logs"); renderLogs(a.items, "#alerts");
      cfg.fields.forEach(name => field(name, s.item.config[name]));
      message("已更新 " + new Date().toLocaleTimeString());
    } catch (e) { message(e.message, true); }
  }
  $("#config-form").onsubmit = async (e) => { e.preventDefault(); const body = {}; cfg.fields.forEach(name => { const node = $("[data-field='" + name + "']"); if (!node) return; body[name] = node.type === "number" ? Number(node.value) : (node.type === "checkbox" ? (node.checked ? 1 : 0) : node.value); }); try { await api(`/api/${cfg.slug}/config`, {method:"PATCH", body:JSON.stringify(body)}); message("配置已保存"); await load(); } catch (err) { message(err.message, true); } };
  $("#start").onclick = async () => { try { await api(`/api/${cfg.slug}/start`, {method:"POST", body:"{}"}); message("服务已启动"); await load(); } catch(e) { message(e.message, true); } };
  $("#stop").onclick = async () => { try { await api(`/api/${cfg.slug}/stop`, {method:"POST", body:"{}"}); message("服务已停止"); await load(); } catch(e) { message(e.message, true); } };
  $("#refresh").onclick = load;
  $("#poll").onclick = async () => { try { const result = await api(`/api/${cfg.slug}/poll`, {method:"POST", body:"{}"}); message("采集完成 " + (result.item.count ?? 0) + " 个值"); await load(); } catch (e) { message(e.message, true); } };
  $("#points").onclick = async (e) => { const b = e.target.closest("button"); if (!b) return; const kind = b.dataset.update || b.dataset.delete, point = b.dataset.point; try { if (b.dataset.update) { const value = prompt("输入新值", "0"); if (value === null) return; await api(`/api/${cfg.slug}/update/incremental`, {method:"POST", body:JSON.stringify({kind, pnt_no:Number(point), value: kind === "yx" || kind === "yk" ? Number(value) !== 0 : Number(value)})}); } else { await api(`/api/${cfg.slug}/points/${kind}/${point}`, {method:"DELETE", body:"{}"}); } await load(); } catch(err) { message(err.message, true); } };
  $("#add-point").onsubmit = async (e) => { e.preventDefault(); const kind = $("#new-kind").value; const pnt_no = Number($("#new-point").value); const name = $("#new-name").value; try { await api(`/api/${cfg.slug}/points`, {method:"POST", body:JSON.stringify({kind, pnt_no, name, value:0, status:0})}); message("点定义已新增"); e.target.reset(); await load(); } catch(err) { message(err.message, true); } };
  $("#frame-form")?.addEventListener("submit", async e => { e.preventDefault(); try { const body = {arbitration_id:Number($("#frame-id").value), data:$("#frame-data").value, is_extended_id:Number($("#frame-ext").value)}; await api("/api/can/frame", {method:"POST", body:JSON.stringify(body)}); message("CAN 帧已发送"); await load(); } catch(err) { message(err.message, true); } });
  load(); setInterval(() => { if (!document.hidden) load(); }, 5000);
})();

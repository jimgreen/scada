/* Keep live status polls independent of edits in a protocol configuration form. */
function protocolForm(form) {
  const edits = new Map();
  let revision = 0, generation = 0, pending = 0;
  form.addEventListener("input", event => edits.set(event.target, ++revision));
  return {
    editableFromServer: node => !edits.has(node),
    async save(write) {
      const submitted = new Map(edits);
      await write();
      ++generation; // A GET started before the save may carry the old config.
      for (const [node, version] of submitted) {
        if (edits.get(node) === version) edits.delete(node);
      }
    },
    async refresh(read, render, error, background = false) {
      if (background && pending) return;
      const request = ++generation;
      ++pending;
      try {
        const data = await read();
        if (request === generation) render(data);
      } catch (cause) {
        if (request === generation) error(cause);
      } finally {
        --pending;
      }
    },
  };
}

(function () {
  "use strict";
  const configs = {
    "modbus-tcp": {slug:"modbus-tcp", fields:["mode","rtu_id","bind_ip","listen_port","remote_host","remote_port","unit_id","response_timeout","connect_timeout","retry_count","keep_alive"]},
    "modbus-tcp-server": {slug:"modbus-tcp-server", fields:["rtu_id","bind_ip","listen_port","unit_id"]},
    "modbus-rtu": {slug:"modbus-rtu", fields:["mode","rtu_id","serial_port","baudrate","bytesize","parity","stopbits","unit_id","response_timeout","retry_count"]},
    "modbus-rtu-server": {slug:"modbus-rtu-server", fields:["rtu_id","serial_port","baudrate","bytesize","parity","stopbits","unit_id","response_timeout","simulation"]},
    "can": {slug:"can", fields:["rtu_id","interface","channel","bitrate","data_bitrate","fd","receive_own_messages","receive_timeout","is_extended_id"]},
    "can-server": {slug:"can-server", fields:["rtu_id","interface","channel","bitrate","data_bitrate","fd","receive_own_messages","receive_timeout","request_id","response_id","response_data","simulation"]},
    "opc-server": {slug:"opc-server", fields:["rtu_id","bind_ip","port","endpoint_path","namespace_uri","device_name","simulation"]},
    "bacnet-server": {slug:"bacnet-server", fields:["rtu_id","bind_ip","bind_port","device_id","device_name","simulation"]},
  };
  const cfg = configs[document.body.dataset.protocol];
  if (!cfg) return;  // IEC104 uses its own dashboard and the same form state.
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const form = protocolForm($("#config-form"));
  let status = null;
  async function api(path, options = {}) {
    const response = await fetch(path, {headers: {"Content-Type":"application/json", ...(options.headers || {})}, ...options});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "请求失败");
    return body;
  }
  function message(text, error = false) { const el = $("#message"); el.textContent = text || ""; el.className = "message" + (error ? " error" : ""); }
  const booleanFields = new Set(["simulation", "keep_alive", "fd", "receive_own_messages", "is_extended_id"]);
  function field(name, value) {
    const node = $("[data-field='" + name + "']");
    if (!node || !form.editableFromServer(node)) return;
    if (node.type === "checkbox") node.checked = value === true || Number(value) === 1;
    else node.value = value ?? "";
  }
  const keyValue = raw => Number.isSafeInteger(Number(raw)) ? Number(raw) : raw;
  function fieldValue(name, node) {
    if (node.type === "checkbox") return node.checked ? 1 : 0;
    if (name === "rtu_id") return node.value === "" ? null : keyValue(node.value);
    if (node.type === "number" || booleanFields.has(name)) return Number(node.value);
    return node.value;
  }
  function isCommand(kind) { return status?.config?.mode === "master" && (kind === "yk" || kind === "yt"); }
  function renderStatus(item) {
    status = item;
    $("#status-grid").innerHTML = [
      ["运行状态", item.running ? "运行中" : "已停止"], ["工作模式", item.config.mode], ["RTU", item.rtu_id || "未选择"], ["连接/客户端", item.open_connections || 0], ["点数", item.point_count || 0]
    ].map(([a,b]) => `<div class="metric"><b>${esc(b)}</b><span>${esc(a)}</span></div>`).join("");
    $("#status-json").textContent = JSON.stringify(item, null, 2);
    $("#start").disabled = !!item.running; $("#stop").disabled = !item.running;
  }
  let previousPoints;
  function renderPoints(items) {
    const body = $("#points");
    const rows = items.map(p => [p.kind,p.pnt_no,p.name,p.value,p.status,isCommand(p.kind) ? "下发" : "更新"]
      .map(value => String(value ?? "")));
    const stable = previousPoints && rows.length === previousPoints.length &&
      rows.every((r,i) => r[0] === previousPoints[i][0] && r[1] === previousPoints[i][1]);
    if (!stable) {
      body.innerHTML = rows.map(([kind,no,name,value,status,action]) =>
        `<tr><td>${esc(kind)}</td><td>${esc(no)}</td><td>${esc(name)}</td><td>${esc(value)}</td><td>${esc(status)}</td><td><button data-update="${esc(kind)}" data-point="${esc(no)}">${action}</button> <button data-delete="${esc(kind)}" data-point="${esc(no)}" class="danger">删除</button></td></tr>`).join("") ||
        '<tr><td colspan="6" class="muted">暂无点定义</td></tr>';
    } else {
      rows.forEach((r,i) => {
        for (let j=2; j<=5; j++) if (r[j] !== previousPoints[i][j]) {
          const cell = body.rows[i].cells[j];
          (j===5 ? cell.querySelector("[data-update]") : cell).textContent = r[j];
        }
      });
    }
    previousPoints = rows;
  }
  function renderLogs(items, target) { $(target).innerHTML = items.slice().reverse().map(x => `<div class="${esc(x.level)}">[${esc(x.time)}] ${esc(x.message)}</div>`).join("") || "暂无记录"; }
  function load(background = false) {
    return form.refresh(() => Promise.all([api(`/api/${cfg.slug}/status`), api(`/api/${cfg.slug}/points?view=table`), api(`/api/${cfg.slug}/logs?limit=100`), api(`/api/${cfg.slug}/alerts?limit=100`)]), ([s, p, l, a]) => {
      renderStatus(s.item); renderPoints(p.items); renderLogs(l.items, "#logs"); renderLogs(a.items, "#alerts");
      cfg.fields.forEach(name => field(name, s.item.config[name]));
      message("已更新 " + new Date().toLocaleTimeString());
    }, e => message(e.message, true), background === true);
  }
  $("#config-form").onsubmit = async (e) => { e.preventDefault(); const body = {}; cfg.fields.forEach(name => { const node = $("[data-field='" + name + "']"); if (!node) return; body[name] = fieldValue(name, node); }); try { await form.save(() => api(`/api/${cfg.slug}/config`, {method:"PATCH", body:JSON.stringify(body)})); await load(); message("配置已保存"); } catch (err) { message(err.message, true); } };
  $("#start").onclick = async () => { try { await api(`/api/${cfg.slug}/start`, {method:"POST", body:"{}"}); message("服务已启动"); await load(); } catch(e) { message(e.message, true); } };
  $("#stop").onclick = async () => { try { await api(`/api/${cfg.slug}/stop`, {method:"POST", body:"{}"}); message("服务已停止"); await load(); } catch(e) { message(e.message, true); } };
  $("#refresh").onclick = load;
  $("#poll").onclick = async () => { try { const result = await api(`/api/${cfg.slug}/poll`, {method:"POST", body:"{}"}); await load(); message("采集完成 " + (result.item.count ?? 0) + " 个值"); } catch (e) { message(e.message, true); } };
  $("#points").onclick = async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const kind = b.dataset.update || b.dataset.delete, point = b.dataset.point;
    try {
      if (b.dataset.update) {
        const command = isCommand(kind);
        const input = prompt(command ? "输入下发值" : "输入新值", "0");
        if (input === null) return;
        const numeric = Number(input);
        if (!input.trim() || !Number.isFinite(numeric)) throw new Error("请输入有限数字");
        if ((kind === "yx" || kind === "yk") && numeric !== 0 && numeric !== 1) throw new Error("遥信/遥控值只能为 0 或 1");
        const value = kind === "yx" || kind === "yk" ? numeric === 1 : numeric;
        const path = command ? `/api/${cfg.slug}/commands/${kind}/${point}` : `/api/${cfg.slug}/update/incremental`;
        const body = command ? {value} : {kind, pnt_no:keyValue(point), value};
        await api(path, {method:"POST", body:JSON.stringify(body)});
        await load();
        message(command ? "指令已发送" : "点值已更新");
      } else {
        await api(`/api/${cfg.slug}/points/${kind}/${point}`, {method:"DELETE", body:"{}"});
        await load();
      }
    } catch(err) { message(err.message, true); }
  };
  $("#add-point").onsubmit = async (e) => { e.preventDefault(); const kind = $("#new-kind").value; const pnt_no = keyValue($("#new-point").value); const name = $("#new-name").value; try { await api(`/api/${cfg.slug}/points`, {method:"POST", body:JSON.stringify({kind, pnt_no, name, value:0, status:0})}); message("点定义已新增"); e.target.reset(); await load(); } catch(err) { message(err.message, true); } };
  $("#frame-form")?.addEventListener("submit", async e => { e.preventDefault(); try { const body = {arbitration_id:Number($("#frame-id").value), data:$("#frame-data").value, is_extended_id:Number($("#frame-ext").value)}; await api(`/api/${cfg.slug}/frame`, {method:"POST", body:JSON.stringify(body)}); message("CAN 帧已发送"); await load(); } catch(err) { message(err.message, true); } });
  load(); setInterval(() => { if (!document.hidden) load(true); }, 5000);
})();

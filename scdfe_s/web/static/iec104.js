(function () {
  "use strict";
  const $ = s => document.querySelector(s);
  const keyValue = raw => Number.isSafeInteger(Number(raw)) ? Number(raw) : raw;
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  async function api(path, options = {}) {
    const {headers, ...request} = options;
    const response = await fetch(path, {...request, headers:{"Content-Type":"application/json", ...(headers || {})}});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "请求失败");
    return body;
  }
  function message(text, error = false) {
    $("#message").textContent = text || "";
    $("#message").className = "message" + (error ? " error" : "");
  }
  let allPoints = [], pointByKey = new Map(), editingPoint = null;
  let activeKind = document.querySelector("[data-iec-point-tab][aria-selected='true']")?.dataset.iecPointTab || "yc";
  function pointBody() { return document.querySelector(`[data-iec-point-panel="${activeKind}"] tbody`) || $("#points"); }
  const pointKey = (kind, pointNo) => `${kind}:${String(pointNo)}`;
  function setPointTab(kind) {
    activeKind = ["yc", "yx", "yk", "yt"].includes(kind) ? kind : "yc";
    document.querySelectorAll("[data-iec-point-tab]").forEach(tab => {
      const active = tab.dataset.iecPointTab === activeKind;
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll("[data-iec-point-panel]").forEach(panel => { panel.hidden = panel.dataset.iecPointPanel !== activeKind; });
    renderPoints(allPoints);
  }
  function renderPoints(items) {
    allPoints = items || [];
    pointByKey = new Map(allPoints.map(point => [pointKey(point.kind, point.pnt_no), point]));
    const body = pointBody();
    if (!body) return;
    const points = allPoints.filter(point => point.kind === activeKind);
    if (!points.length) {
      body.innerHTML = '<tr><td colspan="7">暂无点</td></tr>';
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const point of points) {
      const tr = document.createElement("tr");
      tr.dataset.pointKind = point.kind;
      tr.dataset.pointNo = String(point.pnt_no);
      for (const value of [point.kind, point.pnt_no, point.io_address ?? "—", point.type_id ?? "—", point.value, point.status]) {
        tr.insertCell().textContent = String(value ?? "");
      }
      const action = tr.insertCell();
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.pointEdit = pointKey(point.kind, point.pnt_no);
      button.textContent = "编辑";
      action.append(button);
      fragment.append(tr);
    }
    body.replaceChildren(fragment);
  }
  function openPointEditor(point) {
    if (!point) return;
    editingPoint = point;
    $("#edit-kind").value = point.kind || "";
    $("#edit-point").value = point.pnt_no ?? "";
    $("#edit-ioa").value = point.io_address ?? "—";
    $("#edit-type-id").value = point.type_id ?? "—";
    $("#edit-value").value = point.value === true ? "1" : point.value === false ? "0" : point.value ?? "";
    $("#edit-status").value = point.status ?? 0;
    const dialog = $("#point-editor");
    if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", "");
  }
  function closePointEditor() {
    const dialog = $("#point-editor");
    if (dialog.close) dialog.close(); else dialog.removeAttribute("open");
    editingPoint = null;
  }
  function render(status, points) {
    $("#status-grid").innerHTML = [["运行状态",status.running ? "运行中" : "已停止"], ["RTU",status.rtu_id || "未选择"], ["连接",status.active_connections || 0], ["点数",status.point_count || 0], ["错误",status.last_error || "无"]]
      .map(([label,value]) => `<div class="metric"><b>${esc(value)}</b><span>${esc(label)}</span></div>`).join("");
    $("#status-json").textContent = JSON.stringify(status,null,2);
    renderPoints(points.items || []);
    $("#connections").innerHTML = (status.connections || []).map(c => `<div>[${esc(c.time)}] ${esc(c.ip)}</div>`).join("") || "暂无连接";
    $("#start").disabled = !!status.running;
    $("#stop").disabled = !status.running;
  }
  const form = protocolForm($("#config-form"));
  function load(background = false) {
    return form.refresh(() => Promise.all([api("/api/iec104/status"), api("/api/iec104/points?view=table")]), ([status,points]) => {
      render(status.item,points);
      const config = status.item.config || {};
      for (const [id, value] of [["bind-ip", config.bind_ip], ["port", config.port],
        ["rtu-id", config.rtu_id], ["max-connections", config.max_connections ?? 0],
        ["tick-rate", config.tick_rate_ms ?? 100]]) {
        const input = $("#" + id);
        if (form.editableFromServer(input)) input.value = value ?? "";
      }
      message("已更新 " + new Date().toLocaleTimeString());
    }, error => message(error.message,true), background === true);
  }
  $("#config-form").onsubmit = async event => {
    event.preventDefault();
    try {
      await form.save(() => api("/api/iec104/config", {method:"PATCH", body:JSON.stringify({
        bind_ip:$("#bind-ip").value.trim(), port:Number($("#port").value),
        rtu_id:$("#rtu-id").value ? keyValue($("#rtu-id").value) : null,
        max_connections:Number($("#max-connections").value), tick_rate_ms:Number($("#tick-rate").value),
      })}));
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
  $("#point-editor-form").onsubmit = async event => {
    event.preventDefault();
    try {
      if (!editingPoint) throw new Error("请先选择要编辑的点");
      const kind = editingPoint.kind, input = $("#edit-value").value.trim(), numeric = Number(input);
      if (!input || !Number.isFinite(numeric)) throw new Error("值必须是有限数字");
      if ((kind === "yx" || kind === "yk") && numeric !== 0 && numeric !== 1) throw new Error("遥信/遥控值只能为 0 或 1");
      const value = kind === "yx" || kind === "yk" ? numeric === 1 : numeric;
      const status = Number($("#edit-status").value);
      if (!Number.isInteger(status)) throw new Error("状态必须是整数");
      const headers = editingPoint._version == null ? {} : {"If-Match":String(editingPoint._version)};
      await api(`/api/iec104/points/${encodeURIComponent(kind)}/${encodeURIComponent(editingPoint.pnt_no)}`, {
        method:"PATCH", headers, body:JSON.stringify({value, status})
      });
      closePointEditor();
      await load();
      message("点值已更新");
    } catch (error) { message(error.message,true); }
  };
  $("#point-editor-cancel").onclick = closePointEditor;
  $("#full-update").onclick = async () => {
    try {
      await api("/api/iec104/update/full", {method:"POST", body:JSON.stringify({values:{}})});
      await load();
      message("已执行全量更新");
    } catch (error) { message(error.message,true); }
  };
  document.querySelectorAll("[data-iec-point-tab]").forEach(tab => tab.addEventListener("click", () => setPointTab(tab.dataset.iecPointTab)));
  document.addEventListener("click", event => {
    const button = event.target.closest("button[data-point-edit]");
    if (button) openPointEditor(pointByKey.get(button.dataset.pointEdit));
  });
  document.addEventListener("dblclick", event => {
    const row = event.target.closest("tbody tr[data-point-kind]");
    if (row) openPointEditor(pointByKey.get(pointKey(row.dataset.pointKind, row.dataset.pointNo)));
  });
  setPointTab(activeKind);
  load();
  setInterval(() => { if (!document.hidden) load(true); }, 5000);
})();

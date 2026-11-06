/* Channel-list controls share the same persisted policy as the log page. */
(() => {
  "use strict";
  let policies = new Map(), loading = null, revision = 0, readError = "", defaultEnabled = false;
  const saving = new Map(), errors = new Map();
  const widgets = new WeakMap();
  const cells = () => [...document.querySelectorAll("[data-packet-channel]")];
  const text = (node, value) => { if (node.textContent !== value) node.textContent = value; };
  async function request(path = "", options) {
    const response = await fetch("/api/packet-recording" + path, {cache: "no-store", ...options});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "报文保存设置失败");
    return data;
  }
  function render(rows = cells()) {
    for (const cell of rows) {
      const cid = cell.dataset.packetChannel;
      let widget = widgets.get(cell);
      if (!widget) {
        const box = document.createElement("div");
        box.className = "packet-policy";
        box.innerHTML = '<label class="packet-policy-toggle"><input type="checkbox"><strong></strong></label>' +
          '<div class="packet-policy-meta"><span></span><button type="button">恢复默认</button></div>' +
          '<span class="packet-policy-error" role="status"></span>';
        const toggle = box.querySelector("input"), reset = box.querySelector("button");
        toggle.setAttribute("aria-label", `通道 ${cid} 保存报文`);
        reset.setAttribute("aria-label", `通道 ${cid} 恢复默认报文设置`);
        // Row selection must not rebuild the checkbox before its change event.
        box.addEventListener("click", event => event.stopPropagation());
        box.addEventListener("dblclick", event => event.stopPropagation());
        toggle.addEventListener("change", () => save(cid, toggle.checked));
        reset.addEventListener("click", () => save(cid, null));
        cell.replaceChildren(box);
        widget = {box, toggle, reset, status:box.querySelector('strong'),
                  source:box.querySelector('.packet-policy-meta span'), error:box.querySelector('.packet-policy-error')};
        widgets.set(cell, widget);
      }
      const policy = policies.get(cid), busy = saving.has(cid);
      const {toggle, reset} = widget;
      toggle.checked = busy ? (saving.get(cid) ?? defaultEnabled) : !!policy?.enabled;
      toggle.disabled = busy || !policy || !!readError;
      reset.disabled = toggle.disabled || policy?.override == null;
      text(widget.status, busy ? "保存中…" : !policy ? "未读取" : policy.enabled ? "开启" : "关闭");
      text(widget.source, !policy ? "" : policy.override === null ? "沿用默认" : "单独设置");
      text(widget.error, errors.get(cid) || readError);
      widget.box.classList.toggle("is-enabled", toggle.checked);
    }
  }
  async function refresh() {
    if (loading || saving.size || document.hidden || !cells().some(cell => cell.offsetParent !== null)) return loading;
    const current = revision;
    loading = (async () => {
      try {
        const data = await request();
        // An earlier refresh must never overwrite a later acknowledged save.
        if (current !== revision) return;
        policies = new Map(data.channels.map(row => [String(row.channel_id), row]));
        defaultEnabled = data.enabled;
        readError = "";
      } catch (error) {
        if (current === revision) readError = "状态读取失败：" + error.message;
      } finally { loading = null; render(); }
    })();
    return loading;
  }
  async function save(cid, enabled) {
    if (saving.has(cid)) return;
    saving.set(cid, enabled);
    errors.delete(cid);
    revision++;
    render();
    try {
      const result = await request("/channels/" + encodeURIComponent(cid), {
        method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({enabled}),
      });
      policies.set(cid, result);
      defaultEnabled = result.default_enabled;
    } catch (error) {
      errors.set(cid, "保存失败：" + error.message);
    } finally {
      saving.delete(cid);
      revision++;
      render();
      document.dispatchEvent(new Event("packet-recording-changed"));
    }
  }
  window.PacketRecordingChannels = {
    cell(channelId) {
      const cid = String(channelId ?? "");
      return /^\d+$/.test(cid) ? `<td data-packet-channel="${cid}"></td>` : '<td>未配置通道</td>';
    },
    mount() {
      const rows = cells();
      render(rows);
      // Table redraw and the policy timer operate independently. Reuse the
      // current catalog unless a newly created channel still needs loading.
      if (rows.some(cell => !policies.has(cell.dataset.packetChannel))) refresh();
    },
    refresh,
  };
  document.addEventListener("visibilitychange", refresh);
  document.addEventListener("packet-recording-changed", refresh);
  window.addEventListener("hashchange", refresh);
  setInterval(refresh, 5000);
})();

/* Log-page control: recording is independent from historical log filters. */
(() => {
  "use strict";
  const toggle = document.getElementById("packet-recording");
  const message = document.getElementById("packet-recording-status");
  const target = document.getElementById("packet-recording-channel");
  const reset = document.getElementById("packet-recording-reset");
  if (!toggle || !message || !target || !reset) return;
  let busy = false, state = null;
  async function request(path = "", options) {
    const response = await fetch("/api/packet-recording" + path, {cache: "no-store", ...options});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "报文保存设置失败");
    return data;
  }
  function selected() {
    return target.value === "" ? state : state?.channels.find(row => String(row.channel_id) === target.value);
  }
  function controls() {
    const row = selected();
    toggle.disabled = busy || !row;
    target.disabled = busy || !state;
    reset.disabled = busy || !row || target.value === "" || row.override === null;
  }
  function display(data = state) {
    state = data;
    const options = [["", "默认设置（未单独设置的通道）"], ...data.channels.map(row => [String(row.channel_id), row.label])];
    const signature = JSON.stringify(options);
    if (target.dataset.signature !== signature) {
      const previous = target.value;
      target.replaceChildren(...options.map(([value, label]) => new Option(label, value)));
      target.value = options.some(([value]) => value === previous) ? previous : "";
      target.dataset.signature = signature;
    }
    const row = selected();
    toggle.checked = row.enabled;
    const status = row.enabled ? "开启" : "关闭";
    if (target.value === "") {
      const overrides = data.channels.filter(item => item.override !== null).length;
      message.textContent = `默认${status}；${overrides} 个通道已单独设置。仅影响报文文件保存，约 1 秒内生效。`;
    } else {
      message.textContent = `${row.override === null ? "使用默认设置" : "通道单独设置"}：${status}。普通日志照常保存，约 1 秒内生效。`;
    }
    controls();
  }
  async function refresh() {
    if (busy || document.hidden || toggle.offsetParent === null) return;
    busy = true;
    controls();
    try { display(await request()); }
    catch (error) { message.textContent = error.message; }
    finally { busy = false; controls(); }
  }
  async function save(enabled) {
    if (busy) { toggle.checked = !!selected()?.enabled; return; }
    const path = target.value === "" ? "" : "/channels/" + encodeURIComponent(target.value);
    busy = true;
    controls();
    try {
      const result = await request(path, {method: "PUT", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled})});
      // Render the acknowledged save before any subsequent refresh can fail.
      if (!path) display(result);
      else { Object.assign(selected(), result); display(); }
      document.dispatchEvent(new Event("packet-recording-changed"));
    } catch (error) {
      toggle.checked = !!selected()?.enabled;
      message.textContent = error.message;
    } finally { busy = false; controls(); }
  }
  target.addEventListener("change", () => display());
  toggle.addEventListener("change", () => save(toggle.checked));
  reset.addEventListener("click", () => save(null));
  document.addEventListener("visibilitychange", refresh);
  window.addEventListener("hashchange", refresh);
  setInterval(() => { if (toggle.offsetParent !== null) refresh(); }, 5000);
  document.addEventListener("packet-recording-changed", () => { if (toggle.offsetParent !== null) refresh(); });
  new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) refresh(); }).observe(toggle);
})();

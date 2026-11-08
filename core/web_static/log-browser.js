/* Shared hourly log-file browser for scdfe_s and scdfe_t. */
(() => {
  "use strict";
  const $ = (name) => document.getElementById("lb-" + name);
  const module = document.body.dataset.logModule;
  const PAGE_SIZE = 50;
  let activeRecord = "runtime";
  const serviceNames = {
    iec104: "IEC104", "modbus-tcp": "Modbus TCP 主站", "modbus-tcp-server": "Modbus TCP 从站",
    "modbus-rtu": "Modbus RTU 主站", "modbus-rtu-server": "Modbus RTU 从站",
    can: "CAN 主站", "can-server": "CAN 服务端", "opc-server": "OPC UA 服务端",
    "bacnet-server": "BACnet 服务端",
  };
  // Protocol pages can open the common browser with a preselected service;
  // the standalone S/T log page keeps the value empty and shows all files.
  let requestedProcess = new URLSearchParams(location.search).get("process")
    || document.body.dataset.logProcess || "";
  let files = [], selected = "", filePage = 0, rows = [], renderedTree = "", processOptions = "", channelOptions = "";
  let channelCatalog = new Map();
  let cursors = [""], batch = 0, nextCursor = null, searchVersion = 0, listVersion = 0;
  let controller, debounce, busy = false, refreshing = false;
  const closed = new Set();
  const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
  const size = (bytes) => bytes < 1024 ? bytes + " B" : bytes < 1048576
    ? (bytes / 1024).toFixed(1) + " KB" : (bytes / 1048576).toFixed(1) + " MB";
  const processName = (name) => module === "s" ? serviceNames[name.replace(/^scdfe_s-/, "")] || name
    : "转发通道 " + name.replace(/^scdfe_t-channel-/, "");
  const channelName = (file) => {
    const channelId = file?.channel_id || (String(file?.process || "").match(/^scdfe_s-channel-(\d+)$/)?.[1]);
    if (!channelId) return processName(file?.process || "");
    const row = channelCatalog.get(String(channelId));
    if (!row) return "通信通道 " + channelId;
    const station = row.name || row.code || "未命名";
    return `RTU ${row.rtu_id ?? "—"} · ${station} · 通道 ${channelId}`;
  };
  const status = (text, error = false) => {
    $("status").textContent = text;
    $("status").classList.toggle("lb-error", error);
  };
  function initFilterToggle() {
    const reader = document.querySelector(".lb-reader");
    const head = reader?.querySelector(".lb-reader-head");
    const tools = reader ? [...reader.querySelectorAll(".lb-reader-tools")] : [];
    const exportButton = $("export");
    if (!reader || !head || !tools.length || !exportButton) return;
    const actions = head.querySelector(".lb-reader-actions") || document.createElement("div");
    actions.classList.add("lb-reader-actions");
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "lb-filter-toggle";
    toggle.id = "lb-toggle-filters";
    toggle.setAttribute("aria-controls", tools.map((node, index) => {
      const id = node.id || ("lb-filter-tools-" + (index + 1));
      node.id = id;
      return id;
    }).join(" "));
    const followLabel = $("follow")?.closest("label");
    if (!actions.parentNode) exportButton.replaceWith(actions);
    if (followLabel && !actions.contains(followLabel)) actions.insertBefore(followLabel, actions.firstChild);
    if (!actions.contains(toggle)) actions.append(toggle);
    if (!actions.contains(exportButton)) actions.append(exportButton);
    // The v2 key intentionally ignores the old expanded default so existing
    // sessions also open with the compact reader on their next visit.
    const key = "scdfe_log_filters_collapsed_v2_" + (module || location.port || "default");
    // Start with the reader expanded and the optional filter rows collapsed so
    // the log text gets the largest useful area on first open.  The user's
    // choice is remembered per service and can be changed at any time.
    let collapsed = true;
    try {
      const stored = window.localStorage.getItem(key);
      if (stored !== null) collapsed = stored === "1";
    } catch (_) { /* private mode */ }
    const apply = (value) => {
      collapsed = Boolean(value);
      reader.classList.toggle("is-filters-collapsed", collapsed);
      toggle.setAttribute("aria-expanded", String(!collapsed));
      toggle.textContent = collapsed ? "显示筛选" : "隐藏筛选";
      toggle.title = collapsed ? "显示日志筛选条件" : "隐藏日志筛选条件，扩大内容显示区域";
      try { window.localStorage.setItem(key, collapsed ? "1" : "0"); } catch (_) { /* private mode */ }
    };
    toggle.addEventListener("click", () => apply(!collapsed));
    apply(collapsed);
  }
  async function api(path, signal) {
    const response = await fetch(path, {cache: "no-store", signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || data.error || data.message || "读取日志失败");
    return data;
  }
  function visibleFiles() {
    const process = $("process")?.value || "", needle = $("file-query").value.trim().toLowerCase();
    const channel = $("channel")?.value || "";
    return files.filter((file) => (!process || file.process === process)
      && (module !== "s" || file.channel_id != null)
      && (!channel || String(file.channel_id || "") === String(channel))
      && file.name.split("/").pop().toLowerCase().includes(needle));
  }
  function updateChannels(rows) {
    channelCatalog = new Map((rows || []).filter((row) => row?.channel_id != null)
      .map((row) => [String(row.channel_id), row]));
    const select = $("channel");
    if (!select) return;
    const value = select.value;
    const options = '<option value="">全部通信通道</option>'
      + [...channelCatalog.values()].sort((a, b) => Number(a.channel_id) - Number(b.channel_id))
        .map((row) => '<option value="' + esc(row.channel_id) + '">' + esc(
          `RTU ${row.rtu_id ?? "—"} · ${row.name || row.code || "未命名"} · 通道 ${row.channel_id}`) + '</option>').join("");
    if (options !== channelOptions) { select.innerHTML = options; channelOptions = options; }
    if ([...select.options].some((option) => option.value === value)) select.value = value;
  }
  function updateProcesses() {
    const select = $("process");
    if (!select) return;
    const value = select.value || requestedProcess;
    const processes = [...new Set(files.map((file) => file.process))].sort((a, b) =>
      a.localeCompare(b, undefined, {numeric: true}));
    if (value && !processes.includes(value)) processes.push(value);
    const options = '<option value="">全部进程</option>'
      + processes.map((name) => '<option value="' + esc(name) + '">' + esc(processName(name)) + "</option>").join("");
    if (options !== processOptions) { select.innerHTML = options; processOptions = options; }
    select.value = value;
    requestedProcess = "";
  }
  function renderTree() {
    const all = visibleFiles();
    filePage = Math.min(filePage, Math.max(0, Math.ceil(all.length / PAGE_SIZE) - 1));
    const page = all.slice(filePage * PAGE_SIZE, (filePage + 1) * PAGE_SIZE);
    const signature = JSON.stringify([filePage, all.length, selected,
      page.map((f) => [f.name, f.hour, f.process, f.size, f.modified_at])]);
    // A quiet follow refresh must leave the actual buttons and focus in place.
    if (signature === renderedTree) return;
    renderedTree = signature;
    const root = new Map();
    for (const file of page) {
      const hour = file.hour;
      const parts = [hour.slice(0, 4) + " 年", hour.slice(4, 6) + " 月",
        hour.slice(6, 8) + " 日", hour.slice(9, 11) + " 时"];
      let children = root, key = "";
      for (const part of parts) {
        key += "/" + part;
        if (!children.has(part)) children.set(part, {key, children: new Map(), files: [], count: 0});
        const group = children.get(part);
        group.count++;
        if (part === parts[3]) group.files.push(file);
        children = group.children;
      }
    }
    function groups(nodes) {
      return [...nodes].map(([label, group]) => '<details class="lb-group" data-key="' + esc(group.key)
        + '"' + (closed.has(group.key) ? "" : " open") + '><summary>' + esc(label) + "<small>"
        + group.count + ' 个文件</small></summary><div class="lb-children">' + groups(group.children)
        + group.files.map((file) => '<button class="lb-file" data-file="' + esc(file.name)
          + '" title="' + esc(file.name) + '" aria-label="' + esc(file.name)
          + '" aria-pressed="' + (selected === file.name) + '"><strong>'
          + esc(channelName(file)) + '</strong><small>' + size(file.size) + "</small></button>").join("")
        + "</div></details>").join("");
    }
    const tree = $("tree"), scroll = tree.scrollTop;
    const focus = tree.contains(document.activeElement) ? document.activeElement : null;
    const focusFile = focus?.dataset.file, focusGroup = focus?.parentElement.dataset.key;
    const empty = activeRecord === "packet"
      ? "暂无报文文件；请在“通道参数”中开启“保存报文”，通信线程产生收发报文后再刷新。"
      : "暂无匹配的日志文件";
    tree.innerHTML = all.length ? groups(root) : '<div class="lb-empty">' + empty + '</div>';
    tree.querySelectorAll("details").forEach((detail) => detail.addEventListener("toggle", () => {
      if (detail.open) closed.delete(detail.dataset.key);
      else closed.add(detail.dataset.key);
    }));
    tree.querySelectorAll("[data-file]").forEach((button) => button.addEventListener("click", () => {
      selected = button.dataset.file;
      $("follow").checked = false;
      $("scope").value = "file";
      tree.querySelectorAll("[data-file]").forEach((node) =>
        node.setAttribute("aria-pressed", String(node.dataset.file === selected)));
      resetSearch();
    }));
    if (focusFile || focusGroup) {
      const target = focusFile
        ? [...tree.querySelectorAll("[data-file]")].find((node) => node.dataset.file === focusFile)
        : [...tree.querySelectorAll("details")].find((node) => node.dataset.key === focusGroup)?.querySelector("summary");
      target?.focus({preventScroll: true});
    }
    tree.scrollTop = scroll;
    $("files-page").textContent = all.length
      ? (filePage + 1) + " / " + Math.ceil(all.length / PAGE_SIZE) + " · " + all.length + " 个"
      : "0 个文件";
    $("files-prev").disabled = filePage === 0;
    $("files-next").disabled = (filePage + 1) * PAGE_SIZE >= all.length;
  }
  function reconcileSelection(followLatest = false) {
    const all = visibleFiles();
    const current = all.find((file) => file.name === selected);
    if (!current) selected = all[0]?.name || "";
    else if (followLatest && $("scope").value === "file" && !$("end").value) {
      // Continue the selected service/channel across rotation. A newer
      // file belonging to a different process must not steal the reader.
      const next = all.find((file) => file.process === current.process && file.hour > current.hour);
      if (next) {
        selected = next.name;
        filePage = Math.floor(all.indexOf(next) / PAGE_SIZE);
      }
    }
    renderTree();
  }
  function invalidate() {
    clearTimeout(debounce);
    ++searchVersion;
    controller?.abort();
    busy = false;
    rows = [];
    $("export").disabled = true;
    $("newer").disabled = true;
    $("older").disabled = true;
  }
  function resetSearch(delay = 0) {
    invalidate();
    cursors = [""];
    batch = 0;
    nextCursor = null;
    $("content").innerHTML = '<div class="lb-empty">正在检索…</div>';
    $("result").textContent = "";
    debounce = setTimeout(() => search(), delay);
  }
  function query() {
    const params = new URLSearchParams();
    if ($("scope").value === "file") {
      if (!selected) return null;
      params.set("filename", selected);
    }
    for (const [id, key] of [["process", "process"], ["file-query", "file_query"],
      ["keyword", "q"], ["direction", "direction"], ["level", "level"],
      ["channel", "channel_id"], ["limit", "limit"]]) {
      const node = $(id);
      if (!node) continue;
      const value = node.value.trim();
      if (!value) continue;
      if (module === "t" && key === "process" && /^scdfe_t-channel-([1-9]\d*)$/.test(value)) {
        params.set("channel_id", value.slice("scdfe_t-channel-".length));
      } else params.set(key, value);
    }
    params.set("record", activeRecord);
    if ($("channel")?.value.trim()) params.set("channel_id", $("channel").value.trim());
    const selectedDate = /-(\d{8})-\d{2}\.log$/.exec(selected || "")?.[1];
    for (const key of ["start", "end"]) {
      if (!$(key).validity.valid) throw new Error("请输入有效的开始和结束时间");
      if (!$(key).value) continue;
      if (!selectedDate) throw new Error("请先选择日志文件");
      const value = $(key).value;
      const date = selectedDate.slice(0, 4) + "-" + selectedDate.slice(4, 6) + "-" + selectedDate.slice(6, 8);
      const parsed = new Date(date + "T" + value);
      if (!Number.isFinite(parsed.valueOf())) throw new Error("请输入有效的开始和结束时间");
      params.set(key, parsed.toISOString());
    }
    if (params.has("start") && params.has("end") && params.get("start") > params.get("end"))
      throw new Error("开始时间不能晚于结束时间");
    if (cursors[batch]) params.set("cursor", cursors[batch]);
    return params;
  }
  function highlight(text, keyword) {
    if (!keyword) return esc(text);
    // Escape both text and keyword; logs are always displayed as text.
    const pattern = new RegExp(keyword.replace(/[.*+?^{}()|[\]\\$]/g, "\\$&"), "gi");
    let start = 0, html = "";
    for (const match of text.matchAll(pattern)) {
      html += esc(text.slice(start, match.index)) + "<mark>" + esc(match[0]) + "</mark>";
      start = match.index + match[0].length;
    }
    return html + esc(text.slice(start));
  }
  async function search(preserveScroll = false) {
    const version = ++searchVersion;
    controller?.abort();
    controller = new AbortController();
    busy = true;
    $("older").disabled = $("newer").disabled = true;
    $("export").disabled = true;
    $("content").setAttribute("aria-busy", "true");
    $("name").textContent = $("scope").value === "all" ? "全部匹配文件" : selected || "请选择日志文件";
    const file = files.find((item) => item.name === selected);
    $("meta").textContent = $("scope").value === "all" ? "跨文件检索 · 按文件小时由新到旧分批读取"
    : file ? channelName(file) + " · " + size(file.size) + " · 本批按原文顺序显示" : "当前目录没有匹配文件";
    const scroll = $("content").scrollTop;
    try {
      const params = query();
      if (!params) {
        rows = []; nextCursor = null;
        $("content").innerHTML = '<div class="lb-empty">暂无日志文件；服务运行后可点击刷新</div>';
        $("result").textContent = "0 条";
        return;
      }
      const result = await api("/api/log-files/search?" + params, controller.signal);
      if (version !== searchVersion) return;
      rows = result.items.slice().reverse();
      nextCursor = result.next_cursor;
      const all = $("scope").value === "all";
      $("content").innerHTML = rows.length ? rows.map((row, i) =>
        '<div class="lb-line ' + esc(row.level) + '"><span class="lb-line-no">' + (i + 1)
        + '</span><span class="lb-line-text">' + (all ? '<span class="lb-source">' + esc(row.filename) + "</span>" : "")
        + highlight(row.text, result.keyword) + "</span></div>").join("")
        : '<div class="lb-empty">' + (nextCursor ? "本批扫描范围内没有匹配记录，可继续查看较早一批" : "没有符合筛选条件的记录") + "</div>";
      $("content").scrollTop = preserveScroll && !$("follow").checked ? scroll : $("content").scrollHeight;
      $("result").textContent = "第 " + (batch + 1) + " 批 · " + rows.length + " 条 · 扫描 "
        + result.scanned_files + " 个文件" + (nextCursor ? " · 还有更早记录" : " · 已到最早")
        + (result.skipped_lines ? " · 跳过 " + result.skipped_lines + " 条超长记录" : "");
      $("export").disabled = rows.length === 0;
      $("older").disabled = !nextCursor;
      $("newer").disabled = batch === 0;
    } catch (error) {
      if (version !== searchVersion || error.name === "AbortError") return;
      rows = []; nextCursor = null;
      cursors = [""]; batch = 0;
      $("content").innerHTML = '<div class="lb-empty">' + esc(error.message) + "</div>";
      $("result").textContent = "检索失败，请调整筛选条件或刷新重试";
    } finally {
      if (version === searchVersion) {
        busy = false;
        $("content").setAttribute("aria-busy", "false");
      }
    }
  }
  async function refresh(follow = false) {
    if (follow && (refreshing || busy)) return;
    const version = ++listVersion;
    refreshing = true;
    status("正在读取文件目录…");
    try {
      const [result, channels] = await Promise.all([
        api("/api/log-files?record=" + encodeURIComponent(activeRecord)),
        module === "s" ? api("/api/rtus?limit=500&offset=0&sort=channel_id&order=asc") : Promise.resolve(null),
      ]);
      if (version !== listVersion) return;
      files = result.items;
      if (channels) updateChannels(channels.items);
      updateProcesses();
      const previous = selected;
      reconcileSelection($("follow").checked);
      $("directory").textContent = (activeRecord === "packet" ? "报文目录：" : "运行日志目录：") + result.directory;
      status("目录已更新 · " + new Date().toLocaleTimeString());
      if (selected !== previous) resetSearch();
      else if (!follow || batch === 0) await search(true);
    } catch (error) {
      if (version === listVersion) status(error.message + "，可点击刷新重试", true);
    } finally {
      if (version === listVersion) refreshing = false;
    }
  }
  initFilterToggle();
  $("refresh").addEventListener("click", () => refresh());
  document.querySelectorAll(".lb-record-tab").forEach((tab) => tab.addEventListener("click", () => {
    const record = tab.dataset.record;
    if (!record || record === activeRecord || refreshing) return;
    activeRecord = record;
    document.querySelectorAll(".lb-record-tab").forEach((node) =>
      node.setAttribute("aria-selected", String(node.dataset.record === activeRecord)));
    $("record").value = activeRecord;
    $("direction").disabled = activeRecord === "runtime";
    if (activeRecord === "runtime") $("direction").value = "";
    selected = ""; filePage = 0; files = []; renderedTree = "";
    resetSearch();
    refresh();
  }));
  $("files-prev").addEventListener("click", () => { filePage--; renderTree(); });
  $("files-next").addEventListener("click", () => { filePage++; renderTree(); });
  $("process")?.addEventListener("change", () => {
    selected = ""; filePage = 0; reconcileSelection(); resetSearch();
  });
  $("file-query").addEventListener("input", () => {
    filePage = 0; reconcileSelection(); resetSearch(250);
  });
  for (const id of ["scope", "record", "direction", "level", "limit", "start", "end"]) {
    $(id).addEventListener("change", () => {
      if (id === "record") {
        const requested = $("record").value;
        if (requested !== activeRecord) {
          document.querySelector('.lb-record-tab[data-record="' + requested + '"]')?.click();
          return;
        }
        $("direction").disabled = activeRecord === "runtime";
        if ($("direction").disabled) $("direction").value = "";
      }
      resetSearch();
    });
  }
  $("channel")?.addEventListener("input", () => { filePage = 0; selected = ""; reconcileSelection(); resetSearch(250); });
  $("channel")?.addEventListener("change", () => { filePage = 0; selected = ""; reconcileSelection(); resetSearch(); });
  $("keyword").addEventListener("input", () => resetSearch(250));
  $("apply").addEventListener("click", () => search());
  $("clear").addEventListener("click", () => {
    $("record").value = activeRecord;
    for (const id of ["direction", "level", "channel", "keyword", "start", "end"]) $(id).value = "";
    $("direction").disabled = activeRecord === "runtime";
    if ($("channel")) $("channel").value = "";
    resetSearch();
  });
  $("wrap").addEventListener("change", () => $("content").classList.toggle("lb-wrap", $("wrap").checked));
  $("older").addEventListener("click", () => {
    if (!nextCursor || busy) return;
    $("follow").checked = false;
    cursors[++batch] = nextCursor;
    search();
  });
  $("newer").addEventListener("click", () => { if (batch && !busy) { batch--; search(); } });
  $("follow").addEventListener("change", () => {
    if ($("follow").checked) { reconcileSelection(true); resetSearch(); }
  });
  $("export").addEventListener("click", () => {
    if (!rows.length) return;
    const text = rows.map((row) => ($("scope").value === "all" ? "[" + row.filename + "] " : "") + row.text).join("\r\n") + "\r\n";
    const url = URL.createObjectURL(new Blob(["\ufeff", text], {type: "text/plain;charset=utf-8"}));
    const link = document.createElement("a");
    link.href = url;
    link.download = "scdfe_" + module + "-" + activeRecord + "-batch-" + (batch + 1) + ".log";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  if (module === "t") {
    const channel = new URLSearchParams(location.search).get("channel_id");
    if (/^[1-9]\d*$/.test(channel || "")) {
      const option = new Option("转发通道 " + channel, "scdfe_t-channel-" + channel);
      $("process")?.add(option);
      if ($("process")) $("process").value = option.value;
    }
  }
  // Unified S communication-test pages switch the selected protocol without
  // reloading the shared hourly-file browser.  Keep the browser's own
  // directory, pagination and search state authoritative while changing only
  // the process filter.
  document.addEventListener("scada:log-process", (event) => {
    const process = String(event.detail?.process || "");
    requestedProcess = process;
    const select = $("process");
    if (!select) return;
    const option = [...select.options].find((node) => node.value === process);
    select.value = option ? process : "";
    selected = "";
    filePage = 0;
    reconcileSelection();
    resetSearch();
  });
  $("record").value = activeRecord;
  $("direction").disabled = true;
  // The file catalogue is live state, independent of the reader's "follow
  // latest" option.  Poll while this page is visible so a newly rotated
  // hourly file (or a growing runtime/packet file) appears without a manual
  // refresh.  `refresh` preserves the selected file and filters when follow
  // is off; with follow on it advances to the newest hour for that process.
  setInterval(() => { if (!document.hidden) refresh(true); }, 5000);
  refresh();
})();

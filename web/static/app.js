"use strict";

const $ = (selector) => document.querySelector(selector);
const state = {
  schemas: new Map(),
  tableGroups: { parameters: [], helpers: [] },
  groupSelection: {},
  resource: "rtus",
  rtu: "",
  view: "common",
  offset: 0,
  limit: 50,
  sort: "",
  order: "asc",
  items: [],
  total: 0,
  editing: null,
  request: 0,
  saving: false,
  loading: false,
};
const resources = {
  rtus: ["fes_rtu_define", "RTU 与通道"],
  yc: ["fes_yc_define", "遥测点表"],
  yx: ["fes_yx_define", "遥信点表"],
  yt: ["fes_yt_define", "遥调点表"],
  yk: ["fes_yk_define", "遥控点表"],
};
const pointKinds = ["yc", "yx", "yt", "yk"];
const parameterTables = {
  fes_channel_iec104_define: "IEC104",
  fes_channel_modbus_tcp_define: "Modbus-TCP",
  fes_channel_modbus_rtu_define: "Modbus-RTU",
  fes_channel_can_define: "CAN",
  fes_channel_bacnet_define: "BACnet",
  fes_channel_opc_define: "OPC",
};
const channelFields = ["channel_id", "channel_type"];
const rtuRuntimeFields = [
  "process_enable",
  "process_status",
  "process_pid",
  "process_start_time",
  "process_stop_time",
  "process_heartbeat_time",
  "process_exit_code",
  "process_error",
];
const common = {
  rtus: [
    "rtu_id",
    "name",
    "channel_id",
    "channel_type",
    "process_enable",
    "process_status",
    "process_pid",
    "process_heartbeat_time",
  ],
  yc: [
    "rtu_id",
    "pnt_no",
    "name",
    "raw_value",
    "raw_status",
    "value",
    "status",
    "refresh_time",
  ],
  yx: [
    "rtu_id",
    "pnt_no",
    "name",
    "name_0",
    "name_1",
    "value",
    "status",
    "refresh_time",
  ],
  yt: [
    "rtu_id",
    "pnt_no",
    "name",
    "raw_value",
    "value",
    "status",
    "raw_scale",
    "refresh_time",
  ],
  yk: ["rtu_id", "pnt_no", "name", "value", "status", "refresh_time"],
};

function esc(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );
}
function channelTypeLabel(value) {
  return (
    {
      iec104: "IEC104",
      "modbus-rtu": "Modbus-RTU",
      "modbus-tcp": "Modbus-TCP",
      can: "CAN",
      bacnet: "BACnet",
      opc: "OPC",
      custom: "自定义",
    }[value] || value
  );
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = await response.json();
  if (!response.ok) {
    let message = data.error?.message || "请求失败";
    if (data.error?.code === "referenced") {
      message +=
        "：" +
        Object.entries(data.error.details)
          .map(
            ([name, n]) =>
              (state.schemas.get(name)?.head.chn_name || name) +
              " " +
              n +
              " 条",
          )
          .join("、");
    }
    throw new Error(message);
  }
  return data;
}

function toast(message, error = false) {
  const box = $("#toast");
  box.textContent = message;
  box.className = "toast show" + (error ? " error" : "");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    box.className = "toast";
  }, 4000);
}
function metadata(resource = state.resource) {
  return (
    resources[resource] || [
      resource,
      state.schemas.get(resource)?.head.chn_name || resource,
    ]
  );
}
function schemaFor(resource = state.resource) {
  return state.schemas.get(metadata(resource)[0]);
}
function keyOf(item, schema) {
  return schema.primary_keys.map((key) => String(item[key])).join(",");
}
function context() {
  const resource = state.resource;
  const schema = schemaFor(resource);
  const hasRtu =
    resource !== "rtus" && schema.columns.some((c) => c.eng_name === "rtu_id");
  return { resource, schema, rtu: hasRtu ? state.rtu : "" };
}
function pathFor(ctx, row = null) {
  let url;
  if (pointKinds.includes(ctx.resource)) {
    url = ctx.rtu
      ? "/api/rtus/" + encodeURIComponent(ctx.rtu) + "/points/" + ctx.resource
      : "/api/points/" + ctx.resource;
  } else if (ctx.resource === "rtus") {
    url = "/api/rtus";
  } else {
    url =
      "/api/tables/" + encodeURIComponent(ctx.schema.head.eng_name) + "/rows";
  }
  if (row) {
    const key =
      pointKinds.includes(ctx.resource) && ctx.rtu
        ? String(row.pnt_no)
        : keyOf(row, ctx.schema);
    url += "/" + encodeURIComponent(key);
  }
  return url;
}

async function refreshSummary() {
  const data = await api("/api/overview");
  const names = [
    ["fes_rtu_define", "RTU/通道"],
    ["fes_yc_define", "遥测"],
    ["fes_yx_define", "遥信"],
    ["fes_yt_define", "遥调"],
    ["fes_yk_define", "遥控"],
  ];
  $("#summary").innerHTML = names
    .map(
      ([name, label]) =>
        '<div class="metric"><strong>' +
        esc((data.counts[name] || 0).toLocaleString()) +
        "</strong><span>" +
        label +
        "</span></div>",
    )
    .join("");
  $("#database-path").textContent = data.database;
}
async function loadRtuOptions() {
  const data = await api("/api/rtus?limit=500&sort=rtu_id");
  // Suggestions are bounded; the field also accepts any existing RTU number.
  $("#rtu-options").innerHTML = data.items
    .map(
      (row) =>
        '<option value="' +
        esc(row.rtu_id) +
        '">' +
        esc(row.name || row.code || "未命名") +
        "</option>",
    )
    .join("");
}
function tableGroup(resource) {
  return Object.keys(state.tableGroups).find((group) =>
    state.tableGroups[group].includes(resource),
  );
}
function showView() {
  const [, title] = metadata();
  const schema = schemaFor();
  const isPoint = pointKinds.includes(state.resource);
  $("#section-title").textContent = title;
  const group = tableGroup(state.resource);
  $("#data-tabs").querySelectorAll("button").forEach((tab) => {
    const selected = tab.dataset.tabResource === state.resource ||
      (!!group && tab.dataset.tabGroup === group);
    tab.classList.toggle("selected", selected);
    tab.setAttribute("aria-pressed", String(selected));
  });
  $("#helper-tabs").hidden = !group;
  $("#helper-tabs").querySelectorAll("button").forEach((tab) => {
    const selected = tab.dataset.tabResource === state.resource;
    tab.hidden = tab.dataset.tableGroup !== group;
    tab.classList.toggle("selected", selected);
    tab.setAttribute("aria-pressed", String(selected));
  });
  $("#rtu-picker").hidden =
    state.resource === "rtus" ||
    !schema.columns.some((c) => c.eng_name === "rtu_id");
  $("#view-live").hidden = !isPoint;
  $("#add-channel").hidden = state.resource !== "rtus";
  $("#add").textContent = state.resource === "rtus" ? "＋ 新建 RTU" : "＋ 新增记录";
  for (const view of ["common", "live", "all"]) {
    $("#view-" + view).classList.toggle("selected", state.view === view);
  }
}
async function loadRows() {
  if (!state.schemas.size || ["processes", "scdfe", "logs"].includes(state.resource)) return;
  const sequence = ++state.request;
  state.loading = true;
  const ctx = context();
  showView();
  $("#table-error").hidden = true;
  $("#add").disabled = true;
  const params = new URLSearchParams({
    limit: String(state.limit),
    offset: String(state.offset),
    q: $("#search").value,
  });
  if (state.sort) {
    params.set("sort", state.sort);
    params.set("order", state.order);
  }
  if (ctx.rtu && !pointKinds.includes(ctx.resource))
    params.set("rtu_id", ctx.rtu);
  try {
    const data = await api(pathFor(ctx) + "?" + params);
    if (sequence !== state.request) return;
    if (state.offset && state.offset >= data.total) {
      state.offset = Math.max(
        0,
        Math.floor((data.total - 1) / state.limit) * state.limit,
      );
      return loadRows();
    }
    state.items = data.items;
    state.total = data.total;
    renderTable();
    $("#add").disabled = false;
    $("#health").className = "health ok";
    markUpdated();
  } catch (error) {
    if (sequence !== state.request) return;
    state.items = [];
    state.total = 0;
    renderTable();
    $("#table-error").textContent = error.message;
    $("#table-error").hidden = false;
  } finally {
    if (sequence === state.request) state.loading = false;
  }
}
function visibleColumns() {
  const schema = schemaFor();
  if (state.view === "all") return schema.columns;
  let names =
    common[state.resource] || schema.columns.slice(0, 8).map((c) => c.eng_name);
  if (state.view === "live" && pointKinds.includes(state.resource)) {
    names = [
      "rtu_id",
      "pnt_no",
      "name",
      "value",
      "status",
      "refresh_time",
      "raw_value",
      "raw_status",
      "raw_time",
      "cmd_time",
      "cmd_handle_time",
    ];
  }
  return names
    .map((n) => schema.columns.find((c) => c.eng_name === n))
    .filter(Boolean);
}
function renderTable() {
  const columns = visibleColumns();
  const schema = schemaFor();
  $("#table-head").innerHTML =
    "<tr>" +
    columns
      .map(
        (column) =>
          '<th scope="col" aria-sort="' +
          (state.sort === column.eng_name
            ? state.order === "asc"
              ? "ascending"
              : "descending"
            : "none") +
          '"><button class="sort-button" data-sort="' +
          esc(column.eng_name) +
          '">' +
          esc(column.chn_name) +
          (state.sort === column.eng_name
            ? state.order === "asc"
              ? " ↑"
              : " ↓"
            : "") +
          "<small>" +
          esc(column.eng_name) +
          "</small></button></th>",
      )
      .join("") +
    '<th class="sticky-actions">操作</th></tr>';
  $("#table-body").innerHTML = state.items.length
    ? state.items
        .map(
          (item, index) =>
            '<tr data-row="' +
            index +
            '">' +
            columns
              .map(
                (column) =>
                  '<td class="' +
                  (column.primary_key ? "key" : "") +
                  '" title="' +
                  esc(item[column.eng_name] ?? "—") +
                  '">' +
                  cellValue(column.eng_name, item[column.eng_name]) +
                  "</td>",
              )
              .join("") +
            '<td class="actions-cell sticky-actions"><button class="row-button" data-detail="' +
            index +
            '">详情</button><button class="row-button" data-edit="' +
            index +
            '">编辑</button>' +
            (state.resource === "rtus"
              ? '<button class="row-button" data-rtu-params="' +
                index +
                '">RTU参数</button><button class="row-button" data-channel="' +
                index +
                '">通道维护</button><button class="row-button" data-channel-params="' +
                index +
                '">通道参数</button><button class="row-button" data-points="' +
                index +
                '">四遥</button><button class="row-button" data-rtu-log="' +
                esc(item.rtu_id) +
                '">日志</button>'
              : "") +
            '<button class="row-button delete" data-delete="' +
            index +
            '">删除</button></td></tr>',
        )
        .join("")
    : '<tr><td class="empty" colspan="' +
      (columns.length + 1) +
      '">没有匹配的记录</td></tr>';
  $("#table-head")
    .querySelectorAll("[data-sort]")
    .forEach((button) => {
      button.onclick = () => {
        state.order =
          state.sort === button.dataset.sort && state.order === "asc"
            ? "desc"
            : "asc";
        state.sort = button.dataset.sort;
        state.offset = 0;
        loadRows();
      };
    });
  for (const [action, handler] of Object.entries({
    detail: openDetails,
    edit: openEditor,
    delete: removeRow,
  })) {
    $("#table-body")
      .querySelectorAll("[data-" + action + "]")
      .forEach((button) => {
        button.onclick = () =>
          handler(state.items[Number(button.dataset[action])]);
      });
  }
  for (const [attribute, handler] of Object.entries({
    "rtu-params": openRtuParameters,
    channel: openChannelEditor,
    "channel-params": openChannelParameters,
  })) {
    $("#table-body")
      .querySelectorAll("[data-" + attribute + "]")
      .forEach((button) => {
        button.onclick = () => handler(state.items[Number(button.getAttribute("data-" + attribute))]);
      });
  }
  $("#table-body")
    .querySelectorAll("[data-row]")
    .forEach((row) => {
      row.ondblclick = (event) => {
        if (!event.target.closest("button"))
          openDetails(state.items[Number(row.dataset.row)]);
      };
    });
  $("#table-body")
    .querySelectorAll("[data-points]")
    .forEach((button) => {
      button.onclick = () => {
        state.rtu = String(state.items[Number(button.dataset.points)].rtu_id);
        $("#rtu-filter").value = state.rtu;
        state.view = "live";
        changeResource("yc");
      };
    });
  $("#table-body")
    .querySelectorAll("[data-rtu-log]")
    .forEach((button) => {
      button.onclick = () => openRtuLogs(button.dataset.rtuLog);
    });
  $("#result-count").textContent =
    "共 " +
    state.total.toLocaleString() +
    " 条 · 当前 " +
    (state.total ? state.offset + 1 : 0) +
    "–" +
    (state.offset + state.items.length) +
    " · " +
    schema.columns.length +
    " 个字段";
  $("#page").textContent =
    Math.floor(state.offset / state.limit) +
    1 +
    " / " +
    Math.max(1, Math.ceil(state.total / state.limit));
  $("#prev").disabled = state.offset === 0;
  $("#next").disabled = state.offset + state.limit >= state.total;
}

async function openEditor(row = null, options = {}) {
  const base = context();
  const ctx = {
    ...base,
    rtu: options.rtu ?? base.rtu,
    schema: options.schema || base.schema,
    columns: options.columns || (options.schema ? options.schema.columns : base.schema.columns),
    pathOverride: options.pathOverride || null,
    fetchPath: options.fetchPath || null,
    mode: options.mode || "record",
  };
  try {
    // Always edit the latest record. A later concurrent edit is protected by If-Match.
    if (row) row = (await api(ctx.fetchPath || pathFor(ctx, row))).item;
    if (ctx.resource !== state.resource) return;
    state.editing = { ...ctx, row };
    $("#form-title").textContent =
      (row ? "编辑 · " : "新增 · ") + (options.title || ctx.schema.head.chn_name);
    $("#form-error").textContent = "";
    $("#form-fields").innerHTML = ctx.columns
      .map((column) => {
        const name = column.eng_name;
        const scoped = name === "rtu_id" && !!ctx.rtu;
        const disabled = (!!row && column.primary_key) || options.autoPrimaryKeys?.includes(name);
        const required =
          (!options.autoPrimaryKeys?.includes(name) &&
            (column.primary_key || column.required === true || name === "rtu_id"));
        const value = row?.[name] ?? (scoped ? ctx.rtu : "");
        const attrs =
          (disabled ? " disabled" : "") +
          (scoped ? " readonly" : "") +
          (required ? " required" : "");
        const numeric = column.data_type !== "str";
        const type = column.data_type === "float" ? "number" : "text";
        const list =
          name === "rtu_id" && ctx.resource !== "rtus"
            ? ' list="rtu-options"'
            : "";
        const allowedOptions = Array.isArray(column.allowed_values)
          ? column.allowed_values
          : null;
        const control = allowedOptions
          ? '<select id="f-' +
            esc(name) +
            '" data-field="' +
            esc(name) +
            '"' +
            attrs +
            ">" +
            allowedOptions
              .map(
                (option) =>
                  '<option value="' +
                  esc(option) +
                  '"' +
                  (String(value) === option ? " selected" : "") +
                  ">" +
                  esc(
                    name === "channel_type" ? channelTypeLabel(option) : option,
                  ) +
                  "</option>",
              )
              .join("") +
            "</select>"
          : '<input id="f-' +
            esc(name) +
            '" data-field="' +
            esc(name) +
            '" type="' +
            type +
            '" step="any"' +
            (numeric ? ' inputmode="decimal"' : "") +
            list +
            attrs +
            ' value="' +
            esc(value) +
            '">';
        return (
          '<div class="field"><label for="f-' +
          esc(name) +
          '">' +
          esc(column.chn_name) +
          (required ? " *" : "") +
          "<small>" +
          esc(name) +
          (column.primary_key ? " · 主键" : "") +
          "</small></label>" +
          control +
          "</div>"
        );
      })
      .join("");
    $("#field-search").value = "";
    $("#editor").showModal();
    const preferred =
      state.view === "live" && row
        ? $("#f-value")
        : $("#form-fields input:not(:disabled):not([readonly])");
    preferred?.focus();
  } catch (error) {
    toast(error.message, true);
  }
}

async function openNewRtu() {
  const schema = state.schemas.get("fes_rtu_define");
  if (!schema) return toast("RTU 表结构尚未加载", true);
  const columns = schema.columns.filter((column) => !rtuRuntimeFields.includes(column.eng_name));
  await openEditor(null, { schema, columns, title: "RTU 与通道", mode: "new-rtu" });
}

async function openRtuParameters(row) {
  const schema = state.schemas.get("fes_rtu_define");
  if (!schema) return toast("RTU 表结构尚未加载", true);
  const columns = schema.columns.filter(
    (column) => !rtuRuntimeFields.includes(column.eng_name) && !channelFields.includes(column.eng_name),
  );
  await openEditor(row, { schema, columns, title: "RTU 参数", mode: "rtu-parameters" });
}

async function openChannelEditor(row, create = false) {
  const schema = state.schemas.get("fes_rtu_define");
  if (!schema || !row) return toast("请选择一个 RTU", true);
  const rtu = String(row.rtu_id);
  const path = "/api/rtus/" + encodeURIComponent(rtu) + "/channel";
  await openEditor(create ? null : row, {
    schema,
    columns: schema.columns.filter((column) => channelFields.includes(column.eng_name)),
    title: "通道参数",
    mode: create ? "new-channel" : "channel-parameters",
    rtu,
    pathOverride: path,
    fetchPath: "/api/rtus/" + encodeURIComponent(rtu),
  });
}

async function openChannelParameters(row) {
  try {
    const rtu = String(row.rtu_id);
    const info = await api("/api/rtus/" + encodeURIComponent(rtu) + "/channel/parameters");
    if (!info.parameter_schema) return toast("该通道类型没有可维护的协议参数表", true);
    const schema = info.parameter_schema;
    state.schemas.set(schema.head.eng_name, schema);
    const columns = schema.columns.filter((column) => column.eng_name !== "id");
    await openEditor(info.item, {
      schema,
      columns,
      title: channelTypeLabel(row.channel_type) + " 通道参数",
      mode: "protocol-parameters",
      rtu: String(row.rtu_id),
      pathOverride: "/api/rtus/" + encodeURIComponent(row.rtu_id) + "/channel/parameters",
      fetchPath: "/api/rtus/" + encodeURIComponent(row.rtu_id) + "/channel/parameters",
    });
  } catch (error) {
    toast(error.message, true);
  }
}

async function openChannelChooser() {
  try {
    const data = await api("/api/rtus?limit=500&sort=rtu_id");
    if (!data.items.length) return toast("请先新建 RTU，再配置通道", true);
    const select = $("#channel-select-rtu");
    select.innerHTML = data.items
      .map((row) => '<option value="' + esc(row.rtu_id) + '">' + esc(
        String(row.rtu_id) + " · " + (row.name || row.code || "未命名") + " · " + channelTypeLabel(row.channel_type)
      ) + "</option>")
      .join("");
    $("#channel-select-dialog").showModal();
  } catch (error) {
    toast(error.message, true);
  }
}

function typedValue(input, column) {
  const value = column.data_type === "str" ? input.value : input.value.trim();
  if (column.data_type === "str") return value;
  if (value === "") return null;
  if (column.data_type === "int") {
    if (!/^[+-]?[0-9]+$/.test(value))
      throw new Error(column.chn_name + " 必须是整数");
    const number = BigInt(value);
    if (number < -9223372036854775808n || number > 9223372036854775807n)
      throw new Error(column.chn_name + " 超出整数范围");
    return String(number); // Preserve every bit of an int64 in JSON.
  }
  const number = Number(value);
  if (!Number.isFinite(number))
    throw new Error(column.chn_name + " 必须是有限数字");
  return number;
}
async function saveRow(event) {
  event.preventDefault();
  if (state.saving) return;
  const ctx = state.editing;
  if (!ctx) return;
  // A field search must not hide an invalid required field from the browser.
  if (!$("#editor-form").checkValidity()) {
    $("#field-search").value = "";
    $("#form-fields")
      .querySelectorAll(".field")
      .forEach((field) => {
        field.hidden = false;
      });
    $("#editor-form").reportValidity();
    return;
  }
  const body = {};
  try {
    for (const column of ctx.columns || ctx.schema.columns) {
      const input = $("#f-" + column.eng_name);
      if (input.disabled) continue;
      const original = ctx.row?.[column.eng_name];
      // An unchanged empty control keeps NULL as NULL.
      if (ctx.row && input.value === String(original ?? "")) continue;
      if (
        !ctx.row &&
        input.value === "" &&
        !column.primary_key &&
        column.eng_name !== "rtu_id"
      )
        continue;
      body[column.eng_name] = typedValue(input, column);
    }
    if (ctx.row && !Object.keys(body).length) {
      $("#editor").close();
      return toast("没有需要保存的修改");
    }
    state.saving = true;
    $("#save").disabled = true;
    const headers = ctx.row ? { "If-Match": String(ctx.row._version) } : {};
    const savePath = ctx.pathOverride || pathFor(ctx, ctx.row);
    await api(savePath, {
      method: ctx.row ? "PATCH" : "POST",
      headers,
      body: JSON.stringify(body),
    });
    $("#editor").close();
    toast(ctx.row ? "记录已更新" : "记录已创建");
    await loadRows();
    await Promise.all([refreshSummary(), loadRtuOptions()]);
  } catch (error) {
    $("#form-error").textContent = error.message;
  } finally {
    state.saving = false;
    $("#save").disabled = false;
  }
}
async function removeRow(row) {
  const ctx = context();
  const label =
    ctx.schema.head.chn_name +
    " [" +
    keyOf(row, ctx.schema) +
    "] " +
    (row.name || "");
  if (!confirm("确认删除 " + label + "？")) return;
  try {
    await api(pathFor(ctx, row), {
      method: "DELETE",
      headers: { "If-Match": String(row._version) },
    });
    toast("记录已删除");
    await loadRows();
    await Promise.all([refreshSummary(), loadRtuOptions()]);
  } catch (error) {
    toast(error.message, true);
  }
}

const monitor = {
  offset: 0,
  limit: 50,
  total: 0,
  rows: [],
  request: 0,
  loading: false,
};
const logs = {
  files: [],
  filtered: [],
  selected: "",
  content: "",
  offset: 0,
  request: 0,
  contentRequest: 0,
  loading: false,
  contentLoading: false,
  entries: [],
  cursor: null,
  history: [],
  nextCursor: null,
  meta: null,
};
const timeFields = new Set([
  "refresh_time",
  "raw_time",
  "cmd_time",
  "cmd_handle_time",
  "process_start_time",
  "process_stop_time",
  "process_heartbeat_time",
]);
const clockTimeFields = new Set(["process_heartbeat_time"]);
function padTimePart(value) {
  return String(value).padStart(2, "0");
}
function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(Number(value));
  return Number.isNaN(date.valueOf())
    ? String(value)
    : date.getFullYear() +
        "-" +
        padTimePart(date.getMonth() + 1) +
        "-" +
        padTimePart(date.getDate()) +
        " " +
        padTimePart(date.getHours()) +
        ":" +
        padTimePart(date.getMinutes()) +
        ":" +
        padTimePart(date.getSeconds());
}
function fmtClockTime(value) {
  if (!value) return "—";
  const date = new Date(Number(value));
  return Number.isNaN(date.valueOf())
    ? String(value)
    : padTimePart(date.getHours()) +
        ":" +
        padTimePart(date.getMinutes()) +
        ":" +
        padTimePart(date.getSeconds());
}
function statusLabel(value) {
  return (
    {
      0: "停止",
      1: "启动中",
      2: "运行中",
      3: "停止中",
      4: "处理中",
      5: "错误",
    }[value] || String(value ?? "—")
  );
}
function statusBadge(value) {
  return (
    '<span class="status s' +
    esc(value) +
    '">' +
    esc(statusLabel(value)) +
    "</span>"
  );
}
function cellValue(name, value) {
  if (value === null || value === undefined)
    return '<span class="muted">—</span>';
  if (name === "process_status") return statusBadge(value);
  if (name === "process_enable")
    return (
      '<span class="status ' +
      (value === 1 ? "enabled" : "") +
      '">' +
      (value === 1 ? "自动" : "停用") +
      "</span>"
    );
  if (name === "channel_type") return esc(channelTypeLabel(value));
  if (clockTimeFields.has(name)) return esc(fmtClockTime(value));
  if (timeFields.has(name)) return esc(fmtTime(value));
  if (typeof value === "number" && !Number.isInteger(value))
    return esc(Number(value.toPrecision(10)));
  return esc(value);
}
function markUpdated() {
  $("#health").className = "health ok";
  $("#health span:last-child").textContent = "Web 已连接";
  $("#last-updated").textContent =
    "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
}
function showError(selector, error) {
  $(selector).textContent = error?.message || "";
  $(selector).hidden = !error;
}
function changeResource(resource, updateHash = true) {
  if (resource === "data") resource = "rtus";
  if (
    !["processes", "scdfe", "logs", ...Object.keys(resources)].includes(resource) &&
    !state.schemas.has(resource)
  )
    resource = "rtus";
  state.resource = resource;
  const group = tableGroup(resource);
  if (group) state.groupSelection[group] = resource;
  ++state.request;
  state.loading = false;
  const dataResource = !["processes", "scdfe", "logs"].includes(resource);
  $("#data-panel").hidden = !dataResource;
  $("#summary").hidden = ["processes", "scdfe", "logs"].includes(resource);
  $("#processes-panel").hidden = resource !== "processes";
  $("#scdfe-panel").hidden = resource !== "scdfe";
  $("#logs-panel").hidden = resource !== "logs";
  document
    .querySelectorAll(".nav")
    .forEach((button) =>
      button.classList.toggle(
        "active",
        button.dataset.resource === resource ||
          (button.dataset.resource === "data" && dataResource),
      ),
    );
  if (updateHash) history.replaceState(null, "", "#" + resource);
  if (resource === "processes") return loadProcesses();
  if (resource === "scdfe") return loadScdfe();
  if (resource === "logs") return loadScdfeLogs();
  state.offset = 0;
  state.sort = "";
  state.order = "asc";
  $("#search").value = "";
  if (!pointKinds.includes(resource) && state.view === "live")
    state.view = "common";
  loadRows();
}
async function openDetails(row, resource = state.resource) {
  const origin = state.resource;
  try {
    const schema = schemaFor(resource);
    const ctx = { resource, schema, rtu: "" };
    const current = (await api(pathFor(ctx, row))).item;
    if (state.resource !== origin) return;
    state.detail = { resource, row: current };
    $("#detail-title").textContent =
      (current.name || schema.head.chn_name) + " · " + keyOf(current, schema);
    $("#detail-fields").innerHTML = schema.columns
      .map((column) => {
        const value = current[column.eng_name];
        return (
          '<div class="detail-field"><dt>' +
          esc(column.chn_name) +
          "<small>" +
          esc(column.eng_name) +
          "</small></dt><dd>" +
          esc(value ?? "—") +
          (timeFields.has(column.eng_name) && value
            ? '<div class="muted">' +
              esc(clockTimeFields.has(column.eng_name) ? fmtClockTime(value) : fmtTime(value)) +
              "</div>"
            : "") +
          "</dd></div>"
        );
      })
      .join("");
    $("#detail-dialog").showModal();
  } catch (error) {
    toast(error.message, true);
  }
}
const processMonitor = { rows: [], loading: false, busy: false, request: 0, offset: 0, timer: null };
function renderProcesses() {
  const search = $("#process-search").value.trim().toLowerCase();
  const rows = processMonitor.rows.filter((row) =>
    [processLabel(row.log_process), row.process_key, row.process_pid, row.parent_pid].join(" ").toLowerCase().includes(search),
  );
  if (processMonitor.offset >= rows.length) processMonitor.offset = 0;
  const offset = processMonitor.offset;
  $("#process-count").textContent = `匹配 ${rows.length} / 全部 ${processMonitor.rows.length}`;
  $("#process-page").textContent = `${Math.floor(offset / 50) + 1} / ${Math.max(1, Math.ceil(rows.length / 50))}`;
  $("#process-prev").disabled = offset === 0;
  $("#process-next").disabled = offset + 50 >= rows.length;
  $("#process-rows").innerHTML = rows.slice(offset, offset + 50).map((row) => {
    const key = esc(row.process_key), disabled = processMonitor.busy ? " disabled" : "";
    const label = esc(processLabel(row.log_process));
    const toggle = row.kind === "debug"
      ? `<label class="cell-sub"><input type="checkbox" data-process-include="${key}" ${row.include_in_all ? "checked" : ""}${disabled} /> 参与一键启动</label>`
      : '';
    return `<tr><td class="key">${label}${toggle}</td>
      <td>${statusBadge(row.process_status)}<span class="cell-sub">心跳 ${esc(fmtClockTime(row.process_heartbeat_time))}</span></td>
      <td class="mono">${esc(row.process_pid || "—")}<span class="cell-sub">父 ${esc(row.parent_pid || "—")}</span></td>
      <td class="process-times"><span>启 ${esc(fmtTime(row.process_start_time))}</span><span>停 ${esc(fmtTime(row.process_stop_time))}</span></td>
      <td class="process-message">${row.process_error ? `<span class="process-failure">${esc(row.process_error)}</span>` : esc(row.kind === "writer" && !row.process_enable ? "写库已停用，自动通信暂停" : row.alive ? "进程在线" : "等待启动")}
        <span class="cell-sub">退出码 ${esc(row.process_exit_code)} · 期望${row.process_enable ? "运行" : "停止"}</span></td>
      <td class="sticky-actions"><div class="process-actions">
        <button class="row-button" data-process-key="${key}" data-process-action="start"${disabled || (row.alive ? " disabled" : "")} aria-label="启动${label}">启动</button>
        <button class="row-button" data-process-key="${key}" data-process-action="stop"${disabled || (!row.alive && !row.process_enable ? " disabled" : "")} aria-label="停止${label}">停止</button>
        <button class="row-button" data-process-log="${esc(row.log_process)}" aria-label="查看${label}日志">日志</button>
      </div></td></tr>`;
  }).join("") || '<tr><td colspan="6" class="empty">没有匹配的进程</td></tr>';
  for (const id of ["process-all-start", "process-all-stop", "debug-start"]) $("#" + id).disabled = processMonitor.busy;
}
async function loadProcesses() {
  const request = ++processMonitor.request;
  processMonitor.loading = true;
  try {
    const data = await api("/api/processes");
    if (request !== processMonitor.request) return;
    processMonitor.rows = data.items;
    processMonitor.busy = data.busy;
    $("#process-summary").innerHTML = [
      [data.items.length, "登记进程"], [data.items.filter((r) => r.alive).length, "进程在线"],
      [data.items.filter((r) => r.process_status === 5).length, "进程异常"],
      [data.items.filter((r) => r.kind === "debug" && r.include_in_all).length, "已选调试"],
    ].map(([value, label]) => `<div class="metric"><strong>${value}</strong><span>${label}</span></div>`).join("");
    const op = data.operation;
    const panel = $("#process-operation");
    const expanded = panel.querySelector("details")?.open;
    panel.hidden = !op && !data.busy;
    panel.classList.toggle("failed", op?.status === "failed");
    panel.innerHTML = op
      ? `<b>${esc({ running: "执行中", completed: "已完成", failed: "执行失败" }[op.status])} · ${esc(op.stage)}</b>
         <span class="muted">开始 ${esc(fmtTime(op.started_at))}${op.finished_at ? " · 结束 " + esc(fmtTime(op.finished_at)) : ""}</span>
         ${op.error ? `<div role="alert">${esc(op.error)}</div>` : ""}
         <details${expanded ? " open" : ""}><summary>执行步骤（${op.steps.length}）</summary><ol>${op.steps.map((step) => `<li>${esc(fmtClockTime(step.time))} ${esc(step.message)}</li>`).join("")}</ol></details>`
      : '';
    if (data.busy && op?.status !== "running") panel.innerHTML += '<b>其他实例正在操作</b>';
    renderProcesses();
    showError("#process-error", null);
    markUpdated();
    clearTimeout(processMonitor.timer);
    // Operation progress is live even when periodic data refresh is disabled.
    if (data.busy && state.resource === "processes") processMonitor.timer = setTimeout(loadProcesses, 1000);
  } catch (error) {
    if (request === processMonitor.request) showError("#process-error", error);
  } finally {
    if (request === processMonitor.request) processMonitor.loading = false;
  }
}
async function controlProcess(target, action, extra = {}) {
  processMonitor.busy = true;
  renderProcesses();
  try {
    await api("/api/processes/control", { method: "POST", body: JSON.stringify({ target, action, ...extra }) });
    toast("操作已受理");
  } catch (error) {
    toast(error.message, true);
  } finally {
    await loadProcesses();
  }
}
function openProcessLogs(name) {
  const select = $("#log-process");
  if (![...select.options].some((option) => option.value === name)) select.add(new Option(processLabel(name), name));
  select.value = name;
  logs.offset = 0;
  filterLogs();
  changeResource("logs");
}
$("#process-refresh").onclick = loadProcesses;
$("#process-all-start").onclick = () => controlProcess("all", "start");
$("#process-all-stop").onclick = () => controlProcess("all", "stop");
$("#debug-process-form").onsubmit = (event) => {
  event.preventDefault();
  const rtu = $("#debug-rtu").value.trim();
  if (!/^[1-9]\d*$/.test(rtu)) return toast("请输入正整数 RTU 编号", true);
  controlProcess("debug:" + rtu, "start", { include_in_all: $("#debug-include").checked });
};
$("#process-search").oninput = () => { processMonitor.offset = 0; renderProcesses(); };
$("#process-prev").onclick = () => { processMonitor.offset -= 50; renderProcesses(); };
$("#process-next").onclick = () => { processMonitor.offset += 50; renderProcesses(); };
$("#process-rows").onclick = (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.processLog) openProcessLogs(button.dataset.processLog);
  else if (button.dataset.processAction) controlProcess(button.dataset.processKey, button.dataset.processAction);
};
$("#process-rows").onchange = (event) => {
  if (event.target.dataset.processInclude) controlProcess(event.target.dataset.processInclude, "configure", { include_in_all: event.target.checked });
};

async function loadScdfe() {
  const request = ++monitor.request;
  monitor.loading = true;
  const params = new URLSearchParams({
    limit: String(monitor.limit),
    offset: String(monitor.offset),
    q: $("#thread-search").value,
  });
  const filter = $("#thread-status").value;
  if (filter === "disabled") params.set("process_enable", "0");
  else if (filter !== "") params.set("process_status", filter);
  try {
    const data = await api("/api/scdfe_c/status?" + params);
    if (request !== monitor.request) return;
    if (monitor.offset && monitor.offset >= data.total) {
      monitor.offset = 0;
      return loadScdfe();
    }
    monitor.total = data.total;
    monitor.rows = data.rtus;
    const item = data.item;
    for (const [id, field] of [
      ["scdfe-worker-delay", "worker_start_delay_ms"],
      ["scdfe-retention-hours", "log_retention_hours"],
    ]) {
      if (document.activeElement !== $("#" + id) && !$("#" + id).dataset.dirty)
        $("#" + id).value = item[field];
    }
    $("#scdfe-status").innerHTML = [
      [
        statusBadge(item.process_status),
        "主进程 · PID " + (item.process_pid || "—"),
      ],
      [esc(data.counts.total), "RTU 总数"],
      [esc(data.counts.running), "线程运行中"],
      [esc(data.counts.errors), "线程错误"],
      [esc(data.counts.automatic), "参与自动通信"],
    ]
      .map(
        ([value, label]) =>
          '<div class="metric"><strong>' +
          value +
          "</strong><span>" +
          label +
          "</span></div>",
      )
      .join("");
    $("#scdfe-details").innerHTML =
      [
        ["启动", fmtTime(item.process_start_time)],
        ["停止", fmtTime(item.process_stop_time)],
        ["心跳", fmtClockTime(item.process_heartbeat_time)],
        ["退出码", item.process_exit_code ?? "—"],
      ]
        .map(
          ([label, value]) =>
            "<span>" + label + " <b>" + esc(value) + "</b></span>",
        )
        .join("") +
      (item.process_error
        ? '<div class="process-error">主进程错误：' +
          esc(item.process_error) +
          "</div>"
        : "");
    $("#scdfe-rtus").innerHTML =
      data.rtus
        .map(
          (row, index) =>
            "<tr>" +
            '<td class="key">' +
            esc(row.rtu_id) +
            '<span class="cell-sub">' +
            esc(row.name || "未命名") +
            "</span></td><td>" +
            esc(row.channel_id) +
            '<span class="cell-sub">' +
            esc(channelTypeLabel(row.channel_type)) +
            "</span></td>" +
            "<td>" +
            cellValue("process_enable", row.process_enable) +
            "</td><td>" +
            statusBadge(row.process_status) +
            "</td><td>" +
            esc(row.thread_id || "—") +
            "</td><td>" +
            esc(fmtClockTime(row.process_heartbeat_time)) +
            "</td>" +
            '<td class="error-cell"><button class="row-button" data-thread-detail="' +
            index +
            '" title="' +
            esc(row.process_error || "查看完整记录") +
            '">' +
            esc(row.process_error || "—") +
            "</button></td>" +
            '<td class="sticky-actions"><button class="row-button" data-thread-log="' +
            esc(row.rtu_id) +
            '">日志</button><button class="row-button" data-thread-enable="' +
            index +
            '">' +
            (row.process_enable === 1 ? "停用" : "启用") +
            "</button></td></tr>",
        )
        .join("") ||
      '<tr><td class="empty" colspan="8">没有匹配的 RTU</td></tr>';
    $("#scdfe-rtus")
      .querySelectorAll("[data-thread-log]")
      .forEach((button) => {
        button.onclick = () => openRtuLogs(button.dataset.threadLog);
      });
    $("#scdfe-rtus")
      .querySelectorAll("[data-thread-detail]")
      .forEach((button) => {
        button.onclick = () =>
          openDetails(
            monitor.rows[Number(button.dataset.threadDetail)],
            "rtus",
          );
      });
    $("#scdfe-rtus")
      .querySelectorAll("[data-thread-enable]")
      .forEach((button) => {
        button.onclick = () =>
          controlRtu(
            monitor.rows[Number(button.dataset.threadEnable)],
            "enable",
            button,
          );
      });
    $("#thread-count").textContent = "匹配 " + data.total + " 个 RTU";
    $("#thread-page").textContent =
      Math.floor(monitor.offset / monitor.limit) +
      1 +
      " / " +
      Math.max(1, Math.ceil(data.total / monitor.limit));
    $("#thread-prev").disabled = monitor.offset === 0;
    $("#thread-next").disabled = monitor.offset + monitor.limit >= data.total;
    showError("#scdfe-error", null);
    markUpdated();
  } catch (error) {
    if (request === monitor.request) showError("#scdfe-error", error);
  } finally {
    if (request === monitor.request) monitor.loading = false;
  }
}
async function controlRtu(row, action, button) {
  const values = { process_enable: row.process_enable === 1 ? 0 : 1 };
  if (
    !confirm(
      "确认" +
        (values.process_enable ? "启用自动通信：" : "停用自动通信：") +
        " RTU " +
        row.rtu_id +
        "？",
    )
  )
    return;
  button.disabled = true;
  try {
    await api("/api/rtus/" + encodeURIComponent(row.rtu_id), {
      method: "PATCH",
      headers: { "If-Match": String(row._version) },
      body: JSON.stringify(values),
    });
    toast("RTU 控制已写入，等待通信程序响应");
    await loadScdfe();
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
}
function processLabel(name) {
  if (name === "scdfe_c-main") return "主进程";
  if (name === "scdfe_c-db-writer") return "数据库写进程";
  const rtu = /^rtu:(\d+)$/.exec(name);
  if (rtu) return "RTU " + rtu[1] + " · 全部日志";
  const match = /^scdfe_c-(debug-)?rtu-(\d+)$/.exec(name);
  return match
    ? "RTU " +
        match[2] +
        (match[1] ? " · 人工调试" : " · 自动通信")
    : name;
}
function fmtSize(size) {
  return size >= 1048576
    ? (size / 1048576).toFixed(1) + " MB"
    : size >= 1024
      ? (size / 1024).toFixed(1) + " KB"
      : size + " B";
}
function openRtuLogs(rtu) {
  openProcessLogs("rtu:" + rtu);
}
function logProcessFilter() {
  const value = $("#log-process").value;
  const rtu = /^rtu:(\d+)$/.exec(value);
  return rtu ? { rtu_id: rtu[1] } : value ? { process: value } : {};
}
async function loadScdfeLogs(refreshContent = true) {
  const request = ++logs.request;
  logs.loading = true;
  try {
    // Build the process menu from the complete index, never from its filtered
    // subset, so another process remains selectable after a filter change.
    const data = await api("/api/scdfe_c/logs");
    if (request !== logs.request) return;
    logs.files = data.items;
    const selected = $("#log-process").value;
    const processes = [...new Set(data.items.map((item) => item.process))];
    if (selected && !processes.includes(selected)) processes.push(selected);
    $("#log-process").innerHTML =
      '<option value="">全部进程</option>' +
      processes
        .map(
          (name) =>
            '<option value="' +
            esc(name) +
            '">' +
            esc(processLabel(name)) +
            "</option>",
        )
        .join("");
    $("#log-process").value = selected;
    $("#log-directory").textContent = "日志目录：" + data.directory;
    showError("#logs-error", null);
    await filterLogs(true, refreshContent);
    markUpdated();
  } catch (error) {
    if (request === logs.request) showError("#logs-error", error);
  } finally {
    if (request === logs.request) logs.loading = false;
  }
}
async function filterLogs(refresh = false, refreshContent = true) {
  const filters = Object.entries(logProcessFilter());
  logs.filtered = logs.files.filter(
    (item) => filters.every(([key, value]) => String(item[key] ?? "") === value),
  );
  if (logs.offset >= logs.filtered.length) logs.offset = 0;
  let selected = logs.filtered.find((item) => item.name === logs.selected);
  if (refresh && $("#log-follow").checked && !$("#log-end").value && selected)
    selected = logs.filtered.find((item) => item.process === selected.process);
  if (!selected) selected = logs.filtered[0];
  const changed = logs.selected !== (selected?.name || "");
  logs.selected = selected?.name || "";
  renderLogFiles();
  if (!refresh || changed || refreshContent || (!logs.cursor && $("#log-follow").checked))
    await readLog(logs.selected, changed);
}
function renderLogFiles() {
  $("#log-file-count").textContent =
    logs.filtered.length + " / " + logs.files.length + " 个文件";
  const page = logs.filtered.slice(logs.offset, logs.offset + 50);
  $("#scdfe-logs").innerHTML = page.length
    ? renderLogTree(page)
    : '<div class="empty">暂无匹配文件</div>';
  $("#scdfe-logs")
    .querySelectorAll("[data-log-name]")
    .forEach((button) => {
      button.onclick = () => {
        $("#log-scope").value = "file";
        logs.selected = button.dataset.logName;
        renderLogFiles();
        readLog(logs.selected, true);
      };
    });
  $("#log-page").textContent =
    Math.floor(logs.offset / 50) +
    1 +
    " / " +
    Math.max(1, Math.ceil(logs.filtered.length / 50));
  $("#log-prev").disabled = logs.offset === 0;
  $("#log-next").disabled = logs.offset + 50 >= logs.filtered.length;
}
function logTreeDirectoryLabel(name, depth) {
  const suffix = ["年", "月", "日", "时"][depth] || "";
  return suffix ? name + suffix : name;
}
function logTreeFile(item) {
  return (
    '<button class="log-file' +
    (item.name === logs.selected ? " selected" : "") +
    '" data-log-name="' +
    esc(item.name) +
    '" aria-pressed="' +
    (item.name === logs.selected) +
    '" role="treeitem"><strong>' +
    esc(processLabel(item.process)) +
    '</strong><span class="log-file-size">' +
    esc(fmtSize(item.size)) +
    "</span></button>"
  );
}
function renderLogTree(items) {
  const root = { children: new Map(), files: [], count: 0 };
  for (const item of items) {
    const parts = item.name.split("/").filter(Boolean);
    parts.pop();
    let node = root;
    node.count += 1;
    parts.forEach((part, depth) => {
      if (!node.children.has(part))
        node.children.set(part, { name: part, depth, children: new Map(), files: [], count: 0 });
      node = node.children.get(part);
      node.count += 1;
    });
    node.files.push(item);
  }
  function renderNode(node) {
    const groups = [...node.children.values()]
      .map((child) =>
        '<details class="log-tree-group" open><summary role="treeitem">' +
        '<span class="log-tree-folder" aria-hidden="true"></span><strong>' +
        esc(logTreeDirectoryLabel(child.name, child.depth)) +
        '</strong><small>' + child.count + ' 个文件</small></summary>' +
        '<div class="log-tree-children" role="group">' +
        renderNode(child) +
        '</div></details>',
      )
      .join("");
    return (
      groups +
      node.files.map(logTreeFile).join("")
    );
  }
  return renderNode(root);
}
function logSearchParams(filename) {
  const params = new URLSearchParams({ limit: $("#log-tail").value, ...logProcessFilter() });
  for (const [id, key] of [["log-search", "q"], ["log-level", "level"]]) {
    const value = $("#" + id).value.trim();
    if (value) params.set(key, value);
  }
  const start = $("#log-start"), end = $("#log-end");
  if (!start.validity.valid || !end.validity.valid) throw new Error("请输入完整且有效的起止时间");
  const date = /-(\d{4})(\d{2})(\d{2})-\d{2}\.log$/.exec(filename || "");
  if ((start.value || end.value) && !date) throw new Error("请先选择日志文件");
  const day = date ? `${date[1]}-${date[2]}-${date[3]}` : "";
  const from = start.value ? new Date(day + "T" + start.value).getTime() : null;
  const to = end.value ? new Date(day + "T" + end.value).getTime() + 999 : null;
  if ((from !== null && !Number.isFinite(from)) || (to !== null && !Number.isFinite(to)))
    throw new Error("请输入有效的起止时间");
  if (from !== null && to !== null && from > to) throw new Error("开始时间不能晚于结束时间");
  if (from !== null) params.set("start", new Date(from).toISOString());
  if (to !== null) params.set("end", new Date(to).toISOString());
  if ($("#log-scope").value === "file" && filename) params.set("filename", filename);
  return params;
}
async function readLog(filename = logs.selected, changed = false, cursor = null, history = []) {
  const request = ++logs.contentRequest;
  logs.contentLoading = true;
  logs.content = "";
  logs.entries = [];
  logs.meta = null;
  logs.nextCursor = null;
  logs.cursor = cursor;
  logs.history = history;
  $("#log-download").disabled = true;
  $("#log-result-prev").disabled = true;
  $("#log-result-next").disabled = true;
  $("#log-title").textContent = $("#log-scope").value === "all" ? "全部匹配文件 · 日志检索" : filename || "暂无匹配日志";
  $("#log-meta").textContent = "正在按条件检索…";
  $("#log-lines").textContent = "正在检索…";
  $("#log-search-info").textContent = "";
  $("#scdfe-log-content").innerHTML = '<div class="log-empty">正在检索日志…</div>';
  try {
    const params = logSearchParams(filename);
    if (cursor) params.set("cursor", cursor);
    const data = !filename && $("#log-scope").value === "file"
      ? { items: [], next_cursor: null, scanned_files: 0, scanned_bytes: 0, skipped_lines: 0 }
      : await api("/api/scdfe_c/logs/search?" + params);
    if (request !== logs.contentRequest) return;
    logs.entries = data.items.slice().reverse();
    logs.content = logs.entries.map((entry) =>
      ($("#log-scope").value === "all" ? "[" + entry.filename + "] " : "") +
      entry.text).join("\n");
    logs.meta = data;
    logs.nextCursor = data.next_cursor;
    $("#log-meta").textContent = "第 " + (history.length + 1) + " 批 · " + data.scanned_files + " 个文件";
    $("#log-download").disabled = !logs.entries.length;
    $("#log-result-prev").disabled = !history.length;
    $("#log-result-next").disabled = !data.next_cursor;
    showError("#logs-error", null);
    renderLogContent(changed);
    markUpdated();
  } catch (error) {
    if (request !== logs.contentRequest) return;
    $("#log-meta").textContent = "筛选失败";
    $("#scdfe-log-content").innerHTML = '<div class="log-empty">' + esc(error.message) + "</div>";
    $("#log-lines").textContent = "筛选失败";
    showError("#logs-error", error);
  } finally {
    if (request === logs.contentRequest) logs.contentLoading = false;
  }
}
function highlight(text, query) {
  if (!query) return esc(text);
  let output = "",
    offset = 0,
    index;
  const lower = text.toLowerCase();
  while ((index = lower.indexOf(query, offset)) >= 0) {
    output +=
      esc(text.slice(offset, index)) +
      "<mark>" +
      esc(text.slice(index, index + query.length)) +
      "</mark>";
    offset = index + query.length;
  }
  return output + esc(text.slice(offset));
}
function renderLogContent(changed = false) {
  if (!logs.meta) return;
  const query = logs.meta?.keyword?.toLowerCase() || "";
  const content = $("#scdfe-log-content"), scroll = content.scrollTop;
  const allFiles = $("#log-scope").value === "all";
  content.innerHTML = logs.entries.map((entry, index) =>
    '<div class="log-line ' + esc(entry.level.toLowerCase()) + '"><span class="line-no">' + (index + 1) +
    '</span><span class="line-text">' + highlight(entry.text, query) +
    (allFiles ? '<small class="log-source">' + esc(entry.filename) + '</small>' : '') + '</span></div>',
  ).join("") || '<div class="log-empty">' + (logs.nextCursor
    ? "本批无匹配日志"
    : "没有匹配的日志") + '</div>';
  content.classList.toggle("wrap", $("#log-wrap").checked);
  $("#log-lines").textContent = "本批匹配 " + logs.entries.length + " 条" + (logs.meta?.skipped_lines ? " · 已跳过超长日志行" : "");
  $("#log-search-info").textContent = logs.nextCursor
    ? "还有更早日志"
    : "已到起点";
  content.scrollTop = $("#log-follow").checked ? content.scrollHeight : changed ? 0 : scroll;
}
async function controlScdfe(values) {
  try {
    await api("/api/scdfe_c/control", {
      method: "PATCH",
      body: JSON.stringify(values),
    });
    toast("控制配置已写入");
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  } finally {
    loadScdfe();
  }
}
function closeEditor() {
  if (!state.saving) $("#editor").close();
}
function debounce(handler, delay = 250) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => handler(...args), delay);
  };
}
async function boot() {
  try {
    await api("/api/health");
    const { items } = await api("/api/schemas");
    items.forEach((schema) => state.schemas.set(schema.head.eng_name, schema));
    const primary = Object.values(resources).map((item) => item[0]);
    const helperSchemas = items.filter(
      (schema) =>
        !primary.includes(schema.head.eng_name) &&
        !["fes_scdfe_c_define", "fes_process_define"].includes(schema.head.eng_name),
    );
    state.tableGroups.parameters = Object.keys(parameterTables).filter((name) => state.schemas.has(name));
    state.tableGroups.helpers = helperSchemas
      .map((schema) => schema.head.eng_name)
      .filter((name) => !Object.hasOwn(parameterTables, name));
    $("#data-tabs").querySelectorAll("[data-tab-group]").forEach((tab) => {
      tab.disabled = !state.tableGroups[tab.dataset.tabGroup].length;
    });
    $("#helper-tabs").innerHTML = Object.values(state.tableGroups).flat()
      .map(
        (name) =>
          '<button type="button" data-tab-resource="' +
          esc(name) +
          '" data-table-group="' + tableGroup(name) +
          '" title="' +
          esc(state.schemas.get(name).head.chn_name) +
          '">' +
          esc(parameterTables[name] || state.schemas.get(name).head.chn_name.replace(/定义表$|表$/g, "")) +
          "</button>",
      )
      .join("");
    await Promise.all([refreshSummary(), loadRtuOptions()]);
    changeResource(location.hash.slice(1) || "rtus", false);
  } catch (error) {
    $("#health").className = "health error";
    $("#health span:last-child").textContent = "连接失败";
    showError("#table-error", error);
  }
}
document.querySelectorAll(".nav").forEach((button) => {
  button.onclick = () => changeResource(button.dataset.resource);
});
$("#data-tabs").onclick = (event) => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  const group = button.dataset.tabGroup;
  changeResource(group
    ? state.groupSelection[group] || state.tableGroups[group][0]
    : button.dataset.tabResource);
};
$("#helper-tabs").onclick = (event) => {
  const button = event.target.closest("[data-tab-resource]");
  if (button) changeResource(button.dataset.tabResource);
};
window.addEventListener("hashchange", () => {
  if (state.schemas.size) changeResource(location.hash.slice(1), false);
});
$("#scdfe-processes").onclick = () => changeResource("processes");
$("#scdfe-refresh").onclick = loadScdfe;
for (const [id, button, field, low, high] of [
  ["scdfe-worker-delay", "scdfe-save-delay", "worker_start_delay_ms", 0, 60000],
  [
    "scdfe-retention-hours",
    "scdfe-save-retention",
    "log_retention_hours",
    1,
    87600,
  ],
]) {
  $("#" + id).oninput = () => {
    $("#" + id).dataset.dirty = "true";
  };
  $("#" + button).onclick = async () => {
    const input = $("#" + id),
      value = Number(input.value);
    if (
      !input.value.trim() ||
      !Number.isInteger(value) ||
      value < low ||
      value > high
    ) {
      toast("请输入 " + low + " 到 " + high + " 的整数", true);
      input.focus();
      return;
    }
    if (await controlScdfe({ [field]: value })) delete input.dataset.dirty;
  };
}
$("#thread-search").oninput = debounce(() => {
  monitor.offset = 0;
  loadScdfe();
});
$("#thread-status").onchange = () => {
  monitor.offset = 0;
  loadScdfe();
};
$("#thread-prev").onclick = () => {
  monitor.offset = Math.max(0, monitor.offset - monitor.limit);
  loadScdfe();
};
$("#thread-next").onclick = () => {
  monitor.offset += monitor.limit;
  loadScdfe();
};
$("#logs-refresh").onclick = () => loadScdfeLogs(true);
$("#log-scope").onchange = () => readLog(logs.selected, true);
$("#log-process").onchange = () => {
  logs.offset = 0;
  filterLogs();
};
$("#log-prev").onclick = () => {
  logs.offset = Math.max(0, logs.offset - 50);
  renderLogFiles();
};
$("#log-next").onclick = () => {
  logs.offset += 50;
  renderLogFiles();
};
function clearLogConditions() {
  for (const id of ["log-search", "log-level", "log-start", "log-end"]) $("#" + id).value = "";
}
$("#log-search").oninput = debounce(() => readLog(logs.selected, true), 300);
for (const id of ["log-level", "log-start", "log-end", "log-tail"])
  $("#" + id).onchange = () => readLog(logs.selected, true);
$("#log-search").onkeydown = (event) => { if (event.key === "Enter") readLog(logs.selected, true); };
$("#log-apply").onclick = () => readLog(logs.selected, true);
$("#log-content-clear").onclick = () => { clearLogConditions(); readLog(logs.selected, true); };
$("#log-result-next").onclick = () => {
  if (!logs.nextCursor) return;
  $("#log-follow").checked = false;
  readLog(logs.selected, true, logs.nextCursor, [...logs.history, logs.cursor]);
};
$("#log-result-prev").onclick = () => {
  if (!logs.history.length) return;
  const history = logs.history.slice(), cursor = history.pop();
  readLog(logs.selected, true, cursor, history);
};
$("#log-wrap").onchange = () => renderLogContent();
$("#log-follow").onchange = () => {
  if ($("#log-follow").checked) loadScdfeLogs();
};
$("#log-download").onclick = () => {
  const url = URL.createObjectURL(
    new Blob([logs.content], { type: "text/plain;charset=utf-8" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = ($("#log-scope").value === "all" ? "scdfe_c-logs" : logs.selected) + ".filtered.txt";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$("#refresh").onclick = async () => {
  await loadRows();
  try {
    await Promise.all([refreshSummary(), loadRtuOptions()]);
  } catch (error) {
    toast(error.message, true);
  }
};
$("#add").onclick = () =>
  state.resource === "rtus" ? openNewRtu() : openEditor();
$("#add-channel").onclick = openChannelChooser;
$("#editor-form").onsubmit = saveRow;
$("#close-editor").onclick = $("#cancel-editor").onclick = closeEditor;
$("#editor").addEventListener("cancel", (event) => {
  if (state.saving) event.preventDefault();
});
$("#field-search").oninput = () => {
  const query = $("#field-search").value.trim().toLowerCase();
  $("#form-fields")
    .querySelectorAll(".field")
    .forEach((field) => {
      field.hidden = !field.textContent.toLowerCase().includes(query);
    });
};
$("#close-detail").onclick = () => $("#detail-dialog").close();
$("#cancel-channel-select").onclick = () => $("#channel-select-dialog").close();
$("#channel-select-form").onsubmit = (event) => {
  event.preventDefault();
  const selected = $("#channel-select-rtu").value;
  $("#channel-select-dialog").close();
  const row = state.items.find((item) => String(item.rtu_id) === selected);
  if (row) return openChannelEditor(row);
  api("/api/rtus/" + encodeURIComponent(selected))
    .then((data) => openChannelEditor(data.item))
    .catch((error) => toast(error.message, true));
};
$("#detail-edit").onclick = () => {
  const detail = state.detail;
  $("#detail-dialog").close();
  if (detail.resource !== state.resource) changeResource(detail.resource);
  openEditor(detail.row);
};
$("#search").oninput = debounce(() => {
  state.offset = 0;
  loadRows();
});
$("#rtu-filter").oninput = (event) => {
  state.rtu = event.target.value.trim();
  state.offset = 0;
  ++state.request;
  $("#add").disabled = true;
  clearTimeout(state.rtuTimer);
  state.rtuTimer = setTimeout(loadRows, 300);
};
$("#clear-filters").onclick = () => {
  state.rtu = "";
  $("#rtu-filter").value = "";
  $("#search").value = "";
  state.offset = 0;
  loadRows();
};
for (const view of ["common", "live", "all"])
  $("#view-" + view).onclick = () => {
    state.view = view;
    showView();
    renderTable();
  };
$("#page-size").onchange = () => {
  state.limit = Number($("#page-size").value);
  state.offset = 0;
  loadRows();
};
$("#prev").onclick = () => {
  state.offset = Math.max(0, state.offset - state.limit);
  loadRows();
};
$("#next").onclick = () => {
  state.offset += state.limit;
  loadRows();
};
$("#show-schema").onclick = () => {
  const schema = schemaFor();
  if (!schema) return;
  $("#schema-title").textContent =
    schema.head.chn_name + " · " + schema.columns.length + " 字段";
  $("#schema-json").textContent = JSON.stringify(schema, null, 2);
  $("#schema-dialog").showModal();
};
$("#close-schema").onclick = () => $("#schema-dialog").close();
setInterval(() => {
  if (
    !$("#auto-refresh").checked ||
    document.hidden ||
    document.querySelector("dialog[open]") ||
    state.saving
  )
    return;
  if (state.resource === "processes") {
    if (!processMonitor.loading) loadProcesses();
  } else if (state.resource === "scdfe") {
    if (!monitor.loading) loadScdfe();
  } else if (state.resource === "logs") {
    if (!logs.loading && !logs.contentLoading) loadScdfeLogs(false);
  } else if (!state.loading) loadRows();
}, 5000);
boot();

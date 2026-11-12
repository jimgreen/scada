"use strict";

const $ = (selector) => document.querySelector(selector);
const state = {
  schemas: new Map(),
  tableGroups: { parameters: [], helpers: [] },
  groupSelection: {},
  resource: "channels",
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
  listStates: {},
};
const resources = {
  channels: ["channels", "通道定义"],
  rtus: ["fes_rtu_define", "RTU 定义"],
  yc: ["fes_yc_define", "遥测点表"],
  yx: ["fes_yx_define", "遥信点表"],
  yt: ["fes_yt_define", "遥调点表"],
  yk: ["fes_yk_define", "遥控点表"],
};
const CHANNEL_TYPE_OPTIONS = ["iec104", "modbus-rtu", "modbus-tcp", "can", "bacnet", "opc"];
const RTU_EDITOR_ORDER = [
  "rtu_id",
  "name",
  "channel_type",
  "channel_id",
  "status",
  "refresh_time",
  "packet_recording",
];
const channelSchema = {
  head: { eng_name: "channels", chn_name: "通道定义" },
  primary_keys: ["channel_type", "channel_id"],
  columns: [
    { chn_name: "通道标识", eng_name: "channel_id", primary_key: true, data_type: "int", disp_type: "INT" },
    { chn_name: "通道类型", eng_name: "channel_type", primary_key: true, data_type: "str", disp_type: "STRING", allowed_values: ["iec104", "modbus-rtu", "modbus-tcp", "can", "bacnet", "opc"] },
    { chn_name: "中文名称", eng_name: "name", data_type: "str", disp_type: "STRING" },
    { chn_name: "保存报文", eng_name: "packet_recording", data_type: "int", disp_type: "INT" },
    { chn_name: "绑定 RTU", eng_name: "rtu_id", data_type: "int", disp_type: "INT" },
    { chn_name: "RTU 名称", eng_name: "rtu_name", data_type: "str", disp_type: "STRING" },
    { chn_name: "已绑定", eng_name: "bound", data_type: "int", disp_type: "INT" },
    { chn_name: "参数已配置", eng_name: "parameter_ready", data_type: "int", disp_type: "INT" },
  ],
};
const pointKinds = ["yc", "yx", "yt", "yk"];
const parameterTables = {
  fes_channel_iec104_define: "IEC104",
  fes_channel_modbus_tcp_define: "Modbus-TCP",
  fes_channel_modbus_rtu_define: "Modbus-RTU",
  fes_channel_can_define: "CAN",
  fes_channel_bacnet_define: "BACnet",
  fes_channel_opc_define: "OPC",
  fes_channel_iec104_point_define: "IEC104点映射",
  fes_channel_bacnet_point_define: "BACnet点映射",
  fes_channel_opc_point_define: "OPC点映射",
  fes_channel_can_point_define: "CAN报文解析",
};
const channelFields = ["channel_id", "channel_type", "packet_recording"];
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
  channels: [
    "channel_id",
    "channel_type",
    "name",
    "packet_recording",
    "rtu_id",
    "rtu_name",
    "parameter_ready",
  ],
  rtus: [
    "rtu_id",
    "name",
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
    "cmd_time",
    "cmd_handle_time",
  ],
  yk: ["rtu_id", "pnt_no", "name", "value", "status", "cmd_time", "cmd_handle_time"],
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

function schemaTypeLabel(column) {
  const value = String(column?.disp_type || "");
  return value === "LONG" ? "INT" : value;
}

// Protocol auxiliary tables have constraints that are more specific than the
// generic SQLite column type. Keep the UI rules here so a user sees the same
// ranges and choices that the protocol adapters require before submitting.
const MODBUS_ENUM_LABELS = {
  icode: {
    1: "1 · 线圈",
    2: "2 · 离散输入",
    3: "3 · 保持寄存器",
    4: "4 · 输入寄存器",
    5: "5 · 单线圈",
    6: "6 · 单寄存器",
    16: "16 · 多寄存器",
  },
  trans_type: {
    1: "1 · int16",
    2: "2 · uint16",
    3: "3 · int32",
    4: "4 · uint32",
    5: "5 · float32",
    6: "6 · byte",
    7: "7 · int8",
    8: "8 · bit",
    9: "9 · multi-bit",
    10: "10 · int64",
    11: "11 · uint64",
  },
  ytyk_type: {
    1: "1 · int16",
    2: "2 · uint16",
    3: "3 · int32",
    4: "4 · uint32",
    5: "5 · float32",
    6: "6 · bit",
    7: "7 · 特殊位",
  },
  parity: { N: "N · 无校验", E: "E · 偶校验", O: "O · 奇校验" },
  keep_alive: { 0: "0 · 请求后断开", 1: "1 · 保持连接" },
  binary: { 0: "0 · 否", 1: "1 · 是" },
  twobytedata_byteseq: {
    0: "0 · 大端",
    1: "1 · 小端",
    2: "2 · 小端",
    3: "3 · 小端",
  },
  fourbytedata_byteseq: {
    0: "0 · DCBA",
    1: "1 · BADC",
    2: "2 · CDAB",
    3: "3 · ABCD",
  },
};

const PROTOCOL_RULES = {
  fes_rtu_define: {
    packet_recording: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary, default: 0 },
  },
  fes_channel_modbus_tcp_define: {
    remote_port: { min: 1, max: 65535, step: 1 },
    unit_id: { min: 1, max: 247, step: 1 },
    connect_timeout: { min: 0.000001, step: "any" },
    response_timeout: { min: 0.000001, step: "any" },
    retry_count: { min: 0, max: 10, step: 1 },
    keep_alive: { options: [0, 1], labels: MODBUS_ENUM_LABELS.keep_alive },
  },
  fes_channel_modbus_rtu_define: {
    baudrate: { min: 1, max: 10000000, step: 1 },
    bytesize: { options: [8] },
    parity: { options: ["N", "E", "O"], labels: MODBUS_ENUM_LABELS.parity },
    stopbits: { options: [1, 2], step: 1 },
    unit_id: { min: 1, max: 247, step: 1 },
    response_timeout: { min: 0.000001, step: "any" },
    retry_count: { min: 0, max: 10, step: 1 },
  },
  fes_channel_modbus_define: {
    irtuaddr: { min: 1, max: 247, step: 1 },
    iplace: { min: 0, max: 65535, step: 1 },
    inum: { min: 1, max: 2000, step: 1 },
    icode: { options: [1, 2, 3, 4], labels: MODBUS_ENUM_LABELS.icode },
    ioffset: { min: 0, max: 65535, step: 1 },
    ibitnum: { min: 1, max: 16, step: 1 },
  },
  fes_channel_modbus_define_call: {
    irtuaddr: { min: 1, max: 247, step: 1 },
    call_seq: { min: 0, step: 1 },
    iplace: { min: 0, max: 65535, step: 1 },
    inum: { min: 1, max: 2000, step: 1 },
    icode: { options: [1, 2, 3, 4], labels: MODBUS_ENUM_LABELS.icode },
  },
  fes_channel_modbus_define_data: {
    call_seq: { min: 0, step: 1 },
    if_yx: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary },
    cnt: { min: 0, max: 2147483647, step: 1 },
    trans_type: { options: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], labels: MODBUS_ENUM_LABELS.trans_type },
    reg_addr: { min: 0, max: 65535, step: 1 },
    offset_bit: { min: 0, max: 15, step: 1 },
    all_bit: { min: 1, max: 16, step: 1 },
    if_reverse: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary },
    twobytedata_byteseq: { options: [0, 1, 2, 3], labels: MODBUS_ENUM_LABELS.twobytedata_byteseq },
    fourbytedata_byteseq: { options: [0, 1, 2, 3], labels: MODBUS_ENUM_LABELS.fourbytedata_byteseq },
  },
  fes_channel_modbus_define_ytyk: {
    ifyk: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary },
    cnt: { min: 0, max: 2147483647, step: 1 },
    irtuaddr: { min: 1, max: 247, step: 1 },
    reg_addr: { min: 0, max: 65535, step: 1 },
    icode: { options: [5, 6, 16], labels: MODBUS_ENUM_LABELS.icode },
    ytyk_type: { options: [1, 2, 3, 4, 5, 6, 7], labels: MODBUS_ENUM_LABELS.ytyk_type },
    ifreverse: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary },
    twobytedata_byteseq: { options: [0, 1, 2, 3], labels: MODBUS_ENUM_LABELS.twobytedata_byteseq },
    fourbytedata_byteseq: { options: [0, 1, 2, 3], labels: MODBUS_ENUM_LABELS.fourbytedata_byteseq },
    multi_para: { step: "any" },
    special_yk_yccnt: { min: 0, max: 2147483647, step: 1 },
    special_yk_bit: { min: 0, max: 15, step: 1 },
    send_ff_replace_01: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary },
  },
  fes_channel_iec104_define: {
    rtu_addr: { min: 1, max: 65534, step: 1, default: 1 },
    ycbegin_addr: { min: 1, max: 16777215, step: 1, default: 16385 },
    yxbegin_addr: { min: 1, max: 16777215, step: 1, default: 1 },
    ykbegin_addr: { min: 1, max: 16777215, step: 1, default: 24577 },
    ytbegin_addr: { min: 1, max: 16777215, step: 1, default: 25089 },
    yx_data_type: {
      options: [1, 2],
      labels: { 1: "1 · 单点", 2: "2 · 双点" },
      default: 1,
    },
    yk_excute_type: {
      options: [1, 2],
      labels: { 1: "1 · 选择执行", 2: "2 · 直接执行" },
      default: 1,
    },
    yc_data_type: {
      options: [1, 2],
      labels: { 1: "1 · 浮点", 2: "2 · 整数" },
      default: 1,
    },
    yt_excute_type: {
      options: [1, 2],
      labels: { 1: "1 · 选择执行", 2: "2 · 直接执行" },
      default: 2,
    },
    call_alldata_time: { min: 60, step: 1, default: 60 },
    if_autosend_alldata: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary, default: 1 },
    remote_host: { required: true, default: "127.0.0.1" },
    remote_port: { min: 1, max: 65535, step: 1, default: 2404 },
  },
  fes_channel_iec104_point_define: {
    kind: {
      options: ["yc", "yx", "yk", "yt"],
      labels: { yc: "yc · 遥测", yx: "yx · 遥信", yk: "yk · 遥控", yt: "yt · 遥调" },
    },
    pnt_no: { min: 1, step: 1 },
    io_address: { min: 1, max: 16777215, step: 1, required: true },
    type_id: {
      options: [1, 2, 3, 5, 9, 11, 13, 21, 30, 31, 32, 34, 35, 36, 45, 46, 48, 49, 50, 58, 59, 61, 62, 63],
      labels: {
        1: "1 · 单点/浮点",
        2: "2 · 双点/标度",
        3: "3 · 短浮点",
        9: "9 · M_ME_NA_1 归一化",
        11: "11 · M_ME_NB_1 标度",
        13: "13 · M_ME_NC_1 短浮点",
        45: "45 · 单点命令",
        46: "46 · 双点命令",
        48: "48 · C_SE_NA_1 归一化设点",
        49: "49 · C_SE_NB_1 标度设点",
        50: "50 · C_SE_NC_1 短浮点设点",
      },
    },
    report_ms: { min: 0, step: 100 },
      time_tag: {options: ["NONE", "CP56Time2a"], labels: {NONE:"无时标", CP56Time2a:"CP56Time2a"}},
    command_mode: {
      options: ["DIRECT", "SELECT_AND_EXECUTE"],
      labels: { DIRECT: "直接执行", SELECT_AND_EXECUTE: "选择后执行" },
    },
    qualifier: { min: 0, max: 127, step: 1 },
    related_io_address: { min: 1, max: 16777215, step: 1 },
    related_io_autoreturn: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary },
  },
  fes_channel_bacnet_define: {
    bind_ip: { default: "0.0.0.0" },
    bind_port: { min: 1, max: 65535, step: 1, default: 47808 },
    dev_id: { min: 0, max: 4194303, step: 1, default: 0 },
    remote_host: { required: true, default: "127.0.0.1" },
    remote_port: { min: 1, max: 65535, step: 1, default: 47808 },
  },
  fes_channel_bacnet_point_define: {
    kind: {
      options: ["yc", "yx", "yk", "yt"],
      labels: { yc: "yc · 遥测", yx: "yx · 遥信", yk: "yk · 遥控", yt: "yt · 遥调" },
    },
    pnt_no: { min: 1, step: 1 },
    object_type: {
      options: [
        "analog-input", "analog-output", "analog-value",
        "binary-input", "binary-output", "binary-value",
        "multi-state-input", "multi-state-output", "multi-state-value",
        "accumulator",
      ],
      labels: {
        "analog-input": "analog-input · 模拟输入",
        "analog-output": "analog-output · 模拟输出",
        "analog-value": "analog-value · 模拟值",
        "binary-input": "binary-input · 二进制输入",
        "binary-output": "binary-output · 二进制输出",
        "binary-value": "binary-value · 二进制值",
        "multi-state-input": "multi-state-input · 多状态输入",
        "multi-state-output": "multi-state-output · 多状态输出",
        "multi-state-value": "multi-state-value · 多状态值",
        accumulator: "accumulator · 累加器",
      },
      required: true,
    },
    instance: { min: 0, max: 4194303, step: 1, required: true },
    property_identifier: { default: "presentValue" },
    quality_property: { default: "statusFlags" },
    priority: { min: 1, max: 16, step: 1 },
    array_index: { min: 0, max: 4294967295, step: 1 },
  },
  fes_channel_opc_define: {
    opc_interval: { min: 1, max: 3600000, step: 1, default: 1000 },
    opc_name: { required: true, default: "opc.tcp://127.0.0.1:4840" },
  },
  fes_channel_opc_point_define: {
    kind: {
      options: ["yc", "yx", "yk", "yt"],
      labels: { yc: "yc · 遥测", yx: "yx · 遥信", yk: "yk · 遥控", yt: "yt · 遥调" },
    },
    pnt_no: { min: 1, step: 1 },
    node_id: { required: true },
    data_type: {
      options: ["bool", "uint", "int", "float", "str"],
      labels: {
        bool: "bool · 布尔",
        uint: "uint · 无符号整数",
        int: "int · 有符号整数",
        float: "float · 浮点数",
        str: "str · 字符串",
      },
    },
    value_attribute: { default: "Value" },
    quality_attribute: {
      options: ["StatusCode"],
      labels: { StatusCode: "StatusCode · 质量码" },
      default: "StatusCode",
    },
    deadband: { min: 0, step: "any", default: 0 },
  },
  fes_channel_can_define: {
    interface: { required: true, default: "virtual" },
    bitrate: { min: 1, max: 10000000, step: 1, default: 500000 },
    data_bitrate: { min: 1, max: 10000000, step: 1, default: 2000000 },
    fd: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary, default: 0 },
    receive_own_messages: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary, default: 0 },
    receive_timeout: { min: 0, step: "any", default: 1 },
    retry_count: { min: 0, max: 10, step: 1, default: 0 },
    is_extended_id: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary, default: 0 },
  },
  fes_channel_can_point_define: {
    kind: {
      options: ["yc", "yx", "yk", "yt"],
      labels: { yc: "yc · 遥测", yx: "yx · 遥信", yk: "yk · 遥控", yt: "yt · 遥调" },
    },
    pnt_no: { min: 1, step: 1 },
    frame_id: { min: 0, max: 536870911, step: 1 },
    tx_frame_id: { min: 0, max: 536870911, step: 1 },
    response_id: { min: 0, max: 536870911, step: 1 },
    is_extended_id: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary, default: 0 },
    is_fd: { options: [0, 1], labels: MODBUS_ENUM_LABELS.binary, default: 0 },
    byte_offset: { min: 0, max: 63, step: 1 },
    bit_offset: { min: 0, max: 7, step: 1 },
    bit_length: { min: 1, max: 64, step: 1 },
    value_type: {
      options: ["bool", "uint", "int", "float"],
      labels: { bool: "bool · 布尔", uint: "uint · 无符号整数", int: "int · 有符号整数", float: "float · 浮点数" },
    },
    byte_order: {
      options: ["big", "little"],
      labels: { big: "big · 大端", little: "little · 小端" },
      default: "big",
    },
    scale: { step: "any", default: 1 },
    offset: { step: "any", default: 0 },
    quality_byte_offset: { min: 0, max: 63, step: 1 },
    quality_mask: { min: 0, max: 255, step: 1 },
  },
};

function protocolFieldRule(schema, column) {
  const table = schema?.head?.eng_name;
  const base = table && PROTOCOL_RULES[table]?.[column.eng_name];
  if (base) return base;
  if (Array.isArray(column.allowed_values)) {
    return { options: column.allowed_values };
  }
  return {};
}

function protocolDisplayLabel(table, name, value) {
  if (value === null || value === undefined) return null;
  if (!table || !PROTOCOL_RULES[table]) return null;
  const labels = PROTOCOL_RULES[table]?.[name]?.labels ||
    (name === "parity" ? MODBUS_ENUM_LABELS.parity : null) ||
    (name === "icode" ? MODBUS_ENUM_LABELS.icode : null) ||
    (name === "trans_type" ? MODBUS_ENUM_LABELS.trans_type : null) ||
    (name === "ytyk_type" ? MODBUS_ENUM_LABELS.ytyk_type : null) ||
    (name === "twobytedata_byteseq" ? MODBUS_ENUM_LABELS.twobytedata_byteseq : null) ||
    (name === "fourbytedata_byteseq" ? MODBUS_ENUM_LABELS.fourbytedata_byteseq : null);
  return labels?.[String(value)] ?? null;
}

async function api(path, options = {}) {
  const requestOptions = { ...options };
  if (requestOptions.body && typeof requestOptions.body === "object")
    requestOptions.body = JSON.stringify(requestOptions.body);
  const response = await fetch(path, {
    ...requestOptions,
    headers: { "Content-Type": "application/json", ...(requestOptions.headers || {}) },
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
  } else if (ctx.resource === "channels") {
    url = "/api/channels";
  } else {
    url =
      "/api/tables/" + encodeURIComponent(ctx.schema.head.eng_name) + "/rows";
  }
  if (row) {
    if (ctx.resource === "channels") {
      url += "/" + encodeURIComponent(row.channel_type) + "/" + encodeURIComponent(row.channel_id);
    } else {
      const key = pointKinds.includes(ctx.resource) && ctx.rtu
        ? String(row.pnt_no)
        : keyOf(row, ctx.schema);
      url += "/" + encodeURIComponent(key);
    }
  }
  return url;
}

async function refreshSummary() {
  const data = await api("/api/overview?view=summary");
  const names = [
    ["fes_rtu_define", "RTU"],
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
let rtuOptionsRequest = 0, pendingRtuOptions = null;
function loadRtuOptions({fresh = false} = {}) {
  if (pendingRtuOptions) return fresh ? pendingRtuOptions.catch(() => {}).then(() => loadRtuOptions()) : pendingRtuOptions;
  pendingRtuOptions = fetchRtuOptions().finally(() => { pendingRtuOptions = null; });
  return pendingRtuOptions;
}
async function fetchRtuOptions() {
  const request = ++rtuOptionsRequest;
  const items = new Map();
  for (let offset = 0; ; ) {
    const data = await api("/api/rtus?limit=500&sort=rtu_id&fields=rtu_id,name&offset=" + offset);
    if (request !== rtuOptionsRequest) return;
    for (const row of data.items) items.set(row.rtu_id, row);
    offset += data.items.length;
    if (!data.items.length || offset >= data.total) break;
  }
  const rows = Array.from(items.values());
  const options = rows
    .map(
      (row) =>
        '<option value="' +
        esc(row.rtu_id) +
        '">' +
        esc(row.rtu_id + " · " + (row.name || "未命名")) +
        "</option>",
    )
    .join("");
  // The filter is a real select so opening it always exposes the complete
  // RTU list.  Keep the datalist as a suggestion source for editor/debug
  // inputs that still accept a numeric RTU value.
  const filter = $("#rtu-filter");
  const markup = '<option value="">全部 RTU</option>' + options;
  if (filter._rtuMarkup !== markup) { filter.innerHTML = markup; filter._rtuMarkup = markup; }
  filter.value = rows.some((row) => String(row.rtu_id) === state.rtu)
    ? state.rtu
    : "";
  const suggestions = rows
    .map(
      (row) =>
        '<option value="' +
        esc(row.rtu_id) +
        '" label="' +
        esc(row.name || "未命名") +
        '"></option>',
    )
    .join("");
  const datalist = $("#rtu-options");
  if (datalist._rtuMarkup !== suggestions) { datalist.innerHTML = suggestions; datalist._rtuMarkup = suggestions; }
}
let channelOptionsRequest = 0;
async function fetchChannelOptions(channelType = "", isCurrent = () => true) {
  const items = [];
  for (let offset = 0; ; ) {
    const filter = channelType
      ? "&channel_type=" + encodeURIComponent(channelType)
      : "";
    const data = await api(
      "/api/channels?limit=500&sort=channel_id&fields=channel_id,channel_type,name&offset=" +
        offset +
        filter,
    );
    if (!isCurrent()) return null;
    items.push(...data.items);
    offset += data.items.length;
    if (!data.items.length || offset >= data.total) break;
  }
  return items;
}
function rtuEditorColumns(columns) {
  const order = new Map(RTU_EDITOR_ORDER.map((name, index) => [name, index]));
  return [...columns].sort(
    (left, right) =>
      (order.get(left.eng_name) ?? RTU_EDITOR_ORDER.length) -
      (order.get(right.eng_name) ?? RTU_EDITOR_ORDER.length),
  );
}
function channelOptionLabel(row) {
  return row.channel_id + " · " + (row.name || "未命名");
}
function channelSelectMarkup(rows, selected, channelType) {
  const options = rows
    .map(
      (row) =>
        '<option value="' +
        esc(row.channel_id) +
        '"' +
        (String(selected ?? "") === String(row.channel_id) ? " selected" : "") +
        ">" +
        esc(channelOptionLabel(row)) +
        "</option>",
    )
    .join("");
  const prompt = channelType
    ? rows.length
      ? "请选择对应通道"
      : "该类型暂无可用通道"
    : "请先选择通道类型";
  return '<option value="">' + esc(prompt) + "</option>" + options;
}
async function updateRtuChannelOptions(channelType, selected = "") {
  const select = $("#f-channel_id");
  if (!select) return;
  const request = ++channelOptionsRequest;
  select.disabled = true;
  select.innerHTML = channelSelectMarkup([], selected, "");
  if (!channelType) return;
  if (!CHANNEL_TYPE_OPTIONS.includes(channelType)) {
    select.innerHTML = channelSelectMarkup(
      selected ? [{ channel_id: selected, name: "当前关联通道" }] : [],
      selected,
      channelType,
    );
    select.disabled = false;
    return;
  }
  try {
    const rows = (await fetchChannelOptions(channelType, () => request === channelOptionsRequest)) || [];
    if (request !== channelOptionsRequest) return;
    select.innerHTML = channelSelectMarkup(rows, selected, channelType);
    select.disabled = false;
  } catch (error) {
    if (request !== channelOptionsRequest) return;
    select.innerHTML = '<option value="">加载通道失败</option>';
    toast(error.message, true);
  }
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
  $("#add").textContent =
    state.resource === "channels"
      ? "＋ 新建通道"
      : state.resource === "rtus"
        ? "＋ 新建 RTU"
        : "＋ 新增记录";
  for (const view of ["common", "live", "all"]) {
    $("#view-" + view).classList.toggle("selected", state.view === view);
  }
}
let displayedTableScope = null;
function guardTableScope(ctx) {
  const stale = displayedTableScope !== JSON.stringify([ctx.resource, ctx.rtu]);
  const body = $("#table-body");
  body.hidden = stale;
  body.inert = stale;
}
let pendingRows = null;
function loadRows({fresh = false} = {}) {
  if (!state.schemas.size || ["processes", "scdfe", "logs"].includes(state.resource)) return Promise.resolve();
  const ctx = context();
  const params = new URLSearchParams({
    limit: String(state.limit),
    offset: String(state.offset),
    q: $("#search").value,
  });
  if (state.sort) {
    params.set("sort", state.sort);
    params.set("order", state.order);
  }
  // Ask the list API only for columns that the current view renders.  The
  // server always adds primary keys and _version, so row actions and
  // optimistic concurrency keep working while wide auxiliary tables avoid
  // shipping hidden fields on every refresh.
  params.set("fields", visibleColumns().map((column) => column.eng_name).join(","));
  if (ctx.rtu && !pointKinds.includes(ctx.resource))
    params.set("rtu_id", ctx.rtu);
  const key = pathFor(ctx) + "?" + params;
  if (pendingRows?.key === key && pendingRows.request === state.request) {
    return fresh ? pendingRows.promise.catch(() => {}).then(() => loadRows()) : pendingRows.promise;
  }
  const pending = {key};
  pending.promise = performLoadRows(ctx, key).finally(() => { if (pendingRows === pending) pendingRows = null; });
  pending.request = state.request;
  pendingRows = pending;
  return pending.promise;
}
async function performLoadRows(ctx, path) {
  const sequence = ++state.request;
  state.loading = true;
  guardTableScope(ctx);
  showView();
  $("#table-error").hidden = true;
  $("#add").disabled = true;
  try {
    const data = await api(path);
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
    if (sequence === state.request) {
      state.loading = false;
      displayedTableScope = JSON.stringify([ctx.resource, ctx.rtu]);
      guardTableScope(ctx);
    }
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
// Only keep markup for the currently visible page. Events read fresh state.items.
// Row values are cached separately so a stable response does not rebuild all
// visible HTML before reconcileBody compares it. The cache is invalidated by
// visible-column changes and is never used as an API data cache.
const tableRender = { head: null, rows: [], entries: [], columnsKey: null };
function reconcileBody(body, rows, cache, patchCells = false) {
  const changed = [];
  if (rows.length === cache.rows.length) {
    rows.forEach((markup, index) => {
      if (markup !== cache.rows[index]) changed.push(index);
    });
  }
  let cells = null;
  if (patchCells && changed.length && rows.length === cache.rows.length) {
    // Monitoring rows have fixed <tr> markup and escaped cell text. Many rows
    // may change only their heartbeat; avoid rebuilding all their controls.
    const updates = [];
    let count = 0;
    for (const index of changed) {
      const previous = cache.rows[index].match(/<td\b[\s\S]*?<\/td>/g) || [];
      const current = rows[index].match(/<td\b[\s\S]*?<\/td>/g) || [];
      if (!current.length || current.length !== previous.length) break;
      count += current.length;
      current.forEach((markup, column) => {
        if (markup !== previous[column]) updates.push([index, column, markup]);
      });
      if (index === changed[changed.length - 1] && updates.length <= count / 4)
        cells = updates;
    }
  }
  if (cells) {
    for (const [row, column, markup] of cells) body.rows[row].cells[column].outerHTML = markup;
  } else if (rows.length !== cache.rows.length || changed.length > rows.length / 4) {
    body.innerHTML = rows.join("");
  } else {
    // Keep focus and selection on unaffected rows; parse dense updates once.
    for (const index of changed) body.children[index].outerHTML = rows[index];
  }
  cache.rows = rows;
}
function renderTable() {
  $("#table-body").closest(".data-table").classList.toggle("rtu-table", state.resource === "rtus");
  const columns = visibleColumns();
  const schema = schemaFor();
  const columnsKey = state.resource + "\u001f" + columns.map((column) => column.eng_name).join("\u001f");
  if (columnsKey !== tableRender.columnsKey) {
    tableRender.columnsKey = columnsKey;
    tableRender.entries = [];
  }
  const head =
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
          "</small></button></th>"
      )
      .join("") +
    '<th class="sticky-actions">操作</th></tr>';
  if (head !== tableRender.head) {
    $("#table-head").innerHTML = head;
    tableRender.head = head;
  }
  const stableKeys = state.items.length === tableRender.entries.length && state.items.every(
    (item, index) => tableRender.entries[index]?.key === keyOf(item, schema));
  const rows = state.items.length
    ? state.items.map((item, index) => {
        const key = keyOf(item, schema);
        const cached = tableRender.entries[index];
        let reusable = cached && cached.key === key;
        if (reusable) {
          for (let column = 0; column < columns.length; column++) {
            if (cached.values[column] !== item[columns[column].eng_name]) {
              reusable = false;
              break;
            }
          }
        }
        if (reusable) return cached.markup;
        const values = columns.map((column) => item[column.eng_name]);
        const markup =
          '<tr data-row="' +
          index +
          '">' +
          columns
            .map(
              (column, columnIndex) =>
                '<td class="' +
                (column.primary_key ? "key" : "") +
                '" title="' +
                esc(values[columnIndex] ?? "—") +
                '">' +
                cellValue(column.eng_name, values[columnIndex], schema.head.eng_name) +
                "</td>"
            )
            .join("") +
          '<td class="actions-cell sticky-actions">' +
          (["yk", "yt"].includes(state.resource)
            ? commandMarkup(item) + '<button class="row-button" data-command="' + index + '">下发</button>'
            : "") +
          '<button class="row-button" data-detail="' +
          index +
          '">详情</button><button class="row-button" data-edit="' +
          index +
          '">编辑</button>' +
          (state.resource === "rtus"
            ? '<button class="row-button" data-rtu-params="' +
              index +
              '">RTU参数</button><button class="row-button" data-channel-view="' +
              index +
              '">查看通道</button><button class="row-button" data-points="' +
              index +
              '">四遥</button><button class="row-button" data-rtu-log="' +
              esc(item.rtu_id) +
              '">日志</button>'
            : state.resource === "channels"
              ? '<button class="row-button" data-channel-params="' +
                index +
                '">协议参数</button>'
            : "") +
          '<button class="row-button delete" data-delete="' +
          index +
          '">删除</button></td></tr>';
        tableRender.entries[index] = { key, values, markup };
        return markup;
      })
    : ['<tr><td class="empty" colspan="' +
      (columns.length + 1) +
      '">没有匹配的记录</td></tr>'];
  reconcileBody($("#table-body"), rows, tableRender, stableKeys);
  tableRender.entries.length = state.items.length;
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

$("#table-head").onclick = (event) => {
  const button = event.target.closest("[data-sort]");
  if (!button) return;
  state.order = state.sort === button.dataset.sort && state.order === "asc" ? "desc" : "asc";
  state.sort = button.dataset.sort;
  state.offset = 0;
  loadRows();
};
const rowActions = {
  command: openCommand,
  detail: openDetails,
  edit: openEditor,
  delete: removeRow,
  "rtu-params": openRtuParameters,
  channel: openChannelEditor,
  "channel-params": openChannelParameters,
};

function commandState(row) {
  if (!Number(row.cmd_time)) return ["idle", "未下发"];
  if (Number(row.cmd_time) > Number(row.cmd_handle_time || 0)) return ["pending", "待下发"];
  if (row.status === 3) return ["failed", "下发失败"];
  return row.status === 1 ? ["sent", "已下发"] : ["idle", "已处理"];
}
function commandMarkup(row) {
  const [status, label] = commandState(row);
  return '<span class="command-state ' + status + '">' + label + '</span>';
}
async function openCommand(row) {
  const kind = state.resource, originRtu = state.rtu;
  if (!["yk", "yt"].includes(kind)) return;
  try {
    const path = "/api/rtus/" + encodeURIComponent(row.rtu_id) + "/points/" + kind + "/" + encodeURIComponent(row.pnt_no);
    const current = (await api(path)).item;
    if (state.resource !== kind || state.rtu !== originRtu) return;
    state.command = { kind, row: current, busy: false };
    $("#command-title").textContent = kind === "yk" ? "遥控下发" : "遥调下发";
    $("#command-point").textContent = "RTU " + current.rtu_id + " · 点 " + current.pnt_no + " · " + (current.name || "未命名");
    $("#command-current").textContent = "当前指令值：" + (current.value ?? "—") + " · " + commandState(current)[1];
    $("#command-value-control").innerHTML = kind === "yk"
      ? '<select id="command-value" required><option value="0">0 · ' + esc(current.name_0 || "分/关") + '</option><option value="1">1 · ' + esc(current.name_1 || "合/开") + '</option></select>'
      : '<input id="command-value" type="number" step="any" required aria-label="遥调设定值" />';
    const input = $("#command-value");
    input.value = current.value ?? (kind === "yk" ? 0 : "");
    if (kind === "yt") {
      if (current.eng_min != null) input.min = current.eng_min;
      if (current.eng_max != null) input.max = current.eng_max;
    }
    $("#command-range").textContent = kind === "yt"
      ? "工程范围：" + (current.eng_min ?? "不限") + " ～ " + (current.eng_max ?? "不限") : "";
    const blocked = current.man_disable || current.auto_disable;
    const pending = commandState(current)[0] === "pending";
    $("#command-error").textContent = blocked ? "测点已闭锁，禁止下发" : pending ? "该测点还有待处理指令" : "";
    $("#send-command").disabled = Boolean(blocked || pending);
    $("#command-dialog").showModal();
    input.focus();
  } catch (error) { toast(error.message, true); }
}
function closeCommand() {
  if (!state.command?.busy) $("#command-dialog").close();
}
$("#close-command").onclick = $("#cancel-command").onclick = closeCommand;
$("#command-dialog").addEventListener("cancel", (event) => {
  if (state.command?.busy) event.preventDefault();
});
$("#command-form").onsubmit = async (event) => {
  event.preventDefault();
  const command = state.command;
  if (!command || command.busy || $("#send-command").disabled) return;
  if (!$("#command-form").reportValidity()) return;
  command.busy = true;
  $("#send-command").disabled = true;
  $("#command-error").textContent = "";
  try {
    const value = Number($("#command-value").value);
    if (!Number.isFinite(value)) throw new Error("下发值必须是有限数字");
    await api("/api/rtus/" + encodeURIComponent(command.row.rtu_id) + "/commands/" + command.kind + "/" + encodeURIComponent(command.row.pnt_no), {
      method: "POST", headers: { "If-Match": String(command.row._version) }, body: JSON.stringify({ value }),
    });
    $("#command-dialog").close();
    toast("指令已提交，等待通信线程下发");
    await loadRows({fresh:true});
  } catch (error) {
    $("#command-error").textContent = error.message;
  } finally {
    command.busy = false;
    $("#send-command").disabled = false;
  }
};
$("#table-body").onclick = (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.hasAttribute("data-rtu-log")) return openRtuLogs(button.dataset.rtuLog);
  if (button.hasAttribute("data-points")) {
    state.rtu = String(state.items[Number(button.dataset.points)].rtu_id);
    $("#rtu-filter").value = state.rtu;
    state.view = "live";
    return changeResource("yc");
  }
  if (button.hasAttribute("data-channel-view")) {
    const row = state.items[Number(button.dataset.channelView)];
    state.rtu = row?.rtu_id ? String(row.rtu_id) : "";
    $("#rtu-filter").value = state.rtu;
    state.offset = 0;
    return changeResource("channels");
  }
  for (const [action, handler] of Object.entries(rowActions)) {
    if (button.hasAttribute("data-" + action))
      return handler(state.items[Number(button.getAttribute("data-" + action))]);
  }
};
$("#table-body").ondblclick = (event) => {
  const row = event.target.closest("[data-row]");
  if (row && !event.target.closest("button")) openDetails(state.items[Number(row.dataset.row)]);
};

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
    if (base.resource !== state.resource || base.rtu !== context().rtu) return;
    const columns =
      ctx.resource === "rtus" ? rtuEditorColumns(ctx.columns) : ctx.columns;
    state.editing = { ...ctx, columns, row };
    const initialChannelType = String(row?.channel_type || "");
    const initialChannelOptions =
      ctx.resource === "rtus" && CHANNEL_TYPE_OPTIONS.includes(initialChannelType)
        ? (await fetchChannelOptions(initialChannelType)) || []
        : [];
    if (
      ctx.resource === "rtus" &&
      row?.channel_id != null &&
      !initialChannelOptions.some(
        (item) => String(item.channel_id) === String(row.channel_id),
      )
    ) {
      initialChannelOptions.unshift({
        channel_id: row.channel_id,
        name: "当前关联通道",
      });
    }
    $("#form-title").textContent =
      (row ? "编辑 · " : "新增 · ") + (options.title || ctx.schema.head.chn_name);
    $("#form-error").textContent = "";
    $("#form-fields").innerHTML = columns
      .map((column) => {
        const name = column.eng_name;
        const isRtuChannelId = ctx.resource === "rtus" && name === "channel_id";
        const scoped = name === "rtu_id" && !!ctx.rtu;
        const disabled =
          (!!row && column.primary_key) ||
          options.autoPrimaryKeys?.includes(name) ||
          (isRtuChannelId && !initialChannelType);
        const required =
          (!options.autoPrimaryKeys?.includes(name) &&
            (column.primary_key || column.required === true || name === "rtu_id"));
        const rule = protocolFieldRule(ctx.schema, column);
        const value = row
          ? row[name] ?? ""
          : scoped
            ? ctx.rtu
            : Object.hasOwn(column, "default") && column.default !== null
              ? column.default
              : rule.default !== undefined
                ? rule.default
                : "";
        const isRequired = required || rule.required === true;
        const attrs =
          (disabled ? " disabled" : "") +
          (scoped ? " readonly" : "") +
          (isRequired ? " required" : "");
        const list = name === "rtu_id" && ctx.resource !== "rtus" ? ' list="rtu-options"' : "";
        const numeric = column.data_type !== "str";
        // Keep unconstrained int64 fields as text so their exact decimal
        // representation survives editing; bounded protocol integers can use
        // native number validation in the browser.
        const type =
          column.data_type === "float" || rule.min !== undefined || rule.max !== undefined
            ? "number"
            : "text";
        const allowedOptions = Array.isArray(options.fieldOptions?.[name])
          ? options.fieldOptions[name]
          : Array.isArray(rule.options)
          ? rule.options
          : Array.isArray(column.allowed_values)
            ? column.allowed_values
            : null;
        const labels = rule.labels || {};
        const step = rule.step ?? (column.data_type === "int" ? 1 : "any");
        const bounds =
          (rule.min === undefined ? "" : ' min="' + esc(rule.min) + '"') +
          (rule.max === undefined ? "" : ' max="' + esc(rule.max) + '"');
        const channelOptions = isRtuChannelId
          ? channelSelectMarkup(initialChannelOptions, value, initialChannelType)
          : "";
        const channelTypePrompt =
          name === "channel_type" &&
          ctx.resource === "rtus" &&
          String(value) === ""
            ? '<option value="" selected>请选择通道类型</option>'
            : "";
        const control = isRtuChannelId || allowedOptions
          ? '<select id="f-' +
            esc(name) +
            '" data-field="' +
            esc(name) +
            '"' +
            attrs +
            ">" +
            (isRtuChannelId
              ? channelOptions
              : channelTypePrompt +
                allowedOptions
                  .map(
                    (option) =>
                      '<option value="' +
                      esc(option) +
                      '"' +
                      (String(value) === String(option) ? " selected" : "") +
                      ">" +
                      esc(
                        name === "channel_type"
                          ? channelTypeLabel(option)
                          : labels[String(option)] ||
                            protocolDisplayLabel(ctx.schema.head.eng_name, name, option) ||
                            option,
                      ) +
                      "</option>",
                  )
                  .join("")) +
            "</select>"
          : '<input id="f-' +
            esc(name) +
            '" data-field="' +
            esc(name) +
            '" type="' +
            type +
            '" step="' +
            esc(step) +
            '"' +
            bounds +
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
          (isRequired ? " *" : "") +
          "<small>" +
          esc(name) +
          (column.primary_key ? " · 主键" : "") +
          (schemaTypeLabel(column) ? " · " + esc(schemaTypeLabel(column)) : "") +
          "</small></label>" +
          control +
          "</div>"
        );
      })
      .join("");
    const channelTypeControl = $("#f-channel_type");
    if (ctx.resource === "rtus" && channelTypeControl && $("#f-channel_id")) {
      channelTypeControl.onchange = () =>
        updateRtuChannelOptions(channelTypeControl.value, "");
    }
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
  const columns = rtuEditorColumns(
    schema.columns.filter((column) => !rtuRuntimeFields.includes(column.eng_name)),
  );
  await openEditor(null, {
    schema,
    columns,
    title: "RTU 定义",
    mode: "new-rtu",
    fieldOptions: {
      channel_type: CHANNEL_TYPE_OPTIONS,
    },
  });
}

async function openNewChannel() {
  const schema = state.schemas.get("channels");
  if (!schema) return toast("通道目录尚未加载", true);
  const columns = schema.columns.filter((column) =>
    ["channel_id", "channel_type", "name"].includes(column.eng_name),
  );
  await openEditor(null, {
    schema,
    columns,
    title: "通道定义",
    mode: "new-channel-definition",
  });
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
    const isDirectory = state.resource === "channels";
    const rtu = row.rtu_id == null ? "" : String(row.rtu_id);
    const endpoint = isDirectory
      ? "/api/channels/" + encodeURIComponent(row.channel_type) + "/" +
        encodeURIComponent(row.channel_id) + "/parameters"
      : "/api/rtus/" + encodeURIComponent(rtu) + "/channel/parameters";
    const info = await api(endpoint);
    if (!info.parameter_schema) return toast("该通道类型没有可维护的协议参数表", true);
    const schema = info.parameter_schema;
    state.schemas.set(schema.head.eng_name, schema);
    const columns = schema.columns.filter((column) => column.eng_name !== "id");
    await openEditor(info.item, {
      schema,
      columns,
      title: channelTypeLabel(row.channel_type) + " 通道参数",
      mode: "protocol-parameters",
      rtu,
      pathOverride: endpoint,
      fetchPath: endpoint,
    });
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

function validateProtocolValue(schema, values) {
  const table = schema?.head?.eng_name;
  const rules = PROTOCOL_RULES[table];
  if (!rules) return;
  const present = (name) =>
    values[name] !== undefined && values[name] !== null && values[name] !== "";
  const numberOf = (name) => {
    const value = Number(values[name]);
    if (!Number.isFinite(value))
      throw new Error(
        (schema.columns.find((column) => column.eng_name === name)?.chn_name || name) +
          " 必须是有限数字",
      );
    return value;
  };
  const columnName = (name) =>
    schema.columns.find((column) => column.eng_name === name)?.chn_name || name;
  const requireField = (name, message) => {
    if (!present(name)) throw new Error(message || columnName(name) + " 不能为空");
  };
  for (const [name, rule] of Object.entries(rules)) {
    if (!present(name)) continue;
    if (
      rule.options &&
      !rule.options.some((option) => String(option) === String(values[name]))
    ) {
      throw new Error(
        columnName(name) + " 的值不在允许范围内",
      );
    }
    if (rule.min !== undefined && numberOf(name) < rule.min)
      throw new Error(columnName(name) + " 不能小于 " + rule.min);
    if (rule.max !== undefined && numberOf(name) > rule.max)
      throw new Error(columnName(name) + " 不能大于 " + rule.max);
  }
  if (
    table === "fes_channel_modbus_define" ||
    table === "fes_channel_modbus_define_call"
  ) {
    if (
      present("icode") &&
      present("inum") &&
      Number(values.icode) >= 3 &&
      Number(values.inum) > 125
    )
      throw new Error("寄存器召唤的寄存器个数不能超过 125");
  }
  if (
    table === "fes_channel_modbus_define_data" &&
    present("trans_type") &&
    Number(values.trans_type) === 9
  ) {
    if (!present("offset_bit") || !present("all_bit"))
      throw new Error("多位数据必须提供偏移位数和占用总位数");
    if (Number(values.offset_bit) + Number(values.all_bit) > 16)
      throw new Error("偏移位数与占用总位数之和不能超过 16");
  }
  if (table === "fes_channel_iec104_define") {
    requireField("remote_host");
  } else if (table === "fes_channel_iec104_point_define") {
    requireField("io_address");
    const kind = String(values.kind || "");
    const allowed = {
        yx: [1, 2, 3, 5, 30, 31, 32],
        yc: [1, 2, 3, 9, 11, 13, 21, 34, 35, 36],
        yk: [1, 2, 45, 46, 58, 59],
        yt: [1, 2, 3, 48, 49, 50, 61, 62, 63],
    }[kind];
    if (present("type_id") && allowed && !allowed.includes(Number(values.type_id)))
      throw new Error("IEC104 类型标识与点类型不匹配");
  } else if (table === "fes_channel_bacnet_define") {
    requireField("remote_host");
  } else if (table === "fes_channel_bacnet_point_define") {
    requireField("object_type");
    requireField("instance");
    const kind = String(values.kind || "");
    const allowed = {
      yc: ["analog-input", "analog-output", "analog-value", "multi-state-input", "multi-state-output", "multi-state-value", "accumulator"],
      yx: ["binary-input", "binary-output", "binary-value"],
      yt: ["analog-output", "analog-value", "multi-state-output", "multi-state-value"],
      yk: ["binary-output", "binary-value"],
    }[kind];
    if (allowed && present("object_type") && !allowed.includes(String(values.object_type)))
      throw new Error("BACnet 对象类型与点类型不匹配");
  } else if (table === "fes_channel_opc_define") {
    requireField("opc_name");
    if (
      present("opc_name") &&
      !["opc.tcp://", "opc.https://", "https://"].some((prefix) =>
        String(values.opc_name).trim().toLowerCase().startsWith(prefix),
      )
    )
      throw new Error("OPC UA 实例名必须是 opc.tcp://、opc.https:// 或 https:// 地址");
  } else if (table === "fes_channel_opc_point_define") {
    requireField("node_id");
    if (
      present("node_id") &&
      !/^(ns=|nsu=|i=|s=|g=|b=)/i.test(String(values.node_id).trim())
    )
      throw new Error("OPC UA NodeId 必须以 ns=、nsu=、i=、s=、g= 或 b= 开头");
  } else if (table === "fes_channel_can_define") {
    requireField("interface");
  } else if (table === "fes_channel_can_point_define") {
    const kind = String(values.kind || "");
    if (["yc", "yx"].includes(kind)) requireField("frame_id");
    if (["yk", "yt"].includes(kind)) requireField("tx_frame_id");
    const extended = Number(values.is_extended_id || 0) === 1;
    const idLimit = extended ? 0x1fffffff : 0x7ff;
    for (const name of ["frame_id", "tx_frame_id", "response_id"]) {
      if (present(name) && numberOf(name) > idLimit)
        throw new Error(columnName(name) + " 超出当前 CAN 帧格式的 ID 范围");
    }
    if (present("value_type") && String(values.value_type) === "float") {
      if (Number(values.bit_offset || 0) !== 0 || ![32, 64].includes(Number(values.bit_length)))
        throw new Error("CAN 浮点类型必须使用位偏移 0 且位长度为 32 或 64");
    }
    if (present("scale") && numberOf("scale") === 0)
      throw new Error("CAN 乘系数不能为 0");
    if (present("byte_offset") && present("bit_offset") && present("bit_length")) {
      const bytes = Number(values.is_fd || 0) === 1 ? 64 : 8;
      const needed = Math.ceil((Number(values.bit_offset) + Number(values.bit_length)) / 8);
      if (Number(values.byte_offset) + needed > bytes)
        throw new Error("CAN 点位域超出当前帧的数据长度");
    }
    for (const name of ["tx_data_hex", "response_data_hex"]) {
      if (!present(name)) continue;
      const compact = String(values[name]).replace(/\s+/g, "");
      const maxBytes = Number(values.is_fd || 0) === 1 ? 64 : 8;
      if (!/^[0-9a-f]*$/i.test(compact) || compact.length % 2 !== 0 || compact.length / 2 > maxBytes)
        throw new Error(columnName(name) + " 必须是有效的偶数位十六进制字符串");
    }
  }
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
    const values = { ...(ctx.row || {}), ...body };
    validateProtocolValue(ctx.schema, values);
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
    await loadRows({fresh:true});
    await Promise.all([refreshSummary(), loadRtuOptions({fresh:true})]);
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
  try {
    let message = "确认删除 " + label + "？";
    if (state.resource === "rtus") {
      const preview = await api(
        "/api/rtus/" + encodeURIComponent(row.rtu_id) + "/delete-preview",
      );
      const references = preview.references
        .map((item) => "· " + item.name + "：" + item.count + " 条")
        .join("\n");
      message =
        "即将删除 RTU " +
        row.rtu_id +
        (row.name ? "（" + row.name + "）" : "") +
        "\n\n以下关联数据将同步删除：\n" +
        references +
        "\n\n合计：" +
        preview.total +
        " 条\n\n该操作不可恢复，确认继续？";
    }
    if (!confirm(message)) return;
    const result = await api(pathFor(ctx, row), {
      method: "DELETE",
      headers: { "If-Match": String(row._version) },
    });
    toast(
      state.resource === "rtus"
        ? "RTU及关联数据已删除，共 " + result.total_deleted + " 条"
        : "记录已删除",
    );
    if (state.resource === "rtus" && String(state.rtu) === String(row.rtu_id)) {
      state.rtu = "";
    }
    await loadRows({fresh:true});
    await Promise.all([refreshSummary(), loadRtuOptions({fresh:true})]);
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
const processRender = { rows: [] };
const threadRender = { rows: [] };
const logs = {
  record: "runtime",
  recordingEnabled: null,
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
  resultKey: null,
  searchTimer: null,
};
const logContentRender = { rows: [] };
// Snapshot just the visible page, never the full log index or API responses.
const logFilesRender = { files: [], selected: null };
const logTreeClosed = new Set();
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
function runtimeBadge(runtime, fallback) {
  const names = {unknown: "未知", stopped: "停止", starting: "启动中", running: "运行中", stopping: "停止中", error: "错误", degraded: "降级"};
  if (!runtime || !runtime.state) return statusBadge(fallback);
  const state = String(runtime.state);
  const numeric = {running: 2, starting: 1, stopping: 3, degraded: 4, error: 5, stopped: 0}[state];
  return '<span class="status s' + esc(numeric ?? 0) + '">' + esc(names[state] || state) + '</span>';
}
function cellValue(name, value, table = "") {
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
  if (name === "packet_recording" || name === "bound" || name === "parameter_ready")
    return '<span class="status ' + (Number(value) === 1 ? "enabled" : "") + '">' +
      (name === "packet_recording"
        ? Number(value) === 1 ? "保存" : "不保存"
        : Number(value) === 1 ? "是" : "否") +
      "</span>";
  if (name === "channel_type") return esc(channelTypeLabel(value));
  const protocolLabel = protocolDisplayLabel(table, name, value);
  if (protocolLabel !== null) return esc(protocolLabel);
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
function saveListState(resource = state.resource) {
  if (!resources[resource] && !pointKinds.includes(resource)) return;
  state.listStates[resource] = {
    offset: state.offset,
    limit: state.limit,
    sort: state.sort,
    order: state.order,
    view: state.view,
    search: $("#search").value,
  };
}
function restoreListState(resource) {
  const saved = state.listStates[resource] || {
    offset: 0,
    limit: 50,
    sort: "",
    order: "asc",
    view: "common",
    search: "",
  };
  state.offset = saved.offset;
  state.limit = saved.limit;
  state.sort = saved.sort;
  state.order = saved.order;
  state.view = saved.view;
  $("#search").value = saved.search;
  $("#page-size").value = String(state.limit);
  $("#rtu-filter").value = state.rtu;
}
function changeResource(resource, updateHash = true) {
  saveListState();
  if (resource === "data") resource = "rtus";
  if (
    !["processes", "scdfe", "logs", ...Object.keys(resources)].includes(resource) &&
    !state.schemas.has(resource)
  )
    resource = "rtus";
  state.resource = resource;
  restoreListState(resource);
  tableRender.head = null;
  tableRender.rows = [];
  tableRender.entries = [];
  tableRender.columnsKey = null;
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
  if (resource === "processes") {
    loadCommandServerConfig();
    return loadProcesses();
  }
  if (resource === "scdfe") return loadScdfe();
  if (resource === "logs") return loadScdfeLogs();
  if (!pointKinds.includes(resource) && state.view === "live")
    state.view = "common";
  loadRows();
}

async function openDetails(row, resource = state.resource) {
  const origin = state.resource, originRtu = state.rtu;
  try {
    const schema = schemaFor(resource);
    const ctx = { resource, schema, rtu: "" };
    const current = (await api(pathFor(ctx, row))).item;
    if (state.resource !== origin || state.rtu !== originRtu) return;
    state.detail = { resource, row: current };
    $("#detail-title").textContent =
      (current.name || schema.head.chn_name) + " · " + keyOf(current, schema);
    $("#detail-fields").innerHTML = schema.columns
      .map((column) => {
        const value = current[column.eng_name];
        const displayValue = protocolDisplayLabel(schema.head.eng_name, column.eng_name, value) ?? value;
        return (
          '<div class="detail-field"><dt>' +
          esc(column.chn_name) +
          "<small>" +
          esc(column.eng_name) +
          "</small></dt><dd>" +
          esc(displayValue ?? "—") +
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
  const visibleRows = rows.slice(offset, offset + 50).map((row) => {
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
  });
  reconcileBody(
    $("#process-rows"),
    visibleRows.length
      ? visibleRows
      : ['<tr><td colspan="6" class="empty">没有匹配的进程</td></tr>'],
    processRender,
    true,
  );
  for (const id of ["process-all-start", "process-all-stop", "debug-start"]) $("#" + id).disabled = processMonitor.busy;
}
let pendingProcesses = null;
function loadProcesses({fresh = false} = {}) {
  if (pendingProcesses) return fresh ? pendingProcesses.then(() => loadProcesses()) : pendingProcesses;
  pendingProcesses = fetchProcesses().finally(() => { pendingProcesses = null; });
  return pendingProcesses;
}
async function fetchProcesses() {
  const request = ++processMonitor.request;
  processMonitor.loading = true;
  try {
    const data = await api("/api/processes");
    if (request !== processMonitor.request) return;
    processMonitor.rows = data.items;
    processMonitor.busy = data.busy;
    const summary = [
      [data.items.length, "登记进程"], [data.items.filter((r) => r.alive).length, "进程在线"],
      [data.items.filter((r) => r.process_status === 5).length, "进程异常"],
      [data.items.filter((r) => r.kind === "debug" && r.include_in_all).length, "已选调试"],
    ].map(([value, label]) => `<div class="metric"><strong>${value}</strong><span>${label}</span></div>`).join("");
    const summaryNode = $("#process-summary");
    if (summaryNode._markup !== summary) { summaryNode.innerHTML = summary; summaryNode._markup = summary; }
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
    await loadProcesses({fresh:true});
  }
}
function openProcessLogs(name) {
  const select = $("#log-process");
  // The compact C log page does not expose the process/file toolbar.  A
  // process-row "日志" action therefore opens the complete runtime index;
  // otherwise a hidden process value would make the page appear to contain
  // only that process's files.
  if (select) select.value = "";
  logs.offset = 0;
  logs.selected = "";
  return changeResource("logs");
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
    const threadRows = data.rtus
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
            runtimeBadge(row.runtime_status, row.process_status) +
            "</td><td>" +
            esc(row.runtime_status?.connection_state || "—") +
            "</td><td>" +
            esc(row.thread_id || "—") +
            '</td><td class="mono">' +
            esc([row.runtime_status?.rx_count || 0, row.runtime_status?.tx_count || 0, row.runtime_status?.error_count || 0].join(" / ")) +
            "</td><td>" +
            esc(fmtClockTime(row.runtime_status?.process_heartbeat_time || row.process_heartbeat_time)) +
            '<span class="cell-sub">采样 ' + esc(fmtClockTime(row.runtime_status?.sampled_at)) + '</span>' +
            "</td>" +
            '<td class="error-cell"><button class="row-button" data-thread-detail="' +
            index +
            '" title="' +
            esc(row.runtime_status?.last_error || row.process_error || "查看完整记录") +
            '">' +
            esc(row.runtime_status?.last_error || row.process_error || "—") +
            "</button></td>" +
            '<td class="sticky-actions"><button class="row-button" data-thread-log="' +
            esc(row.rtu_id) +
            '">日志</button><button class="row-button" data-thread-enable="' +
            index +
            '">' +
            (row.process_enable === 1 ? "停用" : "启用") +
            "</button></td></tr>",
        );
    reconcileBody(
      $("#scdfe-rtus"),
      threadRows.length
        ? threadRows
        : ['<tr><td class="empty" colspan="11">没有匹配的 RTU</td></tr>'],
      threadRender,
      true,
    );
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
  } finally {
    // A successful PATCH followed by a failed refresh must remain retryable.
    button.disabled = false;
  }
}
$("#scdfe-rtus").onclick = (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.hasAttribute("data-thread-log"))
    return openRtuLogs(button.dataset.threadLog);
  const index = Number(
    button.dataset.threadDetail ?? button.dataset.threadEnable,
  );
  if (!Number.isInteger(index) || !monitor.rows[index]) return;
  if (button.hasAttribute("data-thread-detail"))
    return openDetails(monitor.rows[index], "rtus");
  if (button.hasAttribute("data-thread-enable"))
    return controlRtu(monitor.rows[index], "enable", button);
};
function processLabel(name) {
  if (name === "scdfe_c-main") return "主进程";
  if (name === "scdfe_c-db-writer") return "数据库写进程";
  if (name === "scdfe_c-command-server") return "TCP指令服务进程";
  const rtu = /^rtu:(\d+)$/.exec(name);
  if (rtu) return "RTU " + rtu[1] + " · 全部日志";
  const match = /^scdfe_c-(debug-)?rtu-(\d+)$/.exec(name);
  return match
    ? "RTU " +
        match[2] +
        (match[1] ? " · 人工调试" : " · 自动通信")
    : name;
}
async function loadCommandServerConfig() {
  const form = $("#command-server-config");
  if (!form) return;
  try {
    const data = await api("/api/scdfe_c/command-server");
    const row = data.item || {};
    // Keep the row version with the form so concurrent edits are rejected
    // cleanly by the same optimistic-concurrency rule as the data pages.
    form.dataset.version = row._version || "";
    $("#command-server-host").value = row.bind_host || "127.0.0.1";
    $("#command-server-port").value = row.bind_port || 8890;
    $("#command-server-token").value = row.auth_token || "";
    $("#command-server-retention").value = row.log_retention_hours || 720;
    $("#command-server-config-error").textContent = "";
  } catch (error) {
    $("#command-server-config-error").textContent = error.message;
  }
}
$("#command-server-config")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("#command-server-config-error");
  const form = $("#command-server-config");
  error.textContent = "";
  try {
    await api("/api/scdfe_c/command-server", {
      method: "PATCH",
      headers: form.dataset.version
        ? { "If-Match": String(form.dataset.version) }
        : {},
      body: JSON.stringify({
        bind_host: $("#command-server-host").value.trim(),
        bind_port: Number($("#command-server-port").value),
        auth_token: $("#command-server-token").value.trim(),
        log_retention_hours: Number($("#command-server-retention").value),
      }),
    });
    toast("TCP 指令服务配置已保存");
    await loadCommandServerConfig();
    $("#command-server-config-dialog").close();
  } catch (err) {
    error.textContent = err.message;
  }
});
function fmtSize(size) {
  return size >= 1048576
    ? (size / 1048576).toFixed(1) + " MB"
    : size >= 1024
      ? (size / 1024).toFixed(1) + " KB"
      : size + " B";
}
function openRtuLogs(rtu) {
  return openProcessLogs("rtu:" + rtu);
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
    const data = await api("/api/scdfe_c/logs?record=" + encodeURIComponent(logs.record));
    if (request !== logs.request) return;
    logs.files = data.items;
    $("#log-record").value = logs.record;
    $("#log-direction").disabled = logs.record === "runtime";
    if (logs.record === "runtime") $("#log-direction").value = "";
    logs.recordingEnabled = null;
    const processFilter = logProcessFilter();
    if (logs.record === "packet" && processFilter.rtu_id) {
      const policyParams = new URLSearchParams({ record: "packet", rtu_id: processFilter.rtu_id });
      const policy = await api("/api/scdfe_c/logs?" + policyParams);
      if (request !== logs.request) return;
      logs.recordingEnabled = policy.recording_enabled ?? null;
    }
    const selected = $("#log-process").value;
    const processes = [...new Set(data.items.map((item) => item.process))];
    if (selected && !processes.includes(selected)) processes.push(selected);
    // Traffic changes file order on each refresh; keep this menu stable and
    // preserve its option nodes while its membership remains unchanged.
    processes.sort();
    const processOptions =
      '<option value="">全部文件</option>' +
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
    const processMenu = $("#log-process");
    if (processMenu.innerHTML !== processOptions) {
      processMenu.innerHTML = processOptions;
      processMenu.value = selected;
    }
    $("#log-directory").textContent = (logs.record === "packet" ? "报文目录：" : "运行日志目录：") + data.directory;
    if ($("#log-status")) $("#log-status").textContent = "目录已更新 · " + new Date().toLocaleTimeString();
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
  const fileQuery = $("#log-file-query")?.value.trim().toLowerCase() || "";
  logs.filtered = logs.files.filter(
    (item) => filters.every(([key, value]) => String(item[key] ?? "") === value)
      && (!fileQuery || item.name.split("/").pop().toLowerCase().includes(fileQuery)),
  );
  if (logs.offset >= logs.filtered.length) logs.offset = 0;
  let selected = logs.filtered.find((item) => item.name === logs.selected);
  if (refresh && $("#log-follow").checked && !$("#log-end").value && selected) {
    // File modification times can change when an archive is copied back.
    // Follow the file hour of the same process, not its position in the list.
    for (const item of logs.filtered)
      if (item.process === selected.process && item.hour > selected.hour) selected = item;
  }
  if (!selected) selected = logs.filtered[0];
  const changed = logs.selected !== (selected?.name || "");
  logs.selected = selected?.name || "";
  renderLogFiles();
  if (!refresh || changed || refreshContent || (!logs.cursor && $("#log-follow").checked))
    await readLog(logs.selected, changed);
}
function renderLogFiles() {
  if ($("#log-file-count"))
    $("#log-file-count").textContent = logs.filtered.length + " / " + logs.files.length + " 个文件";
  const page = logs.filtered.slice(logs.offset, logs.offset + 50);
  if (logs.selected !== logFilesRender.selected ||
      page.length !== logFilesRender.files.length ||
      page.some((item, index) => {
        const previous = logFilesRender.files[index];
        return item.name !== previous.name || item.process !== previous.process ||
          item.size !== previous.size || item.modified_at !== previous.modified_at;
      })) {
    let empty = "暂无匹配文件";
    if (logs.record === "packet") {
      if (logs.recordingEnabled === false) {
        empty = "当前通道未开启报文保存，请到“通道参数”中将“保存报文”设为“是”，并保持通信线程运行。";
      } else if (!logs.files.length) {
        empty = "暂无报文文件；请在“通道参数”中开启“保存报文”，通信线程产生收发报文后再刷新。";
      } else {
        empty = "当前筛选条件暂无报文文件，请确认对应通道已开启“保存报文”。";
      }
    }
    $("#scdfe-logs").innerHTML = page.length
      ? renderLogTree(page)
      : '<div class="lb-empty">' + esc(empty) + "</div>";
    bindLogTreeState();
    logFilesRender.files = page.map(({ name, process, size, modified_at }) =>
      ({ name, process, size, modified_at }));
    logFilesRender.selected = logs.selected;
  }
  $("#log-page").textContent =
    Math.floor(logs.offset / 50) +
    1 +
    " / " +
    Math.max(1, Math.ceil(logs.filtered.length / 50));
  $("#log-prev").disabled = logs.offset === 0;
  $("#log-next").disabled = logs.offset + 50 >= logs.filtered.length;
}
$("#scdfe-logs").onclick = (event) => {
  const button = event.target.closest("[data-log-name]");
  if (!button) return;
  $("#log-scope").value = "file";
  $("#log-follow").checked = false;
  logs.selected = button.dataset.logName;
  renderLogFiles();
  readLog(logs.selected, true);
};
function logTreeDirectoryLabel(name, depth) {
  const suffix = [" 年", " 月", " 日", " 时"][depth] || "";
  return suffix ? name + suffix : name;
}
function logTreeFile(item) {
  return (
    '<button class="lb-file' +
    (item.name === logs.selected ? " selected" : "") +
    '" data-log-name="' +
    esc(item.name) +
    '" aria-pressed="' +
    (item.name === logs.selected) +
    '" role="treeitem"><strong>' +
    esc(processLabel(item.process)) +
    '</strong><small>' +
    esc(fmtSize(item.size)) +
    "</small></button>"
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
        node.children.set(part, { name: part, depth, path: [...(node.path || []), part], children: new Map(), files: [], count: 0 });
      node = node.children.get(part);
      node.count += 1;
    });
    node.files.push(item);
  }
  function renderNode(node) {
    const groups = [...node.children.values()]
      .map((child) => {
        const key = child.path.join("/");
        return '<details class="lb-group" data-key="' + esc(key) + '"' +
          (logTreeClosed.has(key) ? "" : " open") +
          '><summary role="treeitem"><strong>' +
          esc(logTreeDirectoryLabel(child.name, child.depth)) +
          '</strong><small>' + child.count + ' 个文件</small></summary>' +
          '<div class="lb-children" role="group">' +
          renderNode(child) +
          '</div></details>';
      })
      .join("");
    return (
      groups +
      node.files.map(logTreeFile).join("")
    );
  }
  return renderNode(root);
}
function bindLogTreeState() {
  $("#scdfe-logs").querySelectorAll("details[data-key]").forEach((detail) => {
    detail.addEventListener("toggle", () => {
      if (detail.open) logTreeClosed.delete(detail.dataset.key);
      else logTreeClosed.add(detail.dataset.key);
    });
  });
}
function logSearchParams(filename) {
  const params = new URLSearchParams({ limit: $("#log-tail").value, ...logProcessFilter() });
  params.set("record", logs.record);
  for (const [id, key] of [["log-search", "q"], ["log-level", "level"],
    ["log-direction", "direction"], ["log-channel", "channel_id"],
    ["log-file-query", "file_query"]]) {
    const node = $("#" + id);
    if (!node) continue;
    const value = node.value.trim();
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
function resetLogContent(message) {
  logs.content = "";
  logs.entries = [];
  logs.meta = null;
  logs.nextCursor = null;
  logs.resultKey = null;
  logContentRender.rows = [];
  $("#scdfe-log-content").innerHTML = '<div class="lb-empty">' + esc(message) + '</div>';
}
async function readLog(filename = logs.selected, changed = false, cursor = null, history = []) {
  clearTimeout(logs.searchTimer);
  logs.searchTimer = null;
  const request = ++logs.contentRequest;
  logs.contentLoading = true;
  logs.cursor = cursor;
  logs.history = history;
  $("#log-download").disabled = true;
  $("#log-result-prev").disabled = true;
  $("#log-result-next").disabled = true;
  $("#log-title").textContent = $("#log-scope").value === "all"
    ? "全部匹配文件 · " + (logs.record === "packet" ? "报文" : "运行日志") + "检索"
    : filename || "暂无匹配日志";
  $("#log-meta").textContent = "正在按条件检索…";
  $("#log-lines").textContent = "正在检索…";
  $("#log-search-info").textContent = "";
  $("#scdfe-log-content").setAttribute("aria-busy", "true");
  try {
    const params = logSearchParams(filename);
    if (cursor) params.set("cursor", cursor);
    const scope = $("#log-scope").value;
    const resultKey = JSON.stringify([scope, params.toString()]);
    // Keep the visible batch while polling the same query. A new query must
    // clear it immediately so old lines never appear under new conditions.
    if (resultKey !== logs.resultKey) resetLogContent("正在检索日志…");
    const data = !filename && scope === "file"
      ? { items: [], next_cursor: null, scanned_files: 0, scanned_bytes: 0, skipped_lines: 0 }
      : await api("/api/scdfe_c/logs/search?" + params);
    if (request !== logs.contentRequest) return;
    logs.entries = data.items.slice().reverse();
    logs.content = logs.entries.map((entry) =>
      (scope === "all" ? "[" + entry.filename + "] " : "") +
      entry.text).join("\n");
    logs.meta = data;
    logs.resultKey = resultKey;
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
    resetLogContent(error.message);
    $("#log-meta").textContent = "筛选失败";
    $("#log-lines").textContent = "筛选失败";
    showError("#logs-error", error);
  } finally {
    if (request === logs.contentRequest) {
      logs.contentLoading = false;
      $("#scdfe-log-content").setAttribute("aria-busy", "false");
    }
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
  const rows = logs.entries.map((entry, index) =>
    '<div class="lb-line ' + esc(entry.level) + '"><span class="lb-line-no">' + (index + 1) +
    '</span><span class="lb-line-text">' + highlight(entry.text, query) +
    (allFiles ? '<small class="lb-source">' + esc(entry.filename) + '</small>' : '') + '</span></div>',
  );
  reconcileBody(content, rows.length ? rows : ['<div class="lb-empty">' + (logs.nextCursor
    ? "本批无匹配日志"
    : "没有匹配的日志") + '</div>'], logContentRender);
  content.classList.toggle("lb-wrap", $("#log-wrap").checked);
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
    const [, { items }] = await Promise.all([api("/api/health"), api("/api/schemas")]);
    items.forEach((schema) => state.schemas.set(schema.head.eng_name, schema));
    state.schemas.set("channels", channelSchema);
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
    const supplementary = Promise.allSettled([
      refreshSummary(),
      loadRtuOptions(),
    ]);
    changeResource(location.hash.slice(1) || "channels", false);
    for (const result of await supplementary) {
      if (result.status === "rejected") toast(result.reason.message, true);
    }
  } catch (error) {
    $("#health").className = "health error";
    $("#health span:last-child").textContent = "连接失败";
    showError("#table-error", error);
  }
}
document.querySelectorAll(".nav").forEach((button) => {
  button.onclick = () => changeResource(button.dataset.resource);
});
$('#data-tabs').onclick = (event) => {
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
function openConfigDialog(id) {
  const dialog = $(id);
  if (dialog && !dialog.open) dialog.showModal();
}
function closeConfigDialog(id) {
  const dialog = $(id);
  if (dialog?.open) dialog.close();
}
$("#open-command-server-config")?.addEventListener("click", () => {
  loadCommandServerConfig();
  openConfigDialog("#command-server-config-dialog");
});
$("#close-command-server-config")?.addEventListener("click", () => closeConfigDialog("#command-server-config-dialog"));
$("#cancel-command-server-config")?.addEventListener("click", () => closeConfigDialog("#command-server-config-dialog"));
$("#open-scdfe-config")?.addEventListener("click", () => openConfigDialog("#scdfe-config-dialog"));
$("#close-scdfe-config")?.addEventListener("click", () => closeConfigDialog("#scdfe-config-dialog"));
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
if ($("#logs-refresh")) $("#logs-refresh").onclick = () => loadScdfeLogs(true);
document.querySelectorAll(".lb-record-tab").forEach((tab) => {
  tab.onclick = () => {
    const record = tab.dataset.record;
    if (!record || record === logs.record || logs.loading) return;
    logs.record = record;
    $("#log-direction").disabled = logs.record === "runtime";
    if (logs.record === "runtime") $("#log-direction").value = "";
    logs.recordingEnabled = null;
    logs.files = [];
    logs.filtered = [];
    logs.selected = "";
    logs.offset = 0;
    logs.cursor = null;
    logs.history = [];
    logs.nextCursor = null;
    logFilesRender.files = [];
    logFilesRender.selected = null;
    document.querySelectorAll(".lb-record-tab").forEach((node) =>
      node.setAttribute("aria-selected", String(node.dataset.record === logs.record)));
    resetLogContent("正在读取日志文件列表…");
    loadScdfeLogs(true);
  };
});
$("#log-record").onchange = () => {
  const tab = document.querySelector('.lb-record-tab[data-record="' + $("#log-record").value + '"]');
  if (tab) tab.click();
};
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
  for (const id of ["log-search", "log-level", "log-direction", "log-channel", "log-file-query", "log-start", "log-end"]) {
    const node = $("#" + id);
    if (node) node.value = "";
  }
}
function initLogFilterToggle() {
  const reader = $("#logs-panel .lb-reader");
  const toggle = $("#log-toggle-filters");
  if (!reader || !toggle) return;
  const key = "scdfe_log_filters_collapsed_c";
  let collapsed = false;
  try { collapsed = window.localStorage.getItem(key) === "1"; } catch (_) { /* private mode */ }
  const apply = (value) => {
    collapsed = Boolean(value);
    reader.classList.toggle("is-filters-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.textContent = collapsed ? "显示筛选" : "隐藏筛选";
    toggle.title = collapsed ? "显示日志筛选条件" : "隐藏日志筛选条件，扩大内容显示区域";
    try { window.localStorage.setItem(key, collapsed ? "1" : "0"); } catch (_) { /* private mode */ }
  };
  toggle.onclick = () => apply(!collapsed);
  apply(collapsed);
}
$("#log-search").oninput = () => {
  clearTimeout(logs.searchTimer);
  logs.searchTimer = setTimeout(() => readLog(logs.selected, true), 300);
};
if ($("#log-file-query")) $("#log-file-query").oninput = debounce(() => { logs.offset = 0; filterLogs(); }, 250);
for (const id of ["log-level", "log-direction", "log-channel", "log-start", "log-end", "log-tail"])
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
$("#log-wrap").onchange = () => {
  const content = $("#scdfe-log-content"), scroll = content.scrollTop;
  content.classList.toggle("lb-wrap", $("#log-wrap").checked);
  content.scrollTop = $("#log-follow").checked ? content.scrollHeight : scroll;
};
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
initLogFilterToggle();
let manualRefreshPending = false;
$("#refresh").onclick = async () => {
  if (manualRefreshPending) return;
  manualRefreshPending = true;
  $("#refresh").disabled = true;
  try {
    await loadRows();
    await Promise.all([refreshSummary(), loadRtuOptions()]);
  } catch (error) {
    toast(error.message, true);
  } finally {
    manualRefreshPending = false;
    $("#refresh").disabled = false;
  }
};
$("#add").onclick = () =>
  state.resource === "channels"
    ? openNewChannel()
    : state.resource === "rtus"
      ? openNewRtu()
      : openEditor();
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
$("#rtu-filter").onchange = (event) => {
  state.rtu = event.target.value.trim();
  state.offset = 0;
  ++state.request;
  guardTableScope(context());
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
    loadRows();
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
    document.hidden ||
    document.querySelector("dialog[open]") ||
    state.saving
  )
    return;
  // Log and packet directories are external file state. Keep their catalogue
  // live whenever the log page is open, even if the general data refresh
  // toggle is disabled; the reader preserves the current selection/filter.
  if (state.resource === "logs") {
    if (!logs.loading && !logs.contentLoading) loadScdfeLogs(true);
    return;
  }
  if (!$("#auto-refresh").checked) return;
  if (state.resource === "processes") {
    if (!processMonitor.loading) loadProcesses();
  } else if (state.resource === "scdfe") {
    if (!monitor.loading) loadScdfe();
  } else if (!state.loading) loadRows();
}, 5000);
boot();

"""SCADA 通道与四遥定义 E 文件（CIM/E 风格）导出与导入引擎。

用于将 scdfe_s 中的通道、RTU、规约参数、规约点映射及四遥点表（YC/YX/YK/YT）
导出为标准 E 文件，并由 scdfe_c 解析导入，确保导入后两端可直接正常通信。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core import operation_audit as audit
from scdfe_c.web.service import (
    ApiError,
    CHANNEL_PARAMETER_TABLES,
    CHANNEL_TYPES,
    KIND_TABLES,
    _DEFINITION_RUNTIME_COLUMNS,
    quote_identifier,
)


EFILE_VERSION = "1.0"
MAX_EFILE_CHARS = 15_000_000
_TOKEN_NEEDS_QUOTE = re.compile(r"""[\s#@<>='"!]""")
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+")

# 导出时剥离的纯运行时/瞬态列（保留 packet_recording 与 process_enable 以便通信配置完整迁移）
_EXPORT_SKIP_COLUMNS = (
    _DEFINITION_RUNTIME_COLUMNS - {"packet_recording", "process_enable"}
)

# 不参与通道 E 文件导出的运行时/诊断表
_SKIP_CHANNEL_TABLES = frozenset({
    "fes_channel_runtime_status",
    "fes_channel_can_diagnostic",
})

_POINT_TABLES = (
    KIND_TABLES["yc"],
    KIND_TABLES["yx"],
    KIND_TABLES["yk"],
    KIND_TABLES["yt"],
)

_SERVER_CONFIG_FILES = {
    "iec104": ("iec104_server_config.json",),
    "modbus-tcp": ("modbus_tcp_server_service_config.json", "modbus_tcp_service_config.json"),
    "modbus-rtu": ("modbus_rtu_server_service_config.json", "modbus_rtu_service_config.json"),
    "can": ("can_server_service_config.json", "can_service_config.json"),
    "bacnet": ("bacnet_server_service_config.json",),
    "opc": ("opc_server_service_config.json",),
}


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode_token(value: Any) -> str:
    """将字段值编码为标准 E 文件 token，保证空串与 NULL 可无损往返。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).replace("\r", " ").replace("\n", " ")
    if text == "" or text.lower() == "null" or _TOKEN_NEEDS_QUOTE.search(text):
        return "'" + text.replace("'", "''") + "'"
    return text


def _parse_tokens(line: str) -> list[tuple[str, bool]]:
    """按 E 文件词法切分一行，返回 (值, 是否带引号) 列表。"""
    tokens: list[tuple[str, bool]] = []
    i = 0
    length = len(line)
    while i < length:
        while i < length and line[i].isspace():
            i += 1
        if i >= length:
            break
        quote = line[i]
        if quote in {"'", '"'}:
            i += 1
            buf: list[str] = []
            closed = False
            while i < length:
                ch = line[i]
                if ch == quote:
                    if i + 1 < length and line[i + 1] == quote:
                        buf.append(quote)
                        i += 2
                        continue
                    closed = True
                    i += 1
                    break
                buf.append(ch)
                i += 1
            if not closed:
                raise ApiError(400, f"E 文件存在未闭合引号：{line[:80]}")
            tokens.append(("".join(buf), True))
        else:
            start = i
            while i < length and not line[i].isspace():
                i += 1
            tokens.append((line[start:i], False))
    return tokens


def _parse_kv_text(text: str) -> dict[str, str]:
    """解析 E 文件头或模块头中的 key=value / key = '带 空格 值' 键值对。"""
    result: dict[str, str] = {}
    i = 0
    length = len(text)
    while i < length:
        while i < length and text[i].isspace():
            i += 1
        if i >= length:
            break
        key_start = i
        while i < length and not text[i].isspace() and text[i] != "=":
            i += 1
        key = text[key_start:i].strip()
        while i < length and text[i].isspace():
            i += 1
        if i >= length or text[i] != "=":
            continue
        i += 1  # 跳过 '='
        while i < length and text[i].isspace():
            i += 1
        if i >= length:
            if key:
                result[key] = ""
            break
        quote = text[i]
        if quote in {"'", '"'}:
            i += 1
            buf: list[str] = []
            closed = False
            while i < length:
                ch = text[i]
                if ch == quote:
                    if i + 1 < length and text[i + 1] == quote:
                        buf.append(quote)
                        i += 2
                        continue
                    closed = True
                    i += 1
                    break
                buf.append(ch)
                i += 1
            if not closed:
                raise ApiError(400, f"E 文件头属性存在未闭合引号：{text[:80]}")
            val = "".join(buf)
        else:
            val_start = i
            while i < length and not text[i].isspace():
                i += 1
            val = text[val_start:i]
        if key:
            result[key] = val
    return result


def _parse_header_line(line: str) -> dict[str, str]:
    inner = line[2:]
    if inner.endswith("!>"):
        inner = inner[:-2]
    elif inner.endswith(">"):
        inner = inner[:-1]
    return _parse_kv_text(inner.strip())


def serialize_efile(header: dict[str, Any], modules: list[dict[str, Any]]) -> str:
    """将结构化模块列表序列化为标准 E 文件文本。"""
    lines: list[str] = []
    if header:
        pairs = " ".join(f"{k}={_encode_token(v)}" for k, v in header.items() if v is not None)
        lines.append(f"<! {pairs} !>")
    for module in modules:
        name = str(module["name"])
        utility = module.get("utility") or ""
        title = f"{name}::{utility}" if utility else name
        props = module.get("props") or {}
        prop_text = " ".join(f"{k} = {_encode_token(v)}" for k, v in props.items() if v is not None)
        lines.append(f"<{title} {prop_text}>" if prop_text else f"<{title}>")
        columns = module.get("columns") or []
        lines.append("@" + " ".join(col["name"] for col in columns))
        lines.append("//" + " ".join(_encode_token(col.get("desc") or col["name"]) for col in columns))
        for row in module.get("rows") or []:
            lines.append("#" + " ".join(_encode_token(row[idx] if idx < len(row) else None) for idx in range(len(columns))))
        lines.append(f"</{name}>")
    return "\n".join(lines) + "\n"


def parse_efile(text: str) -> dict[str, Any]:
    """解析标准 E 文件文本为 {header, modules} 结构。"""
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "E 文件内容不能为空")
    if len(text) > MAX_EFILE_CHARS:
        raise ApiError(413, f"E 文件内容过大（上限 {MAX_EFILE_CHARS} 字符）")
    cleaned = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    header: dict[str, str] = {}
    modules: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    seen_columns = False

    for line_no, raw in enumerate(cleaned.split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("<!"):
            header.update(_parse_header_line(line))
            continue
        if line.startswith("</"):
            if current is None:
                raise ApiError(400, f"E 文件第 {line_no} 行出现未匹配的结束标记：{line}")
            closing_name = line[2:].rstrip(">").strip().split("::", 1)[0]
            if closing_name and closing_name != current["name"]:
                raise ApiError(400, f"E 文件第 {line_no} 行模块结束标记不匹配：{closing_name} != {current['name']}")
            current = None
            seen_columns = False
            continue
        if line.startswith("<") and line.endswith(">"):
            if current is not None:
                raise ApiError(400, f"E 文件第 {line_no} 行模块嵌套未闭合：{current['name']}")
            inner = line[1:-1].strip()
            if not inner:
                raise ApiError(400, f"E 文件第 {line_no} 行模块名为空")
            parts_head = inner.split(None, 1)
            title_raw = parts_head[0]
            name, _, utility = title_raw.partition("::")
            props = _parse_kv_text(parts_head[1]) if len(parts_head) > 1 else {}
            current = {"name": name, "utility": utility, "props": props, "columns": [], "rows": []}
            modules.append(current)
            seen_columns = False
            continue
        if current is None:
            if line.startswith("//"):
                continue
            raise ApiError(400, f"E 文件第 {line_no} 行位于模块之外：{line[:60]}")
        if line.startswith("@"):
            col_toks = _parse_tokens(line[1:])
            if not col_toks:
                raise ApiError(400, f"E 文件第 {line_no} 行模块 {current['name']} 列定义为空")
            current["columns"] = [{"name": tok, "desc": ""} for tok, _ in col_toks]
            seen_columns = True
            continue
        if line.startswith("//"):
            if seen_columns and not current["rows"]:
                desc_toks = _parse_tokens(line[2:])
                for i, col in enumerate(current["columns"]):
                    if i < len(desc_toks):
                        col["desc"] = desc_toks[i][0]
            continue
        if line.startswith("#"):
            if not seen_columns:
                raise ApiError(400, f"E 文件第 {line_no} 行在列定义之前出现数据行")
            row_toks = _parse_tokens(line[1:])
            col_count = len(current["columns"])
            if len(row_toks) > col_count:
                raise ApiError(
                    400,
                    f"E 文件第 {line_no} 行列数超出定义（期望 {col_count}，实际 {len(row_toks)}）",
                )
            values: list[Any] = []
            for tok, quoted in row_toks:
                if not quoted and tok.lower() == "null":
                    values.append(None)
                else:
                    values.append(tok)
            while len(values) < col_count:
                values.append(None)
            current["rows"].append(values)
            continue
        raise ApiError(400, f"E 文件第 {line_no} 行存在无法识别的语法：{line[:60]}")

    if current is not None:
        raise ApiError(400, f"E 文件模块未正常闭合：{current['name']}")
    if not modules:
        raise ApiError(400, "E 文件中未找到任何数据模块")
    return {"header": header, "modules": modules}


def _client_host(host: Any, fallback: str = "127.0.0.1") -> str:
    """将服务端监听通配地址转为客户端可直连地址。"""
    text = str(host or "").strip()
    if not text or text in {"0.0.0.0", "::", "*", "[::]"}:
        return fallback
    return text


def _load_server_config(store, channel_type: str, services: dict[str, Any] | None = None) -> dict[str, Any]:
    """读取 scdfe_s 当前运行服务或磁盘配置文件，用于补齐可通信端点参数。"""
    if services:
        candidates = (
            f"{channel_type}-server",
            channel_type,
        )
        for key in candidates:
            svc = services.get(key)
            if svc is not None and hasattr(svc, "config"):
                try:
                    cfg = svc.config()
                    if isinstance(cfg, dict):
                        return cfg
                except Exception:
                    pass
    base_dir = Path(store.db_path).parent
    for filename in _SERVER_CONFIG_FILES.get(channel_type, ()):
        cfg_path = base_dir / filename
        if cfg_path.is_file():
            try:
                loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return loaded
            except (OSError, ValueError):
                pass
    return {}


def _ensure_communicable_parameter(
    store,
    channel_type: str,
    channel_id: int,
    rtu_name: str,
    rows: list[dict[str, Any]],
    server_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """确保导出的规约参数包含 scdfe_c 连接 scdfe_s 所需的完整地址与端口。"""
    table = CHANNEL_PARAMETER_TABLES.get(channel_type)
    if not table:
        return rows
    schema = store.schema(table)
    defaults = {
        col["eng_name"]: col.get("default")
        for col in schema["columns"]
        if col["eng_name"] not in _EXPORT_SKIP_COLUMNS
    }
    if not rows:
        item = dict(defaults)
        item["channel_id"] = channel_id
        if "id" in defaults and item.get("id") is None:
            item["id"] = channel_id
        if "name" in defaults and not item.get("name"):
            item["name"] = rtu_name or f"通道 {channel_id}"
        rows = [item]
    else:
        rows = [dict(r) for r in rows]

    param = rows[0]
    if channel_type == "iec104":
        bind_ip = server_cfg.get("bind_ip")
        listen_port = server_cfg.get("port")
        param["remote_host"] = _client_host(param.get("remote_host") or bind_ip, "127.0.0.1")
        if not param.get("remote_port"):
            param["remote_port"] = int(listen_port) if listen_port else 2404
        for key in ("k", "w", "t1", "t2", "t3", "max_asdu_length"):
            if param.get(key) is None and server_cfg.get(key) is not None:
                param[key] = server_cfg[key]
        if param.get("rtu_addr") is None:
            param["rtu_addr"] = 1
        for key, default_val in (
            ("ycbegin_addr", 16385),
            ("yxbegin_addr", 1),
            ("ykbegin_addr", 24577),
            ("ytbegin_addr", 25089),
            ("yc_data_type", 1),
            ("yx_data_type", 1),
            ("yk_excute_type", 1),
            ("yt_excute_type", 2),
        ):
            if param.get(key) is None:
                param[key] = default_val
    elif channel_type == "modbus-tcp":
        bind_ip = server_cfg.get("bind_ip")
        listen_port = server_cfg.get("listen_port")
        param["remote_host"] = _client_host(param.get("remote_host") or bind_ip, "127.0.0.1")
        if not param.get("remote_port"):
            param["remote_port"] = int(listen_port) if listen_port else 502
        if param.get("unit_id") is None:
            param["unit_id"] = int(server_cfg.get("unit_id") or 1)
    elif channel_type == "bacnet":
        bind_ip = param.get("bind_ip") or server_cfg.get("bind_ip")
        bind_port = param.get("bind_port") or server_cfg.get("bind_port")
        param["remote_host"] = _client_host(param.get("remote_host") or bind_ip, "127.0.0.1")
        if not param.get("remote_port"):
            param["remote_port"] = int(bind_port) if bind_port else 47808
        if param.get("dev_id") is None and server_cfg.get("device_id") is not None:
            param["dev_id"] = int(server_cfg["device_id"])
    elif channel_type == "opc":
        if not str(param.get("opc_name") or "").strip():
            host = _client_host(server_cfg.get("bind_ip"), "127.0.0.1")
            port = int(server_cfg.get("port") or 4840)
            path = str(server_cfg.get("endpoint_path") or "/scada/server/")
            if not path.startswith("/"):
                path = "/" + path
            param["opc_name"] = f"opc.tcp://{host}:{port}{path}"
    return rows


def _channel_tables_for_type(store, channel_type: str) -> list[str]:
    """返回指定 channel_type 关联的规约主表与全部子表（按稳定顺序）。"""
    primary = CHANNEL_PARAMETER_TABLES.get(channel_type)
    result: list[str] = []
    if primary and primary in store.catalog.schemas:
        result.append(primary)
    for name, _label, table_type in store._channel_reference_tables():
        if name in _SKIP_CHANNEL_TABLES or name in result:
            continue
        if table_type == channel_type:
            result.append(name)
        elif table_type is None and channel_type in {"modbus-tcp", "modbus-rtu"} and name.startswith("fes_channel_modbus_"):
            result.append(name)
    return result


def _export_columns(store, table: str) -> list[dict[str, str]]:
    return [
        {"name": col["eng_name"], "desc": col.get("chn_name") or col["eng_name"]}
        for col in store.schema(table)["columns"]
        if col["eng_name"] not in _EXPORT_SKIP_COLUMNS
    ]


def _order_by_pk(store, table: str) -> str:
    pks = [col["eng_name"] for col in store.schema(table)["columns"] if col.get("primary_key")]
    if not pks:
        return ""
    return " ORDER BY " + ", ".join(quote_identifier(pk) for pk in pks)


def export_channel_efile(store, rtu_id: int | str, *, services: dict[str, Any] | None = None) -> dict[str, Any]:
    """将指定通道/RTU 及其关联规约参数、规约点映射和四遥点表导出为标准 E 文件。"""
    keys = store._key_values("fes_rtu_define", [rtu_id])
    target_rtu_id = int(keys["rtu_id"])

    with store.connection() as connection:
        rtu_row = connection.execute(
            "SELECT * FROM fes_rtu_define WHERE rtu_id=?",
            (target_rtu_id,),
        ).fetchone()
        if rtu_row is None:
            raise ApiError(404, f"未找到 RTU {target_rtu_id}", code="not_found")
        rtu = dict(rtu_row)
        channel_id = int(rtu["channel_id"])
        channel_type = str(rtu.get("channel_type") or "custom")
        rtu_name = str(rtu.get("name") or "")

        modules: list[dict[str, Any]] = []
        counts: dict[str, int] = {}

        # 1. RTU / 通道定义
        rtu_cols = _export_columns(store, "fes_rtu_define")
        modules.append({
            "name": "fes_rtu_define",
            "utility": "scada",
            "props": {"rtu_id": target_rtu_id, "channel_id": channel_id, "channel_type": channel_type},
            "columns": rtu_cols,
            "rows": [[rtu.get(col["name"]) for col in rtu_cols]],
        })
        counts["fes_rtu_define"] = 1

        # 2. 规约参数主表与关联子表
        server_cfg = _load_server_config(store, channel_type, services)
        primary_param_table = CHANNEL_PARAMETER_TABLES.get(channel_type)
        for table in _channel_tables_for_type(store, channel_type):
            cols = _export_columns(store, table)
            raw_rows = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {quote_identifier(table)} WHERE channel_id=?{_order_by_pk(store, table)}",
                    (channel_id,),
                ).fetchall()
            ]
            if table == primary_param_table:
                raw_rows = _ensure_communicable_parameter(
                    store, channel_type, channel_id, rtu_name, raw_rows, server_cfg
                )
            if raw_rows:
                modules.append({
                    "name": table,
                    "utility": "scada",
                    "props": {"channel_id": channel_id, "channel_type": channel_type},
                    "columns": cols,
                    "rows": [[item.get(col["name"]) for col in cols] for item in raw_rows],
                })
                counts[table] = len(raw_rows)

        # 3. 四遥点表（YC / YX / YK / YT）
        for table in _POINT_TABLES:
            cols = _export_columns(store, table)
            raw_rows = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {quote_identifier(table)} WHERE rtu_id=?{_order_by_pk(store, table)}",
                    (target_rtu_id,),
                ).fetchall()
            ]
            if raw_rows:
                modules.append({
                    "name": table,
                    "utility": "scada",
                    "props": {"rtu_id": target_rtu_id},
                    "columns": cols,
                    "rows": [[item.get(col["name"]) for col in cols] for item in raw_rows],
                })
                counts[table] = len(raw_rows)

    header = {
        "System": "SCADA",
        "Version": EFILE_VERSION,
        "Source": "scdfe_s",
        "ExportTime": _iso_utc_now(),
        "ChannelType": channel_type,
        "RtuId": target_rtu_id,
        "ChannelId": channel_id,
        "ChannelName": rtu_name or f"rtu_{target_rtu_id}",
    }
    content = serialize_efile(header, modules)
    safe_type = _FILENAME_UNSAFE.sub("_", channel_type).strip("_") or "channel"
    safe_name = _FILENAME_UNSAFE.sub("_", rtu_name).strip("_")[:32]
    stem = f"scdfe_s_rtu{target_rtu_id}_ch{channel_id}_{safe_type}" + (f"_{safe_name}" if safe_name else "")
    return {
        "filename": f"{stem}.e",
        "content": content,
        "summary": {
            "rtu_id": target_rtu_id,
            "channel_id": channel_id,
            "channel_type": channel_type,
            "name": rtu_name,
            "counts": counts,
            "total_points": sum(counts.get(t, 0) for t in _POINT_TABLES),
        },
    }


def _rewrite_opc_host(opc_name: str, host_override: str) -> str:
    text = str(opc_name or "").strip()
    if not text:
        return f"opc.tcp://{host_override}:4840/scada/server/"
    parsed = urlsplit(text)
    if parsed.scheme in {"opc.tcp", "opc.https", "https"}:
        port = parsed.port
        userinfo = ""
        if parsed.username:
            userinfo = parsed.username + (f":{parsed.password}" if parsed.password else "") + "@"
        netloc = f"{userinfo}{host_override}" + (f":{port}" if port else "")
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return text


def _apply_client_endpoint_overrides(
    table: str,
    channel_type: str,
    row: dict[str, Any],
    host_override: str | None,
) -> None:
    """导入到 scdfe_c 时规范化服务端连接地址，并按需应用跨机主机覆写。"""
    if table != CHANNEL_PARAMETER_TABLES.get(channel_type):
        return
    if channel_type in {"iec104", "modbus-tcp", "bacnet"}:
        if host_override:
            row["remote_host"] = host_override
        else:
            row["remote_host"] = _client_host(row.get("remote_host"), "127.0.0.1")
        if channel_type == "iec104" and not row.get("remote_port"):
            row["remote_port"] = 2404
        elif channel_type == "modbus-tcp" and not row.get("remote_port"):
            row["remote_port"] = 502
        elif channel_type == "bacnet" and not row.get("remote_port"):
            row["remote_port"] = 47808
    elif channel_type == "opc":
        if host_override:
            row["opc_name"] = _rewrite_opc_host(row.get("opc_name") or "", host_override)
        elif not str(row.get("opc_name") or "").strip():
            row["opc_name"] = "opc.tcp://127.0.0.1:4840/scada/server/"


def _module_rows_as_dicts(module: dict[str, Any]) -> list[dict[str, Any]]:
    col_names = [col["name"] for col in module["columns"]]
    return [dict(zip(col_names, row)) for row in module["rows"]]


def _allocate_next_rtu_id(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(rtu_id), 0) FROM fes_rtu_define").fetchone()
    return int(row[0]) + 1


def _allocate_next_channel_id(connection: sqlite3.Connection, store, channel_type: str) -> int:
    candidates = [
        int(connection.execute("SELECT COALESCE(MAX(channel_id), 0) FROM fes_rtu_define").fetchone()[0]),
    ]
    for table in _channel_tables_for_type(store, channel_type):
        candidates.append(
            int(connection.execute(f"SELECT COALESCE(MAX(channel_id), 0) FROM {quote_identifier(table)}").fetchone()[0])
        )
    return max(candidates) + 1


def _channel_in_use(connection: sqlite3.Connection, store, channel_id: int, channel_type: str) -> bool:
    if connection.execute(
        "SELECT 1 FROM fes_rtu_define WHERE channel_id=? AND channel_type=? LIMIT 1",
        (channel_id, channel_type),
    ).fetchone() is not None:
        return True
    primary = CHANNEL_PARAMETER_TABLES.get(channel_type)
    if primary and primary in store.catalog.schemas:
        if connection.execute(
            f"SELECT 1 FROM {quote_identifier(primary)} WHERE channel_id=? LIMIT 1",
            (channel_id,),
        ).fetchone() is not None:
            return True
    return False


def _delete_rtu_and_channel_rows(
    connection: sqlite3.Connection,
    store,
    rtu_id: int,
    channel_id: int,
    channel_type: str,
) -> dict[str, int]:
    """在覆盖模式下，于当前事务内清理冲突的 RTU 与通道定义数据。"""
    deleted: dict[str, int] = {}
    affected_rtus = {
        int(r[0])
        for r in connection.execute(
            "SELECT rtu_id FROM fes_rtu_define WHERE rtu_id=? OR (channel_id=? AND channel_type=?)",
            (rtu_id, channel_id, channel_type),
        ).fetchall()
    }
    affected_channels: set[tuple[int, str]] = {(channel_id, channel_type)}
    for rid in affected_rtus:
        row = connection.execute(
            "SELECT channel_id, channel_type FROM fes_rtu_define WHERE rtu_id=?",
            (rid,),
        ).fetchone()
        if row and row[0] is not None:
            affected_channels.add((int(row[0]), str(row[1] or channel_type)))
        for table, _label in store._rtu_reference_tables():
            cnt = connection.execute(
                f"DELETE FROM {quote_identifier(table)} WHERE rtu_id=?",
                (rid,),
            ).rowcount
            if cnt > 0:
                deleted[table] = deleted.get(table, 0) + cnt
        cnt = connection.execute("DELETE FROM fes_rtu_define WHERE rtu_id=?", (rid,)).rowcount
        if cnt > 0:
            deleted["fes_rtu_define"] = deleted.get("fes_rtu_define", 0) + cnt
        connection.execute("DELETE FROM _scada_rtu_version WHERE rtu_id=?", (rid,))

    for cid, ctype in affected_channels:
        for table in _channel_tables_for_type(store, ctype):
            cnt = connection.execute(
                f"DELETE FROM {quote_identifier(table)} WHERE channel_id=?",
                (cid,),
            ).rowcount
            if cnt > 0:
                deleted[table] = deleted.get(table, 0) + cnt
    return deleted


def _prepare_import_row(store, table: str, raw: dict[str, Any]) -> dict[str, Any]:
    """按 ScadaStore schema 强校验并规范化单条导入行。"""
    schema = store.schema(table)
    columns_by_name = {col["eng_name"]: col for col in schema["columns"]}
    cleaned: dict[str, Any] = {}
    for col in schema["columns"]:
        name = col["eng_name"]
        if name in _EXPORT_SKIP_COLUMNS:
            if "default" in col:
                cleaned[name] = col["default"]
            continue
        if name in raw and raw[name] is not None:
            cleaned[name] = store._coerce(raw[name], col)
        elif "default" in col:
            cleaned[name] = col["default"]
        elif col.get("required") or col.get("primary_key"):
            raise ApiError(400, f"表 {table} 缺少必填字段 {name}")
        else:
            cleaned[name] = None
    # 校验不在 schema 中的未知列时忽略空附加列，避免跨版本导出阻塞；但至少有一列匹配。
    if not any(k in columns_by_name for k in raw):
        raise ApiError(400, f"表 {table} 不包含有效字段")
    if hasattr(store, "_validate_protocol_row"):
        store._validate_protocol_row(table, cleaned)
    return cleaned


def _insert_rows(connection: sqlite3.Connection, store, table: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    schema = store.schema(table)
    col_names = [col["eng_name"] for col in schema["columns"] if col["eng_name"] in rows[0]]
    quoted_cols = ", ".join(quote_identifier(c) for c in col_names)
    placeholders = ", ".join("?" for _ in col_names)
    sql = f"INSERT INTO {quote_identifier(table)} ({quoted_cols}) VALUES ({placeholders})"
    payload = [tuple(row.get(c) for c in col_names) for row in rows]
    connection.executemany(sql, payload)
    return len(payload)


def import_channel_efile(
    store,
    content: str,
    *,
    conflict_mode: str = "auto_remap",
    host_override: str | None = None,
) -> dict[str, Any]:
    """解析 E 文件并在单个事务内写入 scdfe_c 数据库。"""
    if conflict_mode not in {"auto_remap", "overwrite"}:
        raise ApiError(400, "conflict_mode 必须为 auto_remap 或 overwrite")
    override_host = str(host_override).strip() if host_override is not None else ""
    if override_host and any(ch.isspace() for ch in override_host):
        raise ApiError(400, "目标主机地址不能包含空白字符")

    parsed = parse_efile(content)
    by_table: dict[str, list[dict[str, Any]]] = {}
    for mod in parsed["modules"]:
        tname = mod["name"]
        if tname not in store.catalog.schemas:
            raise ApiError(400, f"E 文件包含不支持的数据表：{tname}")
        by_table.setdefault(tname, []).extend(_module_rows_as_dicts(mod))

    rtu_rows = by_table.get("fes_rtu_define") or []
    if len(rtu_rows) != 1:
        raise ApiError(400, f"E 文件必须包含且仅包含 1 条 fes_rtu_define 记录（当前 {len(rtu_rows)} 条）")

    raw_rtu = dict(rtu_rows[0])
    channel_type = str(raw_rtu.get("channel_type") or "").strip()
    if channel_type not in CHANNEL_TYPES:
        raise ApiError(400, f"不支持的通道规约类型：{channel_type!r}")

    allowed_tables = {"fes_rtu_define", *_POINT_TABLES, *_channel_tables_for_type(store, channel_type)}
    disallowed = sorted(set(by_table) - allowed_tables)
    if disallowed:
        raise ApiError(400, f"E 文件包含与规约 {channel_type} 不匹配或不允许导入的表：{', '.join(disallowed)}")

    rtu_schema = {c["eng_name"]: c for c in store.schema("fes_rtu_define")["columns"]}
    orig_rtu_id = int(store._coerce(raw_rtu.get("rtu_id"), rtu_schema["rtu_id"]))
    orig_channel_id = int(store._coerce(raw_rtu.get("channel_id"), rtu_schema["channel_id"]))
    if orig_rtu_id <= 0 or orig_channel_id <= 0:
        raise ApiError(400, "rtu_id 和 channel_id 必须是正整数")

    imported_counts: dict[str, int] = {}
    deleted_counts: dict[str, int] = {}
    remapped = False

    try:
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")

            rtu_exists = connection.execute(
                "SELECT 1 FROM fes_rtu_define WHERE rtu_id=?",
                (orig_rtu_id,),
            ).fetchone() is not None
            channel_exists = _channel_in_use(connection, store, orig_channel_id, channel_type)

            if conflict_mode == "overwrite":
                target_rtu_id = orig_rtu_id
                target_channel_id = orig_channel_id
                if rtu_exists or channel_exists:
                    deleted_counts = _delete_rtu_and_channel_rows(
                        connection, store, target_rtu_id, target_channel_id, channel_type
                    )
            else:
                target_rtu_id = _allocate_next_rtu_id(connection) if rtu_exists else orig_rtu_id
                target_channel_id = (
                    _allocate_next_channel_id(connection, store, channel_type)
                    if channel_exists
                    else orig_channel_id
                )
                remapped = (target_rtu_id != orig_rtu_id) or (target_channel_id != orig_channel_id)

            # 1. 写入 fes_rtu_define
            raw_rtu["rtu_id"] = target_rtu_id
            raw_rtu["channel_id"] = target_channel_id
            raw_rtu["channel_type"] = channel_type
            rtu_record = _prepare_import_row(store, "fes_rtu_define", raw_rtu)
            imported_counts["fes_rtu_define"] = _insert_rows(connection, store, "fes_rtu_define", [rtu_record])

            # 2. 写入规约参数主表及关联子表
            for table in _channel_tables_for_type(store, channel_type):
                src_rows = by_table.get(table) or []
                if not src_rows:
                    continue
                schema = store.schema(table)
                pks = [c["eng_name"] for c in schema["columns"] if c.get("primary_key")]
                has_rtu_col = any(c["eng_name"] == "rtu_id" for c in schema["columns"])
                next_id = None
                if pks == ["id"]:
                    next_id = int(
                        connection.execute(f"SELECT COALESCE(MAX(id), 0) FROM {quote_identifier(table)}").fetchone()[0]
                    ) + 1
                prepared_rows: list[dict[str, Any]] = []
                for item in src_rows:
                    row_copy = dict(item)
                    row_copy["channel_id"] = target_channel_id
                    if has_rtu_col:
                        row_copy["rtu_id"] = target_rtu_id
                    if pks == ["id"]:
                        # 若原 id 未冲突且未重映射则保留，否则分配安全新 id 避免撞表内已有记录。
                        raw_id = row_copy.get("id")
                        keep_id = False
                        if not remapped and raw_id is not None:
                            try:
                                cand = int(raw_id)
                                if cand > 0 and connection.execute(
                                    f"SELECT 1 FROM {quote_identifier(table)} WHERE id=?", (cand,)
                                ).fetchone() is None:
                                    row_copy["id"] = cand
                                    next_id = max(next_id, cand + 1)
                                    keep_id = True
                            except (TypeError, ValueError):
                                pass
                        if not keep_id:
                            row_copy["id"] = next_id
                            next_id += 1
                    _apply_client_endpoint_overrides(table, channel_type, row_copy, override_host or None)
                    prepared_rows.append(_prepare_import_row(store, table, row_copy))
                imported_counts[table] = _insert_rows(connection, store, table, prepared_rows)

            # 3. 写入四遥点表
            for table in _POINT_TABLES:
                src_rows = by_table.get(table) or []
                if not src_rows:
                    continue
                prepared_rows = []
                for item in src_rows:
                    row_copy = dict(item)
                    row_copy["rtu_id"] = target_rtu_id
                    prepared_rows.append(_prepare_import_row(store, table, row_copy))
                imported_counts[table] = _insert_rows(connection, store, table, prepared_rows)

            # 确保 _scada_rtu_version 存在并推进定义版本
            connection.execute(
                "INSERT INTO _scada_rtu_version(rtu_id, definition_version) VALUES (?, 1) "
                "ON CONFLICT(rtu_id) DO UPDATE SET definition_version=definition_version+1",
                (target_rtu_id,),
            )
            connection.commit()
            audit.change(
                "fes_rtu_define",
                {"rtu_id": target_rtu_id},
                None,
                rtu_record,
                import_source="efile",
                conflict_mode=conflict_mode,
                imported_counts=imported_counts,
                deleted_counts=deleted_counts,
            )
    except sqlite3.IntegrityError as exc:
        raise ApiError(409, f"导入 E 文件发生数据唯一性冲突：{exc}", details=str(exc), code="conflict") from exc

    return {
        "imported": True,
        "rtu_id": target_rtu_id,
        "channel_id": target_channel_id,
        "channel_type": channel_type,
        "name": rtu_record.get("name") or "",
        "original_rtu_id": orig_rtu_id,
        "original_channel_id": orig_channel_id,
        "remapped": remapped,
        "conflict_mode": conflict_mode,
        "counts": imported_counts,
        "deleted_counts": deleted_counts,
        "total_points": sum(imported_counts.get(t, 0) for t in _POINT_TABLES),
    }

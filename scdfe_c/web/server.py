"""SCADA HTTP API and local web application (Python standard library)."""
from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import parse_qs, unquote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scdfe_c.web.service import ApiError, DEFAULT_DB, SCHEMA_DIR, KIND_TABLES, ScadaStore
else:
    from scdfe_c.web.service import ApiError, DEFAULT_DB, SCHEMA_DIR, KIND_TABLES, ScadaStore

from core.process_registry import PROCESS_TABLE, process_is_alive
from core.web_request import RequestBodyMixin
from scdfe_c.web.process_control import ProcessController


STATIC_DIR = Path(__file__).resolve().parent / "static"
def public_control_row(row):
    return dict(row)


LOG = logging.getLogger("scada.web")


def browser_safe(value):
    """Keep 64-bit KEYIDs exact in JavaScript: large integers become strings."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {key: browser_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [browser_safe(item) for item in value]
    return value


class ScadaHandler(RequestBodyMixin, BaseHTTPRequestHandler):
    server_version = "ScadaWeb/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    @property
    def store(self):
        return self.server.store

    def log_message(self, message, *args):
        LOG.debug(message, *args)

    def _send(self, status, content_type, data, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, payload):
        headers = {}
        if isinstance(payload.get("item"), dict) and "_version" in payload["item"]:
            headers["ETag"] = '"' + str(payload["item"]["_version"]) + '"'
        data = json.dumps(browser_safe(payload), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", data, headers)

    def _if_match(self):
        value = self.headers.get("If-Match")
        if value is None:
            return None
        value = value.strip()
        if re.fullmatch(r'"[1-9][0-9]*"', value):
            value = value[1:-1]
        if not re.fullmatch(r'[1-9][0-9]*', value):
            raise ApiError(400, "If-Match 必须是正整数版本号")
        return int(value)

    def _path(self):
        split = urlsplit(self.path)
        parts = [unquote(p) for p in split.path.strip("/").split("/") if p]
        raw = parse_qs(split.query, keep_blank_values=True, max_num_fields=80)
        if any(len(values) != 1 for values in raw.values()):
            raise ApiError(400, "同一查询参数只能出现一次")
        return parts, {key: values[0] for key, values in raw.items()}

    def _route(self, parts):
        """Return (table, key, scope) for collection or single-record routes."""
        if parts[:1] != ["api"]:
            raise ApiError(404, "路径不存在", code="not_found")
        # RTU and channel are one resource.  Keep the public route explicit so
        # removed channel routes cannot accidentally become table aliases.
        if len(parts) in (2, 3) and parts[1] == "rtus":
            return "fes_rtu_define", parts[2] if len(parts) == 3 else None, None
        if len(parts) in (3, 4) and parts[1] == "points":
            table = KIND_TABLES.get(parts[2])
            if table:
                return table, parts[3] if len(parts) == 4 else None, None
        if len(parts) in (5, 6) and parts[1] == "rtus" and parts[3] == "points":
            table = KIND_TABLES.get(parts[4])
            if table:
                rtu = self.store._key_values("fes_rtu_define", parts[2])["rtu_id"]
                self.store.get_row("fes_rtu_define", [rtu])
                # A scoped item accepts only its point number; never a second RTU.
                point = parts[5] if len(parts) == 6 else None
                if point is not None and "," in point:
                    raise ApiError(400, "RTU 作用域路径只接受单个点号")
                return table, [rtu, point] if point is not None else None, rtu
        if len(parts) in (4, 5) and parts[1] == "tables" and parts[3] == "rows":
            self.store.schema(parts[2])
            return parts[2], parts[4] if len(parts) == 5 else None, None
        raise ApiError(404, "路径不存在", code="not_found")

    def _rtu_id(self, raw):
        return self.store._key_values("fes_rtu_define", raw)["rtu_id"]

    def _channel_endpoint(self, parts):
        """Return (rtu_id, section) for the one-channel-per-RTU API."""
        if len(parts) not in (4, 5) or parts[:2] != ["api", "rtus"] or parts[3] != "channel":
            return None
        rtu_id = self._rtu_id(parts[2])
        self.store.get_row("fes_rtu_define", [rtu_id])
        return rtu_id, parts[4] if len(parts) == 5 else None

    def _scdfe_control(self, parts, query, method):
        """Dedicated control/status/log routes for the scdfe_c supervisor."""
        if parts[:3] not in (["api", "scdfe_c", "status"],
                             ["api", "scdfe_c", "control"],
                             ["api", "scdfe_c", "logs"],
                             ["api", "scdfe_c", "command-server"]):
            return False
        from core.scdfe_control import CONTROL_TABLE, ensure_control_row, log_directory, validate_control
        control_row = ensure_control_row(self.store)
        if parts[:3] == ["api", "scdfe_c", "command-server"]:
            if len(parts) != 3 or query:
                raise ApiError(400, "TCP 指令服务接口不接受子路径或查询参数")
            controller = self.server.process_controller
            if method == "GET":
                # Return configuration and the process-registry row together;
                # callers should not need a second request to determine
                # whether the endpoint is listening.  ``snapshot`` also
                # repairs stale PIDs left by an abrupt child termination.
                snapshot = controller.snapshot()
                process = next(
                    (item for item in snapshot["items"]
                     if item.get("process_key") == "command-server"),
                    None,
                )
                return self._json(200, {"item": controller.command_config(),
                                        "process": process})
            if method != "PATCH":
                raise ApiError(405, "TCP 指令服务配置只支持 GET/PATCH")
            body = self._body()
            allowed = {"bind_host", "bind_port", "auth_token", "log_retention_hours"}
            unknown = set(body) - allowed
            if unknown:
                raise ApiError(400, "TCP 指令服务包含未知配置字段", details=sorted(unknown))
            row = controller.command_config()
            if row.get("process_pid"):
                raise ApiError(409, "请先停止 TCP 指令服务，再修改监听配置", code="command_server_running")
            if "bind_host" in body:
                if not isinstance(body["bind_host"], str) or not body["bind_host"].strip():
                    raise ApiError(400, "bind_host 必须是非空字符串")
                body["bind_host"] = body["bind_host"].strip()
            if "bind_port" in body:
                try:
                    port = int(body["bind_port"])
                except (TypeError, ValueError) as exc:
                    raise ApiError(400, "bind_port 必须是整数") from exc
                if not 1 <= port <= 65535:
                    raise ApiError(400, "bind_port 必须在 1 到 65535 之间")
                body["bind_port"] = port
            if "auth_token" in body:
                if not isinstance(body["auth_token"], str) or not body["auth_token"].strip():
                    raise ApiError(400, "auth_token 必须是非空字符串")
                body["auth_token"] = body["auth_token"].strip()
            if "log_retention_hours" in body:
                try:
                    hours = int(body["log_retention_hours"])
                except (TypeError, ValueError) as exc:
                    raise ApiError(400, "log_retention_hours 必须是整数") from exc
                if not 1 <= hours <= 87600:
                    raise ApiError(400, "log_retention_hours 必须在 1 到 87600 之间")
                body["log_retention_hours"] = hours
            return self._json(200, {"item": self.store.update_row(
                "fes_scdfe_command_define", [1], body, self._if_match()
            )})
        if parts[:3] == ["api", "scdfe_c", "status"]:
            if len(parts) != 3:
                raise ApiError(404, "路径不存在", code="not_found")
            if method != "GET":
                raise ApiError(405, "状态接口只支持 GET")
            if set(query) - {"limit", "offset", "q", "process_status", "process_enable"}:
                raise ApiError(400, "未知状态查询参数")
            control = control_row
            # A Windows console/service stop can terminate the interpreter
            # before its finally block gets a chance to persist state. Repair
            # that small stale window when the Web UI reads status.
            pid = int(control.get("process_pid") or 0)
            if pid and control.get("process_status") in {1, 2, 3, 4}:
                if not process_is_alive(pid):
                    control = self.store.update_row(CONTROL_TABLE, [1], {
                        "process_status": 0, "process_pid": 0,
                        "process_stop_time": int(time.time() * 1000),
                        "process_heartbeat_time": int(time.time() * 1000),
                    })
            result = self._list("fes_rtu_define", {"limit": "50", "sort": "rtu_id", **query}, None)
            rows = result["items"]
            with self.store.connection() as connection:
                counts = dict(connection.execute(
                    "SELECT COUNT(*) AS total, "
                    "COALESCE(SUM(process_status=2),0) AS running, "
                    "COALESCE(SUM(process_status=5),0) AS errors, "
                    "COALESCE(SUM(process_enable=1),0) AS automatic FROM fes_rtu_define"
                ).fetchone())
            # ``process_pid`` is the RTU table's storage column for the native
            # communication-thread ID.  The main-process control row keeps its
            # own ``process_pid`` as the supervisor PID, so expose a distinct
            # API name here and avoid implying that an RTU row contains a
            # process identifier.
            rtus = [{
                "rtu_id": row.get("rtu_id"),
                "name": row.get("name"),
                "_version": row.get("_version"),
                "channel_id": row.get("channel_id"),
                "channel_type": row.get("channel_type"),
                "process_enable": row.get("process_enable"),
                "process_status": row.get("process_status"),
                "thread_id": row.get("process_pid"),
                "process_start_time": row.get("process_start_time"),
                "process_heartbeat_time": row.get("process_heartbeat_time"),
                "process_error": row.get("process_error"),
            } for row in rows]
            return self._json(200, {"item": control, "rtus": rtus, "counts": counts,
                                    "total": result["total"], "limit": result["limit"],
                                    "offset": result["offset"]})
        if parts[:3] == ["api", "scdfe_c", "control"]:
            if len(parts) != 3:
                raise ApiError(404, "路径不存在", code="not_found")
            if method != "PATCH" or query:
                raise ApiError(405, "控制接口只支持 PATCH")
            body = self._body()
            try:
                validate_control(body)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from exc
            if "process_enable" in body:
                if set(body) - {"process_enable"}:
                    raise ApiError(400, "启停请单独提交；运行参数和通信重载使用独立请求")
                operation = self.server.process_controller.submit({
                    "target": "main", "action": "start" if body["process_enable"] else "stop",
                })
                return self._json(202, {"operation": operation,
                                        "item": public_control_row(self.store.get_row(CONTROL_TABLE, [1]))})
            updated = self.server.process_controller.configure_main(body)
            return self._json(200, {"item": public_control_row(updated)})
        if parts[:3] == ["api", "scdfe_c", "logs"]:
            from scdfe_c import ScdfeLogger
            # ``ensure_control_row`` above already loaded the singleton
            # configuration. Reuse it instead of opening a second SQLite
            # connection merely to resolve the log directory on every poll.
            directory = log_directory(self.store, control_row)
            if parts == ["api", "scdfe_c", "logs", "search"]:
                if method != "GET":
                    raise ApiError(405, "日志检索接口只支持 GET")
                from scdfe_c.web.log_query import search_logs
                try:
                    return self._json(200, search_logs(directory, query))
                except ValueError as exc:
                    raise ApiError(400, str(exc)) from exc
            if len(parts) == 3:
                if method != "GET":
                    raise ApiError(405, "日志接口只支持 GET")
                allowed = {"process", "rtu_id"}
                if set(query) - allowed:
                    raise ApiError(400, "未知日志查询参数")
                items = ScdfeLogger.list_files(directory)
                for key in allowed:
                    if key in query:
                        if key == "rtu_id":
                            try:
                                expected = int(query[key])
                            except ValueError as exc:
                                raise ApiError(400, f"{key} 必须是整数") from exc
                        else:
                            expected = query[key]
                        items = [item for item in items if item.get(key) == expected]
                return self._json(200, {"items": items, "total": len(items), "directory": str(directory)})
            if len(parts) >= 4 and method == "GET":
                try:
                    tail = int(query.pop("tail", "200")) if "tail" in query else 200
                except ValueError as exc:
                    raise ApiError(400, "tail 必须是整数") from exc
                if query:
                    raise ApiError(400, "未知日志查询参数")
                try:
                    filename = "/".join(parts[3:])
                    return self._json(200, {"item": ScdfeLogger.read_file(directory, filename, tail=tail)})
                except (ValueError, FileNotFoundError) as exc:
                    raise ApiError(404, str(exc), code="not_found") from exc
            raise ApiError(405, "日志接口只支持 GET")
        return False

    def _list(self, table, query, scope):
        columns = {c["eng_name"]: c for c in self.store.schema(table)["columns"]}
        reserved = {"q", "limit", "offset", "sort", "order", "fields"}
        unknown = set(query) - reserved - set(columns)
        if unknown:
            raise ApiError(400, "未知查询参数", details=sorted(unknown))
        where = {key: value for key, value in query.items() if key not in reserved}
        if scope is not None:
            if "rtu_id" in where and self.store._coerce(where["rtu_id"], columns["rtu_id"]) != scope:
                raise ApiError(400, "RTU 过滤条件与路径不一致")
            where["rtu_id"] = scope
        try:
            limit, offset = int(query.get("limit", "25")), int(query.get("offset", "0"))
        except ValueError as exc:
            raise ApiError(400, "limit 和 offset 必须是整数") from exc
        fields = None
        if "fields" in query:
            fields = [name.strip() for name in query["fields"].split(",")]
        return self.store.list_rows(table, where=where, search=query.get("q", ""),
                                    limit=limit, offset=offset, sort=query.get("sort"),
                                    order=query.get("order", "asc"), fields=fields)

    def _dispatch(self):
        from core.packet_logging import handle_packet_recording
        self._prepare_request()
        parts, query = self._path()
        method = "GET" if self.command == "HEAD" else self.command
        if handle_packet_recording(self, parts, query, method, self.store, 'c'):
            return
        if len(parts) == 6 and parts[:2] == ['api', 'rtus'] and parts[3] == 'commands':
            if method != 'POST':
                raise ApiError(405, '指令下发只支持 POST')
            if query:
                raise ApiError(400, '指令下发不接受查询参数')
            item = self.store.submit_command(self._rtu_id(parts[2]), parts[4], parts[5],
                                             self._body(), self._if_match())
            return self._json(202, {'item': item, 'command_status': 'pending'})
        if parts[:2] == ["api", "processes"]:
            if query:
                raise ApiError(400, "进程接口不接受查询参数")
            if len(parts) == 2 and method == "GET":
                return self._json(200, self.server.process_controller.snapshot())
            if parts == ["api", "processes", "control"] and method == "POST":
                operation = self.server.process_controller.submit(self._body())
                return self._json(202, {"operation": operation})
            raise ApiError(405, "进程接口支持 GET /api/processes 和 POST /api/processes/control")
        # False means unhandled; a handled route returns _json()'s None after
        # writing its response. Do not fall through and append a second 404.
        if self._scdfe_control(parts, query, method) is not False:
            return
        if method == "GET":
            if not parts:
                return self._static("index.html")
            if parts == ["api-docs"]:
                return self._static("api.html")
            if parts == ["favicon.ico"]:
                return self._send(204, "image/x-icon", b"")
            if parts[:1] == ["static"]:
                return self._static("/".join(parts[1:]))
            if parts == ["api", "health"]:
                return self._json(200, {"status": "ok", "database": str(self.store.db_path)})
            if len(parts) == 4 and parts[:2] == ["api", "rtus"] and parts[3] == "delete-preview":
                if query:
                    raise ApiError(400, "删除预览接口不接受查询参数")
                return self._json(200, self.store.describe_rtu_cascade(parts[2]))
            if parts == ["api", "schemas"]:
                items = self.store.schemas()
                return self._json(200, {"items": items, "total": len(items)})
            if len(parts) in (3, 4) and parts[:2] == ["api", "tables"] and (len(parts) == 3 or parts[3] == "schema"):
                return self._json(200, {"schema": self.store.catalog.public(parts[2])})
            if parts == ["api", "overview"]:
                if set(query) - {"rtu_id"}:
                    raise ApiError(400, "未知查询参数")
                rid = self.store._key_values("fes_rtu_define", query["rtu_id"])["rtu_id"] if "rtu_id" in query else None
                return self._json(200, self.store.overview(rid))
            channel_endpoint = self._channel_endpoint(parts)
            if channel_endpoint is not None:
                rtu_id, section = channel_endpoint
                if query:
                    raise ApiError(400, "单个通道查询不接受过滤参数")
                info = self.store.channel_for_rtu(rtu_id)
                if section is None:
                    return self._json(200, info)
                if section == "parameters":
                    return self._json(200, {
                        "parameter_table": info["parameter_table"],
                        "parameter_schema": info["parameter_schema"],
                        "item": info["parameter"],
                    })
                raise ApiError(404, "通道子资源不存在", code="not_found")
        channel_endpoint = self._channel_endpoint(parts)
        if channel_endpoint is None:
            table, key, scope = self._route(parts)
        else:
            table, key, scope = None, None, None
        if method == "GET":
            if key is not None:
                if query:
                    raise ApiError(400, "单条查询不接受过滤参数")
                return self._json(200, {"item": self.store.get_row(table, key)})
            return self._json(200, self._list(table, query, scope))
        if query:
            raise ApiError(400, "写操作不接受查询参数")
        if channel_endpoint is not None:
            rtu_id, section = channel_endpoint
            info = None
            if section not in (None, "parameters"):
                raise ApiError(404, "通道子资源不存在", code="not_found")
            if section is None:
                if method == "POST":
                    return self._json(201, {"item": self.store.create_channel(rtu_id, self._body())})
                if method == "PATCH":
                    return self._json(200, {"item": self.store.update_channel(
                        rtu_id, self._body(), self._if_match()
                    )})
                raise ApiError(405, "通道接口只支持 POST 或 PATCH", code="method_not_allowed")
            info = self.store.channel_for_rtu(rtu_id)
            if section == "parameters":
                parameter_table = info["parameter_table"]
                if not parameter_table:
                    raise ApiError(409, "该通道类型没有参数定义表", code="unsupported_channel_type")
                if method == "POST":
                    return self._json(201, {"item": self.store.create_channel_parameter(
                        rtu_id, self._body()
                    )})
                if info["parameter"] is None:
                    raise ApiError(404, "该通道尚未配置参数定义", code="not_found")
                parameter_key = [info["parameter"][key] for key in self.store.catalog.public(parameter_table)["primary_keys"]]
                if method == "PATCH":
                    return self._json(200, {"item": self.store.update_row(parameter_table, parameter_key, self._body(), self._if_match())})
                if method == "DELETE":
                    self.store.delete_row(parameter_table, parameter_key, self._if_match())
                    return self._json(200, {"deleted": True})
            raise ApiError(405, "该路径不支持此方法", code="method_not_allowed")
        if table in {PROCESS_TABLE, "fes_scdfe_c_define"}:
            raise ApiError(405, "运行控制表仅允许通过专用控制接口修改")
        if method == "POST" and key is None:
            body = self._body()
            if scope is not None:
                col = next(c for c in self.store.schema(table)["columns"] if c["eng_name"] == "rtu_id")
                if "rtu_id" in body and self.store._coerce(body["rtu_id"], col) != scope:
                    raise ApiError(400, "路径中的 RTU 与请求体不一致")
                body["rtu_id"] = scope
            return self._json(201, {"item": self.store.create_row(table, body)})
        if method == "PATCH" and key is not None:
            body = self._body()
            return self._json(200, {"item": self.store.update_row(table, key, body, self._if_match())})
        if method == "DELETE" and key is not None:
            if table == "fes_rtu_define":
                return self._json(200, self.store.delete_rtu_cascade(key, self._if_match()))
            self.store.delete_row(table, key, self._if_match())
            return self._json(200, {"deleted": True})
        raise ApiError(405, "该路径不支持此方法", code="method_not_allowed")

    def _handle(self):
        try:
            self._dispatch()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            LOG.debug("client disconnected")
        except ApiError as error:
            payload = {"code": error.code, "message": error.message}
            if error.details is not None:
                payload["details"] = error.details
            try:
                self._json(error.status, {"error": payload})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                LOG.debug("client disconnected while sending API error")
        except ValueError:
            self._json(400, {"error": {"code": "bad_request", "message": "无效的请求参数"}})
        except sqlite3.OperationalError:
            LOG.exception("database operation failed")
            self._json(503, {"error": {"code": "database_error", "message": "数据库暂不可用，请稍后重试"}})
        except Exception:
            LOG.exception("unhandled request error")
            self._json(500, {"error": {"code": "internal_error", "message": "服务器内部错误，请查看服务日志"}})

    do_GET = do_HEAD = do_POST = do_PATCH = do_DELETE = do_PUT = _handle

    def _static(self, name):
        if name == 'packet-recording.js':
            from core.web_workbench import ASSET_DIR
            return self._send(200, 'application/javascript; charset=utf-8',
                              (ASSET_DIR / name).read_bytes())
        allowed = {"index.html": "text/html", "api.html": "text/html",
                   "app.css": "text/css", "app.js": "application/javascript"}
        if name not in allowed or not (STATIC_DIR / name).is_file():
            raise ApiError(404, "资源不存在", code="not_found")
        self._send(200, allowed[name] + "; charset=utf-8", (STATIC_DIR / name).read_bytes())


class ScadaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR):
        self.store = ScadaStore(db_path, schema_dir, connection_pool_size=4)
        try:
            self.process_controller = ProcessController(self.store)
            super().__init__(address, ScadaHandler)
        except BaseException:
            self.store.close()
            raise

    def server_close(self):
        try:
            super().server_close()
        finally:
            self.store.close()


def make_server(host="127.0.0.1", port=8877, *, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR):
    return ScadaHTTPServer((host, port), db_path=db_path, schema_dir=schema_dir)


def main():
    parser = argparse.ArgumentParser(description="SCADA definitions and RTU data web service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    server = make_server(args.host, args.port, db_path=args.db, schema_dir=args.schema_dir)
    print(f"SCADA web: http://{args.host}:{args.port}/", flush=True)
    print(f"SQLite: {server.store.db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

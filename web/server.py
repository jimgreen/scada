"""SCADA HTTP API and local web application (Python standard library)."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib.parse import parse_qs, unquote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from web.service import ApiError, DEFAULT_DB, SCHEMA_DIR, KIND_TABLES, ScadaStore
else:
    from .service import ApiError, DEFAULT_DB, SCHEMA_DIR, KIND_TABLES, ScadaStore

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _scdfe_popen_kwargs() -> dict[str, object]:
    """Return process-isolation options for the current operating system.

    Windows uses a new process group so a console/service stop can address the
    supervisor independently.  POSIX uses a new session so the supervisor and
    its multiprocessing children do not remain attached to the Web server's
    process group.  Keeping the keyword sets separate avoids passing the
    Windows-only ``creationflags`` argument on Linux and other POSIX systems.
    """
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            "close_fds": True,
        }
    return {
        "start_new_session": True,
        "close_fds": True,
    }


def _process_is_alive(pid: int) -> bool:
    """Probe a process without sending a termination signal on Windows."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True
    # ``os.kill(pid, 0)`` is not a harmless existence probe on Windows on all
    # supported Python builds.  Use a query-only handle and inspect the exit
    # code instead; STILL_ACTIVE (259) means the process is running.
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # Access denied generally means the PID exists but belongs to a
            # process with a stronger token.  Treat it as active to avoid
            # launching a duplicate supervisor; an invalid PID remains false.
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError):
        return False
ROOT_DIR = Path(__file__).resolve().parents[1]
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


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复 JSON 字段: {key}")
        result[key] = value
    return result


class ScadaHandler(BaseHTTPRequestHandler):
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

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "不支持分块请求体")
        if self.headers.get_content_type() != "application/json":
            raise ApiError(415, "请使用 Content-Type: application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(400, "无效的 Content-Length") from exc
        if length < 0 or length > 2_000_000:
            raise ApiError(413, "请求体长度必须在 0 到 2000000 字节之间")
        def invalid_constant(value):
            raise ValueError(f"非法 JSON 数字: {value}")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求体不完整")
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object,
                              parse_constant=invalid_constant)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ApiError(400, "请求体必须是有效 UTF-8 JSON 对象") from exc
        if not isinstance(body, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return body

    def _request_guard(self):
        host = self.headers.get("Host", "")
        hostname = urlsplit("//" + host).hostname
        if self.server.server_address[0] in {"127.0.0.1", "::1"}:
            if hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ApiError(403, "Host 不被允许", code="forbidden")
        if self.command not in {"GET", "HEAD"}:
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise ApiError(403, "请求来源不被允许", code="forbidden")
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + host:
                raise ApiError(403, "请求来源不被允许", code="forbidden")

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
        if parts[:3] != ["api", "scdfe_c", "status"] and parts[:3] != ["api", "scdfe_c", "control"] and parts[:3] != ["api", "scdfe_c", "logs"]:
            return False
        from core.scdfe_control import CONTROL_TABLE, ensure_control_row, log_directory, validate_control
        ensure_control_row(self.store)
        if parts[:3] == ["api", "scdfe_c", "status"]:
            if len(parts) != 3:
                raise ApiError(404, "路径不存在", code="not_found")
            if method != "GET" or query:
                raise ApiError(405 if method != "GET" else 400, "状态接口只支持无参数 GET")
            control = self.store.get_row(CONTROL_TABLE, [1])
            # A Windows console/service stop can terminate the interpreter
            # before its finally block gets a chance to persist state. Repair
            # that small stale window when the Web UI reads status.
            pid = int(control.get("process_pid") or 0)
            if pid and control.get("process_status") in {1, 2, 3, 4}:
                if not _process_is_alive(pid):
                    control = self.store.update_row(CONTROL_TABLE, [1], {
                        "process_status": 0, "process_pid": 0,
                        "process_stop_time": int(time.time() * 1000),
                        "process_heartbeat_time": int(time.time() * 1000),
                    })
            rows = self.store.list_rows("fes_rtu_define", limit=500, sort="rtu_id")["items"]
            rtus = [{key: row.get(key) for key in ("rtu_id", "channel_id", "channel_type", "process_enable", "process_status", "process_pid", "process_start_time", "process_heartbeat_time", "process_error")} for row in rows]
            return self._json(200, {"item": control, "rtus": rtus})
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
            before = self.store.get_row(CONTROL_TABLE, [1])
            updated = self.store.update_row(CONTROL_TABLE, [1], body)
            pid_before = int(before.get("process_pid") or 0)
            pid_active = False
            if pid_before and before.get("process_status") in {1, 2, 3, 4}:
                pid_active = _process_is_alive(pid_before)
            if int(body.get("process_enable", before.get("process_enable", 0))) == 0:
                # The supervisor consumes process_enable=0, joins all channel
                # workers, flushes logs and persists its final state. Do not
                # SIGTERM a Python process here: Windows can terminate it
                # before those finally blocks run.
                if pid_before and pid_active:
                    LOG.info("scdfe_c stop requested pid=%s; waiting for cooperative shutdown", pid_before)
            elif (body.get("process_enable") == 1 or body.get("process_restart") == 1) and not pid_active:
                command = [sys.executable, "-m", "scdfe_c", "--db", str(self.store.db_path),
                           "--schema-dir", str(self.store.catalog.directory)]
                try:
                    subprocess.Popen(command, cwd=str(ROOT_DIR), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     **_scdfe_popen_kwargs())
                except OSError as exc:
                    raise ApiError(503, f"无法启动 scdfe_c: {exc}", code="process_start_failed") from exc
            return self._json(200, {"item": updated})
        if parts[:3] == ["api", "scdfe_c", "logs"]:
            from scdfe_c import ScdfeLogger
            directory = log_directory(self.store)
            if len(parts) == 3:
                if method != "GET":
                    raise ApiError(405, "日志接口只支持 GET")
                allowed = {"process", "rtu_id", "channel_id"}
                if set(query) - allowed:
                    raise ApiError(400, "未知日志查询参数")
                items = ScdfeLogger.list_files(directory)
                for key in allowed:
                    if key in query:
                        if key in {"rtu_id", "channel_id"}:
                            try:
                                expected = int(query[key])
                            except ValueError as exc:
                                raise ApiError(400, f"{key} 必须是整数") from exc
                        else:
                            expected = query[key]
                        items = [item for item in items if item.get(key) == expected]
                return self._json(200, {"items": items, "total": len(items), "directory": str(directory)})
            if len(parts) == 4 and method == "GET":
                try:
                    tail = int(query.pop("tail", "200")) if "tail" in query else 200
                except ValueError as exc:
                    raise ApiError(400, "tail 必须是整数") from exc
                if query:
                    raise ApiError(400, "未知日志查询参数")
                try:
                    return self._json(200, {"item": ScdfeLogger.read_file(directory, parts[3], tail=tail)})
                except (ValueError, FileNotFoundError) as exc:
                    raise ApiError(404, str(exc), code="not_found") from exc
            raise ApiError(405, "日志接口只支持 GET")
        return False

    def _list(self, table, query, scope):
        columns = {c["eng_name"]: c for c in self.store.schema(table)["columns"]}
        reserved = {"q", "limit", "offset", "sort", "order"}
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
        return self.store.list_rows(table, where=where, search=query.get("q", ""),
                                    limit=limit, offset=offset, sort=query.get("sort"),
                                    order=query.get("order", "asc"))

    def _dispatch(self):
        self._request_guard()
        parts, query = self._path()
        method = "GET" if self.command == "HEAD" else self.command
        if self._scdfe_control(parts, query, method):
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
            info = self.store.channel_for_rtu(rtu_id)
            if section is None:
                raise ApiError(405, "通道字段已合并到 RTU 记录，请使用 /api/rtus/{rtu_id}", code="method_not_allowed")
            if section == "parameters":
                parameter_table = info["parameter_table"]
                if not parameter_table:
                    raise ApiError(409, "该通道类型没有参数定义表", code="unsupported_channel_type")
                if method == "POST":
                    body = self._body()
                    body["rtu_id"] = rtu_id
                    if info["parameter"] is not None:
                        raise ApiError(409, "该 RTU 已存在此通道类型的参数定义", code="conflict")
                    return self._json(201, {"item": self.store.create_row(parameter_table, body)})
                if info["parameter"] is None:
                    raise ApiError(404, "该通道尚未配置参数定义", code="not_found")
                parameter_key = [info["parameter"][key] for key in self.store.catalog.public(parameter_table)["primary_keys"]]
                if method == "PATCH":
                    return self._json(200, {"item": self.store.update_row(parameter_table, parameter_key, self._body(), self._if_match())})
                if method == "DELETE":
                    self.store.delete_row(parameter_table, parameter_key, self._if_match())
                    return self._json(200, {"deleted": True})
            raise ApiError(405, "该路径不支持此方法", code="method_not_allowed")
        if method == "POST" and key is None:
            body = self._body()
            if scope is not None:
                col = next(c for c in self.store.schema(table)["columns"] if c["eng_name"] == "rtu_id")
                if "rtu_id" in body and self.store._coerce(body["rtu_id"], col) != scope:
                    raise ApiError(400, "路径中的 RTU 与请求体不一致")
                body["rtu_id"] = scope
            return self._json(201, {"item": self.store.create_row(table, body)})
        if method == "PATCH" and key is not None:
            return self._json(200, {"item": self.store.update_row(table, key, self._body(), self._if_match())})
        if method == "DELETE" and key is not None:
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
        allowed = {"index.html": "text/html", "api.html": "text/html",
                   "app.css": "text/css", "app.js": "application/javascript"}
        if name not in allowed or not (STATIC_DIR / name).is_file():
            raise ApiError(404, "资源不存在", code="not_found")
        self._send(200, allowed[name] + "; charset=utf-8", (STATIC_DIR / name).read_bytes())


class ScadaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR):
        self.store = ScadaStore(db_path, schema_dir)
        super().__init__(address, ScadaHandler)


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

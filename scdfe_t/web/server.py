"""Independent WEB service for scdfe_t four-remote transmission channels."""
from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import parse_qs, unquote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scdfe_c.web.service import ApiError, DEFAULT_DB, SCHEMA_DIR
from scdfe_t.web.service import CHANNEL_TABLE, POINT_TABLE, TransStore
from core.network_limits import LISTEN_BACKLOG
from core.web_request import RequestBodyMixin
from core.web_database import (
    DEFAULT_WORKSPACE_ROOT,
    DatabaseSelectionMixin,
    database_request,
    handle_database,
    resolve_web_database_args,
    selected_database,
    selection_file,
)
from core.workspace_paths import add_workspace_arguments
from core import operation_audit as audit
from core.web_http import (
    BaseScadaHandlerMixin,
    browser_safe as _safe,
    encode_json,
    handle_packet_recording,
    parse_request_path,
    send_static,
)
from core.web_workbench import ASSET_DIR, ASSETS, render_workbench
from core.web_logs import log_browser

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOG = logging.getLogger("scdfe_t.web")


class TransHandler(BaseScadaHandlerMixin, RequestBodyMixin, BaseHTTPRequestHandler):
    server_version = "ScdfeTWeb/1.0"

    @property
    def service(self):
        return self.server.trans_store

    def log_message(self, message, *args):
        LOG.debug(message, *args)

    def _json(self, status, payload):
        if status < 400:
            payload = self.server.public_file_paths(payload)
        audit.response(self, status, payload)
        headers = {}
        item = payload.get("item") if isinstance(payload, dict) else None
        if isinstance(item, dict) and "_version" in item:
            headers["ETag"] = '"' + str(item["_version"]) + '"'
        data = encode_json(_safe(payload))
        self._send_json(status, data, headers)

    def _parts(self):
        return parse_request_path(self, max_num_fields=40)

    @staticmethod
    def _id(raw):
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "channel_id 必须是整数") from exc
        if value <= 0:
            raise ApiError(400, "channel_id 必须是正整数")
        return value

    def _schema(self, table):
        return {"item": self.service.store.catalog.public(table)}

    def _packet_recording(self, parts, query, method):
        return handle_packet_recording(self, parts, query, method, self.service.db_path, "t")

    def _api(self, method, parts, query):
        if parts == ["api", "protocol-capabilities"]:
            if method != 'GET' or query:
                raise ApiError(400, "规约能力接口只接受无参数 GET")
            from core.protocol_capabilities import protocol_capabilities
            return self._json(200, protocol_capabilities('scdfe_t'))
        if self._packet_recording(parts, query, method) is not False:
            return
        if parts in (["api", "log-files"], ["api", "log-files", "search"]):
            if method != "GET":
                raise ApiError(405, "日志文件接口只支持 GET")
            from core.log_paths import packet_log_directory
            runtime_directory = self.service.db_path.parent / "logs"
            return self._json(200, log_browser(runtime_directory, "t", len(parts) == 3, query,
                                               packet_directory=packet_log_directory(runtime_directory)))
        if parts == ["api", "health"]:
            if method != "GET":
                raise ApiError(405, "只支持 GET")
            return self._json(200, {"status": "ok", "service": "scdfe_t", "database": self.server.relative_database_path(self.service.db_path)})
        if parts == ["api", "trans", "schema"]:
            if method != "GET":
                raise ApiError(405, "只支持 GET")
            return self._json(200, {"items": {"channel": self._schema(CHANNEL_TABLE)["item"],
                                               "point": self._schema(POINT_TABLE)["item"]}})
        if parts[:2] != ["api", "trans"]:
            raise ApiError(404, "路径不存在", code="not_found")
        prefix = ["api", "trans"]
        tail = parts[len(prefix):]
        if not tail or tail[0] not in {"channels", "status", "processes", "logs", "log", "source-catalog"}:
            raise ApiError(404, "路径不存在", code="not_found")
        resource = tail[0]
        if resource == "status":
            if method != "GET" or len(tail) != 1 or query:
                raise ApiError(405, "转发运行状态接口只支持 GET")
            return self._json(200, self.service.runtime_status())
        if resource == "source-catalog":
            if method != "GET" or len(tail) != 1:
                raise ApiError(405, "源数据目录只支持 GET")
            if (set(query) - {"kind", "rtu_id", "include_rtus"} or query.get("kind", "") not in {"yc", "yx", "yk", "yt"}
                    or query.get("include_rtus", "1") not in {"0", "1"}):
                raise ApiError(400, "源数据目录参数不正确")
            return self._json(200, self.service.source_catalog(query["kind"], query.get("rtu_id"),
                              include_rtus=query.get("include_rtus", "1") == "1"))
        if resource == "processes":
            if method != "GET" or len(tail) != 1:
                raise ApiError(405, "进程状态只支持 GET")
            return self._json(200, {"items": self.service.processes()})
        if resource == "logs":
            if method != "GET" or len(tail) != 1:
                raise ApiError(405, "日志列表只支持 GET")
            allowed = {"channel_id", "limit", "record"}
            if set(query) - allowed:
                raise ApiError(400, "未知日志查询参数")
            cid = self._id(query["channel_id"]) if query.get("channel_id") else None
            limit = int(query.get("limit", "200"))
            record = query.get("record", "runtime") or "runtime"
            return self._json(200, self.service._logs_for_record(cid, limit, record))
        if resource == "log":
            if method != "GET" or len(tail) != 1:
                raise ApiError(405, "日志内容只支持 GET")
            filename = query.get("filename")
            if not filename:
                raise ApiError(400, "缺少 filename")
            record = query.get("record", "runtime") or "runtime"
            return self._json(200, self.service.read_log(filename, int(query.get("tail", "300")), record=record))
        # channels collection and channel-scoped resources
        if len(tail) == 1:
            if method == "GET":
                if set(query) - {"view"} or query.get("view", "full") not in {"selector", "full"}:
                    raise ApiError(400, "未知通道列表查询参数")
                return self._json(200, {"items": self.service.channels(selector=query.get("view")=="selector")})
            if method == "POST":
                return self._json(201, {"item": self.service.create_channel(self._body())})
            raise ApiError(405, "通道集合只支持 GET/POST")
        cid = self._id(tail[1])
        if len(tail) == 2:
            if method == "GET":
                return self._json(200, {"item": self.service.get_channel(cid)})
            if method == "PATCH":
                return self._json(200, {"item": self.service.update_channel(cid, self._body(), self._if_match())})
            if method == "DELETE":
                self.service.delete_channel(cid, self._if_match())
                return self._json(200, {"deleted": True})
            raise ApiError(405, "通道只支持 GET/PATCH/DELETE")
        action = tail[2]
        if action in {"start", "stop"}:
            if method != "POST" or len(tail) != 3:
                raise ApiError(405, "进程操作只支持 POST")
            return self._json(200, {"item": getattr(self.service, action)(cid)})
        if action != "points":
            raise ApiError(404, "路径不存在", code="not_found")
        if len(tail) == 4 and tail[3] == "batch":
            if method != "POST":
                raise ApiError(405, "批量点映射只支持 POST")
            return self._json(201, {"items": self.service.create_points_batch(cid, self._body())})
        if len(tail) == 3:
            if method == "GET":
                if set(query) - {"kind", "limit", "offset", "name_contains"}:
                    raise ApiError(400, "未知点映射查询参数")
                return self._json(200, self.service.point_page(
                    cid, kind=query.get("kind"), limit=int(query.get("limit", "100")),
                    offset=int(query.get("offset", "0")),
                    name_contains=query.get("name_contains", ""),
                ))
            if method == "POST":
                return self._json(201, {"item": self.service.create_point(cid, self._body())})
            raise ApiError(405, "点映射集合只支持 GET/POST")
        if len(tail) != 5 or not tail[3]:
            raise ApiError(404, "路径不存在", code="not_found")
        try:
            kind = tail[3].lower()
            pnt = int(tail[4])
        except ValueError as exc:
            raise ApiError(400, "pnt_no 必须是整数") from exc
        key = self.service._point_key(cid, kind, pnt)
        if method == "GET":
            return self._json(200, {"item": self.service.get_point(cid, key)})
        if method == "PATCH":
            return self._json(200, {"item": self.service.update_point(cid, key, self._body(), self._if_match())})
        if method == "DELETE":
            self.service.delete_point(cid, key, self._if_match())
            return self._json(200, {"deleted": True})
        raise ApiError(405, "点映射只支持 PATCH/DELETE")

    def _static(self, parts):
        # 默认首页统一渲染 communication.html，消除旧 index.html 8/10 列错位。
        name = "communication.html" if (not parts or parts == ["index.html"]) else "/".join(parts)
        if parts and parts[0] == "static":
            name = "/".join(parts[1:])
            if name == "index.html":
                name = "communication.html"
        if name in ASSETS:
            return send_static(self, ASSET_DIR / name, ASSETS[name])
        allowed = {
            "index.html": "text/html; charset=utf-8", "static/index.html": "text/html; charset=utf-8",
            "communication.html": "text/html; charset=utf-8", "static/communication.html": "text/html; charset=utf-8",
            "processes.html": "text/html; charset=utf-8", "static/processes.html": "text/html; charset=utf-8",
            "logs.html": "text/html; charset=utf-8", "static/logs.html": "text/html; charset=utf-8",
            "channels.html": "text/html; charset=utf-8", "static/channels.html": "text/html; charset=utf-8",
            "points.html": "text/html; charset=utf-8", "static/points.html": "text/html; charset=utf-8",
            "communication.js": "application/javascript; charset=utf-8", "static/communication.js": "application/javascript; charset=utf-8",
            "app.css": "text/css; charset=utf-8", "static/app.css": "text/css; charset=utf-8",
            "app.js": "application/javascript; charset=utf-8", "static/app.js": "application/javascript; charset=utf-8",
            "processes.js": "application/javascript; charset=utf-8", "static/processes.js": "application/javascript; charset=utf-8",
            "logs.js": "application/javascript; charset=utf-8", "static/logs.js": "application/javascript; charset=utf-8",
        }
        key = "/".join(parts) if parts and parts[0] == "static" else name
        path = STATIC_DIR / (name.removeprefix("static/") if name.startswith("static/") else name)
        if key not in allowed or not path.is_file():
            raise ApiError(404, "资源不存在", code="not_found")
        if name.endswith(".html"):
            page_name = path.name
            send_static(
                self,
                path,
                allowed[key],
                transform=lambda data, p=page_name: render_workbench(data, "t", p),
                cache_key=f"t:{page_name}",
            )
            return
        send_static(self, path, allowed[key])

    @database_request
    @audit.audit_request('t')
    def _handle(self):
        try:
            self._prepare_request()
            parts, query = self._parts()
            if handle_database(self, parts, query, 'GET' if self.command == 'HEAD' else self.command) is not False:
                return
            from core.web_diagnostics import handle_diagnostics
            from core.web_commands import handle_commands
            if handle_commands(self,parts,query,'GET' if self.command=='HEAD' else self.command,
                               self.server.trans_store.store,'t') is not False:
                return
            if handle_diagnostics(self,parts,query,'GET' if self.command=='HEAD' else self.command,
                                  self.service.store,'t') is not False:
                return
            from core.web_connections import handle_connections
            if handle_connections(self,parts,query,'GET' if self.command=='HEAD' else self.command,
                                  self.service.store,'t') is not False:
                return
            from core.web_soe import handle_soe
            if handle_soe(self,parts,query,'GET' if self.command=='HEAD' else self.command,
                          self.service.store,'t') is not False:
                return
            if parts == ["favicon.ico"] and self.command in {"GET", "HEAD"}:
                return self._send(204, "image/x-icon", b"")
            if parts and parts[0] == "api":
                return self._api("GET" if self.command == "HEAD" else self.command, parts, query)
            if self.command in {"GET", "HEAD"}:
                return self._static(parts)
            raise ApiError(405, "只支持 GET/HEAD")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            audit.exception()
            return
        except ApiError as error:
            audit.exception()
            self._json(error.status, {"error": {"code": error.code, "message": error.message,
                                                  **({"details": error.details} if error.details is not None else {})}})
        except (ValueError, KeyError) as error:
            audit.exception()
            self._json(400, {"error": {"code": "bad_request", "message": str(error)}})
        except sqlite3.OperationalError:
            audit.exception()
            LOG.exception("database operation failed trace_id=%s", audit.trace_id())
            self._json(503, {"error": {"code": "database_error", "message": "数据库暂不可用"}})
        except Exception:
            audit.exception()
            LOG.exception("unhandled request error trace_id=%s", audit.trace_id())
            self._json(500, {"error": {"code": "internal_error", "message": "服务器内部错误，请查看服务日志"}})

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


class TransHTTPServer(DatabaseSelectionMixin, ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG
    allow_reuse_address = True

    def __init__(self, address, *, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR, selection_path=None, workspace_root=None):
        self._init_database('t', db_path, schema_dir, selection_path, workspace_root)
        try:
            super().__init__(address, TransHandler)
        except BaseException:
            self._resources.close()
            raise

    def server_close(self):
        try:
            super().server_close()
        finally:
            self._resources.close()


def make_server(host="127.0.0.1", port=8879, *, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR, selection_path=None, workspace_root=None):
    return TransHTTPServer((host, port), db_path=db_path, schema_dir=schema_dir, selection_path=selection_path, workspace_root=workspace_root)


def main(argv=None):
    parser = argparse.ArgumentParser(description="scdfe_t four-remote transmission WEB service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8879)
    parser.add_argument("--db", type=Path, default=None, help="数据库路径（相对于工作区根目录）；省略时按三参数或Web选择定位")
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    add_workspace_arguments(parser)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    workspace_root, preference, database = resolve_web_database_args('t', args)
    httpd = make_server(args.host, args.port, db_path=database, schema_dir=args.schema_dir, selection_path=preference, workspace_root=workspace_root)
    print(f"scdfe_t WEB: http://{args.host}:{args.port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

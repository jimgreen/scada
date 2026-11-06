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
from core.web_request import RequestBodyMixin
from core.web_workbench import ASSET_DIR, ASSETS, render_workbench
from core.web_logs import log_browser

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOG = logging.getLogger("scdfe_t.web")


def _safe(value):
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


class TransHandler(RequestBodyMixin, BaseHTTPRequestHandler):
    server_version = "ScdfeTWeb/1.0"

    @property
    def service(self):
        return self.server.trans_store

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, message, *args):
        LOG.debug(message, *args)

    def _send(self, status, content_type, data, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, payload):
        headers = {}
        item = payload.get("item") if isinstance(payload, dict) else None
        if isinstance(item, dict) and "_version" in item:
            headers["ETag"] = '"' + str(item["_version"]) + '"'
        data = json.dumps(_safe(payload), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", data, headers)

    def _if_match(self):
        value = self.headers.get("If-Match")
        if value is None:
            return None
        value = value.strip()
        if re.fullmatch(r'"[1-9][0-9]*"', value):
            value = value[1:-1]
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise ApiError(400, "If-Match 必须是正整数版本号")
        return int(value)

    def _parts(self):
        split = urlsplit(self.path)
        parts = [unquote(item) for item in split.path.strip("/").split("/") if item]
        raw = parse_qs(split.query, keep_blank_values=True, max_num_fields=40)
        if any(len(values) != 1 for values in raw.values()):
            raise ApiError(400, "同一查询参数只能出现一次")
        return parts, {key: values[0] for key, values in raw.items()}

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

    def _api(self, method, parts, query):
        from core.packet_logging import handle_packet_recording
        if handle_packet_recording(self, parts, query, method, self.service.store, 't'):
            return
        if parts in (["api", "log-files"], ["api", "log-files", "search"]):
            if method != "GET":
                raise ApiError(405, "日志文件接口只支持 GET")
            return self._json(200, log_browser(self.service.db_path.parent / "logs", "t", len(parts) == 3, query))
        if parts == ["api", "health"]:
            if method != "GET":
                raise ApiError(405, "只支持 GET")
            return self._json(200, {"status": "ok", "service": "scdfe_t", "database": str(self.service.db_path)})
        if parts == ["api", "trans", "schema"]:
            if method != "GET":
                raise ApiError(405, "只支持 GET")
            return self._json(200, {"items": {"channel": self._schema(CHANNEL_TABLE)["item"],
                                               "point": self._schema(POINT_TABLE)["item"]}})
        if parts[:2] != ["api", "trans"]:
            raise ApiError(404, "路径不存在", code="not_found")
        prefix = ["api", "trans"]
        tail = parts[len(prefix):]
        if not tail or tail[0] not in {"channels", "processes", "logs", "log"}:
            raise ApiError(404, "路径不存在", code="not_found")
        resource = tail[0]
        if resource == "processes":
            if method != "GET" or len(tail) != 1:
                raise ApiError(405, "进程状态只支持 GET")
            return self._json(200, {"items": self.service.processes()})
        if resource == "logs":
            if method != "GET" or len(tail) != 1:
                raise ApiError(405, "日志列表只支持 GET")
            allowed = {"channel_id", "limit"}
            if set(query) - allowed:
                raise ApiError(400, "未知日志查询参数")
            cid = self._id(query["channel_id"]) if query.get("channel_id") else None
            limit = int(query.get("limit", "200"))
            return self._json(200, self.service.logs(cid, limit))
        if resource == "log":
            if method != "GET" or len(tail) != 1:
                raise ApiError(405, "日志内容只支持 GET")
            filename = query.get("filename")
            if not filename:
                raise ApiError(400, "缺少 filename")
            return self._json(200, self.service.read_log(filename, int(query.get("tail", "300"))))
        # channels collection and channel-scoped resources
        if len(tail) == 1:
            if method == "GET":
                return self._json(200, {"items": self.service.channels()})
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
        if len(tail) == 3:
            if method == "GET":
                if set(query) - {"kind", "limit", "offset"}:
                    raise ApiError(400, "未知点映射查询参数")
                return self._json(200, self.service.point_page(
                    cid, kind=query.get("kind"), limit=int(query.get("limit", "100")),
                    offset=int(query.get("offset", "0")),
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
        name = "index.html" if not parts else "/".join(parts)
        if parts and parts[0] == "static":
            name = "/".join(parts[1:])
        if name in ASSETS:
            return self._send(200, ASSETS[name], (ASSET_DIR / name).read_bytes())
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
        data = path.read_bytes()
        if name.endswith(".html"):
            data = render_workbench(data, "t", path.name)
        self._send(200, allowed[key], data)

    def _handle(self):
        try:
            self._prepare_request()
            parts, query = self._parts()
            if parts == ["favicon.ico"] and self.command in {"GET", "HEAD"}:
                return self._send(204, "image/x-icon", b"")
            if parts and parts[0] == "api":
                return self._api("GET" if self.command == "HEAD" else self.command, parts, query)
            if self.command in {"GET", "HEAD"}:
                return self._static(parts)
            raise ApiError(405, "只支持 GET/HEAD")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            return
        except ApiError as error:
            self._json(error.status, {"error": {"code": error.code, "message": error.message,
                                                  **({"details": error.details} if error.details is not None else {})}})
        except (ValueError, KeyError) as error:
            self._json(400, {"error": {"code": "bad_request", "message": str(error)}})
        except sqlite3.OperationalError:
            LOG.exception("database operation failed")
            self._json(503, {"error": {"code": "database_error", "message": "数据库暂不可用"}})
        except Exception:
            LOG.exception("unhandled request error")
            self._json(500, {"error": {"code": "internal_error", "message": "服务器内部错误，请查看服务日志"}})

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


class TransHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR):
        self.trans_store = TransStore(db_path, schema_dir, connection_pool_size=4)
        try:
            super().__init__(address, TransHandler)
        except BaseException:
            self.trans_store.store.close()
            raise

    def server_close(self):
        try:
            self.trans_store.store.close()
        finally:
            super().server_close()


def make_server(host="127.0.0.1", port=8879, *, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR):
    return TransHTTPServer((host, port), db_path=db_path, schema_dir=schema_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(description="scdfe_t four-remote transmission WEB service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8879)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    httpd = make_server(args.host, args.port, db_path=args.db, schema_dir=args.schema_dir)
    print(f"scdfe_t WEB: http://{args.host}:{args.port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

from core.web_database import (
    DatabaseSelectionMixin,
    DEFAULT_WORKSPACE_ROOT,
    database_request,
    handle_database,
    selected_database,
    selection_file,
)
from core.web_http import encode_json, prepare_response, send_static
from core.web_request import RequestBodyMixin
from core.web_workbench import ASSET_DIR, ASSETS, render_workbench
from scdfe_c.web.service import ApiError
from scdfe_d.device_panel import device_catalog, read_device, update_parameters

STATIC = Path(__file__).with_name("static")
LOG = logging.getLogger("scdfe_d.web")


class Handler(RequestBodyMixin, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        LOG.debug(fmt, *args)

    def _send(self, status, content_type, data, headers=None):
        data, extra = prepare_response(self, status, content_type, data, headers)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if "cache-control" not in {key.lower() for key in extra}:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'self'; base-uri 'self'",
        )
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, payload):
        self._send(
            status,
            "application/json; charset=utf-8",
            encode_json(payload),
        )

    def _api(self, method, parts, query):
        store, collector = self.server.store, self.server.collector
        if parts == ["api", "health"] and method == "GET":
            return self._json(200, dict(status="ok", module="scdfe_d"))
        if parts == ["api", "overview"] and method == "GET":
            return self._json(200, dict(store.overview(), collector=collector.status()))
        if parts == ["api", "devices"]:
            if method == "GET":
                return self._json(200, dict(items=store.devices(query)))
            if method == "POST":
                return self._json(201, dict(item=store.create_device(self._body())))
        if parts == ["api", "device-tables"] and method == "GET":
            return self._json(200, dict(items=store.device_tables()))
        if parts == ["api", "device-catalog"] and method == "GET":
            return self._json(200, device_catalog(store))
        if parts == ["api", "device-measurements"] and method == "GET":
            view = query.get("view", "full")
            if view not in ("full", "live"):
                raise ApiError(400, "量测视图无效")
            return self._json(
                200,
                read_device(
                    store, query.get("table"), query.get("idx"), live=view == "live"
                ),
            )
        if parts == ["api", "device-parameters"] and method == "PUT":
            return self._json(200, update_parameters(store, self._body()))
        if parts == ["api", "model", "import"] and method == "POST":
            self.server._assert_database_idle()
            return self._json(200, store.import_model(self._body()["path"]))
        if (
            len(parts) == 4
            and parts[:2] == ["api", "devices"]
            and parts[3] == "values"
            and method == "GET"
        ):
            device_id = int(parts[2])
            with store.connection() as con:
                device = con.execute(
                    "SELECT model_table,model_idx FROM device WHERE device_id=?",
                    (device_id,),
                ).fetchone()
                if not device:
                    raise ApiError(404, "设备不存在")
                row = con.execute(
                    f"SELECT * FROM {store._table_name(device['model_table'])} WHERE idx=?",
                    (device["model_idx"],),
                ).fetchone()
            return self._json(200, dict(item=dict(row)))
        if parts == ["api", "source-points"] and method == "GET":
            return self._json(200, store.source_points(query))
        if parts == ["api", "measurements"]:
            if method == "GET":
                return self._json(200, store.measurements(query))
            if method == "POST":
                return self._json(201, dict(item=store.save_measurement(self._body())))
        if len(parts) == 3 and parts[:2] == ["api", "measurements"]:
            if method == "PUT":
                return self._json(
                    200, dict(item=store.save_measurement(self._body(), int(parts[2])))
                )
            if method == "DELETE":
                store.delete_measurement(int(parts[2]), self._body()["version"])
                return self._json(200, dict(deleted=True))
        if parts == ["api", "history"] and method == "GET":
            return self._json(200, store.history())
        if parts == ["api", "collector", "interval"] and method == "PUT":
            body = self._body()
            if set(body) != {"interval_seconds"}:
                raise ApiError(400, "需要 interval_seconds 字段")
            return self._json(200, collector.set_interval(body["interval_seconds"]))
        if len(parts) == 3 and parts[:2] == ["api", "collector"] and method == "POST":
            body = self._body()
            if parts[2] == "start":
                return self._json(200, collector.start(body.get("interval_seconds")))
            if parts[2] == "stop":
                collector.close()
                return self._json(200, collector.status())
            if parts[2] == "once":
                return self._json(200, collector.once())
        raise ApiError(404, "接口不存在")

    @database_request
    def _handle(self):
        try:
            self._prepare_request()
            url = urlsplit(self.path)
            parts = [unquote(v) for v in url.path.strip("/").split("/") if v]
            raw = parse_qs(url.query, keep_blank_values=True, max_num_fields=30)
            if any(len(v) != 1 for v in raw.values()):
                raise ApiError(400, "查询参数不能重复")
            query = {k: v[0] for k, v in raw.items()}
            method = "GET" if self.command == "HEAD" else self.command
            if handle_database(self, parts, query, method) is not False:
                return
            if parts[:1] == ["api"]:
                return self._api(method, parts, query)
            if method != "GET":
                raise ApiError(405, "此路径只支持 GET")
            if len(parts) == 2 and parts[0] == "static":
                if parts[1] in ASSETS:
                    return send_static(self, ASSET_DIR / parts[1], ASSETS[parts[1]])
                if parts[1] in ("app.css", "app.js"):
                    return send_static(
                        self,
                        STATIC / parts[1],
                        "text/css; charset=utf-8"
                        if parts[1].endswith("css")
                        else "application/javascript; charset=utf-8",
                    )
            pages = {
                "/": "overview.html",
                "/devices": "measurements.html",
                "/measurements": "measurements.html",
                "/parameters": "parameters.html",
                "/mappings": "mappings.html",
                "/history": "history.html",
            }
            if url.path in pages:
                page_name = pages[url.path]
                return send_static(
                    self,
                    STATIC / "index.html",
                    "text/html; charset=utf-8",
                    transform=lambda data, p=page_name: render_workbench(data, "d", p),
                    cache_key=f"d:{page_name}",
                )
            if parts == ["favicon.ico"]:
                return self._send(204, "image/x-icon", b"")
            raise ApiError(404, "页面不存在")
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
        ):
            return
        except ApiError as exc:
            self._json(exc.status, dict(error=dict(message=exc.message, code=exc.code)))
        except (ValueError, TypeError, KeyError) as exc:
            self._json(400, dict(error=dict(message="参数无效：" + str(exc))))
        except sqlite3.Error:
            LOG.exception("database error")
            self._json(
                503, dict(error=dict(message="数据库暂不可用，请重试或检查服务日志"))
            )
        except Exception:
            LOG.exception("request failed")
            self._json(500, dict(error=dict(message="服务器内部错误，请检查服务日志")))

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _handle


class DeviceHTTPServer(DatabaseSelectionMixin, ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, db_path, workspace_root=None, selection_path=None):
        self._init_database("d", db_path, None, selection_path, workspace_root)
        try:
            super().__init__(address, Handler)
        except BaseException:
            self._resources.close()
            raise

    def server_close(self):
        try:
            self._resources.close()
        finally:
            super().server_close()


def make_server(host="127.0.0.1", port=8980, **kwargs):
    return DeviceHTTPServer((host, port), **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description="scdfe_d 设备量测 Web 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8980)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--workspace-root", type=Path, default=DEFAULT_WORKSPACE_ROOT)
    parser.add_argument(
        "--collect", action="store_true", help="启动 Web 时同时启动采集"
    )
    parser.add_argument(
        "--model", help="导入设备模型（根目录相对路径）；只导入标识，保留已有映射"
    )
    args = parser.parse_args(argv)
    preference = selection_file("d", args.host, args.port)
    database = selected_database(
        preference,
        args.db,
        args.workspace_root / "scdfe_d.sqlite3",
        workspace_root=args.workspace_root,
    )
    logging.basicConfig(level=logging.INFO)
    server = make_server(
        args.host,
        args.port,
        db_path=database,
        workspace_root=args.workspace_root,
        selection_path=preference,
    )
    try:
        if args.model:
            server.store.import_model(args.model)
        if args.collect:
            server.collector.start()
        print(f"scdfe_d WEB: http://{args.host}:{args.port}/", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

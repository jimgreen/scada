"""SCADA 综合网站 HTTP 服务（Python 标准库，默认端口 8981）。"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import sys
from urllib.parse import parse_qs, unquote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.network_limits import LISTEN_BACKLOG
from core.web_database import DEFAULT_WORKSPACE_ROOT
from core.web_http import BaseScadaHandlerMixin, browser_safe, encode_json, prepare_response
from core.web_workbench import ASSET_DIR as SHARED_ASSET_DIR
from core.workspace_paths import add_workspace_arguments
from scada_portal.service import PortalService

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOG = logging.getLogger("scada.portal")

# 允许内嵌 SVG 的 style 属性（例如 qinling.svg 中的 writing-mode / display），保持 script-src 严格为 'self'
PORTAL_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
)

_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class PortalHandler(BaseScadaHandlerMixin, BaseHTTPRequestHandler):
    @property
    def service(self) -> PortalService:
        return self.server.service

    def log_message(self, message, *args):
        LOG.debug(message, *args)

    def _send_bytes(self, status: int, content_type: str, data: bytes, headers: dict[str, str] | None = None):
        payload, merged = prepare_response(self, status, content_type, data, headers)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", PORTAL_CSP)
        if content_type.startswith("application/json"):
            self.send_header("Cache-Control", "no-store")
        for key, value in merged.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, payload: dict):
        data = encode_json(browser_safe(payload))
        self._send_bytes(status, "application/json; charset=utf-8", data)

    def _serve_file(self, file_path: Path):
        if not file_path.is_file():
            return self._json(404, {"error": "资源不存在"})
        ext = file_path.suffix.lower()
        ctype = _MIME_TYPES.get(ext, "application/octet-stream")
        cache = "public, max-age=300" if ext in {".jpg", ".jpeg", ".png", ".webp", ".svg"} else "no-cache"
        self._send_bytes(200, ctype, file_path.read_bytes(), {"Cache-Control": cache})

    def _safe_under(self, base: Path, rel: str) -> Path | None:
        candidate = (base / rel).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            return None
        return candidate

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = {k: v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=False).items()}

        try:
            # 静态页面与前端资源
            if path in {"/", "/index.html", "/energy-flow", "/powerflow", "/devices", "/comm", "/logs"}:
                return self._serve_file(STATIC_DIR / "index.html")
            if path.startswith("/static/"):
                rel = path.removeprefix("/static/")
                local_file = self._safe_under(STATIC_DIR, rel)
                if local_file is not None and local_file.is_file():
                    return self._serve_file(local_file)
                shared_file = self._safe_under(SHARED_ASSET_DIR, rel)
                if shared_file is not None and shared_file.is_file():
                    return self._serve_file(shared_file)
                return self._json(404, {"error": "静态资源不存在"})

            # API 路由
            if path == "/api/health":
                return self._json(200, {
                    "status": "ok",
                    "module": "scada_portal",
                    "scene": f"{self.service.runtime_name}/{self.service.scene_name}",
                    "svg_ready": self.service.svg_path.is_file(),
                    "c_db_ready": self.service.c_db.is_file(),
                    "d_db_ready": self.service.d_db.is_file(),
                })
            if path == "/api/overview":
                return self._json(200, self.service.get_overview())
            if path == "/api/energy-flow":
                return self._json(200, self.service.get_energy_flow())
            if path == "/api/svg":
                svg_bytes = self.service.get_svg_bytes()
                return self._send_bytes(200, "image/svg+xml; charset=utf-8", svg_bytes, {"Cache-Control": "no-cache"})
            if path == "/api/powerflow":
                return self._json(200, self.service.get_powerflow())
            if path == "/api/devices":
                return self._json(200, self.service.get_devices(
                    category=query.get("category"),
                    search=query.get("search"),
                ))
            parts = [p for p in path.split("/") if p]
            if len(parts) == 5 and parts[:2] == ["api", "devices"] and parts[4] == "trend":
                table_name = parts[2]
                idx = int(parts[3])
                hours = float(query.get("hours", "24"))
                start_ms = int(query["start_ms"]) if query.get("start_ms") else None
                end_ms = int(query["end_ms"]) if query.get("end_ms") else None
                return self._json(
                    200,
                    self.service.get_device_trend(
                        table_name, idx, hours=hours, start_ms=start_ms, end_ms=end_ms
                    ),
                )
            if path == "/api/comm/rtus":
                return self._json(200, self.service.get_comm_rtus())
            if path == "/api/comm/channels":
                return self._json(200, self.service.get_comm_channels())
            if path == "/api/comm/points":
                rtu_val = int(query["rtu_id"]) if query.get("rtu_id") else None
                return self._json(200, self.service.get_comm_points(
                    kind=query.get("kind", "yc"),
                    rtu_id=rtu_val,
                    search=query.get("search"),
                    limit=int(query.get("limit", "50")),
                    offset=int(query.get("offset", "0")),
                ))
            if path == "/api/logs/files":
                return self._json(200, self.service.list_log_files(record=query.get("record", "runtime")))
            if path == "/api/logs/read":
                return self._json(200, self.service.read_logs(
                    record=query.get("record", "runtime"),
                    filename=query.get("filename"),
                    process=query.get("process"),
                    level=query.get("level"),
                    direction=query.get("direction"),
                    keyword=query.get("keyword"),
                    limit=int(query.get("limit", "300")),
                ))
            return self._json(404, {"error": "接口路径不存在"})
        except FileNotFoundError as exc:
            return self._json(404, {"error": str(exc)})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            LOG.exception("Portal API error on %s", path)
            return self._json(500, {"error": f"内部错误：{exc}"})

    def do_PUT(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > 65536:
                return self._json(400, {"error": "请求体大小超出限制"})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return self._json(400, {"error": f"请求体 JSON 格式错误：{exc}"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "请求体须为 JSON 对象"})

            parts = [p for p in path.split("/") if p]
            if len(parts) == 5 and parts[:2] == ["api", "devices"] and parts[4] == "limits":
                table_name = parts[2]
                idx = int(parts[3])
                result = self.service.update_device_measurement_limits(
                    table_name,
                    idx,
                    str(body.get("metric") or ""),
                    body.get("max_value"),
                    body.get("min_value"),
                )
                return self._json(200, result)
            return self._json(404, {"error": "接口路径不存在"})
        except FileNotFoundError as exc:
            return self._json(404, {"error": str(exc)})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            LOG.exception("Portal PUT error on %s", path)
            return self._json(500, {"error": f"内部错误：{exc}"})


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = LISTEN_BACKLOG

    def __init__(self, server_address, handler_class, *, service: PortalService):
        self.service = service
        super().__init__(server_address, handler_class)

    def server_close(self):
        try:
            self.service.close()
        finally:
            super().server_close()


def make_server(
    host: str = "127.0.0.1",
    port: int = 8981,
    *,
    workspace_root: Path | str = DEFAULT_WORKSPACE_ROOT,
    runtime_name: str = "realtime",
    scene_name: str = "qinling",
    c_db: Path | str | None = None,
    d_db: Path | str | None = None,
    model_db: Path | str | None = None,
    svg_path: Path | str | None = None,
    log_dir: Path | str | None = None,
    history_db: Path | str | None = None,
    sample_interval_sec: float = 5.0,
    enable_sampler: bool = True,
) -> PortalServer:
    service = PortalService(
        workspace_root=workspace_root,
        runtime_name=runtime_name,
        scene_name=scene_name,
        c_db=c_db,
        d_db=d_db,
        model_db=model_db,
        svg_path=svg_path,
        log_dir=log_dir,
        history_db=history_db,
        sample_interval_sec=sample_interval_sec,
        enable_sampler=enable_sampler,
    )
    return PortalServer((host, port), PortalHandler, service=service)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="南极秦岭站综合能源 SCADA Web 门户")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8981)
    parser.add_argument("--sample-interval", type=float, default=5.0, help="设备日曲线采样周期（秒）")
    add_workspace_arguments(parser)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    server = make_server(
        args.host,
        args.port,
        workspace_root=args.workspace_root or DEFAULT_WORKSPACE_ROOT,
        runtime_name=args.runtime_name or "realtime",
        scene_name=args.scene_name or "qinling",
        sample_interval_sec=args.sample_interval,
    )
    LOG.info("SCADA Portal listening on http://%s:%s (scene=%s/%s)", args.host, server.server_port, args.runtime_name, args.scene_name)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

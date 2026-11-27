"""SCADA 综合网站 HTTP 服务（Python 标准库，默认端口 8981）。"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
from pathlib import Path
import queue
import sqlite3
import sys
from urllib.parse import parse_qs, unquote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.network_limits import LISTEN_BACKLOG
from core.web_database import DEFAULT_WORKSPACE_ROOT
from core.web_http import BaseScadaHandlerMixin, browser_safe, encode_json, load_static, prepare_response
from core.web_workbench import ASSET_DIR as SHARED_ASSET_DIR
from core.workspace_paths import add_workspace_arguments
from scada_portal.service import PortalService

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOG = logging.getLogger("scada.portal")
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1


def _parse_int(raw: str | None, name: str, *, default: int | None = None, min_val: int = _INT64_MIN, max_val: int = _INT64_MAX) -> int | None:
    if raw is None or not str(raw).strip():
        return default
    try:
        val = int(str(raw).strip(), 10)
    except ValueError as exc:
        raise ValueError(f"参数 {name} 必须为整数") from exc
    if val < min_val or val > max_val:
        raise ValueError(f"参数 {name} 超出有效范围")
    return val


def _parse_float(raw: str | None, name: str, *, default: float) -> float:
    if raw is None or not str(raw).strip():
        return default
    try:
        val = float(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"参数 {name} 必须为有效数值") from exc
    if not math.isfinite(val):
        raise ValueError(f"参数 {name} 必须为有限数值")
    return val

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
        has_cc = any(k.lower() == "cache-control" for k in merged)
        if content_type.startswith("application/json") and not has_cc:
            self.send_header("Cache-Control", "no-store")
        for key, value in merged.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, payload: dict, headers: dict[str, str] | None = None):
        data = encode_json(browser_safe(payload))
        self._send_bytes(status, "application/json; charset=utf-8", data, headers)

    def _serve_file(self, file_path: Path):
        if not file_path.is_file():
            return self._json(404, {"error": "资源不存在"})
        ext = file_path.suffix.lower()
        ctype = _MIME_TYPES.get(ext, "application/octet-stream")
        cache = "public, max-age=300" if ext in {".jpg", ".jpeg", ".png", ".webp", ".svg"} else "no-cache"
        raw, gzipped, etag = load_static(file_path)
        headers = {"Cache-Control": cache, "ETag": etag}
        if self.headers.get("If-None-Match") == etag:
            self._send_bytes(304, ctype, b"", headers)
            return
        if gzipped is not None and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
            self._send_bytes(200, ctype, gzipped, headers)
            return
        self._send_bytes(200, ctype, raw, headers)

    def _safe_under(self, base: Path, rel: str) -> Path | None:
        if "\x00" in rel:
            return None
        try:
            candidate = (base / rel).resolve()
            candidate.relative_to(base.resolve())
        except (ValueError, OSError):
            return None
        return candidate

    def _method_not_allowed(self):
        self._json(405, {"error": "不支持该 HTTP 请求方法"}, {"Allow": "GET, HEAD, POST, PUT"})

    def _read_json_body(self) -> dict:
        length = _parse_int(self.headers.get("Content-Length"), "Content-Length", default=0, min_val=0)
        if length > 65536:
            raise OverflowError("请求体大小超出限制")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体 JSON 格式错误") from exc
        if not isinstance(body, dict):
            raise ValueError("请求体须为 JSON 对象")
        return body

    def do_POST(self):
        parsed = urlsplit(self.path)
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        path = "/" + "/".join(parts) if parts else "/"
        try:
            if path == "/api/alarms/ack":
                body = self._read_json_body()
                keys = body.get("keys")
                if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)):
                    raise ValueError("keys 必须为字符串数组")
                ack_all = bool(body.get("ack_all"))
                operator = str(body.get("operator") or "")
                note = str(body.get("note") or "")
                return self._json(200, self.service.ack_alarms(keys=keys, ack_all=ack_all, operator=operator, note=note))
            if path == "/api/maintenance":
                body = self._read_json_body()
                table_name = str(body.get("table_name") or "").strip()
                idx_val = _parse_int(str(body.get("idx")) if body.get("idx") is not None else None, "idx", min_val=0)
                if idx_val is None:
                    raise ValueError("设备编号 idx 不能为空")
                hours_val = _parse_float(
                    str(body.get("hours")) if body.get("hours") is not None else None,
                    "hours",
                    default=8.0,
                )
                return self._json(
                    200,
                    self.service.set_maintenance(
                        table_name=table_name,
                        idx=idx_val,
                        device_name=str(body.get("device_name") or ""),
                        reason=str(body.get("reason") or ""),
                        operator=str(body.get("operator") or ""),
                        hours=hours_val,
                    ),
                )
            if len(parts) == 5 and parts[:2] == ["api", "devices"] and parts[4] == "control":
                body = self._read_json_body()
                ctrl = body.get("control")
                if not isinstance(ctrl, str) or not ctrl.strip():
                    raise ValueError("控调属性名不能为空")
                if "value" not in body:
                    raise ValueError("下发指令需要 value 字段")
                table_name = parts[2]
                idx = _parse_int(parts[3], "idx", min_val=0)
                result = self.service.dispatch_device_control(
                    table_name, idx, ctrl.strip(), body.get("value")
                )
                return self._json(200, result)
            return self._method_not_allowed()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            LOG.debug("Client disconnected during POST %s", path)
            return
        except FileNotFoundError as exc:
            return self._json(404, {"error": str(exc)})
        except OverflowError as exc:
            return self._json(413, {"error": str(exc)})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except sqlite3.Error:
            LOG.exception("Portal DB error on POST %s", path)
            return self._json(503, {"error": "数据库繁忙或暂不可用，请重试"})
        except Exception:
            LOG.exception("Portal POST error on %s", path)
            return self._json(500, {"error": "服务器内部错误，请查看运行日志"})

    def do_DELETE(self):
        parsed = urlsplit(self.path)
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        try:
            if len(parts) == 3 and parts[:2] == ["api", "maintenance"] and parts[2]:
                return self._json(200, self.service.clear_maintenance(parts[2]))
            return self._method_not_allowed()
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception:
            LOG.exception("Portal DELETE error on %s", parsed.path)
            return self._json(500, {"error": "服务器内部错误，请查看运行日志"})

    def _serve_sse(self):
        """建立轻量 SSE 实时事件流，客户端断开或服务关闭时自动回收。"""
        q = self.service.event_bus.subscribe()
        if q is None:
            return self._json(503, {"error": "实时订阅连接数已达上限"})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(
                self.service.event_bus.format_sse("hello", {"version": self.service.event_bus.version})
            )
            self.wfile.flush()
            while not self.service._stop_event.is_set():
                try:
                    event_name, payload = q.get(timeout=15.0)
                    self.wfile.write(self.service.event_bus.format_sse(event_name, payload))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            pass
        finally:
            self.service.event_bus.unsubscribe(q)

    def do_PATCH(self):
        self._method_not_allowed()

    def do_OPTIONS(self):
        self._method_not_allowed()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlsplit(self.path)
        raw_parts = [unquote(p) for p in parsed.path.split("/") if p]
        path = "/" + "/".join(raw_parts) if raw_parts else "/"
        try:
            query = {
                k: v[-1]
                for k, v in parse_qs(parsed.query, keep_blank_values=False, max_num_fields=64).items()
            }
        except ValueError:
            return self._json(400, {"error": "查询参数数量过多或格式无效"})

        try:
            # 静态页面与前端资源
            if path in {"/", "/index.html", "/cockpit", "/dispatch", "/energy-flow", "/powerflow", "/devices", "/alarms", "/comm", "/logs"}:
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
                    "collector_running": self.service.is_collector_running(),
                    "sse_clients": self.service.event_bus.subscriber_count,
                    "history": self.service.get_history_summary(),
                })
            if path == "/api/events":
                return self._serve_sse()
            if path == "/api/overview":
                return self._json(200, self.service.get_overview())
            if path == "/api/polar-cockpit":
                hours = _parse_float(query.get("hours"), "hours", default=6.0)
                return self._json(200, self.service.get_polar_cockpit(hours=hours))
            if path == "/api/energy-flow":
                return self._json(200, self.service.get_energy_flow())
            if path == "/api/svg":
                if not self.service.svg_path.is_file():
                    raise FileNotFoundError(f"未找到潮流图 SVG 文件：{self.service.svg_path}")
                return self._serve_file(self.service.svg_path)
            if path == "/api/powerflow":
                return self._json(200, self.service.get_powerflow())
            if path == "/api/powerflow/meta":
                etag, payload = self.service.get_powerflow_meta()
                headers = {"Cache-Control": "no-cache", "ETag": etag}
                if self.headers.get("If-None-Match") == etag:
                    self._send_bytes(304, "application/json; charset=utf-8", b"", headers)
                    return
                return self._json(200, payload, headers)
            if path == "/api/powerflow/live":
                return self._json(200, self.service.get_powerflow_live())
            if path == "/api/devices":
                return self._json(200, self.service.get_devices(
                    category=query.get("category"),
                    search=query.get("search"),
                ))
            parts = raw_parts
            if len(parts) == 5 and parts[:2] == ["api", "devices"] and parts[4] == "trend":
                table_name = parts[2]
                idx = _parse_int(parts[3], "idx", min_val=0)
                hours = _parse_float(query.get("hours"), "hours", default=24.0)
                start_ms = _parse_int(query.get("start_ms"), "start_ms", min_val=0)
                end_ms = _parse_int(query.get("end_ms"), "end_ms", min_val=0)
                metric = query.get("metric")
                date_str = query.get("date")
                compare = query.get("compare")
                return self._json(
                    200,
                    self.service.get_device_trend(
                        table_name,
                        idx,
                        hours=hours,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        metric=metric,
                        date_str=date_str,
                        compare=compare,
                    ),
                )
            if path == "/api/alarms":
                hide_maint = query.get("hide_maintenance") in {"1", "true", "yes"}
                return self._json(200, self.service.get_alarms(
                    alarm_type=query.get("type"),
                    acked=query.get("acked"),
                    category=query.get("category"),
                    search=query.get("search"),
                    hide_maintenance=hide_maint,
                ))
            if path == "/api/maintenance":
                return self._json(200, self.service.list_maintenance())
            if path == "/api/controls/audit":
                limit_val = _parse_int(query.get("limit"), "limit", default=50, min_val=1, max_val=200)
                return self._json(200, self.service.get_control_audit(limit=limit_val))
            if path == "/api/comm/topology":
                return self._json(200, self.service.get_comm_topology())
            if path == "/api/report/daily":
                fmt = (query.get("format") or "json").lower()
                date_str = query.get("date")
                ctype, data = self.service.build_daily_report(date_str=date_str, fmt=fmt)
                if isinstance(data, dict):
                    return self._json(200, data)
                extra_headers = {}
                if fmt == "csv":
                    fname = f"scada-daily-report-{date_str or 'today'}.csv"
                    extra_headers["Content-Disposition"] = f'attachment; filename="{fname}"'
                return self._send_bytes(200, ctype, data, extra_headers)
            if path == "/api/alarms/history":
                limit_val = _parse_int(query.get("limit"), "limit", default=300, min_val=10, max_val=1000)
                return self._json(200, self.service.get_alarm_history(
                    alarm_type=query.get("type"),
                    state=query.get("state"),
                    search=query.get("search"),
                    limit=limit_val,
                ))
            if path == "/api/trends/compare":
                raw_targets = query.get("targets", "").strip()
                targets = []
                if raw_targets:
                    for item in raw_targets.split(","):
                        seg = item.strip().split(":")
                        if len(seg) != 3 or not seg[0] or not seg[2]:
                            raise ValueError("对比曲线目标格式应为 Table:idx:metric")
                        targets.append((seg[0], _parse_int(seg[1], "idx", min_val=0), seg[2]))
                hours = _parse_float(query.get("hours"), "hours", default=24.0)
                return self._json(200, self.service.compare_trends(targets, hours=hours))
            if path == "/api/comm/rtus":
                return self._json(200, self.service.get_comm_rtus())
            if path == "/api/comm/channels":
                return self._json(200, self.service.get_comm_channels())
            if path == "/api/comm/points":
                rtu_val = _parse_int(query.get("rtu_id"), "rtu_id", min_val=0)
                limit_val = _parse_int(query.get("limit"), "limit", default=50, min_val=1, max_val=200)
                offset_val = _parse_int(query.get("offset"), "offset", default=0, min_val=0)
                return self._json(200, self.service.get_comm_points(
                    kind=query.get("kind", "yc"),
                    rtu_id=rtu_val,
                    search=query.get("search"),
                    limit=limit_val,
                    offset=offset_val,
                ))
            if path == "/api/logs/files":
                return self._json(200, self.service.list_log_files(record=query.get("record", "runtime")))
            if path == "/api/weather":
                return self._json(200, self.service.get_weather_state())
            if path == "/api/logs/read":
                limit_val = _parse_int(query.get("limit"), "limit", default=300, min_val=10, max_val=1000)
                return self._json(200, self.service.read_logs(
                    record=query.get("record", "runtime"),
                    filename=query.get("filename"),
                    process=query.get("process"),
                    level=query.get("level"),
                    direction=query.get("direction"),
                    keyword=query.get("keyword"),
                    limit=limit_val,
                ))
            return self._json(404, {"error": "接口路径不存在"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            LOG.debug("Client disconnected during GET %s", path)
            return
        except FileNotFoundError as exc:
            return self._json(404, {"error": str(exc)})
        except (ValueError, OverflowError) as exc:
            return self._json(400, {"error": str(exc)})
        except sqlite3.Error:
            LOG.exception("Portal DB error on %s", path)
            return self._json(503, {"error": "数据库暂不可用，请稍后重试"})
        except Exception:
            LOG.exception("Portal API error on %s", path)
            return self._json(500, {"error": "服务器内部错误，请查看运行日志"})

    def do_PUT(self):
        parsed = urlsplit(self.path)
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        path = "/" + "/".join(parts) if parts else "/"
        try:
            if path == "/api/weather":
                body = self._read_json_body()
                return self._json(200, self.service.update_weather_profile(body))
            if not (len(parts) == 5 and parts[:2] == ["api", "devices"] and parts[4] == "limits"):
                return self._json(404, {"error": "接口路径不存在"})
            length = _parse_int(self.headers.get("Content-Length"), "Content-Length", default=0, min_val=0)
            if length > 65536:
                return self._json(413, {"error": "请求体大小超出限制"})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._json(400, {"error": "请求体 JSON 格式错误"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "请求体须为 JSON 对象"})
            metric = body.get("metric")
            if not isinstance(metric, str) or not metric.strip():
                return self._json(400, {"error": "量测属性名不能为空"})
            table_name = parts[2]
            idx = _parse_int(parts[3], "idx", min_val=0)
            result = self.service.update_device_measurement_limits(
                table_name,
                idx,
                metric.strip(),
                body.get("max_value"),
                body.get("min_value"),
            )
            return self._json(200, result)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            LOG.debug("Client disconnected during PUT %s", path)
            return
        except FileNotFoundError as exc:
            return self._json(404, {"error": str(exc)})
        except (ValueError, OverflowError) as exc:
            return self._json(400, {"error": str(exc)})
        except sqlite3.Error:
            LOG.exception("Portal DB error on PUT %s", path)
            return self._json(503, {"error": "数据库繁忙或暂不可用，请重试"})
        except Exception:
            LOG.exception("Portal PUT error on %s", path)
            return self._json(500, {"error": "服务器内部错误，请查看运行日志"})


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
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

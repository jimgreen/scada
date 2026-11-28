import argparse
from contextlib import closing
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import time
from urllib.parse import parse_qs, unquote, urlsplit

from core.web_database import (
    DatabaseSelectionMixin,
    DEFAULT_WORKSPACE_ROOT,
    database_request,
    handle_database,
    resolve_web_database_args,
    selected_database,
    selection_file,
)
from core.workspace_paths import add_workspace_arguments
from core import operation_audit as audit
from core.scdfe_logger import ScdfeLogger
from core.web_http import BaseScadaHandlerMixin, encode_json, send_static
from core.web_request import RequestBodyMixin
from core.web_workbench import ASSET_DIR, ASSETS, render_workbench
from core.common import ApiError
from scada_portal.history_store import ensure_extended_history_schema
from scada_portal.report import (
    POLAR_WEATHER_PRESETS,
    compute_polar_weather_coupling,
    default_weather_profile,
    merge_effective_weather,
    validate_weather_payload,
)
from scdfe_d.device_panel import device_catalog, read_device, update_parameters

STATIC = Path(__file__).with_name("static")
LOG = logging.getLogger("scdfe_d.web")


def _d_weather_history_db(store) -> Path:
    """定位与 scada_portal 共享的南极气象工况持久化库。"""
    return store.db_path.parent / "scada_portal_history.db"


def _d_extract_measured_weather(store) -> tuple[dict, float]:
    """从 scdfe_d.db 实时宽表提取现场风速、辐照度、环境气温均值与实时负荷估算。"""
    wind_speeds: list[float] = []
    irradiances: list[float] = []
    temperatures: list[float] = []
    total_load_kw = 0.0
    if store.db_path.is_file():
        try:
            with closing(sqlite3.connect(f"{store.db_path.as_uri()}?mode=ro", uri=True, timeout=5.0)) as conn:
                tables = {
                    r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                for tbl, col, bucket in (
                    ("ACWindGen", "wind_speed", wind_speeds),
                    ("ACPVGen", "irradiance", irradiances),
                    ("DCPVGen", "irradiance", irradiances),
                    ("ACWindGen", "temperature", temperatures),
                    ("ACPVGen", "temperature", temperatures),
                    ("DCPVGen", "temperature", temperatures),
                ):
                    if tbl not in tables:
                        continue
                    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()}
                    if col not in cols:
                        continue
                    for (val,) in conn.execute(f'SELECT "{col}" FROM "{tbl}" WHERE "{col}" IS NOT NULL').fetchall():
                        try:
                            bucket.append(float(val))
                        except (TypeError, ValueError):
                            pass
                for tbl in ("ACLoad", "DCLoad"):
                    if tbl not in tables:
                        continue
                    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()}
                    if "p" not in cols:
                        continue
                    for (val,) in conn.execute(f'SELECT "p" FROM "{tbl}" WHERE "p" IS NOT NULL').fetchall():
                        try:
                            v = float(val)
                            if v > 0:
                                total_load_kw += v
                        except (TypeError, ValueError):
                            pass
        except sqlite3.Error:
            LOG.debug("scdfe_d measured weather read fallback", exc_info=True)
    measured = {
        "wind_speed_ms": round(sum(wind_speeds) / len(wind_speeds), 2) if wind_speeds else None,
        "irradiance_wm2": round(sum(irradiances) / len(irradiances), 1) if irradiances else None,
        "temperature_c": round(sum(temperatures) / len(temperatures), 1) if temperatures else None,
    }
    return measured, (round(total_load_kw, 1) if total_load_kw > 0 else None)


def _d_load_weather_profile(store) -> dict:
    prof = default_weather_profile()
    hdb = _d_weather_history_db(store)
    if not hdb.is_file():
        return prof
    try:
        with closing(sqlite3.connect(hdb, timeout=5.0)) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='polar_weather_profile'"
            ).fetchone()
            if not exists:
                return prof
            row = conn.execute(
                "SELECT mode, preset, temperature_c, wind_speed_ms, irradiance_wm2, "
                "pressure_hpa, icing_level, snow_condition, updated_ms, operator "
                "FROM polar_weather_profile WHERE id=1"
            ).fetchone()
            if row is not None:
                mode = row[0] or "measured"
                if mode == "measured":
                    prof.update({
                        "mode": "measured",
                        "preset": "live",
                        "temperature_c": None,
                        "wind_speed_ms": None,
                        "irradiance_wm2": None,
                        "pressure_hpa": None,
                        "icing_level": None,
                        "snow_condition": None,
                        "updated_ms": int(row[8] or 0),
                        "operator": row[9] or "",
                    })
                else:
                    prof.update({
                        "mode": mode,
                        "preset": row[1] or "custom",
                        "temperature_c": row[2],
                        "wind_speed_ms": row[3],
                        "irradiance_wm2": row[4],
                        "pressure_hpa": float(row[5]) if row[5] is not None else None,
                        "icing_level": int(row[6]) if row[6] is not None else None,
                        "snow_condition": row[7] if row[7] else None,
                        "updated_ms": int(row[8] or 0),
                        "operator": row[9] or "",
                    })
    except sqlite3.Error:
        LOG.debug("scdfe_d weather profile read fallback", exc_info=True)
    return prof


def _d_get_weather_state(store) -> dict:
    measured, load_kw = _d_extract_measured_weather(store)
    profile = _d_load_weather_profile(store)
    effective = merge_effective_weather(measured, profile)
    coupling = compute_polar_weather_coupling(
        effective=effective,
        wind_rated_kw=None,
        pv_rated_kw=None,
        base_load_kw=load_kw,
        base_autonomy_h=None,
    )
    return {
        "measured": measured,
        "profile": profile,
        "effective": effective,
        "coupling": coupling,
        "presets": list(POLAR_WEATHER_PRESETS),
    }


def _d_update_weather_profile(store, body: dict) -> dict:
    current = _d_load_weather_profile(store)
    norm = validate_weather_payload(body, current)
    hdb = _d_weather_history_db(store)
    hdb.parent.mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    with closing(sqlite3.connect(hdb, timeout=10.0)) as conn:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_extended_history_schema(conn)
        conn.execute(
            """
            INSERT INTO polar_weather_profile (
                id, mode, preset, temperature_c, wind_speed_ms, irradiance_wm2,
                pressure_hpa, icing_level, snow_condition, updated_ms, operator
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                mode=excluded.mode,
                preset=excluded.preset,
                temperature_c=excluded.temperature_c,
                wind_speed_ms=excluded.wind_speed_ms,
                irradiance_wm2=excluded.irradiance_wm2,
                pressure_hpa=excluded.pressure_hpa,
                icing_level=excluded.icing_level,
                snow_condition=excluded.snow_condition,
                updated_ms=excluded.updated_ms,
                operator=excluded.operator
            """,
            (
                norm["mode"],
                norm["preset"],
                norm["temperature_c"],
                norm["wind_speed_ms"],
                norm["irradiance_wm2"],
                norm["pressure_hpa"],
                norm["icing_level"],
                norm["snow_condition"],
                now_ms,
                norm["operator"],
            ),
        )
        conn.commit()
    return _d_get_weather_state(store)


class Handler(BaseScadaHandlerMixin, RequestBodyMixin, BaseHTTPRequestHandler):
    security_csp = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "object-src 'none'; frame-ancestors 'self'; base-uri 'self'"
    )
    security_frame_options = "SAMEORIGIN"

    @property
    def store(self):
        return self.server.store

    def log_message(self, fmt, *args):
        LOG.debug(fmt, *args)

    def _json(self, status, payload):
        audit.response(self, status, payload)
        self._send_json(status, encode_json(payload))

    def _api(self, method, parts, query):
        store, collector = self.server.store, self.server.collector
        if parts == ["api", "health"] and method == "GET":
            return self._json(200, dict(status="ok", module="scdfe_d"))
        if parts == ["api", "weather"]:
            if method == "GET":
                return self._json(200, _d_get_weather_state(store))
            if method == "PUT":
                return self._json(200, _d_update_weather_profile(store, self._body()))
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
            return self._json(200, dict(item=store.device_values(device_id)))
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
                body = self._body()
                if "version" not in body or type(body["version"]) is not int or body["version"] < 1:
                    raise ApiError(400, "删除量测需要正整数 version 字段")
                store.delete_measurement(int(parts[2]), body["version"])
                return self._json(200, dict(deleted=True))
        if parts == ["api", "controls"]:
            if method == "GET":
                return self._json(200, store.controls(query))
            if method == "POST":
                return self._json(201, dict(item=store.save_control(self._body())))
        if len(parts) == 3 and parts[:2] == ["api", "controls"]:
            if method == "PUT":
                return self._json(
                    200, dict(item=store.save_control(self._body(), int(parts[2])))
                )
            if method == "DELETE":
                body = self._body()
                if "version" not in body or type(body["version"]) is not int or body["version"] < 1:
                    raise ApiError(400, "删除控调映射需要正整数 version 字段")
                store.delete_control(int(parts[2]), body["version"])
                return self._json(200, dict(deleted=True))
        if (
            len(parts) == 4
            and parts[:2] == ["api", "controls"]
            and parts[3] == "dispatch"
            and method == "POST"
        ):
            body = self._body()
            if "value" not in body:
                raise ApiError(400, "下发指令需要 value 字段")
            result = store.dispatch_control(int(parts[2]), body["value"], source="web")
            if collector.running:
                collector._wake.set()
            return self._json(200, result)
        if parts == ["api", "sync-bindings"] and method == "POST":
            body = self._body()
            return self._json(
                200,
                store.sync_bindings_from_source(
                    scope=body.get("scope", "all"),
                    rtu_id=body.get("rtu_id"),
                    dry_run=bool(body.get("dry_run", True)),
                    source_database=body.get("source_database") or None,
                ),
            )
        if parts == ["api", "control-history"] and method == "GET":
            return self._json(200, store.control_history())
        if parts == ["api", "logs", "files"] and method == "GET":
            return self._json(
                200, dict(items=store.list_log_files(record=query.get("record", "runtime")))
            )
        if parts == ["api", "logs", "read"] and method == "GET":
            return self._json(200, store.read_logs(query))
        if parts == ["api", "history"] and method == "GET":
            return self._json(200, store.history())
        if parts == ["api", "collector", "interval"] and method == "PUT":
            body = self._body()
            if set(body) != {"interval_seconds"}:
                raise ApiError(400, "需要 interval_seconds 字段")
            return self._json(200, collector.set_interval(body["interval_seconds"]))
        if len(parts) == 3 and parts[:2] == ["api", "collector"] and method == "POST":
            if parts[2] == "start":
                content_len = int(self.headers.get("Content-Length") or "0")
                body = self._body() if content_len > 0 else {}
                return self._json(200, collector.start(body.get("interval_seconds")))
            if parts[2] == "stop":
                collector.close()
                return self._json(200, collector.status())
            if parts[2] == "once":
                return self._json(200, collector.once())
        raise ApiError(404, "接口不存在")

    @database_request
    @audit.audit_request("d")
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
            from core.web_diagnostics import handle_diagnostics
            if handle_diagnostics(self, parts, query, method, self.store, "d") is not False:
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
                "/controls": "controls.html",
                "/control-mappings": "control-mappings.html",
                "/history": "history.html",
                "/logs": "logs.html",
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
            audit.exception()
            self._json(exc.status, dict(error=dict(message=exc.message, code=exc.code)))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            audit.exception()
            self._json(400, dict(error=dict(message="参数无效：" + str(exc))))
        except sqlite3.Error as exc:
            audit.exception()
            LOG.exception("database error")
            self._log_file_error(f"database error: {exc}")
            self._json(
                503, dict(error=dict(message="数据库暂不可用，请重试或检查服务日志"))
            )
        except Exception as exc:
            audit.exception()
            LOG.exception("request failed")
            self._log_file_error(f"request failed: {exc}")
            self._json(500, dict(error=dict(message="服务器内部错误，请检查服务日志")))

    def _log_file_error(self, message: str) -> None:
        try:
            log_dir = self.store.db_path.parent / "logs"
            cached = getattr(self.server, "_web_error_logger", None)
            if cached is None or cached[0] != log_dir:
                cached = (log_dir, ScdfeLogger(log_dir, process_name="scdfe_d-web"))
                self.server._web_error_logger = cached
            cached[1].error(message)
        except Exception:
            pass

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
            super().server_close()
        finally:
            self._resources.close()


def make_server(host="127.0.0.1", port=8980, **kwargs):
    return DeviceHTTPServer((host, port), **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description="scdfe_d 设备量测 Web 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8980)
    parser.add_argument("--db", type=Path, default=None, help="数据库路径（相对于工作区根目录）；省略时按三参数或Web选择定位")
    add_workspace_arguments(parser)
    parser.add_argument(
        "--source", type=str, default=None, help="源 FES 数据库相对路径，省略时默认优先同级 ../fes/scdfe_c.db，其次同目录 scdfe_c.db"
    )
    parser.add_argument(
        "--collect", action="store_true", help="启动 Web 时同时启动采集"
    )
    parser.add_argument(
        "--model", help="导入设备模型（根目录相对路径）；只导入标识，保留已有映射"
    )
    args = parser.parse_args(argv)
    workspace_root, preference, database = resolve_web_database_args("d", args)
    logging.basicConfig(level=logging.INFO)
    server = make_server(
        args.host,
        args.port,
        db_path=database,
        workspace_root=workspace_root,
        selection_path=preference,
    )
    try:
        if args.source:
            server.store.set_source_database(args.source)
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

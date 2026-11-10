"""SCADA HTTP API and local web application (Python standard library)."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sqlite3
import sys
import threading
from urllib.parse import parse_qs, unquote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scdfe_c.web.service import ApiError, SCHEMA_DIR, KIND_TABLES
from scdfe_s.database import DEFAULT_DB, SimulatorStore

from scdfe_s.web.iec104_service import IEC104Service, IEC104ServiceError
from scdfe_s.web.protocol_service import (
    BacnetServerService,
    CanServerService,
    CanService,
    ModbusRtuService,
    ModbusRtuServerService,
    ModbusTcpService,
    ModbusTcpServerService,
    ProtocolServiceError,
    OpcServerService,
)

from core.network_limits import LISTEN_BACKLOG
from core.web_request import RequestBodyMixin
from core.service_log import ServiceFileLog
from core.web_workbench import ASSET_DIR, ASSETS, render_workbench
from core.web_logs import log_browser
from core.runtime_status import runtime_status_rows

STATIC_DIR = Path(__file__).resolve().parent / "static"
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
    # Responses carry Content-Length, so HTTP/1.1 keep-alive is safe and
    # avoids a new TCP handshake for each protocol-monitor refresh.
    protocol_version = "HTTP/1.1"
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
        if len(parts) in (2, 3) and parts[1] in {"rtus", "channels"}:
            return "fes_rtu_define", parts[2] if len(parts) == 3 else None, None
        if len(parts) in (3, 4) and parts[1] == "points":
            table = KIND_TABLES.get(parts[2])
            if table:
                return table, parts[3] if len(parts) == 4 else None, None
        if len(parts) in (5, 6) and parts[1] in {"rtus", "channels"} and parts[3] == "points":
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
        if len(parts) in (4, 5) and parts[:2] == ["api", "rtus"] and parts[3] == "channel":
            rtu_id = self._rtu_id(parts[2])
            self.store.get_row("fes_rtu_define", [rtu_id])
            return rtu_id, parts[4] if len(parts) == 5 else None
        # ``channels/{rtu_id}/parameters`` is the multi-channel UI spelling;
        # retain the RTU resource spelling above for the existing API.
        if len(parts) in (4, 5) and parts[:2] == ["api", "channels"] and parts[3] == "parameters":
            rtu_id = self._rtu_id(parts[2])
            self.store.get_row("fes_rtu_define", [rtu_id])
            return rtu_id, "parameters" if len(parts) == 4 else parts[4]
        return None

    def _packet_recording(self, parts, query, method):
        if parts[:2] != ["api", "packet-recording"]:
            return False
        if query:
            raise ApiError(400, "报文记录策略接口不接受查询参数")
        from core.packet_logging import packet_log_switch, packet_recording_state
        switch = packet_log_switch(self.store.db_path, "s")
        if len(parts) == 2:
            if method == "GET":
                return self._json(200, packet_recording_state(self.store.db_path, "s"))
            if method != "PUT":
                raise ApiError(405, "报文记录策略只支持 GET/PUT")
            body = self._body()
            if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
                raise ApiError(400, "enabled 必须是布尔值，且不能包含其他字段")
            switch.set_enabled(body["enabled"])
            return self._json(200, packet_recording_state(self.store.db_path, "s"))
        if len(parts) == 3:
            raw_channel = parts[2]
        elif len(parts) == 4 and parts[2] == "channels":
            raw_channel = parts[3]
        else:
            raise ApiError(404, "路径不存在", code="not_found")
        try:
            channel_id = int(raw_channel)
        except ValueError as exc:
            raise ApiError(400, "channel_id 必须是整数") from exc
        if channel_id <= 0:
            raise ApiError(400, "channel_id 必须是正整数")
        if method == "GET":
            try:
                item = switch.channel_snapshot(channel_id)
            except KeyError as exc:
                raise ApiError(404, str(exc), code="not_found") from exc
            return self._json(200, item)
        if method != "PUT":
            raise ApiError(405, "通道报文记录策略只支持 GET/PUT")
        body = self._body()
        if set(body) != {"enabled"} or (body["enabled"] is not None and type(body["enabled"]) is not bool):
            raise ApiError(400, "enabled 必须是布尔值或 null，且不能包含其他字段")
        try:
            switch.channel_snapshot(channel_id)
            switch.set_channel(channel_id, body["enabled"])
            item = switch.channel_snapshot(channel_id)
        except KeyError as exc:
            raise ApiError(404, str(exc), code="not_found") from exc
        return self._json(200, item)

    def _service_map(self):
        return {
            "iec104": self.server.iec104_service,
            "modbus-tcp": self.server.modbus_tcp_service,
            "modbus-tcp-server": self.server.modbus_tcp_server_service,
            "modbus-rtu": self.server.modbus_rtu_service,
            "modbus-rtu-server": self.server.modbus_rtu_server_service,
            "can": self.server.can_service,
            "can-server": self.server.can_server_service,
            "opc-server": self.server.opc_server_service,
            "bacnet-server": self.server.bacnet_server_service,
        }

    def _channel_service_map(self):
        """Return one protocol service instance for every configured RTU.

        The protocol pages still expose the historical singleton service APIs,
        but process/communication monitoring must operate at channel scope.
        Keeping the instances on the HTTP server makes the lifecycle stable
        across the five-second polling performed by the Web UI.
        """
        return self.server.channel_services()

    def _monitor_control(self, parts, query, method):
        """Aggregated pages for process, communications and runtime logs."""
        if not parts[:2] == ["api", "monitor"]:
            return False
        services = self._service_map()
        channel_services = (
            self._channel_service_map()
            if len(parts) >= 3 and parts[2] in {"processes", "communications"}
            else {}
        )
        if len(parts) == 5 and parts[2] in {"processes", "communications"} and parts[4] in {"start", "stop"}:
            if method != "POST" or query:
                raise ApiError(405, "进程控制接口只支持 POST")
            slug = parts[3]
            service = channel_services.get(slug) or services.get(slug)
            if service is None:
                raise ApiError(404, "服务不存在", code="not_found")
            action = parts[4]
            return self._json(200, {"item": service.start() if action == "start" else service.stop()})
        if parts == ["api", "monitor", "processes"]:
            if method != "GET" or query:
                raise ApiError(405, "进程监控接口只支持 GET")
            items = []
            for slug, service in channel_services.items():
                status = service.status()
                items.append({"name": slug, "label": status.get("label", slug),
                              "rtu_id": status.get("rtu_id"),
                              "running": bool(status.get("running")),
                              "status": status})
            return self._json(200, {"items": items, "total": len(items)})
        if parts == ["api", "monitor", "communications"]:
            if method != "GET" or query:
                raise ApiError(405, "通信监控接口只支持 GET")
            runtime_by_channel = {
                int(row["channel_id"]): row
                for row in runtime_status_rows(self.store, service_name="scdfe_s")
            }
            # Resolve every RTU-to-channel relation in one read.  The monitor
            # is polled every five seconds; opening one SQLite read connection
            # per protocol service scales poorly once several RTUs are
            # configured.
            with self.store.connection() as connection:
                channel_by_rtu = {
                    int(row[0]): int(row[1])
                    for row in connection.execute(
                        "SELECT rtu_id, channel_id FROM fes_rtu_define"
                    )
                }
            items = []
            for slug, service in channel_services.items():
                status = service.status()
                rtu_id = status.get("rtu_id", status.get("config", {}).get("effective_rtu_id"))
                runtime = runtime_by_channel.get(channel_by_rtu.get(int(rtu_id))) if rtu_id is not None else None
                items.append({"name": slug, "label": status.get("label", slug),
                              "running": bool(status.get("running")),
                              "rtu_id": rtu_id,
                              "connections": status.get("connections", []),
                              "open_connections": status.get("open_connections", 0),
                              "point_count": status.get("point_count", 0),
                              "last_error": status.get("last_error"),
                              "rx_count": status.get("rx_count", 0),
                              "tx_count": status.get("tx_count", 0),
                              "error_count": status.get("error_count", 0),
                              "reconnect_count": status.get("reconnect_count", 0),
                              "last_rx_time": status.get("last_rx_time", 0),
                              "last_tx_time": status.get("last_tx_time", 0),
                              "last_error_time": status.get("last_error_time", 0),
                              "diagnostic": status.get("diagnostic", {}),
                              "runtime_status": runtime})
            return self._json(200, {"items": items, "total": len(items)})
        if parts == ["api", "monitor", "logs"]:
            allowed = {"limit", "service", "level", "keyword", "record"}
            if method != "GET" or set(query) - allowed:
                raise ApiError(405 if method != "GET" else 400, "日志监控接口参数不正确")
            try:
                limit = max(1, min(int(query.get("limit", "200")), 1000))
            except ValueError as exc:
                raise ApiError(400, "limit 必须是整数") from exc
            record = query.get("record", "runtime") or "runtime"
            if record not in {"runtime", "packet"}:
                raise ApiError(400, "记录类型必须是 runtime 或 packet")
            selected = [query["service"]] if query.get("service") else list(services)
            unknown = [name for name in selected if name not in services]
            if unknown:
                raise ApiError(400, "未知服务: " + ", ".join(unknown))
            level = query.get("level", "").upper()
            keyword = query.get("keyword", "")
            items = []
            for slug in selected:
                for item in services[slug].logs(limit, record):
                    row = {"service": slug, **item}
                    if level and str(row.get("level", "")).upper() != level:
                        continue
                    if keyword and keyword.lower() not in str(row.get("message", "")).lower():
                        continue
                    items.append(row)
            # A channel service owns the durable file, while the historical
            # protocol singleton may still be the object selected by this
            # aggregate endpoint.  Fall back to the bounded file search so a
            # keyword query sees packets and runtime records emitted by the
            # live channel process immediately.
            if keyword and not items:
                from core.log_paths import packet_log_directory
                from scdfe_c.web.log_query import search_logs
                try:
                    durable = search_logs(
                        self.store.db_path.parent / "logs",
                        # Runtime files from older channel workers may carry
                        # a packet line with an INFO header.  A keyword
                        # monitor search should still surface that durable
                        # evidence; an explicit packet view remains strict.
                        {"q": keyword, "record": "" if record == "runtime" else record,
                         "level": level,
                         "limit": str(limit)},
                        family="s",
                        packet_directory=packet_log_directory(self.store.db_path.parent / "logs"),
                    )
                except ValueError as exc:
                    raise ApiError(400, str(exc)) from exc
                for hit in durable.get("items", []):
                    process = str(hit.get("process") or "")
                    items.append({
                        "service": process.removeprefix("scdfe_s-"),
                        "time": hit.get("time"),
                        "level": hit.get("level"),
                        "message": hit.get("text", ""),
                        "filename": hit.get("filename"),
                    })
            items.sort(key=lambda row: row.get("time", ""), reverse=True)
            return self._json(200, {"items": items[:limit], "total": len(items[:limit])})
        raise ApiError(404, "监控接口路径不存在", code="not_found")

    def _iec104_control(self, parts, query, method):
        """IEC104 测试服务的配置、生命周期、四遥和诊断接口。"""
        if parts[:2] != ["api", "iec104"]:
            return False
        service = self.server.iec104_service
        if parts == ["api", "iec104", "status"]:
            if method != "GET" or query:
                raise ApiError(405, "IEC104 状态接口只支持 GET")
            return self._json(200, {"item": service.status()})
        if parts == ["api", "iec104", "logs"]:
            if method != "GET":
                raise ApiError(405, "IEC104 日志接口只支持 GET")
            if set(query) - {"limit", "record"}:
                raise ApiError(400, "未知 IEC104 日志查询参数")
            record = query.get("record", "runtime") or "runtime"
            try:
                items = service.logs(int(query.get("limit", "200")), record)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from exc
            return self._json(200, {"items": items, "record": record})
        if parts == ["api", "iec104", "alerts"]:
            if method != "GET":
                raise ApiError(405, "IEC104 告警接口只支持 GET")
            if set(query) - {"limit"}:
                raise ApiError(400, "未知 IEC104 告警查询参数")
            return self._json(200, {"items": service.alerts(int(query.get("limit", "100")))})
        if parts == ["api", "iec104", "connections"]:
            if method != "GET" or query:
                raise ApiError(405, "IEC104 连接接口只支持 GET")
            return self._json(200, {"items": service.status()["connections"]})
        if parts == ["api", "iec104", "commands"]:
            if method != "GET" or query:
                raise ApiError(405, "IEC104 命令接口只支持 GET")
            return self._json(200, {"items": service.commands()})
        if parts == ["api", "iec104", "start"]:
            if method != "POST" or query:
                raise ApiError(405, "IEC104 启动接口只支持 POST")
            return self._json(200, {"item": service.start()})
        if parts == ["api", "iec104", "stop"]:
            if method != "POST" or query:
                raise ApiError(405, "IEC104 停止接口只支持 POST")
            return self._json(200, {"item": service.stop()})
        if parts == ["api", "iec104", "config"]:
            if query:
                raise ApiError(400, "IEC104 配置接口不接受查询参数")
            if method == "GET":
                return self._json(200, {"item": service.config()})
            if method == "PATCH":
                return self._json(200, {"item": service.configure(self._body())})
            raise ApiError(405, "IEC104 配置接口只支持 GET 或 PATCH")
        if len(parts) == 3 and parts[2] == "points":
            if method == "GET":
                if set(query) - {"kind", "view"} or query.get("view","full") not in {"full","table"}:
                    raise ApiError(400, "未知 IEC104 点查询参数")
                return self._json(200, {"items": service.points(query.get("kind"), table_view=query.get("view")=="table")})
            if method == "POST":
                if query:
                    raise ApiError(400, "IEC104 点定义不接受查询参数")
                return self._json(201, {"item": service.define_point(self._body())})
            raise ApiError(405, "IEC104 点定义接口只支持 GET 或 POST")
        if len(parts) == 5 and parts[2] == "points":
            kind, raw_point = parts[3], parts[4]
            try:
                point_no = int(raw_point)
            except ValueError as exc:
                raise ApiError(400, "点号必须是整数") from exc
            if method == "GET" and not query:
                table = {"yc": "fes_yc_define", "yx": "fes_yx_define", "yk": "fes_yk_define", "yt": "fes_yt_define"}.get(kind)
                if table is None:
                    raise ApiError(400, "kind 必须是 yc、yx、yk 或 yt")
                config = service.config()
                rtu_id = config.get("effective_rtu_id")
                if rtu_id is None:
                    raise ApiError(404, "没有配置 IEC104 RTU", code="not_found")
                return self._json(200, {"item": self.store.get_row(table, [rtu_id, point_no])})
            if query:
                raise ApiError(400, "IEC104 点定义写操作不接受查询参数")
            if method == "PATCH":
                body = self._body()
                if_match = self._if_match()
                if if_match is not None:
                    body["_if_match"] = if_match
                return self._json(200, {"item": service.update_point(kind, point_no, body)})
            if method == "DELETE":
                service.delete_point(kind, point_no, self._if_match())
                return self._json(200, {"deleted": True})
            raise ApiError(405, "IEC104 单点定义只支持 GET、PATCH 或 DELETE")
        if parts == ["api", "iec104", "update", "incremental"]:
            if method != "POST" or query:
                raise ApiError(405, "增量更新接口只支持 POST")
            body = self._body()
            try:
                return self._json(200, {"item": service.incremental_update(
                    body["kind"], int(body["pnt_no"]), body["value"]
                )})
            except KeyError as exc:
                raise ApiError(400, f"缺少字段: {exc.args[0]}") from exc
        if parts == ["api", "iec104", "update", "full"]:
            if method != "POST" or query:
                raise ApiError(405, "全量更新接口只支持 POST")
            body = self._body()
            return self._json(200, {"item": service.full_update(body.get("values", body))})
        if len(parts) == 5 and parts[2] == "commands":
            if method != "POST" or query:
                raise ApiError(405, "IEC104 测试命令接口只支持 POST")
            body = self._body()
            try:
                return self._json(200, {"item": service.simulate_command(
                    parts[3], int(parts[4]), body["value"]
                )})
            except KeyError as exc:
                raise ApiError(400, f"缺少字段: {exc.args[0]}") from exc
        raise ApiError(404, "IEC104 接口路径不存在", code="not_found")

    def _protocol_control(self, parts, query, method):
        """Shared lifecycle, point, command and diagnostic API for protocol pages."""
        services = {
            "modbus-tcp": self.server.modbus_tcp_service,
            "modbus-tcp-server": self.server.modbus_tcp_server_service,
            "modbus-rtu": self.server.modbus_rtu_service,
            "modbus-rtu-server": self.server.modbus_rtu_server_service,
            "can": self.server.can_service,
            "can-server": self.server.can_server_service,
            "opc-server": self.server.opc_server_service,
            "bacnet-server": self.server.bacnet_server_service,
        }
        if len(parts) < 3 or parts[:1] != ["api"] or parts[1] not in services:
            return False
        slug, service = parts[1], services[parts[1]]
        if parts == ["api", slug, "status"]:
            if method != "GET" or query: raise ApiError(405, "状态接口只支持 GET")
            return self._json(200, {"item": service.status()})
        if parts == ["api", slug, "config"]:
            if query: raise ApiError(400, "配置接口不接受查询参数")
            if method == "GET": return self._json(200, {"item": service.config()})
            if method == "PATCH": return self._json(200, {"item": service.configure(self._body())})
            raise ApiError(405, "配置接口只支持 GET 或 PATCH")
        if parts in (["api", slug, "start"], ["api", slug, "stop"]):
            if method != "POST" or query: raise ApiError(405, "启停接口只支持 POST")
            return self._json(200, {"item": service.start() if parts[-1] == "start" else service.stop()})
        if parts == ["api", slug, "logs"]:
            if method != "GET": raise ApiError(405, "日志接口只支持 GET")
            if set(query) - {"limit", "record"}: raise ApiError(400, "未知日志查询参数")
            record = query.get("record", "runtime") or "runtime"
            try:
                items = service.logs(int(query.get("limit", "200")), record)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from exc
            return self._json(200, {"items": items, "record": record})
        if parts == ["api", slug, "alerts"]:
            if method != "GET": raise ApiError(405, "告警接口只支持 GET")
            if set(query) - {"limit"}: raise ApiError(400, "未知告警查询参数")
            return self._json(200, {"items": service.alerts(int(query.get("limit", "100")))})
        if parts == ["api", slug, "connections"]:
            if method != "GET" or query: raise ApiError(405, "连接接口只支持 GET")
            return self._json(200, {"items": service.connections()})
        if parts == ["api", slug, "poll"]:
            if method != "POST" or query: raise ApiError(405, "采集接口只支持 POST")
            return self._json(200, {"item": service.poll()})
        if len(parts) == 3 and parts[2] == "points":
            if method == "GET":
                if set(query) - {"kind", "view"} or query.get("view","full") not in {"full","table"}:
                    raise ApiError(400, "未知点查询参数")
                return self._json(200, {"items": service.points(query.get("kind"), table_view=query.get("view")=="table")})
            if method == "POST": return self._json(201, {"item": service.define_point(self._body())})
            raise ApiError(405, "点定义接口只支持 GET 或 POST")
        if len(parts) == 5 and parts[2] == "points":
            kind = parts[3]
            try: point_no = int(parts[4])
            except ValueError as exc: raise ApiError(400, "点号必须是整数") from exc
            if query: raise ApiError(400, "点定义写操作不接受查询参数")
            if method == "PATCH": return self._json(200, {"item": service.update_point(kind, point_no, self._body())})
            if method == "DELETE":
                service.delete_point(kind, point_no)
                return self._json(200, {"deleted": True})
            if method == "GET":
                item = next(iter(service.points(kind, point_no=point_no)), None)
                if item is None: raise ApiError(404, "点定义不存在", code="not_found")
                return self._json(200, {"item": item})
            raise ApiError(405, "单点定义只支持 GET、PATCH 或 DELETE")
        if parts == ["api", slug, "update", "incremental"]:
            if method != "POST" or query: raise ApiError(405, "增量更新接口只支持 POST")
            body = self._body()
            try: return self._json(200, {"item": service.incremental_update(body["kind"], int(body["pnt_no"]), body["value"])})
            except KeyError as exc: raise ApiError(400, f"缺少字段: {exc.args[0]}") from exc
        if parts == ["api", slug, "update", "full"]:
            if method != "POST" or query: raise ApiError(405, "全量更新接口只支持 POST")
            body = self._body()
            return self._json(200, {"item": service.full_update(body.get("values", body))})
        if len(parts) == 5 and parts[2] == "commands":
            if method != "POST" or query: raise ApiError(405, "命令接口只支持 POST")
            body = self._body()
            if "value" not in body: raise ApiError(400, "缺少字段: value")
            return self._json(200, {"item": service.command(parts[3], int(parts[4]), body["value"])})
        if slug in {"can", "can-server"} and parts == ["api", slug, "frame"]:
            if method != "POST" or query: raise ApiError(405, "CAN 发帧接口只支持 POST")
            body = self._body()
            if "arbitration_id" not in body or "data" not in body: raise ApiError(400, "需要 arbitration_id 和 data")
            return self._json(200, {"item": service.send_frame(body["arbitration_id"], body["data"], extended=body.get("is_extended_id"))})
        raise ApiError(404, f"{service.LABEL} 接口路径不存在", code="not_found")

    def _list(self, table, query, scope):
        columns = {c["eng_name"]: c for c in self.store.schema(table)["columns"]}
        if "rtu_id" in query and "rtu_id" not in columns and "channel_id" in columns:
            query = dict(query)
            query["channel_id"] = self.store.channel_id_for_rtu(query.pop("rtu_id"))
        reserved = {"q", "limit", "offset", "sort", "order", "fields"}
        unknown = set(query) - reserved - set(columns)
        if unknown:
            raise ApiError(400, "未知查询参数", details=sorted(unknown))
        where = {key: value for key, value in query.items() if key not in reserved}
        if scope is not None:
            if "rtu_id" in columns:
                if "rtu_id" in where and self.store._coerce(where["rtu_id"], columns["rtu_id"]) != scope:
                    raise ApiError(400, "RTU 过滤条件与路径不一致")
                where["rtu_id"] = scope
            elif "channel_id" in columns and table.startswith("fes_channel_"):
                channel_id = self.store.channel_id_for_rtu(scope)
                if "channel_id" in where and int(where["channel_id"]) != channel_id:
                    raise ApiError(400, "通道过滤条件与路径不一致")
                where["channel_id"] = channel_id
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
        self._prepare_request()
        parts, query = self._path()
        method = "GET" if self.command == "HEAD" else self.command
        if self._packet_recording(parts, query, method) is not False:
            return
        if len(parts) == 6 and parts[:2] == ['api', 'rtus'] and parts[3] == 'commands':
            if method != 'POST':
                raise ApiError(405, '指令下发只支持 POST')
            if query:
                raise ApiError(400, '指令下发不接受查询参数')
            item = self.store.submit_command(self._rtu_id(parts[2]), parts[4], parts[5],
                                             self._body(), self._if_match())
            return self._json(202, {'item': item, 'command_status': 'pending'})
        if parts in (["api", "log-files"], ["api", "log-files", "search"]):
            if method != "GET":
                raise ApiError(405, "日志文件接口只支持 GET")
            from core.log_paths import packet_log_directory
            runtime_directory = self.store.db_path.parent / "logs"
            return self._json(200, log_browser(runtime_directory, "s", len(parts) == 3, query,
                                               packet_directory=packet_log_directory(runtime_directory)))
        # Handled routes return None after writing JSON; only False means
        # that dispatch should continue to the next route family.
        if self._monitor_control(parts, query, method) is not False:
            return
        if self._iec104_control(parts, query, method) is not False:
            return
        if self._protocol_control(parts, query, method) is not False:
            return
        if method == "GET":
            if not parts:
                return self._static("index.html")
            if parts == ["test"]:
                return self._static("test.html")
            if parts == ["iec104"]:
                return self._static("iec104.html")
            if parts == ["modbus-tcp"]:
                return self._static("modbus_tcp.html")
            if parts == ["modbus-tcp-server"]:
                return self._static("modbus_tcp_server.html")
            if parts == ["modbus-rtu"]:
                return self._static("modbus_rtu.html")
            if parts == ["modbus-rtu-server"]:
                return self._static("modbus_rtu_server.html")
            if parts == ["can-server"]:
                return self._static("can_server.html")
            if parts == ["opc-server"]:
                return self._static("opc_server.html")
            if parts == ["bacnet-server"]:
                return self._static("bacnet_server.html")
            if parts == ["can"]:
                return self._static("can.html")
            if parts == ["api-docs"]:
                return self._static("api.html")
            if parts in (["processes"], ["processes.html"]):
                return self._static("processes.html")
            if parts in (["communication"], ["communication.html"]):
                return self._static("communication.html")
            if parts in (["logs"], ["logs.html"]):
                return self._static("logs.html")
            if parts in (["channels"], ["channels.html"]):
                return self._static("channels.html")
            if parts in (["points"], ["points.html"]):
                return self._static("points.html")
            if parts == ["favicon.ico"]:
                return self._send(204, "image/x-icon", b"")
            if parts[:1] == ["static"]:
                return self._static("/".join(parts[1:]))
            if parts == ["api", "health"]:
                return self._json(200, {"status": "ok", "service": "scdfe_s", "database_role": "simulator", "database": str(self.store.db_path)})
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
        if table in {"fes_process_define", "fes_scdfe_c_define"}:
            raise ApiError(405, "运行控制表仅允许通过专用控制接口修改")
        if method == "POST" and key is None:
            body = self._body()
            if scope is not None:
                schema_columns = {c["eng_name"]: c for c in self.store.schema(table)["columns"]}
                if "rtu_id" in schema_columns:
                    if "rtu_id" in body and self.store._coerce(body["rtu_id"], schema_columns["rtu_id"]) != scope:
                        raise ApiError(400, "路径中的 RTU 与请求体不一致")
                    body["rtu_id"] = scope
                elif "channel_id" in schema_columns and table.startswith("fes_channel_"):
                    channel_id = self.store.channel_id_for_rtu(scope)
                    if "channel_id" in body and int(body["channel_id"]) != channel_id:
                        raise ApiError(400, "路径中的 RTU 与请求体不一致")
                    body["channel_id"] = channel_id
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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            LOG.debug("client disconnected")
        except ApiError as error:
            payload = {"code": error.code, "message": error.message}
            if error.details is not None:
                payload["details"] = error.details
            try:
                self._json(error.status, {"error": payload})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                LOG.debug("client disconnected while sending API error")
        except (IEC104ServiceError, ProtocolServiceError) as error:
            self._json(409, {"error": {"code": "protocol_service_error", "message": str(error)}})
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
        if name in ASSETS:
            return self._send(200, ASSETS[name], (ASSET_DIR / name).read_bytes())
        allowed = {"index.html": "text/html", "test.html": "text/html", "iec104.html": "text/html", "api.html": "text/html",
                   "processes.html": "text/html", "communication.html": "text/html", "logs.html": "text/html",
                   "channels.html": "text/html", "points.html": "text/html",
                   "modbus_tcp.html": "text/html", "modbus_tcp_server.html": "text/html",
                   "modbus_rtu.html": "text/html", "modbus_rtu_server.html": "text/html",
                   "can.html": "text/html", "can_server.html": "text/html",
                   "opc_server.html": "text/html", "bacnet_server.html": "text/html",
                   "protocol.css": "text/css",
                   "protocol.js": "application/javascript", "test.js": "application/javascript", "monitor.js": "application/javascript",
                   "iec104.js": "application/javascript", "index.js": "application/javascript",
                   "channels.js": "application/javascript"}
        if name not in allowed or not (STATIC_DIR / name).is_file():
            raise ApiError(404, "资源不存在", code="not_found")
        data = (STATIC_DIR / name).read_bytes()
        if name.endswith(".html"):
            data = render_workbench(data, "s", name)
        self._send(200, allowed[name] + "; charset=utf-8", data)


class ScadaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG
    allow_reuse_address = True

    def __init__(self, address, *, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR):
        self._resources = ExitStack()
        self.store = SimulatorStore(db_path, schema_dir, connection_pool_size=4)
        self._channel_service_lock = threading.RLock()
        self._channel_service_cache = {}
        self._channel_service_history = None
        self._resources.callback(self.store.close)
        try:
            history = ServiceFileLog.startup_catalog(self.store.db_path.parent / "logs")
            self._channel_service_history = history
            for attr, cls in [('iec104_service', IEC104Service), ('modbus_tcp_service', ModbusTcpService),
                              ('modbus_tcp_server_service', ModbusTcpServerService), ('modbus_rtu_service', ModbusRtuService),
                              ('modbus_rtu_server_service', ModbusRtuServerService), ('can_service', CanService),
                              ('can_server_service', CanServerService), ('opc_server_service', OpcServerService),
                              ('bacnet_server_service', BacnetServerService)]:
                service = cls(self.store, startup_catalog=history)
                setattr(self, attr, service)
                self._resources.callback(service.close)
            super().__init__(address, ScadaHandler)
        except BaseException:
            self._resources.close()
            raise

    def server_close(self):
        try:
            self._resources.close()
        finally:
            super().server_close()

    def channel_services(self):
        """Create/cache one independently controllable service per RTU.

        A protocol service owns its transport worker and log session.  Using a
        dedicated object per channel prevents one IEC104/Modbus/etc. channel
        from masking another channel in the monitor and gives every row its
        own start/stop endpoint.
        """
        classes = {
            "iec104": IEC104Service,
            "modbus-tcp": ModbusTcpService,
            "modbus-rtu": ModbusRtuService,
            "can": CanService,
            "bacnet": BacnetServerService,
            "opc": OpcServerService,
        }
        with self._channel_service_lock:
            with self.store.connection() as con:
                rows = con.execute(
                    "SELECT rtu_id, channel_type, name FROM fes_rtu_define "
                    "WHERE channel_type IS NOT NULL ORDER BY rtu_id"
                ).fetchall()
                iec_ports = {
                    int(item[0]): item[1] for item in con.execute(
                        "SELECT r.rtu_id, p.remote_port "
                        "FROM fes_rtu_define r JOIN fes_channel_iec104_define p "
                        "ON p.channel_id=r.channel_id"
                    ) if item[1]
                }
            active = set()
            for row in rows:
                rtu_id, channel_type = int(row[0]), str(row[1] or "")
                cls = classes.get(channel_type)
                if cls is None:
                    continue
                slug = f"{channel_type}-rtu-{rtu_id}"
                active.add(slug)
                if slug in self._channel_service_cache:
                    continue
                config_path = self.store.db_path.with_name(
                    f".scdfe_s_{channel_type.replace('-', '_')}_rtu_{rtu_id}.json"
                )
                service = cls(self.store, config_path=config_path,
                              startup_catalog=self._channel_service_history)
                # This is an in-memory channel binding.  Do not persist a
                # monitor click into the shared protocol configuration file.
                service._config["rtu_id"] = rtu_id
                if hasattr(service, "_configured_fields"):
                    service._configured_fields.add("rtu_id")
                if channel_type == "iec104":
                    # IEC104 test definitions use remote_port as the listen
                    # port for each independent station.
                    channel_port = iec_ports.get(rtu_id)
                    if channel_port:
                        try:
                            service._config["port"] = int(channel_port)
                        except (TypeError, ValueError):
                            pass
                self._channel_service_cache[slug] = service
                self._resources.callback(service.close)
            return dict((slug, service) for slug, service in self._channel_service_cache.items()
                        if slug in active)


def make_server(host="127.0.0.1", port=8878, *, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR):
    return ScadaHTTPServer((host, port), db_path=db_path, schema_dir=schema_dir)


def main():
    parser = argparse.ArgumentParser(description="SCADA protocol master/server test web service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8878)
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

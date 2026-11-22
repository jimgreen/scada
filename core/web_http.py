"""Shared HTTP response helpers: compact JSON, gzip compression, static caching."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit


DEFAULT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
)
_IF_MATCH_QUOTED_RE = re.compile(r'"[1-9][0-9]*"')
_IF_MATCH_PLAIN_RE = re.compile(r"[1-9][0-9]*")


_GZIP_THRESHOLD = 1024
_STATIC_CACHE_MAX = 256
_COMPRESSIBLE_PREFIXES = (
    "application/json",
    "application/javascript",
    "text/",
    "image/svg+xml",
)

_static_lock = threading.Lock()
_static_cache: dict[tuple[str, str], tuple[tuple[int, int], bytes, bytes | None, str]] = {}


def encode_json(payload: Any) -> bytes:
    """Serialize JSON compactly without ASCII escaping or non-standard NaN."""
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def accepts_gzip(handler) -> bool:
    """Return True when the HTTP client advertises non-zero gzip support."""
    header = handler.headers.get("Accept-Encoding") if handler.headers else None
    if not header or "gzip" not in header.lower():
        return False
    for token in header.split(","):
        parts = [part.strip().lower() for part in token.split(";")]
        if not parts or parts[0] != "gzip":
            continue
        for param in parts[1:]:
            if param.startswith("q="):
                try:
                    if float(param[2:]) <= 0.0:
                        return False
                except ValueError:
                    return False
        return True
    return False


def _is_compressible(content_type: str) -> bool:
    mime = content_type.split(";", 1)[0].strip().lower()
    return mime.startswith(_COMPRESSIBLE_PREFIXES)


def prepare_response(handler, status: int, content_type: str, data: bytes,
                     headers: dict[str, str] | None = None) -> tuple[bytes, dict[str, str]]:
    """Apply transparent gzip compression when requested and beneficial."""
    merged = dict(headers) if headers else {}
    lower_keys = {key.lower() for key in merged}
    if (status == 304 or status == 204 or len(data) < _GZIP_THRESHOLD
            or "content-encoding" in lower_keys
            or not _is_compressible(content_type)
            or not accepts_gzip(handler)):
        return data, merged
    compressed = gzip.compress(data, compresslevel=5)
    if len(compressed) >= len(data):
        return data, merged
    merged["Content-Encoding"] = "gzip"
    if "vary" in lower_keys:
        for key in list(merged):
            if key.lower() == "vary" and "accept-encoding" not in merged[key].lower():
                merged[key] = merged[key] + ", Accept-Encoding"
    else:
        merged["Vary"] = "Accept-Encoding"
    return compressed, merged


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    canonical = etag[2:] if etag.startswith("W/") else etag
    for token in header.split(","):
        candidate = token.strip()
        if not candidate:
            continue
        if candidate == "*" or candidate == etag or candidate == canonical or candidate == f"W/{canonical}":
            return True
        if candidate.startswith("W/") and candidate[2:] == canonical:
            return True
    return False


def load_static(path: Path | str, *, transform: Callable[[bytes], bytes] | None = None,
                cache_key: str = "") -> tuple[bytes, bytes | None, str]:
    """Read and cache a static asset validated by file mtime and size."""
    resolved = Path(path).resolve()
    stat_result = os.stat(resolved)
    stamp = (stat_result.st_mtime_ns, stat_result.st_size)
    key = (str(resolved), cache_key)
    with _static_lock:
        cached = _static_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1], cached[2], cached[3]
    raw = resolved.read_bytes()
    if transform is not None:
        raw = transform(raw)
    digest = hashlib.sha256(raw).hexdigest()[:24]
    etag = f'"{digest}"'
    gzipped = None
    if len(raw) >= _GZIP_THRESHOLD:
        candidate = gzip.compress(raw, compresslevel=6)
        if len(candidate) < len(raw):
            gzipped = candidate
    with _static_lock:
        if len(_static_cache) >= _STATIC_CACHE_MAX:
            _static_cache.clear()
        _static_cache[key] = (stamp, raw, gzipped, etag)
    return raw, gzipped, etag


def send_static(handler, path: Path | str, content_type: str, *,
                transform: Callable[[bytes], bytes] | None = None,
                cache_key: str = "") -> None:
    """Serve a cached static file with ETag/304 and pre-compressed gzip."""
    raw, gzipped, etag = load_static(path, transform=transform, cache_key=cache_key)
    negotiable = gzipped is not None and _is_compressible(content_type)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if negotiable:
        headers["Vary"] = "Accept-Encoding"
    if _etag_matches(handler.headers.get("If-None-Match") if handler.headers else None, etag):
        handler._send(304, content_type, b"", headers)
        return
    if negotiable and accepts_gzip(handler):
        headers["Content-Encoding"] = "gzip"
        handler._send(200, content_type, gzipped, headers)
        return
    handler._send(200, content_type, raw, headers)


def browser_safe(value: Any) -> Any:
    """将超过 JS 安全整数范围（2^53 - 1）的 64 位整数转为字符串，避免前端精度丢失。"""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {key: browser_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [browser_safe(item) for item in value]
    return value


def write_security_headers(
    handler,
    *,
    csp: str = DEFAULT_CSP,
    frame_options: str = "DENY",
    referrer_policy: str = "same-origin",
) -> None:
    """统一写入防嗅探、防点击劫持、Referrer 与 CSP 安全响应头。"""
    handler.send_header("X-Content-Type-Options", "nosniff")
    if frame_options:
        handler.send_header("X-Frame-Options", frame_options)
    if referrer_policy:
        handler.send_header("Referrer-Policy", referrer_policy)
    if csp:
        handler.send_header("Content-Security-Policy", csp)


def parse_if_match(handler) -> int | None:
    """解析并校验正整数版本号 If-Match 请求头。"""
    from scdfe_c.web.service import ApiError

    value = handler.headers.get("If-Match") if handler.headers else None
    if value is None:
        return None
    value = value.strip()
    if _IF_MATCH_QUOTED_RE.fullmatch(value):
        value = value[1:-1]
    if not _IF_MATCH_PLAIN_RE.fullmatch(value):
        raise ApiError(400, "If-Match 必须是正整数版本号")
    return int(value)


def parse_request_path(handler, *, max_num_fields: int = 80) -> tuple[list[str], dict[str, str]]:
    """解析请求路径段与去重后的查询参数字典。"""
    from scdfe_c.web.service import ApiError

    split = urlsplit(handler.path)
    parts = [unquote(part) for part in split.path.strip("/").split("/") if part]
    raw = parse_qs(split.query, keep_blank_values=True, max_num_fields=max_num_fields)
    if any(len(values) != 1 for values in raw.values()):
        raise ApiError(400, "同一查询参数只能出现一次")
    return parts, {key: values[0] for key, values in raw.items()}


def handle_packet_recording(handler, parts: list[str], query: dict[str, str], method: str,
                            db_path: Path | str, component: str):
    """统一处理 C/S/T 的 /api/packet-recording 全局与通道报文记录策略接口。"""
    from scdfe_c.web.service import ApiError

    if parts[:2] != ["api", "packet-recording"]:
        return False
    if query:
        raise ApiError(400, "报文记录策略接口不接受查询参数")
    from core.packet_logging import packet_log_switch, packet_recording_state

    switch = packet_log_switch(db_path, component)
    if len(parts) == 2:
        if method == "GET":
            return handler._json(200, packet_recording_state(db_path, component))
        if method != "PUT":
            raise ApiError(405, "报文记录策略只支持 GET/PUT")
        body = handler._body()
        if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
            raise ApiError(400, "enabled 必须是布尔值，且不能包含其他字段")
        switch.set_enabled(body["enabled"])
        return handler._json(200, packet_recording_state(db_path, component))
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
        return handler._json(200, item)
    if method != "PUT":
        raise ApiError(405, "通道报文记录策略只支持 GET/PUT")
    body = handler._body()
    if set(body) != {"enabled"} or (body["enabled"] is not None and type(body["enabled"]) is not bool):
        raise ApiError(400, "enabled 必须是布尔值或 null，且不能包含其他字段")
    try:
        switch.channel_snapshot(channel_id)
        switch.set_channel(channel_id, body["enabled"])
        item = switch.channel_snapshot(channel_id)
    except KeyError as exc:
        raise ApiError(404, str(exc), code="not_found") from exc
    return handler._json(200, item)


class BaseScadaHandlerMixin:
    """C/S/T/D 共享的 HTTP Handler 基础能力：超时、安全头、压缩发送与版本头解析。"""

    protocol_version = "HTTP/1.1"
    server_version = "ScadaWeb/1.0"
    security_csp = DEFAULT_CSP
    security_frame_options = "DENY"
    security_referrer_policy = "same-origin"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def _send(self, status, content_type, data, headers=None):
        data, extra = prepare_response(self, status, content_type, data, headers)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if "cache-control" not in {key.lower() for key in extra}:
            self.send_header("Cache-Control", "no-store")
        write_security_headers(
            self,
            csp=self.security_csp,
            frame_options=self.security_frame_options,
            referrer_policy=self.security_referrer_policy,
        )
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self._audit_delivered = True

    def _send_json(self, status, data, headers=None):
        """发送 JSON 响应；对无实体版本 ETag 的 GET 200 接口自动附加弱 ETag 并支持 304 Not Modified。"""
        extra = dict(headers) if headers else {}
        if (
            self.command == "GET"
            and status == 200
            and "etag" not in {key.lower() for key in extra}
        ):
            digest = hashlib.blake2b(data, digest_size=12).hexdigest()
            etag = f'W/"{digest}"'
            extra["ETag"] = etag
            if "cache-control" not in {key.lower() for key in extra}:
                extra["Cache-Control"] = "no-cache"
            if _etag_matches(self.headers.get("If-None-Match") if self.headers else None, etag):
                self._send(304, "application/json; charset=utf-8", b"", extra)
                return
        self._send(status, "application/json; charset=utf-8", data, extra)

    def _if_match(self):
        return parse_if_match(self)


__all__ = [
    "BaseScadaHandlerMixin",
    "DEFAULT_CSP",
    "accepts_gzip",
    "browser_safe",
    "encode_json",
    "handle_packet_recording",
    "load_static",
    "parse_if_match",
    "parse_request_path",
    "prepare_response",
    "send_static",
    "write_security_headers",
]

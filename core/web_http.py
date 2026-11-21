"""Shared HTTP response helpers: compact JSON, gzip compression, static caching."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable


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
    for token in header.split(","):
        candidate = token.strip()
        if candidate == "*" or candidate == etag or candidate == f"W/{etag}":
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


__all__ = ["accepts_gzip", "encode_json", "load_static", "prepare_response", "send_static"]

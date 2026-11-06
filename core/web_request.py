"""Common HTTP framing and browser-origin checks for the three SCADA Webs."""
import json
import socket
import time
from urllib.parse import urlsplit

from scdfe_c.web.service import ApiError


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复 JSON 字段: {key}")
        result[key] = value
    return result


def _invalid_number(value):
    raise ValueError(f"非法 JSON 数字: {value}")


class RequestBodyMixin:
    def finish(self):
        try:
            super().finish()
        finally:
            if getattr(self, "_unread_request", False):
                # Framing or origin was rejected before reading. Send FIN with
                # the error response, then discard a bounded tail. Immediate
                # close with pending TCP input can erase that response on Windows.
                try:
                    self.connection.shutdown(socket.SHUT_WR)
                    deadline = time.monotonic() + .2
                    remaining = 64 * 1024
                    while remaining and (timeout := deadline - time.monotonic()) > 0:
                        self.connection.settimeout(timeout)
                        chunk = self.connection.recv(min(remaining, 8192))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except OSError:
                    pass

    def _prepare_request(self):
        self._unread_request = self.command not in {"GET", "HEAD"}
        host = self.headers.get("Host", "")
        hostname = urlsplit("//" + host).hostname
        if self.server.server_address[0] in {"127.0.0.1", "::1"} and hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ApiError(403, "Host 不被允许", code="forbidden")
        if self.command in {"GET", "HEAD"}:
            return
        origin = self.headers.get("Origin")
        if self.headers.get("Sec-Fetch-Site") == "cross-site" or (origin and origin != "http://" + host):
            raise ApiError(403, "请求来源不被允许", code="forbidden")
        if self.headers.get_all("Transfer-Encoding"):
            raise ApiError(400, "不支持分块请求体")
        lengths = self.headers.get_all("Content-Length") or ["0"]
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            raise ApiError(400, "无效或重复的 Content-Length")
        length = int(lengths[0])
        if length > 2_000_000:
            raise ApiError(413, "请求体不能超过 2000000 字节")
        # Consume the declared body once, even if the route rejects this method.
        self._request_bytes = self.rfile.read(length)
        if len(self._request_bytes) != length:
            raise ApiError(400, "请求体不完整")
        self._unread_request = False

    def _body(self):
        if self.headers.get_content_type() != "application/json":
            raise ApiError(415, "请使用 Content-Type: application/json")
        try:
            value = json.loads(self._request_bytes.decode("utf-8"), object_pairs_hook=_object,
                               parse_constant=_invalid_number)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ApiError(400, "请求体必须是有效 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return value

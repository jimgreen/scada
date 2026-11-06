"""Local IPC bridge to the :mod:`scdfe_c` database writer process.

The communication/transmission runtimes are deliberately kept out of the
writer's ``multiprocessing.JoinableQueue``.  That queue is an implementation
detail of one supervisor and cannot be safely shared with an independently
started ``scdfe_t`` process.  This module provides a small, loopback-only,
newline-delimited JSON protocol instead.  The writer process owns the server
and performs every request in its normal writer loop; clients only receive an
acknowledgement after the command has been committed to SQLite.

The endpoint is discovered through a short-lived JSON file next to the
database.  A random token in the file prevents another local process from
submitting commands accidentally.  Binding to ``127.0.0.1`` keeps the bridge
off the network and works on both Windows and Linux without named-pipe or
Unix-socket special cases.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import queue
import secrets
import socket
import socketserver
import threading
import time
import uuid
from typing import Any


MAX_LINE = 64 * 1024
REQUEST_TIMEOUT = 15.0
ENDPOINT_VERSION = 1


class WriterUnavailable(ConnectionError):
    """The scdfe_c writer endpoint is not running or cannot be reached."""


class WriterRequestError(RuntimeError):
    """The writer received a request but rejected it."""

    def __init__(self, message: str, *, code: str = "writer_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def endpoint_path(db_path: str | Path) -> Path:
    """Return the deterministic endpoint metadata path for ``db_path``."""

    path = Path(db_path).resolve()
    return path.with_name(path.name + ".scdfe_writer.json")


@dataclass
class WriterRequest:
    """A request handed from the socket thread to the writer loop."""

    payload: dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    fingerprint: bytes = b""


def _command_json(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate field")
            obj[key] = value
        return obj
    def invalid(value):
        raise ValueError(value)
    if not raw.endswith(b"\n"):
        raise ValueError("missing frame terminator")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _RequestHandler(socketserver.StreamRequestHandler):
    server: "_WriterTCPServer"

    def handle(self) -> None:  # noqa: D401 - protocol handler
        # A client submits exactly one bounded JSON request per connection.
        try:
            # Do not let an incomplete local client occupy a handler thread
            # forever.  The writer endpoint is loopback-only, but command
            # service shutdown/restart must still be able to drain promptly
            # when a client connected and never sent a newline-delimited
            # request.
            self.request.settimeout(self.server.request_timeout)
            try:
                raw = self.rfile.readline(MAX_LINE + 1)
            except socket.timeout:
                self._send({"ok": False, "error": "读取请求超时", "code": "request_timeout", "status": 408})
                return
            if not raw or len(raw) > MAX_LINE:
                self._send({"ok": False, "error": "请求过大", "code": "request_too_large"})
                return
            try:
                payload = _command_json(raw)
            except (ValueError, RecursionError):
                self._send({"ok": False, "error": "请求不是有效 JSON", "code": "invalid_json"})
                return
            if not isinstance(payload, dict):
                self._send({"ok": False, "error": "请求必须是 JSON 对象", "code": "invalid_request"})
                return
            token = payload.pop("token", None)
            if not isinstance(token, str) or not token.isascii() or not hmac.compare_digest(token, self.server.token):
                self._send({"ok": False, "error": "写库进程令牌无效", "code": "unauthorized", "status": 401})
                return
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
                self._send({"ok": False, "error": "缺少有效 request_id", "code": "invalid_request", "status": 400})
                return
            if payload.get("op", "submit_command") != "submit_command":
                self._send({"ok": False, "error": "不支持的写库操作", "code": "invalid_request", "status": 400})
                return
            request, owner = self.server.claim(request_id, payload)
            if isinstance(request, dict):
                self._send(request)
                return
            if owner:
                try:
                    self.server.requests.put(request, timeout=2.0)
                except queue.Full:
                    self.server.reject_busy(request_id, request)
            if not request.done.wait(self.server.request_timeout):
                self._send({"ok": False, "error": "写库进程响应超时", "code": "writer_timeout", "status": 504})
                return
            self._send(request.response or {
                "ok": False, "error": "写库进程未返回结果", "code": "writer_error", "status": 500,
            })
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The caller may disappear while the writer is stopping.  There
            # is no useful action to take and the server must remain healthy.
            return

    def _send(self, value: dict[str, Any]) -> None:
        try:
            self.wfile.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class _WriterTCPServer(_ThreadingTCPServer):
    def __init__(self, address, requests: queue.Queue[WriterRequest], token: str,
                 request_timeout: float):
        self.requests = requests
        self.token = token
        self.request_timeout = request_timeout
        self._cache: dict[str, tuple[bytes, dict[str, Any]]] = {}
        self._inflight: dict[str, WriterRequest] = {}
        self._cache_lock = threading.RLock()
        super().__init__(address, _RequestHandler)

    def claim(self, request_id: str, payload: dict[str, Any]):
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).digest()
        conflict = {"ok": False, "code": "request_id_conflict", "status": 409,
                    "error": "相同 request_id 不能用于不同指令"}
        # Lookup and reservation share a lock: completion cannot move a request
        # from in-flight to cached between two independent lookups.
        with self._cache_lock:
            cached = self._cache.get(request_id)
            if cached is not None:
                return (cached[1] if cached[0] == fingerprint else conflict), False
            current = self._inflight.get(request_id)
            if current is not None:
                return (current if current.fingerprint == fingerprint else conflict), False
            current = WriterRequest(payload, fingerprint=fingerprint)
            self._inflight[request_id] = current
            return current, True

    def reject_busy(self, request_id: str, request: WriterRequest) -> None:
        with self._cache_lock:
            self._inflight.pop(request_id, None)
            request.response = {"ok": False, "error": "写库请求队列已满", "code": "writer_busy", "status": 503}
            request.done.set()  # also release identical requests that were waiting

    def complete(self, request: WriterRequest) -> None:
        request_id = str(request.payload.get("request_id", ""))
        if not request_id or request.response is None:
            return
        with self._cache_lock:
            self._inflight.pop(request_id, None)
            self._cache[request_id] = (request.fingerprint, dict(request.response))
            while len(self._cache) > 2048:
                self._cache.pop(next(iter(self._cache)))


class WriterIpcServer:
    """Loopback server used by the scdfe_c writer process.

    ``requests`` is drained by the writer process' main loop.  The server
    never opens SQLite itself, so the existing writer transaction and batch
    scheduling remain the single point of database writes.
    """

    def __init__(self, db_path: str | Path, requests: queue.Queue[WriterRequest], *,
                 request_timeout: float = REQUEST_TIMEOUT):
        self.db_path = Path(db_path).resolve()
        self.endpoint = endpoint_path(self.db_path)
        self.requests = requests
        self.request_timeout = float(request_timeout)
        if self.request_timeout <= 0:
            raise ValueError("request_timeout 必须为正数")
        self.token = secrets.token_urlsafe(32)
        self._server: _WriterTCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        if self._server is None:
            return None
        host, port = self._server.server_address
        return str(host), int(port)

    def start(self) -> "WriterIpcServer":
        if self._server is not None:
            raise RuntimeError("写库 IPC 服务已启动")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        server = _WriterTCPServer(("127.0.0.1", 0), self.requests, self.token, self.request_timeout)
        self._server = server
        host, port = server.server_address
        record = {
            "version": ENDPOINT_VERSION,
            "host": str(host),
            "port": int(port),
            "token": self.token,
            "pid": __import__("os").getpid(),
            "created_at": int(time.time() * 1000),
        }
        temporary = self.endpoint.with_name(self.endpoint.name + f".{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.endpoint)
        except Exception:
            server.server_close()
            self._server = None
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._thread = threading.Thread(target=server.serve_forever, name="scdfe-c-writer-ipc", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)
        try:
            record = json.loads(self.endpoint.read_text(encoding="utf-8"))
            if hmac.compare_digest(str(record.get("token", "")), self.token):
                self.endpoint.unlink(missing_ok=True)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            pass

    def complete(self, request: WriterRequest) -> None:
        """Cache a finished request for explicit, idempotent replays."""
        server = self._server
        if server is not None:
            server.complete(request)

    def __enter__(self) -> "WriterIpcServer":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _read_endpoint(db_path: str | Path) -> dict[str, Any]:
    path = endpoint_path(db_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WriterUnavailable("scdfe_c 写库进程未运行") from exc
    if not isinstance(data, dict) or data.get("version") != ENDPOINT_VERSION:
        raise WriterUnavailable("scdfe_c 写库 IPC 端点版本不支持")
    host, port, token = data.get("host"), data.get("port"), data.get("token")
    if host != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str):
        raise WriterUnavailable("scdfe_c 写库 IPC 端点无效")
    data["endpoint_path"] = str(path)
    return data


class WriterClient:
    """Synchronous client for reliable command publication through the writer."""

    def __init__(self, db_path: str | Path, *, timeout: float = 5.0):
        self.db_path = Path(db_path).resolve()
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout 必须为正数")

    def request(self, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("payload 必须是字典")
        endpoint = _read_endpoint(self.db_path)
        data = dict(payload)
        data["token"] = endpoint["token"]
        data.setdefault("request_id", uuid.uuid4().hex)
        wait = self.timeout if timeout is None else float(timeout)
        try:
            with socket.create_connection((endpoint["host"], endpoint["port"]), timeout=wait) as conn:
                conn.settimeout(wait)
                conn.sendall(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                stream = conn.makefile("rb")
                raw = stream.readline(MAX_LINE + 1)
                stream.close()
        except (OSError, TimeoutError) as exc:
            raise WriterUnavailable("无法连接 scdfe_c 写库进程") from exc
        if not raw or len(raw) > MAX_LINE:
            raise WriterUnavailable("scdfe_c 写库进程返回无效响应")
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WriterUnavailable("scdfe_c 写库进程返回无效 JSON") from exc
        if not isinstance(response, dict):
            raise WriterUnavailable("scdfe_c 写库进程返回无效响应")
        if not response.get("ok"):
            raise WriterRequestError(
                str(response.get("error") or "写库进程拒绝请求"),
                code=str(response.get("code") or "writer_error"),
                status=int(response.get("status") or 400),
            )
        return response

    def submit_command(self, rtu_id: int, kind: str, point_no: int, value: Any,
                       *, timeout: float | None = None,
                       request_id: str | None = None) -> dict[str, Any]:
        if type(rtu_id) is not int or rtu_id <= 0 or type(point_no) is not int or point_no <= 0:
            raise ValueError("rtu_id/point_no 必须为正整数")
        response = self.request({
            "op": "submit_command", "rtu_id": rtu_id, "kind": kind,
            "point_no": point_no, "value": value,
            **({"request_id": request_id} if request_id is not None else {}),
        }, timeout=timeout)
        item = response.get("item")
        return item if isinstance(item, dict) else response


__all__ = [
    "ENDPOINT_VERSION", "MAX_LINE", "REQUEST_TIMEOUT", "WriterClient", "WriterIpcServer",
    "WriterRequest", "WriterRequestError", "WriterUnavailable", "endpoint_path",
]

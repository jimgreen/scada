"""TCP command server for publishing YK/YT commands to ``scdfe_c``.

The service accepts one newline-delimited UTF-8 JSON request per connection and
submits validated commands through the existing database-writer IPC bridge.
The writer commits the command to SQLite before this service acknowledges it;
the normal ``scdfe_c`` communication workers then observe the command version
and send it through the configured protocol adapter.

The server can bind to loopback for same-host clients or to an explicitly
configured interface for another server.  A shared token is required for every
request.  The endpoint metadata file is written beside the database so local
processes can discover the selected port and token without hard-coding it.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import hmac
import json
import math
import os
from pathlib import Path

import secrets
import socket
import socketserver
import uuid
import threading
import time
from typing import Any

from core.network_limits import LISTEN_BACKLOG
from core.process_registry import PROCESS_TABLE, ProcessLease
from core.scdfe_control import InstanceLock, ensure_control_row, log_directory
from core.log_paths import packet_log_directory
from . import ScdfeLogger
from .web.service import DEFAULT_DB, SCHEMA_DIR, ScadaStore
from .writer_ipc import WriterClient, WriterRequestError, WriterUnavailable, _command_json

MAX_LINE = 64 * 1024
REQUEST_TIMEOUT = 10.0
ENDPOINT_VERSION = 1
COMMAND_PROCESS_KEY = "command-server"
COMMAND_TABLE = "fes_scdfe_command_define"
PROCESS_STOPPED = 0
PROCESS_STARTING = 1
PROCESS_RUNNING = 2
PROCESS_ERROR = 5


def endpoint_path(db_path: str | Path) -> Path:
    path = Path(db_path).resolve()
    return path.with_name(path.name + ".scdfe_command.json")


def _validate_request(payload: Mapping[str, Any]) -> tuple[str, int, str, int, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("请求必须是 JSON 对象")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise ValueError("request_id 必须是非空字符串且不超过 128 个字符")
    op = payload.get("op", "submit_command")
    if op not in ("command", "submit_command"):
        raise ValueError("op 只能是 command 或 submit_command")
    # JSON command identifiers are integers on the wire.  Do not silently
    # truncate floats (``1.5`` -> ``1``) or accept booleans (``true`` -> ``1``)
    # at this process boundary; either would address a different point than
    # the caller requested.
    rtu_id = payload.get("rtu_id")
    point_no = payload.get("point_no")
    if (isinstance(rtu_id, bool) or not isinstance(rtu_id, int) or
            isinstance(point_no, bool) or not isinstance(point_no, int)):
        raise ValueError("rtu_id 和 point_no 必须是整数")
    if rtu_id <= 0 or point_no <= 0:
        raise ValueError("rtu_id 和 point_no 必须为正整数")
    kind = payload.get("kind")
    if kind not in ("yk", "yt"):
        raise ValueError("只支持遥控 yk 和遥调 yt")
    if "value" not in payload:
        raise ValueError("缺少 value")
    value = payload["value"]
    if kind == "yk":
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, int) and value in {0, 1}:
            value = int(value)
        elif isinstance(value, float) and value in {0.0, 1.0}:
            value = int(value)
        else:
            raise ValueError("遥控 value 只能是 0 或 1")
    else:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("遥调 value 必须是数字") from exc
        if not math.isfinite(numeric):
            raise ValueError("遥调 value 必须是有限数字")
        value = numeric
    return request_id, rtu_id, kind, point_no, value


class _CommandTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG

    def __init__(self, address, owner: "CommandServer"):
        self.owner = owner
        super().__init__(address, _CommandRequestHandler)


class _CommandRequestHandler(socketserver.StreamRequestHandler):
    server: _CommandTCPServer

    def _send(self, payload: dict[str, Any]) -> None:
        try:
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            self.wfile.write(raw + b"\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def handle(self) -> None:
        owner = self.server.owner
        try:
            # A client that connects and never sends a newline must not keep
            # one of the ThreadingMixIn worker threads occupied forever.
            self.request.settimeout(owner.request_timeout)
            try:
                raw = self.rfile.readline(MAX_LINE + 1)
            except socket.timeout:
                self._send(owner.error_response("request_timeout", "读取请求超时", 408))
                return
            if not raw or len(raw) > MAX_LINE:
                self._send(owner.error_response("request_too_large", "请求过大", 413))
                return
            try:
                payload = _command_json(raw)
            except (ValueError, RecursionError):
                self._send(owner.error_response("invalid_json", "请求不是有效 JSON", 400))
                return
            if not isinstance(payload, dict):
                self._send(owner.error_response("invalid_request", "请求必须是 JSON 对象", 400))
                return
            token = payload.pop("token", None)
            if not isinstance(token, str) or not hmac.compare_digest(token.encode("utf-8", "surrogatepass"), owner.token.encode("utf-8", "surrogatepass")):
                owner.logger.error("TCP 指令认证失败")
                self._send(owner.error_response("unauthorized", "指令服务令牌无效", 401))
                return
            try:
                request_id, rtu_id, kind, point_no, value = _validate_request(payload)
            except ValueError as exc:
                owner.logger.error(f"TCP 指令参数错误: {exc}")
                self._send(owner.error_response("invalid_command", str(exc), 400))
                return
            fingerprint = (rtu_id, kind, point_no, value)
            cached = owner.cached(request_id, fingerprint)
            if cached is not None:
                self._send(cached)
                return
            claim, marker = owner.begin_request(request_id, fingerprint)
            if claim == "cached":
                self._send(marker)
                return
            if claim == "conflict":
                self._send(marker)
                return
            if claim == "wait":
                # A duplicate request arriving while the first one is being
                # committed waits for that result. This prevents two
                # concurrent sockets with the same request_id from issuing
                # two physical commands when a lightweight writer test double
                # does not provide its own idempotency layer.
                if not marker.wait(owner.request_timeout):
                    self._send(owner.error_response("request_in_progress",
                                                     "相同 request_id 的请求仍在处理中",
                                                     409, request_id=request_id))
                    return
                cached = owner.cached(request_id, fingerprint)
                if cached is None:
                    self._send(owner.error_response("command_error", "请求处理结果不可用",
                                                     500, request_id=request_id))
                else:
                    self._send(cached)
                return
            try:
                item = owner.submit(request_id, rtu_id, kind, point_no, value)
                response = {"ok": True, "item": item, "command_status": "pending", "request_id": request_id}
            except WriterRequestError as exc:
                response = owner.error_response(exc.code, str(exc), int(exc.status), request_id=request_id)
            except WriterUnavailable as exc:
                response = owner.error_response("writer_unavailable", str(exc), 503, request_id=request_id)
            except Exception as exc:  # noqa: BLE001 - isolate one client request
                owner.logger.error(f"TCP 指令处理失败: {exc}")
                response = owner.error_response("command_error", str(exc), 500, request_id=request_id)
            owner.complete_request(request_id, response, fingerprint)
            self._send(response)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class CommandClient:
    """Client for the authenticated TCP command service."""

    def __init__(self, db_path: str | Path = DEFAULT_DB, *, host: str | None = None,
                 port: int | None = None, token: str | None = None, timeout: float = REQUEST_TIMEOUT):
        self.db_path = Path(db_path).resolve()
        self.host, self.port, self.token = host, port, token
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout 必须为正数")

    def _endpoint(self) -> tuple[str, int, str]:
        if self.host is not None and self.port is not None and self.token:
            return str(self.host), int(self.port), self.token
        try:
            record = json.loads(endpoint_path(self.db_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConnectionError("TCP 指令服务未运行") from exc
        host = self.host or record.get("host")
        port = self.port if self.port is not None else record.get("port")
        token = self.token or record.get("token")
        if not isinstance(host, str) or type(port) is not int or not isinstance(token, str):
            raise ConnectionError("TCP 指令服务端点无效")
        return host, port, token

    def submit_command(self, rtu_id: int, kind: str, point_no: int, value: Any,
                       *, request_id: str | None = None, timeout: float | None = None) -> dict[str, Any]:
        if type(rtu_id) is not int or rtu_id <= 0:
            raise ValueError("rtu_id 必须为正整数")
        if type(point_no) is not int or point_no <= 0:
            raise ValueError("point_no 必须为正整数")
        if kind not in {"yk", "yt"}:
            raise ValueError("kind 只能是 yk 或 yt")
        host, port, token = self._endpoint()
        payload = {"request_id": request_id or uuid.uuid4().hex, "op": "command",
                   "rtu_id": rtu_id, "kind": str(kind), "point_no": point_no,
                   "value": value, "token": token}
        wait = self.timeout if timeout is None else float(timeout)
        with socket.create_connection((host, port), timeout=wait) as connection:
            connection.settimeout(wait)
            connection.sendall(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
            raw = connection.makefile("rb").readline(MAX_LINE + 1)
        if not raw or len(raw) > MAX_LINE:
            raise ConnectionError("TCP 指令服务返回无效响应")
        response = json.loads(raw.decode("utf-8"))
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "指令被拒绝"))
        return response


class CommandServer:
    """Authenticated TCP command endpoint backed by ``WriterClient``."""

    def __init__(self, db_path: str | Path = DEFAULT_DB, *, host: str = "127.0.0.1",
                 port: int = 0, token: str | None = None, log_dir: str | Path | None = None,
                 request_timeout: float = REQUEST_TIMEOUT, retention_hours: int = 720,
                 writer: WriterClient | None = None):
        self.db_path = Path(db_path).resolve()
        self.host = str(host)
        self.port = int(port)
        if not 0 <= self.port <= 65535:
            raise ValueError("port 必须在 0 到 65535 之间")
        self.token = token or secrets.token_urlsafe(32)
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("token 不能为空")
        self.request_timeout = float(request_timeout)
        self.retention_hours = int(retention_hours)
        if self.retention_hours < 1:
            raise ValueError("retention_hours 必须为正整数")
        if self.request_timeout <= 0:
            raise ValueError("request_timeout 必须为正数")
        runtime_log_dir = Path(log_dir).resolve() if log_dir else self.db_path.parent / "logs"
        self.logger = ScdfeLogger(runtime_log_dir, process_name="scdfe_c-command-server",
                                  packet_directory=packet_log_directory(runtime_log_dir))
        self.writer = writer or WriterClient(self.db_path, timeout=self.request_timeout)
        self._server: _CommandTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._cache: dict[str, dict[str, Any]] = {}
        self._request_fingerprints: dict[str, tuple[Any, ...]] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._cache_lock = threading.RLock()

    @property
    def address(self) -> tuple[str, int] | None:
        if self._server is None:
            return None
        host, port = self._server.server_address
        return str(host), int(port)

    def start(self) -> "CommandServer":
        if self._server is not None:
            raise RuntimeError("TCP 指令服务已启动")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        server = _CommandTCPServer((self.host, self.port), self)
        self._server = server
        host, port = server.server_address
        record = {"version": ENDPOINT_VERSION, "host": str(host), "port": int(port),
                  "token": self.token, "pid": os.getpid(), "created_at": int(time.time() * 1000)}
        endpoint = endpoint_path(self.db_path)
        temporary = endpoint.with_name(endpoint.name + f".{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            temporary.replace(endpoint)
        except Exception:
            server.server_close()
            self._server = None
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._thread = threading.Thread(target=server.serve_forever, name="scdfe-c-command-server", daemon=True)
        self._thread.start()
        self.logger.process(f"TCP 指令服务启动，监听 {host}:{port}")
        return self

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        if self._server is None:
            self.start()
        assert self._server is not None
        self._server.serve_forever(poll_interval=poll_interval)

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        endpoint = endpoint_path(self.db_path)
        try:
            record = json.loads(endpoint.read_text(encoding="utf-8"))
            if record.get("token") == self.token:
                endpoint.unlink(missing_ok=True)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            pass
        self.logger.process("TCP 指令服务停止")

    @staticmethod
    def _request_conflict(request_id: str) -> dict[str, Any]:
        return CommandServer.error_response(
            "request_id_conflict",
            "request_id 已用于另一条指令",
            409,
            request_id=request_id,
        )

    def cached(self, request_id: str, fingerprint: tuple[Any, ...] | None = None) -> dict[str, Any] | None:
        with self._cache_lock:
            response = self._cache.get(request_id)
            if response is None:
                return None
            if (fingerprint is not None and request_id in self._request_fingerprints and
                    self._request_fingerprints[request_id] != fingerprint):
                return self._request_conflict(request_id)
            return dict(response)

    def begin_request(self, request_id: str, fingerprint: tuple[Any, ...] | None = None) -> tuple[str, dict[str, Any] | threading.Event]:
        """Atomically claim a request id or join its in-flight operation."""
        with self._cache_lock:
            response = self._cache.get(request_id)
            if response is not None:
                if (fingerprint is not None and request_id in self._request_fingerprints and
                        self._request_fingerprints[request_id] != fingerprint):
                    return "conflict", self._request_conflict(request_id)
                return "cached", dict(response)
            event = self._inflight.get(request_id)
            if event is not None:
                if (fingerprint is not None and request_id in self._request_fingerprints and
                        self._request_fingerprints[request_id] != fingerprint):
                    return "conflict", self._request_conflict(request_id)
                return "wait", event
            event = threading.Event()
            self._inflight[request_id] = event
            if fingerprint is not None:
                self._request_fingerprints[request_id] = fingerprint
            return "owner", event

    def complete_request(self, request_id: str, response: dict[str, Any],
                         fingerprint: tuple[Any, ...] | None = None) -> None:
        """Publish a request result and release concurrent duplicate callers."""
        with self._cache_lock:
            self._cache[request_id] = dict(response)
            if fingerprint is not None:
                self._request_fingerprints[request_id] = fingerprint
            event = self._inflight.pop(request_id, None)
            while len(self._cache) > 2048:
                expired = next(iter(self._cache))
                self._cache.pop(expired)
                self._request_fingerprints.pop(expired, None)
            if event is not None:
                event.set()

    def cache(self, request_id: str, response: dict[str, Any]) -> None:
        # Retain the small public helper for callers/tests while routing all
        # normal responses through the in-flight aware implementation.
        self.complete_request(request_id, response)

    def submit(self, request_id: str, rtu_id: int, kind: str, point_no: int, value: Any) -> dict[str, Any]:
        item = self.writer.submit_command(rtu_id, kind, point_no, value,
                                          timeout=self.request_timeout, request_id=request_id)
        self.logger.normal(f"TCP 指令已写入 {kind.upper()} {point_no}")
        return item

    @staticmethod
    def error_response(code: str, message: str, status: int, *, request_id: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "error": message, "code": code, "status": int(status)}
        if request_id:
            result["request_id"] = request_id
        return result

    def __enter__(self) -> "CommandServer":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _ensure_command_row(store: ScadaStore) -> dict[str, Any] | None:
    row = _command_row(store)
    if row is not None:
        return row
    try:
        return store.create_row(COMMAND_TABLE, {
            "id": 1, "bind_host": "127.0.0.1", "bind_port": 8890,
            "auth_token": "", "process_enable": 0, "process_status": PROCESS_STOPPED,
            "process_pid": 0, "process_start_time": 0, "process_stop_time": 0,
            "process_heartbeat_time": 0, "process_exit_code": 0, "process_error": "",
            "log_retention_hours": 720,
        })
    except Exception:
        return _command_row(store)

def _command_row(store: ScadaStore) -> dict[str, Any] | None:
    """Return the singleton command-service configuration when its schema exists."""
    try:
        return store.get_row(COMMAND_TABLE, [1])
    except Exception:
        return None


def _update_command_row(store: ScadaStore, **values: Any) -> None:
    row = _command_row(store)
    if row is None:
        return
    allowed = set(row) - {"_version", "id"}
    values = {key: value for key, value in values.items() if key in allowed}
    if values:
        try:
            store.update_row(COMMAND_TABLE, [1], values)
        except Exception:
            # The process registry remains authoritative if a deployment is
            # upgrading its command schema while this process is stopping.
            pass


def run_process(db_path: str | Path = DEFAULT_DB, schema_dir: str | Path = SCHEMA_DIR, *,
                host: str | None = None, port: int | None = None, token: str | None = None,
                log_dir: str | Path | None = None, stop_event: threading.Event | None = None) -> int:
    """Run the managed command server process until disabled or signalled.

    The singleton ``fes_scdfe_command_define`` row is the preferred runtime
    configuration.  Explicit CLI arguments override its bind address, port,
    token and log directory, which keeps standalone diagnostics convenient.
    """
    db = Path(db_path).resolve()
    store = ScadaStore(db, schema_dir)
    config = _ensure_command_row(store) or {}
    configured_host = str(config.get("bind_host") or "127.0.0.1")
    configured_port = int(config["bind_port"]) if config.get("bind_port") is not None else 8890
    configured_token = config.get("auth_token") or None
    # Keep the command process in the same configured log tree as the main
    # supervisor and writer.  ``fes_scdfe_command_define`` intentionally only
    # stores command-server-specific retention; the shared directory remains
    # in ``fes_scdfe_c_define`` so the Web log browser sees every process.
    if log_dir is None:
        try:
            configured_log = log_directory(store, ensure_control_row(store))
        except Exception:
            configured_log = db.parent / "logs"
    else:
        configured_log = log_dir
    retention_hours = int(config.get("log_retention_hours") or 720)
    host = configured_host if host is None else str(host)
    port = configured_port if port is None else int(port)
    token = configured_token if token is None else token
    if not token:
        token = secrets.token_urlsafe(32)
        _update_command_row(store, auth_token=token)
    lock = InstanceLock(db, COMMAND_PROCESS_KEY)
    if not lock.acquire():
        raise RuntimeError("本数据库已有 TCP 指令服务实例")
    lease = None
    server = None
    failed = False
    lease_finished = False
    event = stop_event or threading.Event()
    try:
        _update_command_row(store, process_status=PROCESS_STARTING, process_pid=os.getpid(),
                            process_start_time=int(time.time() * 1000), process_error="", process_exit_code=0)
        lease = ProcessLease(store, COMMAND_PROCESS_KEY)
        # ``ensure_process`` creates the optional command-server registry row
        # disabled by default.  A directly launched managed process must
        # mirror the command schema's enable flag before the first loop, or
        # ``lease.enabled()`` would make it exit immediately even though the
        # configured service is enabled.  Web starts already set this field,
        # but keeping the child self-contained also covers CLI/system starts.
        try:
            store.update_row(PROCESS_TABLE, [COMMAND_PROCESS_KEY], {
                "process_enable": int(bool(config.get("process_enable", 0))),
            })
        except Exception:
            pass
        server = CommandServer(db, host=host, port=port, token=token, log_dir=configured_log,
                              retention_hours=retention_hours).start()
        address = server.address or (host, port)
        _update_command_row(store, bind_host=address[0], bind_port=address[1], process_status=PROCESS_RUNNING,
                            process_pid=os.getpid(), process_heartbeat_time=int(time.time() * 1000))
        while not event.is_set():
            try:
                current = _command_row(store)
                if current is not None and not bool(current.get("process_enable", 1)):
                    break
                if not lease.enabled():
                    break
                lease.heartbeat()
                _update_command_row(store, process_status=PROCESS_RUNNING, process_pid=os.getpid(),
                                    process_heartbeat_time=int(time.time() * 1000))
                ScdfeLogger.cleanup(server.logger.directory, retention_hours=server.retention_hours,
                                    process_name=server.logger.process_name,
                                    packet_directory=server.logger.packet_directory)
            except Exception as exc:  # keep the endpoint alive while recording failure
                try:
                    lease.heartbeat(status=PROCESS_ERROR, error=str(exc), force=True)
                except Exception:
                    pass
                _update_command_row(store, process_status=PROCESS_ERROR, process_error=str(exc))
            event.wait(1.0)
        return 0
    except Exception as exc:
        failed = True
        _update_command_row(store, process_status=PROCESS_ERROR, process_pid=0,
                            process_stop_time=int(time.time() * 1000), process_exit_code=-1, process_error=str(exc))
        if lease is not None:
            lease.finish(str(exc))
            lease_finished = True
        raise
    finally:
        if server is not None:
            server.close()
        if lease is not None and not lease_finished:
            try:
                lease.update(process_enable=0)
                lease.finish()
            except Exception:
                pass
        if not failed:
            _update_command_row(store, process_status=PROCESS_STOPPED, process_pid=0,
                                process_stop_time=int(time.time() * 1000), process_heartbeat_time=int(time.time() * 1000))
        lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="scdfe_c TCP 遥控/遥调指令服务")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--log-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    stop_event = threading.Event()
    try:
        import signal
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is not None:
                signal.signal(sig, lambda *_: stop_event.set())
        return run_process(args.db, args.schema_dir, host=args.host, port=args.port,
                           token=args.token, log_dir=args.log_dir, stop_event=stop_event)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"scdfe_c TCP 指令服务: {exc}", flush=True)
        return 2


__all__ = ["COMMAND_PROCESS_KEY", "CommandClient", "CommandServer", "ENDPOINT_VERSION", "MAX_LINE",
           "REQUEST_TIMEOUT", "endpoint_path", "main", "run_process"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

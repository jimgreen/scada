"""Serialized, asynchronous process control; no PID-based termination."""
from __future__ import annotations

import os
import logging
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from core.process_registry import PROCESS_TABLE, ensure_process, now_ms, process_is_alive
from core.scdfe_control import CONTROL_TABLE, InstanceLock, ServerInstanceLock, ensure_control_row
from web.service import ApiError, CHANNEL_PARAMETER_TABLES

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("scada.process-control")


def popen_options():
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def runtime_python():
    """Return the interpreter used for scdfe_c child processes.

    The Web UI is often launched with the system ``python`` command while
    protocol dependencies are installed in the project virtual environment.
    Reusing ``sys.executable`` in that situation starts workers in the wrong
    environment and every protocol reports a misleading missing-dependency
    error.  An explicit ``SCADA_PYTHON`` override wins; otherwise the local
    project environment is preferred when it exists, with the Web interpreter
    as the portable fallback for deployments without a project venv.
    """
    configured = os.environ.get("SCADA_PYTHON", "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_file():
            return str(configured_path.resolve())
        # Allow a command name such as ``python3`` in PATH.  Let Popen produce
        # its normal useful error if it is not resolvable.
        return configured
    executable = "python.exe" if os.name == "nt" else "python"
    candidate = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / executable
    if candidate.is_file():
        return str(candidate.resolve())
    return sys.executable


class ProcessController:
    def __init__(self, store, *, timeout=150):
        self.store = store
        self.timeout = timeout
        self._mutex = threading.RLock()
        self._operation = None
        self._children = {}
        self._thread = None

    def _row(self, key):
        row = ensure_process(self.store, key)
        pid = int(row["process_pid"] or 0)
        if pid and not process_is_alive(pid):
            # Compare identity so a concurrent registration is never erased.
            with self.store.connection() as connection:
                connection.execute(
                    f"UPDATE {PROCESS_TABLE} SET process_pid=0, process_status=5, "
                    "process_stop_time=?, process_error=?, process_exit_code=-1, _version=_version+1 "
                    "WHERE process_key=? AND owner_token=? AND process_pid=?",
                    (now_ms(), "进程已退出，未收到正常退出状态", key, row["owner_token"], pid),
                )
                connection.commit()
            row = self.store.get_row(PROCESS_TABLE, [key])
        return row

    def snapshot(self):
        control = ensure_control_row(self.store)
        ensure_process(self.store, "main")
        ensure_process(self.store, "writer")
        with self.store.connection() as connection:
            keys = [r[0] for r in connection.execute(
                f"SELECT process_key FROM {PROCESS_TABLE} "
                "ORDER BY CASE kind WHEN 'main' THEN 0 WHEN 'writer' THEN 1 ELSE 2 END, target_rtu"
            )]
        items = []
        for key in keys:
            row = self._row(key)
            row.pop("owner_token", None)
            row["alive"] = bool(row["process_pid"])
            if key == "main":
                row["process_enable"] = control["process_enable"]
            row["log_process"] = {"main": "scdfe_c-main", "writer": "scdfe_c-db-writer"}.get(
                key, f"scdfe_c-debug-rtu-{row['target_rtu']}"
            )
            items.append(row)
        with self._mutex:
            operation = dict(self._operation) if self._operation else None
            if operation:
                operation["steps"] = list(operation["steps"])
        return {"items": items, "operation": operation,
                "busy": bool(operation and operation["status"] == "running") or
                InstanceLock.held(self.store.db_path, "process-control")}

    def _validate_debug(self, key):
        rtu = self.store.get_row("fes_rtu_define", [int(key.split(":")[1])])
        if rtu["process_enable"]:
            raise ApiError(409, "请先在通信监控中停用该 RTU 的自动通信，再启动独立调试")
        if rtu["channel_id"] is None or rtu["channel_type"] not in CHANNEL_PARAMETER_TABLES:
            raise ApiError(409, "该 RTU 尚未配置支持的通道")

    def submit(self, body):
        if set(body) - {"target", "action", "include_in_all"}:
            raise ApiError(400, "未知进程控制参数")
        target, action = body.get("target"), body.get("action")
        if (not isinstance(target, str) or not isinstance(action, str)
                or action not in {"start", "stop", "configure"}):
            raise ApiError(400, "需要 target 和 action=start/stop/configure；重启操作已移除")
        if "include_in_all" in body and type(body["include_in_all"]) is not bool:
            raise ApiError(400, "include_in_all 必须是布尔值")
        if action == "configure" and (not target.startswith("debug:") or "include_in_all" not in body):
            raise ApiError(400, "configure 仅用于调试任务的一键启动配置")
        lock = InstanceLock(self.store.db_path, "process-control")
        with self._mutex:
            if (self._operation and self._operation["status"] == "running") or not lock.acquire():
                raise ApiError(409, "已有进程控制操作正在执行，请等待当前步骤结束", code="process_control_busy")
            try:
                ensure_control_row(self.store)
                if target != "all":
                    if target.startswith("debug:") and (action == "start" or
                                                         (action == "configure" and body["include_in_all"])):
                        self._validate_debug(target)
                    try:
                        row = ensure_process(self.store, target)
                    except ValueError as exc:
                        raise ApiError(400, str(exc)) from exc
                    if action == "start":
                        rtu = row["target_rtu"] if target.startswith("debug:") else None
                        if target != "writer" and not self._row(target)["process_pid"] and ServerInstanceLock.held(rtu):
                            raise ApiError(409, "本服务器已有相应通信实例，等待原实例退出后再启动", code="process_already_running")
                    if "include_in_all" in body:
                        if not target.startswith("debug:"):
                            raise ApiError(400, "仅调试任务可以选择是否参与一键启动")
                        self.store.update_row(PROCESS_TABLE, [target], {"include_in_all": int(body["include_in_all"])})
                elif action == "configure" or "include_in_all" in body:
                    raise ApiError(400, "一键操作只接受 start/stop")
                self._operation = {"id": uuid.uuid4().hex, "target": target, "action": action,
                                   "status": "running", "stage": "准备执行", "steps": [],
                                   "started_at": now_ms(), "finished_at": 0, "error": ""}
                self._thread = threading.Thread(target=self._execute, args=(target, action, lock),
                                                name="scada-process-control", daemon=True)
                try:
                    self._thread.start()
                except Exception as exc:
                    self._operation.update(status="failed", error=str(exc), finished_at=now_ms())
                    raise
                return dict(self._operation)
            except Exception:
                lock.release()
                raise

    def _stage(self, message):
        with self._mutex:
            self._operation["stage"] = message
            self._operation["steps"].append({"time": now_ms(), "message": message})

    def _wait(self, predicate, message):
        deadline = time.monotonic() + self.timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(message + "；停止继续执行后续步骤，实际状态请查看进程列表和日志")
            time.sleep(0.1)

    def _spawn(self, key):
        row = self._row(key)
        if row["process_pid"]:
            return
        rtu = row["target_rtu"] if key.startswith("debug:") else None
        if ServerInstanceLock.held(rtu):
            raise RuntimeError("本服务器已有相应通信实例，请先停止原实例")
        token = uuid.uuid4().hex
        command = [runtime_python(), "-m", "scdfe_c", "--db", str(self.store.db_path),
                   "--schema-dir", str(self.store.catalog.directory), "--launch-token", token]
        if rtu is not None:
            self._validate_debug(key)
            control = ensure_control_row(self.store)
            command += ["--rtu-id", str(rtu), "--scan-interval", "0.2",
                        "--poll-interval", str(control["poll_interval_ms"] / 1000),
                        "--retry-interval", str(control["retry_interval_ms"] / 1000),
                        "--retention-hours", str(control["log_retention_hours"])]
        self.store.update_row(PROCESS_TABLE, [key], {"process_status": 1, "process_error": ""})
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen_options())
        with self._mutex:
            self._children[process.pid] = process

        def reap():
            process.wait()
            with self._mutex:
                self._children.pop(process.pid, None)

        threading.Thread(target=reap, name=f"scada-reap-{process.pid}", daemon=True).start()

        def started():
            if process.poll() is not None:
                raise RuntimeError(f"{key} 启动失败，退出码 {process.returncode}；请查看运行日志")
            item = self._row(key)
            # Windows venv python may launch a second interpreter. The token
            # identifies the runtime that registered, independently of its PID.
            return item["owner_token"] == token and bool(item["process_pid"]) and item["process_status"] == 2
        self._wait(started, f"等待 {key} 启动超时")

    def _start(self, key):
        self._stage(f"启动 {self._label(key)}")
        if key in {"main", "writer"}:
            if key == "writer":
                ensure_process(self.store, "writer")
                self.store.update_row(PROCESS_TABLE, ["writer"], {"process_enable": 1})
            self.store.update_row(CONTROL_TABLE, [1], {"process_enable": 1})
            if key == "writer" and not self._row("main")["process_pid"]:
                self._stage("启动主进程，为数据库写进程建立通信队列")
            self._spawn("main")
            if key == "writer" or self._row("writer")["process_enable"]:
                self._stage("等待写库进程就绪，随后按启动间隔恢复自动通信")
                def writer_ready():
                    if not self._row("main")["process_pid"]:
                        raise RuntimeError("主进程已退出，写库进程无法启动")
                    row = self._row("writer")
                    return bool(row["process_pid"]) and row["process_status"] == 2
                self._wait(writer_ready, "等待写库进程就绪超时")
        else:
            self.store.update_row(PROCESS_TABLE, [key], {"process_enable": 1})
            self._spawn(key)

    def _stop(self, key):
        self._stage(f"停止 {self._label(key)}" + ("：等待通信线程退出、排空写库队列" if key in {"main", "writer"} else ""))
        before = self._row(key)
        pid = before["process_pid"]
        requested_at = now_ms()
        if key == "main":
            self.store.update_row(CONTROL_TABLE, [1], {"process_enable": 0})
        else:
            values = {"process_enable": 0}
            if key == "writer" and self._row("main")["process_pid"]:
                # Parent acknowledges pause even when writer is between crash
                # recovery and registration, closing the transient PID=0 gap.
                values.update(process_status=3, process_stop_time=0)
            self.store.update_row(PROCESS_TABLE, [key], values)
        def stopped():
            row = self._row(key)
            # The original PID must have exited as well as its registry row;
            # finalizers may still be closing handles after finish() writes.
            gone = not row["process_pid"] and (not pid or not process_is_alive(pid))
            if key == "writer" and self._row("main")["process_pid"]:
                gone = gone and row["process_stop_time"] >= requested_at
            return gone
        self._wait(stopped, f"等待 {key} 停止超时")
        if key == "main":
            self._wait(lambda: not self._row("writer")["process_pid"], "等待写库进程退出超时")

    @staticmethod
    def _label(key):
        return {"main": "主进程", "writer": "数据库写进程"}.get(key, "RTU " + key.split(":")[-1] + " 调试进程")

    def _execute(self, target, action, lock):
        try:
            if target == "all":
                with self.store.connection() as connection:
                    debug = [dict(row) for row in connection.execute(
                        f"SELECT * FROM {PROCESS_TABLE} WHERE kind='debug' ORDER BY target_rtu"
                    )]
                if action == "stop":
                    for row in debug:
                        self._stop(row["process_key"])
                    self._stop("writer")
                    self._stop("main")
                if action == "start":
                    # Validate all selected debug tasks before starting dependencies.
                    for row in debug:
                        if row["include_in_all"]:
                            self._validate_debug(row["process_key"])
                    self._start("writer")  # starts main, waits for writer readiness
                    delay = ensure_control_row(self.store)["worker_start_delay_ms"] / 1000
                    for row in debug:
                        if row["include_in_all"]:
                            time.sleep(delay)
                            self._start(row["process_key"])
            elif action != "configure":
                if action == "stop":
                    self._stop(target)
                if action == "start":
                    self._start(target)
            with self._mutex:
                self._operation.update(status="completed", stage="操作完成", finished_at=now_ms())
        except Exception as exc:
            with self._mutex:
                self._operation.update(status="failed", error=str(exc), finished_at=now_ms())
            # Error reporting must remain available even if the DB itself failed.
            try:
                if target != "all":
                    row = self._row(target)
                    if not row["process_pid"] and row["process_status"] == 1:
                        self.store.update_row(PROCESS_TABLE, [target], {
                            "process_status": 5, "process_error": str(exc), "process_exit_code": -1,
                        })
            except Exception:
                LOG.exception("Unable to persist process-control failure")
        finally:
            lock.release()

    def configure_main(self, values):
        """Serialize runtime configuration changes against process jobs."""
        lock = InstanceLock(self.store.db_path, "process-control")
        if not lock.acquire():
            raise ApiError(409, "进程控制正在执行，请稍后修改配置")
        try:
            ensure_control_row(self.store)
            return self.store.update_row(CONTROL_TABLE, [1], values)
        finally:
            lock.release()

"""Process identity and cooperative control shared by the runtime and Web UI."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time
import uuid

PROCESS_TABLE = "fes_process_define"


def now_ms():
    return int(time.time() * 1000)


def process_is_alive(pid):
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def ensure_process(store, key):
    kind, _, target = key.partition(":")
    if key not in {"main", "writer"} and not (
        kind == "debug" and target.isdecimal() and 0 < int(target) < 2**63 and str(int(target)) == target
    ):
        raise ValueError("进程标识必须为 main、writer 或 debug:RTU编号")
    try:
        return store.get_row(PROCESS_TABLE, [key])
    except Exception as exc:
        if getattr(exc, "code", None) != "not_found":
            raise
    with store.connection() as connection:
        connection.execute(
            f"INSERT OR IGNORE INTO {PROCESS_TABLE} "
            "(process_key, kind, target_rtu, process_enable, include_in_all) VALUES (?,?,?,?,?)",
            (key, kind, int(target or 0), int(kind == "writer"), 1),
        )
        connection.commit()
    return store.get_row(PROCESS_TABLE, [key])


class ProcessLease:
    """Only the holder of an OS instance lock may claim this DB identity.

    Conditional writes keep a late heartbeat/finally from replacing a newer
    process's state. PID values are display/probe data, never kill targets.
    """

    def __init__(self, store, key, token=None):
        self.store, self.key = store, key
        self.token = token or uuid.uuid4().hex
        self.next_heartbeat = 0.0
        ensure_process(store, key)
        store.update_row(PROCESS_TABLE, [key], {
            "owner_token": self.token, "process_status": 1,
            "process_pid": os.getpid(), "parent_pid": os.getppid(),
            "process_start_time": now_ms(), "process_heartbeat_time": now_ms(),
            "process_error": "", "process_exit_code": 0,
        })

    def update(self, **values):
        assignments = ", ".join(f'"{name}"=?' for name in values)
        with self.store.connection() as connection:
            result = connection.execute(
                f"UPDATE {PROCESS_TABLE} SET {assignments}, _version=_version+1 "
                "WHERE process_key=? AND owner_token=?",
                (*values.values(), self.key, self.token),
            )
            connection.commit()
        return bool(result.rowcount)

    def heartbeat(self, status=2, error="", *, force=False):
        if force or time.monotonic() >= self.next_heartbeat:
            self.update(process_status=status, process_heartbeat_time=now_ms(), process_error=error)
            self.next_heartbeat = time.monotonic() + 2.0

    def enabled(self):
        row = self.store.get_row(PROCESS_TABLE, [self.key])
        return row["owner_token"] == self.token and bool(row["process_enable"])

    def finish(self, error=""):
        self.update(process_status=5 if error else 0, process_pid=0,
                    process_stop_time=now_ms(), process_heartbeat_time=now_ms(),
                    process_exit_code=1 if error else 0, process_error=error,
                    **({"process_enable": 0} if self.key.startswith("debug:") else {}))

"""Shared scdfe_c control contract and cross-process instance locks."""
from __future__ import annotations

import os
from pathlib import Path
import re


CONTROL_TABLE = "fes_scdfe_c_define"
CONTROL_FIELDS = frozenset({
    "process_enable", "process_restart", "log_dir", "log_retention_hours",
    "scan_interval_ms", "poll_interval_ms", "retry_interval_ms",
    "worker_start_delay_ms",
})
CONFIG_FIELDS = CONTROL_FIELDS - {"process_enable", "process_restart"}
LOG_PATTERN = re.compile(
    r"(?P<process>scdfe_c-[A-Za-z0-9-]+)-(?P<hour>\d{8}-\d{2})\.log$"
)


def validate_control(values):
    unknown = set(values) - CONTROL_FIELDS
    if unknown:
        raise ValueError("不允许修改运行状态字段: " + ", ".join(sorted(unknown)))
    if not values:
        raise ValueError("至少需要修改一个控制字段")
    for name, value in values.items():
        if name == "log_dir":
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError("log_dir 必须是非空目录路径")
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} 必须是整数")
        if name in {"process_enable", "process_restart"}:
            low, high = 0, 1
        elif name == "log_retention_hours":
            low, high = 1, 87600
        elif name == "worker_start_delay_ms":
            low, high = 0, 60000
        else:
            low, high = 50, 60000
        if not low <= value <= high:
            raise ValueError(f"{name} 必须在 {low} 到 {high} 之间")


def log_directory(store, row=None):
    row = row or store.get_row(CONTROL_TABLE, [1])
    path = Path(row["log_dir"])
    return (path if path.is_absolute() else store.db_path.parent / path).resolve()


def ensure_control_row(store):
    """Create the singleton configuration row when the current schema is ready."""
    try:
        return store.get_row(CONTROL_TABLE, [1])
    except Exception as exc:
        if getattr(exc, "code", None) != "not_found":
            raise
        return store.create_row(CONTROL_TABLE, {
            "id": 1, "process_enable": 1, "process_restart": 0,
            "process_status": 0, "process_pid": 0, "process_start_time": 0,
            "process_stop_time": 0, "process_restart_time": 0,
            "process_heartbeat_time": 0, "process_exit_code": 0,
            "process_restart_count": 0, "process_error": "",
            "log_dir": "logs", "log_retention_hours": 720,
            "scan_interval_ms": 2000, "poll_interval_ms": 1000,
            "retry_interval_ms": 5000,
            "worker_start_delay_ms": 250,
        })


class InstanceLock:
    """An OS-released lock; a crashed process never leaves a stale PID lock."""

    def __init__(self, db_path, role="main"):
        self.path = Path(str(db_path) + f".scdfe_c-{role}.lock")
        self.stream = None

    def acquire(self):
        stream = self.path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def release(self):
        if self.stream is not None:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()
            self.stream = None

    @classmethod
    def held(cls, db_path, role="main"):
        lock = cls(db_path, role)
        if not lock.acquire():
            return True
        lock.release()
        return False

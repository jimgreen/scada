"""Shared scdfe_c control contract and cross-process instance locks."""
from __future__ import annotations

import os
from pathlib import Path
import re
import errno
import socket
import sys


CONTROL_TABLE = "fes_scdfe_c_define"
CONTROL_FIELDS = frozenset({
    "process_enable", "log_dir", "log_retention_hours",
    "scan_interval_ms", "poll_interval_ms", "retry_interval_ms",
    "worker_start_delay_ms",
})
CONFIG_FIELDS = CONTROL_FIELDS - {"process_enable"}
LOG_PATTERN = re.compile(
    r"(?P<process>scdfe_c-(?:main|db-writer|(?:debug-)?rtu-\d+))-"
    r"(?P<hour>\d{8}-\d{2})\.log$"
)
LOG_LEVEL_LABELS = {
    "INFO": "日志",
    "WARNING": "警告",
    "ERROR": "错误",
    "PROCESS": "进程",
}
LOG_LEVEL_CODES = {label: code for code, label in LOG_LEVEL_LABELS.items()}
LOG_WRITER_PID = re.compile(r"写库\s*pid\s*=\s*\d+\b", re.IGNORECASE)
LOG_IDENTITY = re.compile(r"\b(?:pid|rtu|channel|chanel|tid)\s*=\s*\d+\b", re.IGNORECASE)


def sanitize_log_message(message):
    """Keep runtime identity fields out of persisted log content."""
    value = LOG_WRITER_PID.sub("", str(message))
    value = LOG_IDENTITY.sub("", value)
    return re.sub(r"[ \t]{2,}", " ", value).strip()


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
        if name == "process_enable":
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
            "id": 1, "process_enable": 1,
            "process_status": 0, "process_pid": 0, "process_start_time": 0,
            "process_stop_time": 0,
            "process_heartbeat_time": 0, "process_exit_code": 0,
            "process_error": "",
            "log_dir": "logs", "log_retention_hours": 720,
            "scan_interval_ms": 2000, "poll_interval_ms": 1000,
            "retry_interval_ms": 5000,
            "worker_start_delay_ms": 250,
        })


class InstanceLock:
    """Database/file-scoped lock for offline tools, not runtime ownership."""

    def __init__(self, db_path, role="main"):
        self.path = Path(str(Path(db_path).resolve()) + f".scdfe_c-{role}.lock")
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


class ServerInstanceLock:
    """Host-wide runtime lease, independent of database, directory and user.

    No RTU means the automatic supervisor; an RTU ID means its communication
    owner (automatic or debug). Windows uses the lifetime of a Global named
    kernel object, Linux an abstract UNIX socket binding. Neither uses a PID
    file or a network port. Closing the handle/socket, including process death,
    releases ownership. Our writer processes use spawn and never inherit it.

    Linux containers with separate network namespaces have separate leases;
    use the host network namespace if several containers share this service.
    """

    def __init__(self, rtu_id: int | None = None):
        if rtu_id is not None and (type(rtu_id) is not int or rtu_id <= 0):
            raise ValueError("rtu_id 必须为正整数")
        self.name = "SCADA.scdfe_c." + ("main" if rtu_id is None else f"rtu-{rtu_id}")
        self._socket = None
        self._handle = None
        self._kernel32 = None

    def acquire(self) -> bool:
        if self._socket is not None or self._handle is not None:
            raise RuntimeError("同一个锁对象不能重复获取")
        if os.name == "nt":
            return self._acquire_windows()
        if not sys.platform.startswith("linux"):
            raise OSError("服务器级 scdfe_c 互斥支持 Windows/Linux")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            # Abstract names are system-wide within the network namespace;
            # filesystem permissions and per-user TMPDIR cannot split them.
            sock.bind("\0" + self.name)
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE:
                return False
            raise
        self._socket = sock
        return True

    def _acquire_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD),
                        ("lpSecurityDescriptor", wintypes.LPVOID),
                        ("bInheritHandle", wintypes.BOOL)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.CreateMutexExW.argtypes = [ctypes.POINTER(SecurityAttributes), wintypes.LPCWSTR,
                                          wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateMutexExW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
                            ctypes.POINTER(wintypes.DWORD)]
        convert.restype = wintypes.BOOL
        descriptor = wintypes.LPVOID()
        # Allow other users/services to probe existence, without granting them
        # mutation rights. CreateMutexEx requests SYNCHRONIZE only. Ownership is
        # the object's lifetime, not thread-affine Wait/ReleaseMutex ownership.
        if not convert("D:(A;;0x00100000;;;WD)", 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexExW(ctypes.byref(attributes), "Global\\" + self.name,
                                            0, 0x00100000)
            error = ctypes.get_last_error()
        finally:
            kernel32.LocalFree(descriptor)
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS: close the probe, not the owner.
            kernel32.CloseHandle(handle)
            return False
        self._kernel32 = kernel32
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
            self._kernel32 = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    @classmethod
    def held(cls, rtu_id: int | None = None) -> bool:
        lock = cls(rtu_id)
        if not lock.acquire():
            return True
        lock.release()
        return False

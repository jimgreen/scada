"""多线程/多进程安全的 scdfe_c / scdfe_t 按小时轮转运行与报文日志写入器。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from itertools import chain
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any

from core.log_paths import log_entries
from core.log_retention import _prune_empty_dirs
from core.scdfe_control import (
    LOG_LEVEL_LABELS,
    LOG_PATTERN as LOG_FILE_RE,
    log_message_text,
)

LOG_CATEGORIES = ("normal", "alarm", "error", "process")


class ScdfeLogger:
    """多线程/多进程安全的轻量日志写入器。

    每个逻辑进程独立使用一个按小时轮转的运行日志文件，例如
    ``2026/10/29/18/scdfe_c-main-20261029-18.log``。通信线程日志文件只使用
    RTU 编号，例如 ``2026/10/29/18/scdfe_c-rtu-7-20261029-18.log``。
    报文通过 ``packet_directory`` 写入同样的时间树和文件名，运行日志与报文目录
    分离；清理旧文件时同时回收空日期/小时目录。
    """

    def __init__(self, directory: Path | str, *, process_name: str, console: bool = False,
                 packet_switch=None, packet_directory: Path | str | None = None,
                 runtime_packet_compat: bool = False):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.packet_directory = (self.directory if packet_directory is None
                                 else Path(packet_directory).resolve())
        self.packet_directory.mkdir(parents=True, exist_ok=True)
        self.console = console
        self.process_name = process_name
        self.packet_switch = packet_switch
        self.runtime_packet_compat = bool(runtime_packet_compat)
        self._write_lock = threading.Lock()
        self._hour_keys = {"runtime": None, "packet": None}
        self._paths = {"runtime": None, "packet": None}
        self._streams = {"runtime": None, "packet": None}
        self._stream_identities = {"runtime": None, "packet": None}
        self._sessions = 0
        self._context_seed = os.urandom(6).hex()
        self._context_serial = 0
        self._business_contexts = {}
        self._context_file = None

    @property
    def _stream(self):
        """兼容旧版调用的运行日志流句柄别名。"""
        return self._streams["runtime"]

    def _close_stream(self, kind=None):
        kinds = ("runtime", "packet") if kind is None else (kind,)
        for stream_kind in kinds:
            stream, self._streams[stream_kind] = self._streams[stream_kind], None
            self._stream_identities[stream_kind] = None
            if stream is None:
                continue
            stream.close()

    @contextmanager
    def session(self):
        """在工作线程生命周期内复用文件句柄，每条记录立即 flush，退出时关闭。"""
        with self._write_lock:
            self._sessions += 1
        try:
            yield self
        finally:
            with self._write_lock:
                self._sessions -= 1
                if not self._sessions:
                    self._close_stream()

    def emit(self, category: str, message: str, *, level: str = "INFO") -> None:
        self._emit_lines(category, (message,), level=level)

    def _emit_lines(self, category, messages, *, level="INFO", context=None, capture=False):
        if category not in LOG_CATEGORIES:
            raise ValueError(f"未知日志类别: {category}")
        label = LOG_LEVEL_LABELS.get(level)
        if label is None:
            raise ValueError(f"未知日志级别: {level}")
        kind = "packet" if level == "PACKET" else "runtime"
        root = self.packet_directory if kind == "packet" else self.directory
        with self._write_lock:
            parent = sys.modules.get("scdfe_c")
            dt_cls = getattr(parent, "datetime", datetime) if parent is not None else datetime
            now = dt_cls.now()
            stamp = f"{now.hour:02d}:{now.minute:02d}:{now.second:02d}"
            hour_key = (now.year, now.month, now.day, now.hour)
            if hour_key != self._hour_keys[kind]:
                self._close_stream(kind)
                directory = root / now.strftime("%Y/%m/%d/%H")
                if not directory.resolve().is_relative_to(root):
                    raise OSError("日志文件超出日志目录")
                directory.mkdir(parents=True, exist_ok=True)
                self._paths[kind] = directory / f"{self.process_name}-{now:%Y%m%d-%H}.log"
                self._hour_keys[kind] = hour_key
            path = self._paths[kind]
            stream = self._streams[kind]
            if stream is not None and os.name != "nt":
                try:
                    stat = path.stat()
                    identity = (stat.st_dev, stat.st_ino)
                except FileNotFoundError:
                    identity = None
                if identity != self._stream_identities[kind]:
                    self._close_stream(kind)
            try:
                if self._streams[kind] is None:
                    if not path.resolve().is_relative_to(root):
                        raise OSError("日志文件超出日志目录")
                    try:
                        self._streams[kind] = path.open("a", encoding="utf-8", newline="")
                    except FileNotFoundError:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        self._streams[kind] = path.open("a", encoding="utf-8", newline="")
                    stat = os.fstat(self._streams[kind].fileno())
                    self._stream_identities[kind] = (stat.st_dev, stat.st_ino)
                context_id = None
                definitions = []
                if context is not None:
                    context_file = (path, self._stream_identities[kind])
                    if context_file != self._context_file:
                        self._business_contexts.clear()
                        self._context_file = context_file
                    key = tuple((name, log_message_text(value)) for name, value in context.items())
                    context_id = self._business_contexts.get(key)
                    if context_id is None:
                        self._context_serial += 1
                        context_id = f"{self._context_seed}.{self._context_serial:x}"
                        definitions.append(
                            "日志上下文 @" + context_id + " " + " ".join(f"{name}={value}" for name, value in key)
                        )
                pending, displayed = [], [] if capture else None
                try:
                    original = (
                        (f"@{context_id} " + log_message_text(message)) for message in messages
                    ) if context_id else messages
                    for message in chain(definitions, original):
                        message = log_message_text(message)
                        if displayed is not None:
                            displayed.append(message)
                        line = f"{stamp} [{label}] {message}\n"
                        pending.append(line)
                        if self.console:
                            print(line, end="", flush=True)
                        if len(pending) >= 128:
                            block = "".join(pending)
                            pending.clear()
                            self._streams[kind].write(block)
                finally:
                    if pending:
                        self._streams[kind].write("".join(pending))
                self._streams[kind].flush()
                if context_id is not None and definitions:
                    if len(self._business_contexts) >= 128:
                        self._business_contexts.pop(next(iter(self._business_contexts)))
                    self._business_contexts[key] = context_id
                return displayed
            except Exception:
                self._close_stream()
                raise
            finally:
                if not self._sessions:
                    self._close_stream()

    def normal(self, message: str) -> None:
        self.emit("normal", message)

    def business_lines(self, lines):
        """批量刷新单帧或单批变化日志并保留全部测点记录。"""
        self._emit_lines("normal", lines)

    def business_context_lines(self, lines, *, context, capture=False):
        """在同一小时文件内仅定义一次共享上下文头并返回预览文本。"""
        return self._emit_lines("normal", lines, context=context, capture=capture)

    def packet(self, direction, data, detail="") -> None:
        if self.packet_switch is not None and not self.packet_switch.enabled():
            return
        from core.packet_trace import packet_lines
        lines = tuple(packet_lines(direction, data, detail))
        self._emit_lines("normal", lines, level="PACKET")
        if self.runtime_packet_compat:
            self._emit_lines("normal", lines, level="INFO")

    def packet_text(self, message: str) -> None:
        """将已格式化的报文文本写入报文日志流。"""
        self._emit_lines("normal", (message,), level="PACKET")

    def alarm(self, message: str) -> None:
        self.emit("alarm", message, level="WARNING")

    def error(self, message: str) -> None:
        self.emit("error", message, level="ERROR")

    def process(self, message: str) -> None:
        self.emit("process", message, level="PROCESS")

    @classmethod
    def cleanup(cls, directory: Path | str, *, retention_hours: int = 720,
                now: float | None = None, process_name: str | None = None,
                packet_directory: Path | str | None = None) -> list[Path]:
        """清理超过保留小时数的按进程小时日志文件，并回收空日期/小时目录。"""
        if isinstance(retention_hours, bool) or retention_hours < 1:
            raise ValueError("retention_hours 必须是正整数")
        parent = sys.modules.get("scdfe_c")
        time_mod = getattr(parent, "time", time) if parent is not None else time
        cutoff = (time_mod.time() if now is None else now) - retention_hours * 3600
        removed: list[Path] = []
        roots = [Path(directory).resolve()]
        if packet_directory is not None:
            packet_root = Path(packet_directory).resolve()
            if packet_root != roots[0]:
                roots.append(packet_root)
        for root in roots:
            if not root.is_dir():
                continue
            for entry, _ in log_entries(root, "scdfe_c-"):
                path = Path(entry.path)
                match = LOG_FILE_RE.fullmatch(entry.name)
                if not match or (process_name is not None and match.group("process") != process_name):
                    continue
                try:
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff and path.resolve().is_relative_to(root):
                        path.unlink()
                        removed.append(path)
                except OSError:
                    continue
            _prune_empty_dirs(root)
        return removed

    @classmethod
    def list_files(cls, directory: Path | str, *, record: str = "runtime",
                   packet_directory: Path | str | None = None) -> list[dict[str, Any]]:
        if record not in {"runtime", "packet"}:
            raise ValueError("记录类型无效")
        if record == "packet" and packet_directory is not None:
            directory = packet_directory
        root = Path(directory).resolve()
        items = []
        if not root.is_dir():
            return items
        for entry, relative_name in log_entries(root, "scdfe_c-"):
            match = LOG_FILE_RE.fullmatch(entry.name)
            if match is None:
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            item = {
                "name": relative_name,
                "process": match.group("process"),
                "hour": match.group("hour"),
                "size": stat.st_size,
                "modified_at": int(stat.st_mtime * 1000),
            }
            process_match = re.fullmatch(r"scdfe_c-(?:debug-)?rtu-(?P<rtu_id>\d+)", match.group("process"))
            if process_match is not None:
                item["rtu_id"] = int(process_match.group("rtu_id"))
            items.append(item)
        return sorted(items, key=lambda item: (item["modified_at"], item["name"]), reverse=True)

    @classmethod
    def read_file(cls, directory: Path | str, filename: str, *, tail: int = 200,
                  record: str = "runtime", packet_directory: Path | str | None = None) -> dict[str, Any]:
        if record not in {"runtime", "packet"}:
            raise ValueError("记录类型无效")
        if record == "packet" and packet_directory is not None:
            directory = packet_directory
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("日志文件名无效")
        if not 1 <= tail <= 5000:
            raise ValueError("tail 必须在 1 到 5000 之间")
        root = Path(directory).resolve()
        candidate = Path(filename)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ValueError("日志文件名无效")
        path = (root / candidate).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("日志文件名无效") from exc
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(filename)
        match = LOG_FILE_RE.fullmatch(path.name)
        if not match:
            raise ValueError("日志文件名无效")
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            start = stat.st_size
            floor = max(0, start - 2_000_000)
            chunks = []
            newline_count = 0
            while start > floor and newline_count <= tail:
                length = min(16_384, start - floor)
                start -= length
                stream.seek(start)
                chunk = stream.read(length)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
            data = b"".join(reversed(chunks))
        if start:
            data = data.partition(b"\n")[2]
        all_lines = data.decode("utf-8", "replace").splitlines()
        lines = all_lines[-tail:]
        item = {
            "name": path.relative_to(root).as_posix(),
            "process": match.group("process"),
            "hour": match.group("hour"),
            "size": stat.st_size,
            "modified_at": int(stat.st_mtime * 1000),
            "content": "\n".join(lines),
            "line_count": len(lines),
            "truncated": bool(start or len(all_lines) > tail),
        }
        process_match = re.fullmatch(
            r"scdfe_c-(?:debug-)?rtu-(?P<rtu_id>\d+)",
            match.group("process"),
        )
        if process_match is not None:
            item["rtu_id"] = int(process_match.group("rtu_id"))
        return item


__all__ = ["LOG_CATEGORIES", "LOG_FILE_RE", "ScdfeLogger"]

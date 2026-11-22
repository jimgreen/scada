"""Hourly persistent s-service logs with a bounded history for the Web."""
from collections import deque
from contextlib import ExitStack
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import threading
import time

from core.log_paths import log_entries, packet_log_directory
from core.scdfe_control import LOG_LEVEL_CODES

_HEADER = re.compile(r"^(\d{2}:\d{2}:\d{2}) \[([^]]+)\] (.*)$")
_SHARED_LOGGERS_LOCK = threading.Lock()
_SHARED_LOGGERS = {}
_CLEANUP_INFLIGHT_LOCK = threading.Lock()
_CLEANUP_INFLIGHT = set()


def _shared_logger(directory: Path, process_name: str, packet_directory: Path):
    """同进程内按 (目录, 进程/通道名) 共享唯一 ScdfeLogger，防止多线程/多实例跨锁写同文件。"""
    key = (str(directory.resolve()), str(process_name), str(packet_directory.resolve()))
    with _SHARED_LOGGERS_LOCK:
        logger = _SHARED_LOGGERS.get(key)
        if logger is None:
            from scdfe_c import ScdfeLogger
            logger = ScdfeLogger(directory, process_name=process_name, packet_directory=packet_directory)
            _SHARED_LOGGERS[key] = logger
        return logger


def _schedule_async_prune_dirs(directory: Path, packet_directory: Path) -> None:
    """将空日期/小时目录回收放到后台线程，避免在报文/运行日志写热路径阻塞。"""
    key = str(directory.resolve())
    with _CLEANUP_INFLIGHT_LOCK:
        if key in _CLEANUP_INFLIGHT:
            return
        _CLEANUP_INFLIGHT.add(key)

    def _worker():
        try:
            from core.log_retention import _prune_empty_dirs
            _prune_empty_dirs(directory)
            _prune_empty_dirs(packet_directory)
        except Exception:
            logging.getLogger(__name__).debug("异步清理 S 服务空日志目录失败", exc_info=True)
        finally:
            with _CLEANUP_INFLIGHT_LOCK:
                _CLEANUP_INFLIGHT.discard(key)

    threading.Thread(target=_worker, name="scdfe-s-log-prune", daemon=True).start()


class ServiceFileLog:
    @staticmethod
    def startup_catalog(directory):
        """One retention-tree walk shared by all nine S services on startup."""
        from scdfe_c.web.log_query import LOG_PATTERNS, _iter_log_files
        catalog = {}
        for path, _, _, process, _ in _iter_log_files(Path(directory), None, LOG_PATTERNS["s"], "scdfe_s-"):
            catalog.setdefault(process, []).append(Path(path))
        return catalog

    def __init__(self, store, service, *, startup_catalog=None, channel_required=False):
        from scdfe_c import ScdfeLogger
        self.name = f"scdfe_s-{service}"
        self._filename_pattern = re.compile(re.escape(self.name) + r"-\d{8}-\d{2}\.log")
        self._channel_filename_pattern = re.compile(r"scdfe_s-channel-([1-9]\d*)-(\d{8}-\d{2})\.log")
        self.directory = (store.db_path.parent / "logs").resolve()
        self.packet_directory = packet_log_directory(self.directory)
        from core.packet_logging import packet_log_switch
        self.packet_switch = packet_log_switch(store.db_path, 's')
        self._lock = threading.RLock()
        self.channel_required = bool(channel_required)
        self._channel_id = None
        self._loggers = {}
        self.logger = self._logger_for(None)
        self._active = None
        self._next_cleanup = 0
        self._next_error = 0
        self._startup_files = None if startup_catalog is None else startup_catalog.get(self.name, ())

    def _logger_for(self, channel_id):
        """返回对应通道或服务的共享日志记录器（由 self._lock 保护）。"""
        with self._lock:
            name = self.name if channel_id is None else f"scdfe_s-channel-{int(channel_id)}"
            logger = self._loggers.get(name)
            if logger is None:
                logger = _shared_logger(self.directory, name, self.packet_directory)
                self._loggers[name] = logger
            return logger

    def bind_channel(self, channel_id):
        """Bind subsequent runtime and packet records to one channel file."""
        if channel_id is not None:
            try:
                channel_id = int(channel_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("channel_id 必须为正整数") from exc
            if channel_id < 1:
                raise ValueError("channel_id 必须为正整数")
        with self._lock:
            if self._active is not None and channel_id != self._channel_id:
                self.end_session()
            self._channel_id = channel_id
            self.logger = self._logger_for(channel_id)
            # A channel has its own history; the shared startup catalogue only
            # applies to the unbound protocol-manager log.
            self._startup_files = None

    @property
    def channel_id(self):
        return self._channel_id

    def start_session(self):
        """Owned by the protocol lifecycle lock; each emit still flushes."""
        with self._lock:
            if self._active is None:
                self._active = ExitStack()
                self._active.enter_context(self.logger.session())

    def end_session(self):
        with self._lock:
            if self._active is not None:
                session, self._active = self._active, None
                session.close()

    def _files(self, record="runtime"):
        if record not in {"runtime", "packet"}:
            raise ValueError("记录类型无效")
        root = self.packet_directory if record == "packet" else self.directory
        # The active service writes channel files.  Keep the unbound service
        # file for manager diagnostics and direct callers, but never mix it
        # with another protocol's channel files.
        names = ({f"scdfe_s-channel-{self._channel_id}"}
                 if self._channel_id is not None else {self.name})
        return (Path(entry.path) for entry, _ in log_entries(root, "scdfe_s-")
                if any(entry.name.startswith(name + "-") for name in names)
                and (self._filename_pattern.fullmatch(entry.name)
                     or self._channel_filename_pattern.fullmatch(entry.name)))

    def write(self, level, message, *, channel_id=None):
        try:
            with self._lock:
                bound_channel = self._channel_id
                policy_channel = bound_channel if channel_id is None else channel_id
                target_channel = policy_channel
                if self.channel_required and bound_channel is not None and target_channel is None:
                    return
                if level == "PACKET" and " frame=" not in str(message):
                    target_channel = bound_channel
                logger = self._logger_for(target_channel)
                compat_logger = self._logger_for(None) if bound_channel is not None else None
            if level == 'PACKET':
                if self.packet_switch.enabled(policy_channel):
                    logger.packet_text(message)
                    if bound_channel is not None and compat_logger is not None:
                        # 显式开启报文录制时保留旧版服务级运行日志兼容视图（默认关闭时零写入）。
                        compat_logger.packet_text(message)
                        compat_logger.emit("normal", message)
            else:
                logger.emit("normal", message, level="INFO" if level == "DEBUG" else level)
            now = time.monotonic()
            should_cleanup = False
            startup_files = None
            with self._lock:
                if now >= self._next_cleanup:
                    self._next_cleanup = now + 3600
                    startup_files = self._startup_files
                    self._startup_files = None
                    should_cleanup = True
            if should_cleanup:
                cutoff = time.time() - 720 * 3600
                files = [(path, self.directory) for path in
                         (self._files("runtime") if startup_files is None else startup_files)]
                files.extend((path, self.packet_directory) for path in self._files("packet"))
                removed_any = False
                for path, root in files:
                    try:
                        if (path.stat().st_mtime < cutoff and path.resolve().is_relative_to(root)
                                and not path.is_symlink()):
                            path.unlink()
                            removed_any = True
                    except OSError:
                        pass
                if removed_any:
                    _schedule_async_prune_dirs(self.directory, self.packet_directory)
        except OSError:
            now = time.monotonic()
            with self._lock:
                log_err = now >= self._next_error
                if log_err:
                    self._next_error = now + 60
            if log_err:
                logging.getLogger(__name__).exception("服务日志写入失败: %s", self.name)

    def recent(self, limit=500, *, record="runtime"):
        from scdfe_c.web.log_query import _reverse_lines
        items = deque(maxlen=limit)
        if not limit:
            return []
        root = self.packet_directory if record == "packet" else self.directory
        files = self._files(record)
        paths = sorted(files, key=lambda path: (path.stem[-11:], path), reverse=True)
        # At most 4 MiB of recent files, independent of retention duration.
        budget = 4 * 1024 * 1024
        for path in paths:
            if budget <= 0 or len(items) >= limit:
                break
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    continue
                day = datetime.strptime(path.stem[-11:], "%Y%m%d-%H")
                with path.open("rb") as stream:
                    size = stream.seek(0, 2)
                    allowance = min(budget, 2 * 1024 * 1024)
                    left = [allowance]
                    timestamps = {}
                    for raw, _, _ in _reverse_lines(stream, size, left, max_line_bytes=allowance):
                        if not raw:
                            continue
                        match = _HEADER.match(raw.decode('utf-8', errors='replace'))
                        if not match:
                            continue
                        clock, label, message = match.groups()
                        if clock not in timestamps:
                            try:
                                stamp = day.replace(hour=int(clock[:2]), minute=int(clock[3:5]), second=int(clock[6:]))
                                timestamps[clock] = stamp.astimezone(timezone.utc).isoformat(timespec='milliseconds')
                            except (ValueError, OSError, OverflowError):
                                timestamps[clock] = None
                        if timestamps[clock] is not None:
                            items.appendleft({'time': timestamps[clock], 'level': LOG_LEVEL_CODES.get(label, 'INFO'), 'message':message})
                            if len(items) >= limit:
                                break
                    budget -= allowance - left[0]
            except (OSError, ValueError):
                continue
        return list(items)

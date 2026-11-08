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
        """Return the logger whose file identity is one communication channel."""
        if channel_id is None:
            name = self.name
        else:
            name = f"scdfe_s-channel-{int(channel_id)}"
        logger = self._loggers.get(name)
        if logger is None:
            from scdfe_c import ScdfeLogger
            logger = ScdfeLogger(self.directory, process_name=name,
                                 packet_directory=self.packet_directory)
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
            target_channel = self._channel_id if channel_id is None else channel_id
            if self.channel_required and target_channel is None:
                return
            logger = self._logger_for(target_channel)
            if level == 'PACKET':
                if self.packet_switch.enabled(target_channel):
                    logger.packet_text(message)
            else:
                logger.emit("normal", message, level="INFO" if level == "DEBUG" else level)
            now = time.monotonic()
            if now >= self._next_cleanup:
                self._next_cleanup = now + 3600
                cutoff = time.time() - 720 * 3600
                files = [(path, self.directory) for path in
                         (self._files("runtime") if self._startup_files is None else self._startup_files)]
                files.extend((path, self.packet_directory) for path in self._files("packet"))
                self._startup_files = None  # later retention checks must visit the live directory
                for path, root in files:
                    try:
                        if (path.stat().st_mtime < cutoff and path.resolve().is_relative_to(root)
                                and not path.is_symlink()):
                            path.unlink()
                    except OSError:
                        pass
        except OSError:
            now = time.monotonic()
            if now >= self._next_error:
                self._next_error = now + 60
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

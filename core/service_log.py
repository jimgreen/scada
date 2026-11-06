"""Hourly persistent s-service logs with a bounded history for the Web."""
from collections import deque
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import time

from core.log_paths import log_entries
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

    def __init__(self, store, service, *, startup_catalog=None):
        from scdfe_c import ScdfeLogger
        self.name = f"scdfe_s-{service}"
        self._filename_pattern = re.compile(re.escape(self.name) + r"-\d{8}-\d{2}\.log")
        self.directory = (store.db_path.parent / "logs").resolve()
        from core.packet_logging import packet_log_switch
        self.packet_switch = packet_log_switch(store.db_path, 's')
        self.logger = ScdfeLogger(self.directory, process_name=self.name)
        self._next_cleanup = 0
        self._next_error = 0
        self._startup_files = None if startup_catalog is None else startup_catalog.get(self.name, ())

    def _files(self):
        # Protocol client/server names share a prefix, but never a log file.
        return (Path(entry.path) for entry, _ in log_entries(self.directory, self.name + "-")
                if self._filename_pattern.fullmatch(entry.name))

    def write(self, level, message, *, channel_id=None):
        try:
            if level != 'PACKET' or self.packet_switch.enabled(channel_id):
                self.logger.emit("normal", message, level="INFO" if level == "DEBUG" else level)
            now = time.monotonic()
            if now >= self._next_cleanup:
                self._next_cleanup = now + 3600
                cutoff = time.time() - 720 * 3600
                files = self._files() if self._startup_files is None else self._startup_files
                self._startup_files = None  # later retention checks must visit the live directory
                for path in files:
                    try:
                        if path.stat().st_mtime < cutoff and path.resolve().is_relative_to(self.directory) and not path.is_symlink():
                            path.unlink()
                    except OSError:
                        pass
        except OSError:
            now = time.monotonic()
            if now >= self._next_error:
                self._next_error = now + 60
                logging.getLogger(__name__).exception("服务日志写入失败: %s", self.name)

    def recent(self, limit=500):
        from scdfe_c.web.log_query import _reverse_lines
        items = deque(maxlen=limit)
        if not limit:
            return []
        files = self._files() if self._startup_files is None else self._startup_files
        paths = sorted(files, key=lambda path: (path.stem[-11:], path), reverse=True)
        # At most 4 MiB of recent files, independent of retention duration.
        budget = 4 * 1024 * 1024
        for path in paths:
            if budget <= 0 or len(items) >= limit:
                break
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(self.directory):
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

"""统一的日志与报文目录保留清理器（支持按小时过期、空目录回收与磁盘总配额水位线）。"""
from __future__ import annotations

import os
from pathlib import Path
import re
import time

from core.log_paths import log_entries

DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024  # 默认日志与报文总配额 4 GiB
_HOURLY_SUFFIX_RE = re.compile(r"-\d{8}-\d{2}\.log$")


def _prune_empty_dirs(root: Path) -> int:
    """自底向上清理日志根目录下的空日期/小时子目录，保留根目录本身。"""
    removed = 0
    if not root.is_dir():
        return 0
    try:
        resolved_root = root.resolve()
    except OSError:
        return 0
    for current, dirnames, filenames in os.walk(resolved_root, topdown=False, followlinks=False):
        current_path = Path(current)
        if current_path == resolved_root:
            continue
        if dirnames or filenames:
            # 再次确认子目录是否已空（子层可能刚刚被删除）
            try:
                if any(current_path.iterdir()):
                    continue
            except OSError:
                continue
        try:
            current_path.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


def cleanup_log_tree(
    log_dir: str | Path,
    *,
    packet_dir: str | Path | None = None,
    retention_hours: float = 720,
    prefix: str = "",
    hourly_only: bool = False,
    max_total_bytes: int | None = DEFAULT_MAX_TOTAL_BYTES,
) -> dict[str, int]:
    """清理过期的运行日志、操作审计日志与报文日志，并按总容量水位线淘汰最旧文件。

    删除顺序：
    1. 优先删除早于 ``retention_hours`` 的所有匹配文件（同时覆盖 ``log_dir`` 和 ``packet_dir``）；
    2. 若剩余文件总大小仍超过 ``max_total_bytes``，优先从最旧的报文文件开始淘汰，再淘汰最旧的运行日志；
    3. 自底向上清理空的 ``YYYY/MM/DD/HH`` 目录。
    """
    roots: list[tuple[bool, Path]] = []
    log_root = Path(log_dir).resolve()
    roots.append((False, log_root))
    if packet_dir is not None:
        pkt_root = Path(packet_dir).resolve()
        if pkt_root != log_root:
            roots.append((True, pkt_root))

    cutoff = time.time() - max(1.0, float(retention_hours)) * 3600.0
    removed_files = 0
    freed_bytes = 0
    retained: list[tuple[int, float, int, Path]] = []

    for is_packet, root in roots:
        if not root.is_dir():
            continue
        for entry, _ in log_entries(root, prefix):
            if hourly_only and not _HOURLY_SUFFIX_RE.search(entry.name):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                path = Path(entry.path)
                if not path.resolve().is_relative_to(root):
                    continue
                if st.st_mtime < cutoff:
                    os.unlink(path)
                    removed_files += 1
                    freed_bytes += st.st_size
                else:
                    # 优先级：0 = 报文（超配额时优先淘汰），1 = 运行/审计日志
                    retained.append((0 if is_packet else 1, st.st_mtime, st.st_size, path))
            except OSError:
                continue

    if max_total_bytes is not None and max_total_bytes > 0:
        total_size = sum(item[2] for item in retained)
        if total_size > max_total_bytes:
            # 优先淘汰报文目录中最早的文件，再淘汰运行日志中最早的文件
            retained.sort(key=lambda item: (item[0], item[1]))
            for _, _, size, path in retained:
                if total_size <= max_total_bytes:
                    break
                try:
                    os.unlink(path)
                    total_size -= size
                    removed_files += 1
                    freed_bytes += size
                except OSError:
                    continue

    removed_dirs = 0
    for _, root in roots:
        removed_dirs += _prune_empty_dirs(root)

    return {
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
        "freed_bytes": freed_bytes,
    }

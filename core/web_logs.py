"""Read-only hourly file catalogue for the s/t log browsers."""
from pathlib import Path

from core.log_paths import log_entries
from scdfe_c.web.log_query import LOG_PATTERNS, search_logs


def log_files(directory, family, query, *, packet_directory=None):
    if set(query) - {"process", "record", "channel_id"}:
        raise ValueError("未知日志文件查询参数")
    record = query.get("record", "runtime") or "runtime"
    if record not in {"runtime", "packet"}:
        raise ValueError("记录类型无效")
    root = Path(packet_directory if record == "packet" and packet_directory is not None else directory).resolve()
    items = []
    prefix = query["process"] + "-" if query.get("process") else f"scdfe_{family}-"
    for entry, name in log_entries(root, prefix):
        match = LOG_PATTERNS[family].fullmatch(entry.name)
        if match is None:
            continue
        hour, process = match.group('hour', 'process')
        if query.get("process") and query["process"] != process:
            continue
        try:
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        items.append({"name": name, "hour": hour, "process": process,
                      "size": stat.st_size, "modified_at": int(stat.st_mtime * 1000)})
    items.sort(key=lambda row: (row["hour"], row["name"]), reverse=True)
    return {"items": items, "total": len(items), "directory": str(root), "record": record}


def log_browser(directory, family, search, query, *, packet_directory=None):
    if family == "t" and "channel_id" in query:
        channel = query["channel_id"]
        if (not isinstance(channel, str) or len(channel) > 19 or not channel.isascii()
                or not channel.isdecimal() or not 0 < int(channel) < 2**63 or str(int(channel)) != channel):
            raise ValueError("转发通道号必须为正整数")
        process = f"scdfe_t-channel-{channel}"
        if query.get("process") and query["process"] != process:
            raise ValueError("转发通道号与进程筛选不一致")
        # One T process owns one channel; historical packets need no channel marker.
        query = {k: v for k, v in query.items() if k != "channel_id"}
        query["process"] = process
    return (search_logs(directory, query, family=family, packet_directory=packet_directory)
            if search else log_files(directory, family, query, packet_directory=packet_directory))

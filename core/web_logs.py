"""Read-only hourly file catalogue for the s/t log browsers."""
from pathlib import Path

from core.log_paths import log_entries
from scdfe_c.web.log_query import LOG_PATTERNS, search_logs


def log_files(directory, family, query):
    if set(query) - {"process"}:
        raise ValueError("未知日志文件查询参数")
    root = Path(directory).resolve()
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
    return {"items": items, "total": len(items), "directory": str(root)}


def log_browser(directory, family, search, query):
    return search_logs(directory, query, family=family) if search else log_files(directory, family, query)

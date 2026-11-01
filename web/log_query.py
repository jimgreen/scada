"""Bounded log searches with byte cursors, across hourly files or one file."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re

from core.scdfe_control import LOG_PATTERN, compact_log_line

MAX_SCAN_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_LINE_BYTES = 128 * 1024
CHUNK_SIZE = 64 * 1024
HEADER = re.compile(r"^(\S+) \[(INFO|WARNING|ERROR|PROCESS)\](?:\s|$)")
CLOCK = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        return stamp.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("时间必须是带时区的 ISO 8601 格式") from exc


def _log_timestamp(value, file_hour):
    """Parse current and legacy log line timestamps.

    Current files contain only local ``HH:MM:SS``. Their date/hour comes from
    the validated hourly filename. Older files may still contain a full ISO
    timestamp, which remains readable for historical log searches.
    """
    if CLOCK.fullmatch(value):
        try:
            stamp = datetime.strptime(file_hour, "%Y%m%d-%H").replace(
                hour=int(value[0:2]), minute=int(value[3:5]), second=int(value[6:8]),
            )
            return stamp.astimezone().timestamp()
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("日志时间无效") from exc
    return _timestamp(value)


def _file_key(name):
    return LOG_PATTERN.fullmatch(name).group("hour"), name


def _encode(payload):
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def _decode(cursor, fingerprint):
    try:
        if len(cursor) > 2048:
            raise ValueError
        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (not isinstance(data, dict) or data.get("query") != fingerprint
                or not isinstance(data.get("file"), str) or not LOG_PATTERN.fullmatch(data["file"])
                or type(data.get("offset")) is not int or data["offset"] < 0
                or not isinstance(data.get("identity"), list) or len(data["identity"]) != 2
                or any(type(n) is not int for n in data["identity"])
                or type(data.get("size")) is not int or data["size"] < data["offset"]
                or type(data.get("discard", False)) is not bool):
            raise ValueError
        return data
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("检索游标无效或筛选条件已改变，请重新筛选") from exc


def _reverse_lines(stream, end, budget, discard=False):
    """Yield whole lines with the byte offset needed to resume before them.

    Oversized lines are discarded without retaining unbounded strings. Reading
    stops at a whole-line boundary; the caller can resume an unfinished chunk.
    """
    position = end
    buffer = b""
    oversized = discard
    while position > 0 or buffer:
        if position:
            length = min(CHUNK_SIZE, position, budget[0])
            position -= length
            stream.seek(position)
            buffer = stream.read(length) + buffer
            budget[0] -= length
        while (split := buffer.rfind(b"\n")) >= 0:
            line = buffer[split + 1:]
            boundary = position + split + 1
            buffer = buffer[:split]
            if oversized or len(line) > MAX_LINE_BYTES:
                yield None, boundary, False
                oversized = False
            elif line:
                yield line.rstrip(b"\r"), boundary, False
        if len(buffer) > MAX_LINE_BYTES:
            oversized = True
            buffer = b""
        if position == 0:
            if oversized or buffer:
                yield None if oversized else buffer.rstrip(b"\r"), 0, False
            break
        if budget[0] <= 0:
            # Resume before the unfinished line, rereading at most 128 KiB.
            # For a very long line the cursor remembers to discard its prefix.
            yield b"", position + len(buffer), oversized
            break


def search_logs(directory, query):
    allowed = {"q", "level", "start", "end", "process", "rtu_id", "filename", "file_query", "limit", "cursor"}
    if set(query) - allowed:
        raise ValueError("未知日志检索参数")
    query = {key: value for key, value in query.items() if value != ""}
    keyword = query.get("q", "").strip()
    if len(keyword) > 256 or len(query.get("file_query", "")) > 256:
        raise ValueError("关键词最多 256 个字符")
    level = query.get("level", "")
    if level not in {"", "INFO", "WARNING", "ERROR", "PROCESS"}:
        raise ValueError("日志类型必须为 INFO、WARNING、ERROR 或 PROCESS")
    try:
        limit = int(query.get("limit", "1000"))
        rtu = int(query["rtu_id"]) if "rtu_id" in query else None
    except ValueError as exc:
        raise ValueError("limit 和 rtu_id 必须为整数") from exc
    if not 1 <= limit <= 5000 or (rtu is not None and not 0 < rtu < 2**63):
        raise ValueError("每批条数必须在 1..5000 之间，RTU 编号必须为有效正整数")
    start = _timestamp(query["start"]) if "start" in query else None
    end = _timestamp(query["end"]) if "end" in query else None
    if start is not None and end is not None and start > end:
        raise ValueError("开始时间不能晚于结束时间")
    filename = query.get("filename")
    if filename and not LOG_PATTERN.fullmatch(filename):
        raise ValueError("日志文件名无效")
    fingerprint = hashlib.sha256(json.dumps(
        {k: v for k, v in query.items() if k not in {"cursor", "limit"}}, sort_keys=True,
    ).encode()).hexdigest()
    cursor = _decode(query["cursor"], fingerprint) if "cursor" in query else None
    root = Path(directory).resolve()
    files = []
    for path in root.glob("scdfe_c-*.log"):
        match = LOG_PATTERN.fullmatch(path.name)
        if not match or (filename and path.name != filename):
            continue
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            continue
        process = match.group("process")
        if query.get("process") and query["process"] != process:
            continue
        if rtu is not None and process not in {f"scdfe_c-rtu-{rtu}", f"scdfe_c-debug-rtu-{rtu}"}:
            continue
        if query.get("file_query", "").casefold() not in path.name.casefold():
            continue
        if cursor and _file_key(path.name) > _file_key(cursor["file"]):
            continue
        files.append(path)
    files.sort(key=lambda p: _file_key(p.name), reverse=True)
    items, scanned_files, skipped = [], 0, 0
    budget = [MAX_SCAN_BYTES]
    result_bytes = 0
    next_cursor = None
    needle = keyword.casefold()
    for index, path in enumerate(files):
        try:
            # Retention cleanup may delete files between listing and opening.
            if path.resolve().parent != root or path.is_symlink():
                continue
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = [stat.st_dev, stat.st_ino]
                offset = stat.st_size
                discard = False
                if cursor and path.name == cursor["file"]:
                    if cursor["identity"] != identity or cursor["size"] > offset:
                        raise ValueError("日志文件已替换或截断，请刷新后重新筛选")
                    offset = cursor["offset"]
                    discard = cursor.get("discard", False)
                scanned_files += 1
                boundary = offset
                for raw, boundary, discard in _reverse_lines(stream, offset, budget, discard):
                    if raw is None:
                        skipped += 1
                    elif raw:
                        line = raw.decode("utf-8", "replace")
                        header = HEADER.match(line)
                        stamp = None
                        if header:
                            try:
                                stamp = _log_timestamp(header[1], match.group("hour"))
                            except ValueError:
                                pass
                        if ((not level or (header and header[2] == level))
                                and (start is None or (stamp is not None and stamp >= start))
                                and (end is None or (stamp is not None and stamp <= end))
                                and (not needle or needle in line.casefold())):
                            items.append({"filename": path.name, "text": compact_log_line(line),
                                          "level": header[2] if header else "",
                                          "time": int(stamp * 1000) if stamp is not None else None})
                            result_bytes += len(raw)
                    if len(items) >= limit or budget[0] <= 0 or result_bytes >= MAX_RESULT_BYTES:
                        if boundary > 0 or index + 1 < len(files):
                            next_cursor = _encode({"file": path.name, "offset": boundary,
                                                   "identity": identity, "query": fingerprint,
                                                   "discard": discard, "size": stat.st_size})
                        break
                if not next_cursor and scanned_files >= 128 and index + 1 < len(files):
                    next_cursor = _encode({"file": path.name, "offset": 0,
                                           "identity": identity, "query": fingerprint, "size": stat.st_size})
                if next_cursor:
                    break
        except FileNotFoundError:
            continue
    return {"items": items, "next_cursor": next_cursor, "has_more": next_cursor is not None,
            "scanned_files": scanned_files, "scanned_bytes": MAX_SCAN_BYTES - budget[0],
            "skipped_lines": skipped, "keyword": keyword,
            "order": "hour_file_desc_line_desc"}

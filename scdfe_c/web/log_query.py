"""Bounded log searches with byte cursors, across hourly files or one file."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re

from core.log_paths import log_entries
from core.scdfe_control import LOG_LEVEL_CODES, LOG_PATTERN

MAX_SCAN_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_LINE_BYTES = 128 * 1024
CHUNK_SIZE = 64 * 1024
HEADER = re.compile(r"^(\d{2}:\d{2}:\d{2}) \[(" + "|".join(LOG_LEVEL_CODES) + r")\](?:\s|$)")
PACKET = re.compile(r"报文\s+(RX|TX)\b")
PACKET_CHANNEL = re.compile(r"\s+通道=(0|[1-9][0-9]*)(?=\s)")
LOG_PATTERNS = {
    "c": LOG_PATTERN,
    # S runtime and packet files are owned by a communication channel.  The
    # protocol service is still present in the process monitor, but it is not
    # used as the log-file identity because one service can be rebound to
    # another RTU/channel over its lifetime.
    "s": re.compile(r"(?P<process>scdfe_s-channel-[1-9]\d*|scdfe_s-(?:iec104|modbus-(?:tcp|rtu)(?:-server)?|can(?:-server)?|opc-server|bacnet-server))-(?P<hour>\d{8}-\d{2})\.log$"),
    "t": re.compile(r"(?P<process>scdfe_t-channel-[1-9]\d*)-(?P<hour>\d{8}-\d{2})\.log$"),
}


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        return stamp.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("时间必须是带时区的 ISO 8601 格式") from exc


def _log_timestamp(value, file_date):
    """Combine the filename date with the HH:MM:SS log clock."""
    try:
        stamp = file_date.replace(
            hour=int(value[0:2]), minute=int(value[3:5]), second=int(value[6:8]),
        )
        return stamp.astimezone().timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("日志时间无效") from exc


def _local_day(stamp):
    if stamp is None:
        return None
    try:
        value = datetime.fromtimestamp(stamp)
        return f'{value.year:04d}{value.month:02d}{value.day:02d}'
    except (ValueError, OverflowError, OSError):
        # Extreme ISO timestamps can exceed platform localtime limits.
        # Pruning is optional; the exact per-line comparison still applies.
        return None


def _file_key(name, pattern=LOG_PATTERN):
    match = pattern.fullmatch(Path(name).name)
    if match is None:
        raise ValueError("日志文件名无效")
    return match.group("hour"), name


def _relative_filename(root: Path, value: str, pattern=LOG_PATTERN) -> str:
    """Validate and normalize a client-supplied relative log path."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("日志文件名无效")
    candidate = Path(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("日志文件名无效")
    normalized = candidate.as_posix()
    if pattern.fullmatch(candidate.name) is None:
        raise ValueError("日志文件名无效")
    path = (root / candidate).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("日志文件名无效") from exc
    return normalized


def _iter_log_files(root: Path, filename: str | None, pattern=LOG_PATTERN, prefix="scdfe_c-"):
    """Yield validated log paths without repeated pathlib resolution.

    The all-files search is already bounded by ``MAX_SCAN_BYTES`` but can
    still visit hundreds of hourly files.  ``Path.rglob`` followed by
    ``resolve``/``is_file`` for every entry performs avoidable Win32 calls.
    ``os.scandir`` supplies directory traversal and non-following symlink
    checks in one pass.  The final boolean marks entries that were checked
    during traversal; an explicitly selected file still receives the stricter
    path checks in ``search_logs`` because it may disappear or be replaced
    between enumeration and opening.
    """
    if filename is not None:
        path = root / filename
        match = pattern.fullmatch(path.name)
        if match is not None:
            yield path, filename, match.group("hour"), match.group("process"), False
        return
    for entry, name in log_entries(root, prefix):
        match = pattern.fullmatch(entry.name)
        if match is not None:
            yield entry.path, name, match.group("hour"), match.group("process"), True


def _encode(payload):
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def _decode(cursor, fingerprint, pattern=LOG_PATTERN):
    try:
        if len(cursor) > 2048:
            raise ValueError
        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (not isinstance(data, dict) or data.get("query") != fingerprint
                or not isinstance(data.get("file"), str)
                or Path(data["file"]).is_absolute()
                or any(part in {"", ".", ".."} for part in Path(data["file"]).parts)
                or pattern.fullmatch(Path(data["file"]).name) is None
                or type(data.get("offset")) is not int or data["offset"] < 0
                or not isinstance(data.get("identity"), list) or len(data["identity"]) != 2
                or any(type(n) is not int for n in data["identity"])
                or type(data.get("size")) is not int or data["size"] < data["offset"]
                or type(data.get("discard", False)) is not bool):
            raise ValueError
        return data
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("检索游标无效或筛选条件已改变，请重新筛选") from exc


def _reverse_lines(stream, end, budget, discard=False, *, max_line_bytes=None, initial_chunk=None):
    """Yield whole lines with the byte offset needed to resume before them.

    Oversized lines are discarded without retaining unbounded strings. Reading
    stops at a whole-line boundary; the caller can resume an unfinished chunk.
    """
    position = end
    if max_line_bytes is None:
        max_line_bytes = MAX_LINE_BYTES
    buffer = b""
    oversized = discard
    chunk = min(initial_chunk or CHUNK_SIZE, CHUNK_SIZE)
    while position > 0 or buffer:
        if position:
            length = min(chunk, position, budget[0])
            chunk = min(CHUNK_SIZE, chunk * 2)
            position -= length
            stream.seek(position)
            buffer = stream.read(length) + buffer
            budget[0] -= length
        # Split once per chunk instead of copying the remaining buffer for
        # every short line (quadratic copying within each 64 KiB chunk).
        parts = buffer.split(b"\n")
        boundary = position + len(buffer) + 1
        buffer = parts[0]
        for line in reversed(parts[1:]):
            boundary -= len(line) + 1
            if oversized or len(line) > max_line_bytes:
                yield None, boundary, False
                oversized = False
            elif line:
                yield line.rstrip(b"\r"), boundary, False
        if len(buffer) > max_line_bytes:
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


def search_logs(directory, query, *, family="c", packet_directory=None):
    pattern = LOG_PATTERNS[family]
    allowed = {"q", "level", "start", "end", "process", "rtu_id", "filename", "file_query", "limit", "cursor", "record", "direction"}
    if family in {"c", "s"}:
        allowed.add("channel_id")
    if set(query) - allowed:
        raise ValueError("未知日志检索参数")
    query = {key: value for key, value in query.items() if value != ""}
    record, direction = query.get("record", ""), query.get("direction", "")
    if record not in {"", "packet", "runtime"} or direction not in {"", "RX", "TX"}:
        raise ValueError("记录类型或收发方向无效")
    channel = query.get("channel_id")
    if channel is not None and (len(channel) > 19 or not channel.isascii() or not channel.isdecimal()
                                or str(int(channel)) != channel or int(channel) >= 2**63):
        raise ValueError("通道编号必须为有效非负整数")
    keyword = query.get("q", "").strip()
    if len(keyword) > 256 or len(query.get("file_query", "")) > 256:
        raise ValueError("关键词最多 256 个字符")
    level = query.get("level", "")
    if level and level not in LOG_LEVEL_CODES.values():
        raise ValueError("日志类型必须为 INFO、WARNING、ERROR、PROCESS 或 PACKET")
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
    # File dates are part of the persisted format. Exclude other local days
    # before opening files or consuming a scan page; retain per-line clock
    # and timezone conversion for exact boundaries and DST behavior.
    start_day = _local_day(start)
    end_day = _local_day(end)
    # A PACKET level query is an explicit packet-log request even when a
    # caller omits the redundant record=packet parameter.  The Web UI always
    # sends both, while scripts may send only the level filter.
    packet_query = record == "packet" or (not record and level == "PACKET")
    if packet_query and packet_directory is not None:
        root = Path(packet_directory).resolve()
    else:
        root = Path(directory).resolve()
    filename = _relative_filename(root, query["filename"], pattern) if query.get("filename") else None
    fingerprint = hashlib.sha256(json.dumps(
        {k: v for k, v in query.items() if k not in {"cursor", "limit"}}, sort_keys=True,
    ).encode()).hexdigest()
    cursor = _decode(query["cursor"], fingerprint, pattern) if "cursor" in query else None
    files = []
    # An explicitly selected file does not require walking the retention tree.
    # The all-files path uses scandir so each directory entry is checked once.
    prefix = query["process"] + "-" if query.get("process") else f"scdfe_{family}-"
    paths = _iter_log_files(root, filename, pattern, prefix)
    cursor_key = _file_key(cursor["file"], pattern) if cursor else None
    file_needle = query.get("file_query", "").casefold()
    for path, relative_name, file_hour, process, enumerated in paths:
        if not enumerated:
            if path.is_symlink() or not path.is_file():
                continue
            try:
                path.resolve().relative_to(root)
            except ValueError:
                continue
        if query.get("process") and query["process"] != process:
            continue
        if (family == "s" and channel is not None
                and process.startswith("scdfe_s-channel-")
                and process != f"scdfe_s-channel-{channel}"):
            continue
        if rtu is not None and process not in {f"scdfe_c-rtu-{rtu}", f"scdfe_c-debug-rtu-{rtu}"}:
            continue
        if file_needle not in relative_name.rsplit("/", 1)[-1].casefold():
            continue
        if ((start_day is not None and file_hour[:8] < start_day)
                or (end_day is not None and file_hour[:8] > end_day)):
            continue
        key = (file_hour, relative_name)
        if cursor_key and key > cursor_key:
            continue
        files.append((path, relative_name, key[0]))
    files.sort(key=lambda item: (item[2], item[1]), reverse=True)
    items, scanned_files, skipped = [], 0, 0
    budget = [MAX_SCAN_BYTES]
    result_bytes = 0
    next_cursor = None
    needle = keyword.casefold()
    # These exact byte sequences are necessary (but not sufficient) for a
    # match. Keep Unicode casefold and header parsing for every candidate.
    if channel is not None and family != "s":
        required = f"通道={channel}".encode()  # the delimiter may be any whitespace
    elif direction:
        required = direction.encode('ascii')
    elif level:
        required = next(f'[{label}]'.encode() for label, code in LOG_LEVEL_CODES.items() if code == level)
    else:
        required = '报文'.encode() if record == 'packet' else None
    for index, (path, relative_name, file_hour) in enumerate(files):
        process = pattern.fullmatch(Path(relative_name).name).group("process")
        file_channel = (process.removeprefix("scdfe_s-channel-")
                        if family == "s" and process.startswith("scdfe_s-channel-") else None)
        try:
            # Retention cleanup may delete files between listing and opening.
            if not Path(path).resolve().is_relative_to(root):
                continue
            with open(path, "rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = [stat.st_dev, stat.st_ino]
                offset = stat.st_size
                discard = False
                if cursor and relative_name == cursor["file"]:
                    if cursor["identity"] != identity or cursor["size"] > offset:
                        raise ValueError("日志文件已替换或截断，请刷新后重新筛选")
                    offset = cursor["offset"]
                    discard = cursor.get("discard", False)
                scanned_files += 1
                try:
                    file_date = datetime.strptime(file_hour, "%Y%m%d-%H")
                except (ValueError, OverflowError, OSError):
                    file_date = None
                # The log clock has second precision. Cache only within this
                # file/query so repeated seconds share the same conversion,
                # without stale timezone data or unbounded cross-file state.
                timestamps = {}
                boundary = offset
                # Small live previews usually need only the tail of the file.
                # Sparse filters grow to normal scan blocks after the first reads.
                for raw, boundary, discard in _reverse_lines(
                        stream, offset, budget, discard, initial_chunk=4096 if limit <= 32 else None):
                    if raw is None:
                        skipped += 1
                    line = raw.decode("utf-8", "replace") if raw and (required is None or required in raw) else None
                    if line is not None and (not needle or needle in line.casefold()):
                        header = HEADER.match(line)
                        header_level = LOG_LEVEL_CODES[header[2]] if header else ""
                        packet = (PACKET.search(line, header.end()) if (record or direction or channel is not None)
                                  and header_level in {"INFO", "PACKET"} else None)
                        # Historical packets used [日志]; keep their record and
                        # direction filters while new records have their own type.
                        # Error/warning messages about packets remain runtime logs.
                        is_packet = header_level == "PACKET" or bool(packet)
                        # Cheap filters first; rejected records need no local
                        # datetime conversion. Always reach the scan-budget
                        # checks below, including when nothing matches.
                        owner = PACKET_CHANNEL.match(line, packet.end()) if packet and channel is not None else None
                        if ((channel is None or (file_channel == channel) or (owner and owner[1] == channel))
                                and (not level or header_level == level)
                                and (not record or is_packet == (record == "packet"))
                                and (not direction or (packet and packet[1] == direction))):
                            stamp = None
                            if header and file_date is not None:
                                clock = header[1]
                                try:
                                    stamp = timestamps[clock]
                                except KeyError:
                                    try:
                                        stamp = _log_timestamp(clock, file_date)
                                    except ValueError:
                                        pass
                                    timestamps[clock] = stamp
                            if ((start is None or (stamp is not None and stamp >= start))
                                    and (end is None or (stamp is not None and stamp <= end))):
                                items.append({"filename": relative_name, "text": line,
                                              "level": header_level,
                                              "time": int(stamp * 1000) if stamp is not None else None})
                                result_bytes += len(raw)
                    if len(items) >= limit or budget[0] <= 0 or result_bytes >= MAX_RESULT_BYTES:
                        if boundary > 0 or index + 1 < len(files):
                            next_cursor = _encode({"file": relative_name, "offset": boundary,
                                                   "identity": identity, "query": fingerprint,
                                                   "discard": discard, "size": stat.st_size})
                        break
                if not next_cursor and scanned_files >= 128 and index + 1 < len(files):
                    next_cursor = _encode({"file": relative_name, "offset": 0,
                                           "identity": identity, "query": fingerprint, "size": stat.st_size})
                if next_cursor:
                    break
        except FileNotFoundError:
            continue
    return {"items": items, "next_cursor": next_cursor, "has_more": next_cursor is not None,
            "scanned_files": scanned_files, "scanned_bytes": MAX_SCAN_BYTES - budget[0],
            "skipped_lines": skipped, "keyword": keyword,
            "order": "hour_file_desc_line_desc"}

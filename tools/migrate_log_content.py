"""One-time log conversion. Stop log writers before applying this tool."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

# Allow both ``python -m tools.migrate_log_content`` and direct execution from
# the tools directory on Windows/Linux.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scdfe_control import LOG_LEVEL_LABELS, LOG_PATTERN


HEADER = re.compile(
    r"^(?P<stamp>\S+) \[(?P<level>INFO|WARNING|ERROR|PROCESS|日志|警告|错误|进程)\](?P<message>.*)$"
)
OLD_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
IDENTITY = re.compile(
    r"(?<!\S)(?:写库\s*pid|pid|rtu|channel|chanel|tid)\s*=\s*\d+(?=$|\s|[:;,，；：])",
    re.IGNORECASE,
)


def normalize_line(line: str) -> str:
    """Return one log line with Chinese types and no runtime identities."""
    newline = ""
    if line.endswith("\r\n"):
        body, newline = line[:-2], "\r\n"
    elif line.endswith(("\n", "\r")):
        body, newline = line[:-1], line[-1:]
    else:
        body = line
    match = HEADER.match(body)
    if not match:
        return line
    stamp, level, message = match.group("stamp", "level", "message")
    old_stamp = OLD_TIMESTAMP.fullmatch(stamp)
    if old_stamp:
        stamp = old_stamp[1]
    message, removed = IDENTITY.subn("", message)
    if removed:
        message = re.sub(r"[ \t]{2,}", " ", message).rstrip()
        message = re.sub(r"[ \t]+([:;,，；：])", r"\1", message)
    return f"{stamp} [{LOG_LEVEL_LABELS.get(level, level)}]{message}" + newline


def _signature(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def migrate_file(path: Path, *, dry_run: bool = False) -> tuple[bool, int]:
    before = _signature(path)
    changed_lines = 0
    temporary = None
    output = None
    try:
        if not dry_run:
            output = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                                 dir=path.parent, prefix=f".{path.name}.", delete=False)
            temporary = Path(output.name)
        with path.open("r", encoding="utf-8", newline="") as source:
            for line in source:
                normalized = normalize_line(line)
                changed_lines += int(normalized != line)
                if output:
                    output.write(normalized)
        if output:
            output.close()
        if changed_lines and not dry_run:
            if _signature(path) != before:
                raise RuntimeError(f"日志仍在写入，请先停止通信进程: {path}")
            shutil.copystat(path, temporary)
            os.replace(temporary, path)
        return bool(changed_lines), changed_lines
    finally:
        if output:
            output.close()
        if temporary and temporary.exists():
            temporary.unlink()


def migrate(root: Path | str, *, dry_run: bool = False) -> dict[str, int]:
    root = Path(root).resolve()
    changed_files = changed_lines = scanned_files = 0
    if root.is_dir():
        for path in root.rglob("*.log"):
            if path.is_symlink() or not path.is_file() or LOG_PATTERN.fullmatch(path.name) is None:
                continue
            try:
                path.resolve().relative_to(root)
            except ValueError:
                continue
            scanned_files += 1
            changed, lines = migrate_file(path, dry_run=dry_run)
            changed_files += int(changed)
            changed_lines += lines
    return {
        "scanned_files": scanned_files,
        "changed_files": changed_files,
        "changed_lines": changed_lines,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = migrate(args.directory, dry_run=args.dry_run)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

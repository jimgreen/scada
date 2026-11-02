"""Move flat SCADA hourly logs into ``YYYY/MM/DD/HH`` directories.

The migration is deliberately limited to the configured log root. Legacy
channel-suffixed communication files are merged into the RTU-only filename so
the current API continues to expose one file per RTU and hour.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil


CURRENT = re.compile(
    r"^(?P<process>scdfe_c-(?:main|db-writer|(?:debug-)?rtu-\d+))-(?P<hour>\d{8}-\d{2})\.log$"
)
LEGACY_CHANNEL = re.compile(
    r"^scdfe_c-rtu-(?P<rtu>\d+)-ch-\d+-(?P<hour>\d{8}-\d{2})\.log$"
)


def _target(root: Path, process: str, hour: str) -> Path:
    year, month, day, clock_hour = hour[:4], hour[4:6], hour[6:8], hour[9:]
    directory = root / year / month / day / clock_hour
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{process}-{hour}.log"


def _append(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_size = source.stat().st_size
    source_ends_newline = True
    if source_size:
        with source.open("rb") as stream:
            stream.seek(-1, 2)
            source_ends_newline = stream.read(1) == b"\n"
    destination_size = destination.stat().st_size if destination.exists() else 0
    with source.open("rb") as src, destination.open("ab") as dst:
        if destination_size:
            with destination.open("rb") as dst_tail:
                dst_tail.seek(-1, 2)
                if dst_tail.read(1) != b"\n":
                    dst.write(b"\n")
        shutil.copyfileobj(src, dst)
        if source_size and not source_ends_newline:
            dst.write(b"\n")
    source.unlink()


def migrate(root: Path | str) -> dict[str, int | str]:
    directory = Path(root).resolve()
    if not directory.is_dir():
        raise FileNotFoundError(directory)
    moved = merged = 0
    # Merged channel files are ordered by their last write time so the single
    # RTU file remains close to the original event order. The filename is a
    # deterministic tie breaker for files with equal timestamps.
    sources = sorted(
        directory.glob("*.log"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    for source in sources:
        current = CURRENT.fullmatch(source.name)
        legacy = LEGACY_CHANNEL.fullmatch(source.name)
        if current:
            destination = _target(directory, current["process"], current["hour"])
            if destination.exists():
                _append(source, destination)
                merged += 1
            else:
                shutil.move(str(source), str(destination))
                moved += 1
        elif legacy:
            destination = _target(directory, f"scdfe_c-rtu-{legacy['rtu']}", legacy["hour"])
            _append(source, destination)
            merged += 1
    return {"directory": str(directory), "moved": moved, "merged": merged}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(migrate(args.directory))


if __name__ == "__main__":
    main()

"""Rebuild the patched source archive from the unmodified, SHA-256 pinned PyPI sdist."""
import argparse
import copy
import gzip
import hashlib
import io
from pathlib import Path
import re
import tarfile


VERSION = "2.2.1+scada.3"
UPSTREAM_SHA256 = "d1b427f94cd0eeef376020a250ea0e7deb97ac33df5ee4908c7b20452cc8bb23"
# Backport the wait predicate from upstream commit 7f4b22d1ec71b450ace8a3c0a384a1e8db756e09.
WAIT = b'''      if (response_wait.wait_until(map_lock, end) == std::cv_status::timeout) {
        DEBUG_PRINT(Debug::Connection,
                    "await_command_success] Timeout " + cmdId);
        break; // timeout
      }'''
ANCHOR = b'''      DEBUG_PRINT(Debug::Connection,
                  "await_command_success] Non-topic " + cmdId);'''


def replace_once(data, before, after):
    if data.count(before) != 1:
        raise ValueError("upstream source does not match the pinned patch")
    return data.replace(before, after)


def command_patches(filename='command_contracts.patch'):
    """Read a context-checked unified diff; no fuzzy application is allowed."""
    lines = Path(__file__).with_name(filename).read_text(encoding='utf-8').splitlines(keepends=True)
    patches = {}
    index = 0
    while index < len(lines):
        if lines[index].startswith('--- a/'):
            name = lines[index][6:].strip()
            index += 1
            if lines[index].strip() != '+++ b/' + name:
                raise ValueError('patch may not rename files')
            patches[name] = []
        elif lines[index].startswith('@@ '):
            header = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', lines[index])
            if header is None: raise ValueError('invalid patch hunk')
            old, new = [], []
            index += 1
            while index < len(lines) and not lines[index].startswith(('@@ ', '--- a/')):
                line = lines[index]
                if line[0] in ' -': old.append(line[1:])
                if line[0] in ' +': new.append(line[1:])
                if line[0] not in ' +-': raise ValueError('unsupported patch line')
                index += 1
            if len(old) != int(header[2] or 1) or len(new) != int(header[4] or 1):
                raise ValueError('invalid patch line counts')
            patches[name].append((int(header[1])-1, old, new))
            continue
        index += 1
    return patches


def apply_command_patch(data, hunks):
    source = data.decode('utf-8').replace('\r\n', '\n').splitlines(keepends=True)
    result, cursor = [], 0
    for start, before, after in hunks:
        if start < cursor or source[start:start+len(before)] != before:
            raise ValueError('native source does not exactly match command patch')
        result.extend(source[cursor:start]); result.extend(after)
        cursor = start+len(before)
    result.extend(source[cursor:])
    return ''.join(result).encode('utf-8')


def backport(source, destination):
    if hashlib.sha256(source.read_bytes()).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("unexpected upstream sdist checksum")
    patches = command_patches()
    soe_patches = command_patches('soe_delivery.patch')
    with (tarfile.open(source) as src, destination.open("wb") as f,
          gzip.GzipFile(fileobj=f, filename="", mode="wb", mtime=0) as gz,
          tarfile.open(fileobj=gz, mode="w") as dst):
        for original in src:
            entry = copy.copy(original)
            entry.name = entry.name.replace("c104-2.2.1", "c104-" + VERSION, 1)
            entry.pax_headers = {}
            stream = None
            if entry.isfile():
                data = src.extractfile(original).read()
                if entry.name.endswith("/src/remote/Connection.cpp"):
                    data = data.replace(b"\r\n", b"\n")
                    data = replace_once(replace_once(data, WAIT + b"\n\n", b""), ANCHOR, ANCHOR + b"\n\n" + WAIT)
                elif entry.name.endswith("/depends/lib60870/lib60870-C/src/CMakeLists.txt"):
                    # MSVC otherwise emits the same .lib for the shared DLL's
                    # import library and the static library actually linked.
                    data += b"\nif(WIN32)\n  set_target_properties(lib60870-shared PROPERTIES ARCHIVE_OUTPUT_NAME lib60870_import)\nendif()\n"
                elif entry.name == f"c104-{VERSION}/pyproject.toml":
                    data = replace_once(data, b'version = "2.2.1"', f'version = "{VERSION}"'.encode())
                elif entry.name.endswith("/PKG-INFO"):
                    data = data.replace(b"\r\n", b"\n")
                    data = replace_once(data, b"\nVersion: 2.2.1\n", f"\nVersion: {VERSION}\n".encode())
                relative = entry.name.removeprefix(f'c104-{VERSION}/')
                if relative in patches:
                    data = apply_command_patch(data, patches.pop(relative))
                if relative in soe_patches:
                    data = apply_command_patch(data, soe_patches.pop(relative))
                entry.size = len(data)
                stream = io.BytesIO(data)
            dst.addfile(entry, stream)
    if patches:
        raise ValueError(f'patch files absent from sdist: {list(patches)}')
    if soe_patches:
        raise ValueError(f'SOE patch files absent from sdist: {list(soe_patches)}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    backport(args.source, Path(__file__).with_name(f"c104-{VERSION}.tar.gz"))

"""Traversal of regular log files without following directory or file links."""
import os
import stat


def log_entries(root, prefix=""):
    """Yield a DirEntry and its slash-separated relative name; reuse scandir metadata."""
    stack = [(os.fspath(root), "")]
    while stack:
        current, base = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    directory = entry.is_dir(follow_symlinks=False)
                    if not directory and not (entry.name.startswith(prefix) and entry.name.endswith(".log")):
                        continue
                    # Windows junctions are directories and is_symlink() is false.
                    if entry.is_symlink() or (os.name == "nt" and
                            entry.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                        continue
                    if directory:
                        stack.append((entry.path, base + entry.name + "/"))
                    elif entry.is_file(follow_symlinks=False):
                        yield entry, base + entry.name
                except OSError:
                    continue

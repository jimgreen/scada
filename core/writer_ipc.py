"""Public writer IPC client shared by independent SCADA runtimes.

The transport and authenticated endpoint are owned by ``scdfe_c.writer_ipc``.
This facade lets ``scdfe_t`` import a stable core path without maintaining a
second socket protocol or creating a second SQLite writer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class WriterIpcError(RuntimeError):
    """The scdfe_c writer is unavailable or rejected a command."""


def metadata_path(db_path: str | Path) -> Path:
    from scdfe_c.writer_ipc import endpoint_path
    return endpoint_path(db_path)


class WriterCommandClient:
    """Submit one YK/YT command through the existing scdfe_c writer."""

    def __init__(self, db_path: str | Path, *, timeout: float = 5.0) -> None:
        self.db_path = Path(db_path).resolve()
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout 必须为正数")

    def submit_command(self, rtu_id: int, kind: str, pnt_no: int, value: Any,
                       *, request_id: str | None = None) -> dict[str, Any]:
        try:
            from scdfe_c.writer_ipc import WriterClient, WriterRequestError, WriterUnavailable
            return WriterClient(self.db_path, timeout=self.timeout).submit_command(
                rtu_id, kind, pnt_no, value, timeout=self.timeout,
                request_id=request_id,
            )
        except (WriterRequestError, WriterUnavailable, OSError, ConnectionError) as exc:
            raise WriterIpcError(str(exc)) from exc


__all__ = ["WriterCommandClient", "WriterIpcError", "metadata_path"]

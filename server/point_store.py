"""Thread-safe four-remote point storage shared by protocol servers."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import threading
from typing import Any, Iterable, Mapping


def coerce_value(kind: str, value: Any) -> Any:
    if kind in {"yx", "yk"}:
        return bool(value)
    if kind in {"yc", "yt"}:
        return float(value)
    raise ValueError("kind 必须是 yc、yx、yk 或 yt")


@dataclass
class PointValue:
    kind: str
    pnt_no: int
    value: Any = 0
    status: int = 0
    name: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


class PointStore:
    """A small in-memory point model used by every standalone server.

    The database remains the source of configured point definitions.  This
    store is deliberately independent from SQLite so a protocol server can be
    exercised without opening the SCADA database.
    """

    def __init__(self, points: Iterable[Mapping[str, Any]] | None = None):
        self._lock = threading.RLock()
        self._points: dict[tuple[str, int], PointValue] = {}
        for point in points or ():
            self.define(point)

    def define(self, point: Mapping[str, Any] | PointValue, **kwargs) -> PointValue:
        row = point.public() if isinstance(point, PointValue) else {**point, **kwargs}
        kind = str(row.get("kind", ""))
        pnt_no = int(row.get("pnt_no", 0))
        if kind not in {"yc", "yx", "yk", "yt"} or pnt_no < 1:
            raise ValueError("点定义必须包含有效的 kind 和正整数 pnt_no")
        item = PointValue(
            kind=kind,
            pnt_no=pnt_no,
            value=coerce_value(kind, row.get("value", 0)),
            status=int(row.get("status", 0) or 0),
            name=str(row.get("name", "") or ""),
        )
        with self._lock:
            self._points[(kind, pnt_no)] = item
            return PointValue(**item.public())

    def ensure_defaults(self) -> None:
        for kind in ("yc", "yx", "yk", "yt"):
            if (kind, 1) not in self._points:
                self.define({"kind": kind, "pnt_no": 1, "name": f"{kind.upper()}_1"})

    def update(self, kind: str, pnt_no: int, value: Any, *, status: int | None = None) -> PointValue:
        key = (str(kind), int(pnt_no))
        with self._lock:
            item = self._points.get(key)
            if item is None:
                self.define({"kind": key[0], "pnt_no": key[1]})
                item = self._points[key]
            item.value = coerce_value(item.kind, value)
            if status is not None:
                item.status = int(status)
            return PointValue(**item.public())

    def get(self, kind: str, pnt_no: int) -> PointValue | None:
        with self._lock:
            item = self._points.get((str(kind), int(pnt_no)))
            return PointValue(**item.public()) if item else None

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [item.public() for item in self._points.values()]

    def values(self) -> dict[tuple[str, int], Any]:
        with self._lock:
            return {key: item.value for key, item in self._points.items()}

"""Persistence and process control for ``scdfe_t`` transmission channels.

The transmission tables are owned by the SCADA schema.  This module deliberately
uses :class:`scdfe_c.web.service.ScadaStore` instead of a second database so
that channel definitions, point mappings and runtime state are visible to the
same operators and obey the same optimistic-version rules.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from core.process_registry import process_is_alive
from scdfe_c.web.service import ApiError, DEFAULT_DB, SCHEMA_DIR, ScadaStore

CHANNEL_TABLE = "fes_scdfe_t_define"
POINT_TABLE = "fes_scdfe_t_point_define"


def _now_ms() -> int:
    return int(time.time() * 1000)


class TransStore:
    """CRUD, process lifecycle and log projection for transmission channels."""

    def __init__(self, db_path: Path | str = DEFAULT_DB, schema_dir: Path | str = SCHEMA_DIR):
        self.store = ScadaStore(db_path, schema_dir)
        self.db_path = Path(db_path)
        self.schema_dir = Path(schema_dir)
        self._lock = threading.RLock()
        self._children: dict[int, subprocess.Popen] = {}

    def _schema(self, table: str) -> dict[str, Any]:
        try:
            return self.store.catalog.public(table)
        except Exception as exc:
            raise ApiError(503, f"缺少转发定义表 {table}，请先执行数据库结构升级", code="trans_schema_missing") from exc

    def _columns(self, table: str) -> set[str]:
        return {item["eng_name"] for item in self._schema(table)["columns"]}

    def _channel_key(self, value: Any) -> list[Any]:
        keys = self._schema(CHANNEL_TABLE)["primary_keys"]
        if keys != ["channel_id"]:
            raise ApiError(500, "转发表主键必须是 channel_id", code="trans_schema_invalid")
        try:
            channel_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "channel_id 必须是整数") from exc
        if channel_id <= 0:
            raise ApiError(400, "channel_id 必须是正整数")
        return [channel_id]

    def _point_key(self, channel_id: Any, kind: Any, point: Any) -> list[Any]:
        keys = self._schema(POINT_TABLE)["primary_keys"]
        try:
            cid = int(channel_id)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "通道号必须是整数") from exc
        values = {"channel_id": cid}
        if "pnt_no" in keys:
            try:
                values["pnt_no"] = int(point)
            except (TypeError, ValueError) as exc:
                raise ApiError(400, "点号必须是整数") from exc
        if "kind" in keys:
            kind = str(kind).lower()
            if kind not in {"yc", "yx", "yk", "yt"}:
                raise ApiError(400, "点映射 kind 必须是 yc、yx、yk 或 yt")
            values["kind"] = kind
        return [values[key] for key in keys]

    def channels(self) -> list[dict[str, Any]]:
        self._schema(CHANNEL_TABLE)
        return self.store.list_rows(CHANNEL_TABLE, limit=500, sort="channel_id")["items"]

    def get_channel(self, channel_id: Any) -> dict[str, Any]:
        return self.store.get_row(CHANNEL_TABLE, self._channel_key(channel_id))

    def create_channel(self, body: dict[str, Any]) -> dict[str, Any]:
        columns = self._columns(CHANNEL_TABLE)
        unknown = set(body) - columns
        if unknown:
            raise ApiError(400, "转发通道包含未知字段", details={"unknown_fields": sorted(unknown)})
        values = dict(body)
        # ``channel_id`` is an implementation key, so the Web form does not
        # require operators to allocate it manually.  Allocate the next
        # positive integer while holding the same SQLite write transaction
        # used by ``create_row``; the UNIQUE code constraint still protects
        # retries and concurrent requests.
        if "channel_id" not in values:
            with self.store.connection() as con:
                row = con.execute(f"SELECT COALESCE(MAX(channel_id), 0) + 1 FROM {CHANNEL_TABLE}").fetchone()
                values["channel_id"] = int(row[0])
        values.setdefault("process_enable", 0)
        values.setdefault("process_status", 0)
        values.setdefault("process_pid", 0)
        values.setdefault("process_start_time", 0)
        values.setdefault("process_stop_time", 0)
        values.setdefault("process_heartbeat_time", 0)
        values.setdefault("process_exit_code", 0)
        values.setdefault("process_error", "")
        if "protocol" in columns:
            protocol = str(values.get("protocol", "")).lower()
            if protocol not in {"iec104", "modbus-tcp"}:
                raise ApiError(400, "转发规约只能是 iec104 或 modbus-tcp")
            values["protocol"] = protocol
        if not values.get("code"):
            raise ApiError(400, "转发通道 code 不能为空")
        if not values.get("name"):
            values["name"] = values["code"]
        return self.store.create_row(CHANNEL_TABLE, values)

    def update_channel(self, channel_id: Any, body: dict[str, Any], if_match: int | None = None) -> dict[str, Any]:
        self.get_channel(channel_id)
        columns = self._columns(CHANNEL_TABLE)
        runtime = {"process_status", "process_pid", "process_start_time", "process_stop_time",
                   "process_heartbeat_time", "process_exit_code", "process_error"}
        unknown = set(body) - columns
        if unknown:
            raise ApiError(400, "转发通道包含未知字段", details={"unknown_fields": sorted(unknown)})
        if set(body) & runtime:
            raise ApiError(400, "运行时字段只能由转发进程更新")
        if "protocol" in body and str(body["protocol"]).lower() not in {"iec104", "modbus-tcp"}:
            raise ApiError(400, "转发规约只能是 iec104 或 modbus-tcp")
        return self.store.update_row(CHANNEL_TABLE, self._channel_key(channel_id), body, if_match)

    def delete_channel(self, channel_id: Any, if_match: int | None = None) -> None:
        self.stop(channel_id)
        cid = self._channel_key(channel_id)[0]
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(f"SELECT _version FROM {CHANNEL_TABLE} WHERE channel_id=?", (cid,)).fetchone()
            if row is None:
                raise ApiError(404, "记录不存在", code="not_found")
            if if_match is not None and int(row[0]) != int(if_match):
                raise ApiError(409, "记录已被其他操作修改，请关闭并重新打开编辑窗口", code="version_conflict")
            con.execute(f"DELETE FROM {POINT_TABLE} WHERE channel_id=?", (cid,))
            con.execute("DELETE FROM " + CHANNEL_TABLE + " WHERE channel_id=?", (cid,))
            con.commit()

    def points(self, channel_id: Any) -> list[dict[str, Any]]:
        self.get_channel(channel_id)
        # ScadaStore deliberately caps a single projection at 500 rows; the
        # transmission UI paginates at channel scope and does not need an
        # oversized request here.
        return self.store.list_rows(POINT_TABLE, where={"channel_id": self._channel_key(channel_id)[0]}, limit=500,
                                    sort="kind")["items"]

    def create_point(self, channel_id: Any, body: dict[str, Any]) -> dict[str, Any]:
        cid = self._channel_key(channel_id)[0]
        self.get_channel(cid)
        columns = self._columns(POINT_TABLE)
        unknown = set(body) - columns - {"channel_id"}
        if unknown:
            raise ApiError(400, "转发点映射包含未知字段", details={"unknown_fields": sorted(unknown)})
        values = dict(body)
        values["channel_id"] = cid
        # The source point kind is normally identical to the exposed point
        # kind.  Keep it explicit in storage while allowing the UI to omit a
        # redundant field.
        values.setdefault("source_kind", values.get("kind"))
        if values.get("kind") not in {"yc", "yx", "yk", "yt"}:
            raise ApiError(400, "点映射 kind 必须是 yc、yx、yk 或 yt")
        if values.get("source_kind") not in {"yc", "yx", "yk", "yt"}:
            raise ApiError(400, "点映射 source_kind 必须是 yc、yx、yk 或 yt")
        if values.get("kind") != values.get("source_kind"):
            raise ApiError(400, "点映射 kind 必须与 source_kind 一致")
        for key in ("pnt_no", "source_rtu_id", "source_pnt_no"):
            try:
                values[key] = int(values[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise ApiError(400, f"点映射 {key} 必须是正整数") from exc
            if values[key] <= 0:
                raise ApiError(400, f"点映射 {key} 必须是正整数")
        # A mapping may be created before the source RTU/point is provisioned.
        # The runtime skips a missing source row and starts transmission as soon
        # as the corresponding scdfe_c definition appears.  This also makes
        # definition maintenance atomic: operators can create the mapping,
        # then add or replace source four-remote points without a race.
        return self.store.create_row(POINT_TABLE, values)

    def get_point(self, channel_id: Any, key: list[Any]) -> dict[str, Any]:
        self.get_channel(channel_id)
        return self.store.get_row(POINT_TABLE, key)

    def update_point(self, channel_id: Any, key: list[Any], body: dict[str, Any], if_match: int | None = None) -> dict[str, Any]:
        self.get_channel(channel_id)
        return self.store.update_row(POINT_TABLE, key, body, if_match)

    def delete_point(self, channel_id: Any, key: list[Any], if_match: int | None = None) -> None:
        self.get_channel(channel_id)
        self.store.delete_row(POINT_TABLE, key, if_match)

    def _update_runtime(self, channel_id: int, **values: Any) -> None:
        allowed = self._columns(CHANNEL_TABLE)
        values = {name: value for name, value in values.items() if name in allowed}
        if values:
            self.store.update_row(CHANNEL_TABLE, [channel_id], values)

    def _command(self, channel_id: int) -> list[str]:
        return [sys.executable, "-B", "-m", "scdfe_t", "--db", str(self.db_path),
                "--schema-dir", str(self.schema_dir), "--channel-id", str(channel_id)]

    def start(self, channel_id: Any) -> dict[str, Any]:
        cid = self._channel_key(channel_id)[0]
        row = self.get_channel(cid)
        with self._lock:
            process = self._children.get(cid)
            if process is not None and process.poll() is None:
                return self.get_channel(cid)
            pid = int(row.get("process_pid") or 0)
            if pid and process_is_alive(pid):
                return row
            started = _now_ms()
            self._update_runtime(cid, process_enable=1, process_status=1, process_pid=0,
                                 process_start_time=started, process_stop_time=0, process_error="")
            try:
                project_root = Path(__file__).resolve().parents[2]
                process = subprocess.Popen(self._command(cid), cwd=str(project_root),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, close_fds=(os.name != "nt"))
            except OSError as exc:
                self._update_runtime(cid, process_enable=0, process_status=5, process_pid=0,
                                     process_stop_time=_now_ms(), process_exit_code=-1, process_error=str(exc))
                raise ApiError(409, f"转发进程启动失败: {exc}", code="trans_start_failed") from exc
            self._children[cid] = process
            self._update_runtime(cid, process_status=2, process_pid=process.pid)
            return self.get_channel(cid)

    def stop(self, channel_id: Any) -> dict[str, Any]:
        cid = self._channel_key(channel_id)[0]
        row = self.get_channel(cid)
        with self._lock:
            process = self._children.get(cid)
            pid = process.pid if process is not None else int(row.get("process_pid") or 0)
            self._update_runtime(cid, process_enable=0, process_status=3)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            elif pid and process_is_alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            self._children.pop(cid, None)
            self._update_runtime(cid, process_status=0, process_pid=0, process_stop_time=_now_ms())
            return self.get_channel(cid)

    def processes(self) -> list[dict[str, Any]]:
        rows = []
        for row in self.channels():
            cid = int(row["channel_id"])
            pid = int(row.get("process_pid") or 0)
            alive = bool(pid and process_is_alive(pid))
            item = dict(row)
            item["alive"] = alive
            if pid and not alive and row.get("process_status") in {1, 2, 3, 4}:
                self._update_runtime(cid, process_status=5, process_pid=0, process_error="进程已退出")
                item.update(process_status=5, process_pid=0, process_error="进程已退出", alive=False)
            rows.append(item)
        return rows

    def logs(self, channel_id: Any | None = None, limit: int = 200) -> dict[str, Any]:
        log_root = self.db_path.parent / "logs"
        if not log_root.is_dir():
            return {"items": [], "total": 0}
        suffix = f"{int(channel_id)}" if channel_id is not None else None
        files = []
        for path in log_root.rglob("*.log"):
            name = path.name
            if "scdfe_t" not in name:
                continue
            if suffix and not name.startswith(f"scdfe_t-channel-{suffix}-"):
                continue
            try:
                files.append({"filename": str(path.relative_to(log_root)).replace("\\", "/"),
                              "size": path.stat().st_size, "mtime": int(path.stat().st_mtime * 1000)})
            except OSError:
                continue
        files.sort(key=lambda item: item["mtime"], reverse=True)
        return {"items": files[:max(1, min(int(limit), 1000))], "total": len(files)}

    def read_log(self, filename: str, tail: int = 300) -> dict[str, Any]:
        root = (self.db_path.parent / "logs").resolve()
        path = (root / filename).resolve()
        if root not in path.parents or path.suffix != ".log" or not path.name.startswith("scdfe_t-channel-"):
            raise ApiError(400, "非法日志路径")
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError as exc:
            raise ApiError(404, "日志文件不存在", code="not_found") from exc
        count = max(1, min(int(tail), 5000))
        return {"filename": filename, "lines": lines[-count:], "total": len(lines)}

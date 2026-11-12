"""Persistence and process control for ``scdfe_t`` transmission channels.

The transmission tables are owned by the SCADA schema.  This module deliberately
uses :class:`scdfe_c.web.service.ScadaStore` instead of a second database so
that channel definitions, point mappings and runtime state are visible to the
same operators and obey the same optimistic-version rules.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
from weakref import WeakValueDictionary

from core.scdfe_control import InstanceLock
from core.runtime_status import runtime_status_rows, state_for
from scdfe_c.web.process_control import runtime_python
from scdfe_c.web.service import ApiError, DEFAULT_DB, KIND_TABLES, SCHEMA_DIR, ScadaStore

CHANNEL_TABLE = "fes_scdfe_t_define"
POINT_TABLE = "fes_scdfe_t_point_define"
RUNTIME_FIELDS = frozenset({
    "_version", "process_status", "process_pid", "process_start_time", "process_stop_time",
    "process_heartbeat_time", "process_exit_code", "process_error", "process_enable",
})
POINT_RUNTIME_FIELDS = frozenset({
    "value", "status", "refresh_time", "forward_time", "command_receive_time",
})
START_TIMEOUT = 10.0
STOP_TIMEOUT = 10.0


def _now_ms() -> int:
    return int(time.time() * 1000)


class TransStore:
    """CRUD, process lifecycle and log projection for transmission channels."""

    def __init__(self, db_path: Path | str = DEFAULT_DB, schema_dir: Path | str = SCHEMA_DIR, *, connection_pool_size=0):
        self.store = ScadaStore(db_path, schema_dir, connection_pool_size=connection_pool_size)
        self.db_path = Path(db_path).resolve()
        self.schema_dir = Path(schema_dir).resolve()
        self._table_schemas = {}
        self._lock_guard = threading.Lock()
        self._channel_locks = WeakValueDictionary()
        self._children: dict[int, subprocess.Popen] = {}

    @contextmanager
    def _control(self, channel_id: int):
        """Serialize mutations and lifecycle requests across Web instances."""
        # Keep only active channel locks. A slow stop must not block other channels.
        with self._lock_guard:
            local = self._channel_locks.get(channel_id)
            if local is None:
                local = self._channel_locks[channel_id] = threading.RLock()
        with local:
            lock = InstanceLock(self.db_path, f"trans-control-{channel_id}")
            if not lock.acquire():
                raise ApiError(409, "该转发通道正在处理其他操作，请稍后重试", code="trans_busy")
            try:
                yield
            finally:
                lock.release()

    def _owned(self, channel_id: int) -> bool:
        # A recycled PID is not evidence of channel ownership. The running
        # CLI holds this OS lock throughout startup, execution and cleanup.
        return InstanceLock.held(self.db_path, f"trans-{channel_id}")

    @staticmethod
    def _definition(row):
        return {key: value for key, value in row.items() if key not in RUNTIME_FIELDS}

    def _schema(self, table: str) -> dict[str, Any]:
        try:
            if table not in self._table_schemas:
                self._table_schemas[table] = self.store.catalog.public(table)
            return self._table_schemas[table]
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

    def channels(self, *, selector=False) -> list[dict[str, Any]]:
        if selector:
            with self.store.connection() as con:
                return [dict(row) for row in con.execute(
                    f"SELECT channel_id,name,protocol FROM {CHANNEL_TABLE} ORDER BY channel_id")]
        columns = self._schema(CHANNEL_TABLE)["columns"]
        with self.store.connection() as con:
            rows = con.execute(f"SELECT * FROM {CHANNEL_TABLE} ORDER BY channel_id").fetchall()
        return self.store._public_rows(rows, columns)

    def runtime_status(self) -> dict[str, Any]:
        """Return transmission channels with one uniform live-status snapshot."""
        status_fields = (
            "channel_id,name,protocol,process_enable,process_status,process_pid,"
            "process_start_time,process_stop_time,process_heartbeat_time,"
            "process_exit_code,process_error,packet_recording"
        )
        with self.store.connection() as con:
            items = [dict(row) for row in con.execute(
                f"SELECT {status_fields}, _version FROM {CHANNEL_TABLE} ORDER BY channel_id"
            )]
        snapshots = {
            int(row["channel_id"]): row
            for row in runtime_status_rows(self.store, service_name="scdfe_t")
        }
        for item in items:
            channel_id = int(item["channel_id"])
            snapshot = snapshots.get(channel_id)
            if snapshot is None:
                process_status = int(item.get("process_status") or 0)
                error = str(item.get("process_error") or "")
                snapshot = {
                    "service_name": "scdfe_t",
                    "channel_id": channel_id,
                    "protocol": item.get("protocol", ""),
                    "channel_name": item.get("name") or "",
                    "state": state_for(process_status, process_status == 2, error),
                    "running": int(process_status == 2),
                    "connection_state": "unknown",
                    "process_status": process_status,
                    "process_pid": item.get("process_pid", 0),
                    "process_start_time": item.get("process_start_time", 0),
                    "process_stop_time": item.get("process_stop_time", 0),
                    "process_heartbeat_time": item.get("process_heartbeat_time", 0),
                    "process_error": error,
                    "rx_count": 0, "tx_count": 0, "error_count": 0,
                    "dropped_count": 0, "reconnect_count": 0,
                    "last_rx_time": 0, "last_tx_time": 0,
                    "last_error_time": 0, "last_error": error,
                    "diagnostic_json": "{}", "diagnostic": {}, "sampled_at": 0,
                }
            item["runtime_status"] = snapshot
        return {"items": items, "total": len(items)}

    def get_channel(self, channel_id: Any) -> dict[str, Any]:
        return self.store.get_row(CHANNEL_TABLE, self._channel_key(channel_id))

    def create_channel(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.keys() & (RUNTIME_FIELDS - {'process_enable'}):
            raise ApiError(400, "运行时字段只能由转发进程更新")
        columns = self._columns(CHANNEL_TABLE)
        unknown = set(body) - columns
        if unknown:
            raise ApiError(400, "转发通道包含未知字段", details={"unknown_fields": sorted(unknown)})
        values = dict(body)
        if "protocol" in columns:
            protocol = str(values.get("protocol", "")).lower()
            if protocol not in {"iec104", "modbus-tcp"}:
                raise ApiError(400, "转发规约只能是 iec104 或 modbus-tcp")
            values["protocol"] = protocol
        if not values.get("name"):
            values["name"] = f"转发通道{values.get('channel_id', '')}".rstrip()
        return self.store.create_row(CHANNEL_TABLE, values, allocate_key="channel_id" not in values)

    def update_channel(self, channel_id: Any, body: dict[str, Any], if_match: int | None = None) -> dict[str, Any]:
        self.get_channel(channel_id)
        columns = self._columns(CHANNEL_TABLE)
        runtime = RUNTIME_FIELDS - {"process_enable"}
        unknown = set(body) - columns
        if unknown:
            raise ApiError(400, "转发通道包含未知字段", details={"unknown_fields": sorted(unknown)})
        if set(body) & runtime:
            raise ApiError(400, "运行时字段只能由转发进程更新")
        if "protocol" in body and str(body["protocol"]).lower() not in {"iec104", "modbus-tcp"}:
            raise ApiError(400, "转发规约只能是 iec104 或 modbus-tcp")
        cid = self._channel_key(channel_id)[0]
        with self._control(cid):
            return self.store.update_row(CHANNEL_TABLE, [cid], body, if_match)

    def delete_channel(self, channel_id: Any, if_match: int | None = None) -> None:
        cid = self._channel_key(channel_id)[0]
        with self._control(cid):
            # Check the caller's version before changing enable/state. A
            # rejected stale DELETE must never stop a running channel.
            with self.store.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(f"SELECT * FROM {CHANNEL_TABLE} WHERE channel_id=?", (cid,)).fetchone()
                if row is None:
                    raise ApiError(404, "记录不存在", code="not_found")
                if if_match is not None and int(row["_version"]) != int(if_match):
                    raise ApiError(409, "记录已被其他操作修改，请重新加载", code="version_conflict")
                definition = self._definition(dict(row))
                con.execute(f"UPDATE {CHANNEL_TABLE} SET process_enable=0, _version=_version+1 WHERE channel_id=?", (cid,))
                con.commit()
            self._stop_locked(cid)
            with self.store.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(f"SELECT * FROM {CHANNEL_TABLE} WHERE channel_id=?", (cid,)).fetchone()
                if row is None:
                    raise ApiError(404, "记录不存在", code="not_found")
                # Runtime heartbeats and our own stop change _version. Check
                # operator definitions instead so those writes do not make a
                # valid DELETE conflict with itself.
                if self._definition(dict(row)) != definition:
                    raise ApiError(409, "通道定义在停止期间发生变化，请重新加载", code="version_conflict")
                con.execute(f"DELETE FROM {POINT_TABLE} WHERE channel_id=?", (cid,))
                con.execute(f"DELETE FROM {CHANNEL_TABLE} WHERE channel_id=?", (cid,))
                con.commit()

    def points(self, channel_id: Any) -> list[dict[str, Any]]:
        """Return all mappings for in-process callers; HTTP uses point_page."""
        cid = self._channel_key(channel_id)[0]
        columns = self._schema(POINT_TABLE)["columns"]
        with self.store.connection() as con:
            con.execute("BEGIN")
            if con.execute(f"SELECT 1 FROM {CHANNEL_TABLE} WHERE channel_id=?", (cid,)).fetchone() is None:
                raise ApiError(404, "转发通道不存在", code="not_found")
            rows = con.execute(f"SELECT * FROM {POINT_TABLE} WHERE channel_id=? ORDER BY pnt_no,kind", (cid,)).fetchall()
        return [self._point_response(row) for row in self.store._public_rows(rows, columns)]

    @staticmethod
    def _point_response(row):
        # The old trans API exposed both names. Derive its read-only alias;
        # the DB, generic table API and operator form have just one type.
        result = {**row, "source_kind": row["kind"]}
        if row['kind'] in {'yk', 'yt'}:
            result['command_confirmation_scope'] = 'writer_commit'
            result['device_execution_confirmed'] = None
        return result

    def point_page(self, channel_id: Any, *, limit: int = 100, offset: int = 0,
                   kind: str | None = None) -> dict[str, Any]:
        self.get_channel(channel_id)
        where = {"channel_id": self._channel_key(channel_id)[0]}
        if kind:
            if kind not in {"yc", "yx", "yk", "yt"}:
                raise ApiError(400, "点映射 kind 必须是 yc、yx、yk 或 yt")
            where["kind"] = kind
        page = self.store.list_rows(POINT_TABLE, where=where, limit=limit, offset=offset, sort="pnt_no")
        page["items"] = [self._point_response(row) for row in page["items"]]
        return page

    def source_catalog(self, kind: str, rtu_id: Any | None = None) -> dict[str, Any]:
        """List selectable source RTUs and points from the C acquisition tables."""
        kind = str(kind or "").lower()
        table = KIND_TABLES.get(kind)
        if table is None:
            raise ApiError(400, "源数据 kind 必须是 yc、yx、yk 或 yt")
        try:
            selected_rtu = None if rtu_id in (None, "") else int(rtu_id)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "源 RTU 必须是正整数") from exc
        if selected_rtu is not None and selected_rtu <= 0:
            raise ApiError(400, "源 RTU 必须是正整数")
        with self.store.connection() as con:
            rtus = [dict(row) for row in con.execute(
                f"SELECT r.rtu_id, COALESCE(r.name,'') AS name, "
                f"COALESCE(r.channel_type,'') AS channel_type, "
                f"COUNT(p.pnt_no) AS point_count "
                f"FROM fes_rtu_define r JOIN {table} p ON p.rtu_id=r.rtu_id "
                "GROUP BY r.rtu_id,r.name,r.channel_type ORDER BY r.rtu_id"
            )]
            points = []
            if selected_rtu is not None:
                points = [dict(row) for row in con.execute(
                    f"SELECT rtu_id,pnt_no,COALESCE(name,'') AS name,value,status FROM {table} "
                    "WHERE rtu_id=? ORDER BY pnt_no", (selected_rtu,)
                )]
        for row in points:
            row["kind"] = kind
        return {"kind": kind, "rtus": rtus, "items": points, "rtu_id": selected_rtu}

    def _point_values(self, channel_id: Any, body: dict[str, Any], *, allow_missing_pnt_no: bool = False) -> tuple[int, dict[str, Any]]:
        cid = self._channel_key(channel_id)[0]
        self.get_channel(cid)
        columns = self._columns(POINT_TABLE)
        runtime_fields = set(body) & POINT_RUNTIME_FIELDS
        if runtime_fields:
            raise ApiError(400, "转发点实时状态只能由转发进程更新",
                           details={"runtime_fields": sorted(runtime_fields)})
        unknown = set(body) - columns - {"channel_id", "source_kind"}
        if unknown:
            raise ApiError(400, "转发点映射包含未知字段", details={"unknown_fields": sorted(unknown)})
        values = dict(body)
        values["channel_id"] = cid
        if values.get("kind") not in {"yc", "yx", "yk", "yt"}:
            raise ApiError(400, "点映射 kind 必须是 yc、yx、yk 或 yt")
        if values.pop("source_kind", values["kind"]) != values["kind"]:
            raise ApiError(400, "点映射 kind 必须与 source_kind 一致")
        values.setdefault("source_pnt_no", values.get("pnt_no"))
        if allow_missing_pnt_no and values.get("pnt_no") in (None, ""):
            values.pop("pnt_no", None)
        for key in (("source_rtu_id", "source_pnt_no") if allow_missing_pnt_no else ("pnt_no", "source_rtu_id", "source_pnt_no")):
            try:
                values[key] = int(values[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise ApiError(400, f"点映射 {key} 必须是正整数") from exc
            if values[key] <= 0:
                raise ApiError(400, f"点映射 {key} 必须是正整数")
        return cid, values

    def create_point(self, channel_id: Any, body: dict[str, Any]) -> dict[str, Any]:
        cid, values = self._point_values(channel_id, body)
        # A mapping may be created before the source RTU/point is provisioned.
        # The runtime skips a missing source row and starts transmission as soon
        # as the corresponding scdfe_c definition appears.  This also makes
        # definition maintenance atomic: operators can create the mapping,
        # then add or replace source four-remote points without a race.
        with self._control(cid):
            self.get_channel(cid)
            return self._point_response(self.store.create_row(POINT_TABLE, values))

    def create_points_batch(self, channel_id: Any, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Create several mappings in one channel-control transaction."""
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list) or not items or len(items) > 500:
            raise ApiError(400, "批量点映射 items 必须包含 1..500 项")
        cid = self._channel_key(channel_id)[0]
        prepared = []
        for item in items:
            if not isinstance(item, dict):
                raise ApiError(400, "批量点映射每一项必须是对象")
            prepared.append(self._point_values(cid, item, allow_missing_pnt_no=True)[1])
        explicit = [item["pnt_no"] for item in prepared if item.get("pnt_no") is not None]
        if len(explicit) != len(set(explicit)):
            raise ApiError(409, "批量点映射包含重复的转发点号", code="conflict")
        with self._control(cid):
            self.get_channel(cid)
            with self.store.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                next_no = con.execute(
                    f"SELECT COALESCE(MAX(pnt_no),0)+1 FROM {POINT_TABLE} WHERE channel_id=?", (cid,)
                ).fetchone()[0]
                used = set(explicit)
                for values in prepared:
                    if values.get("pnt_no") is None:
                        while next_no in used:
                            next_no += 1
                        values["pnt_no"] = next_no
                        used.add(next_no)
                        next_no += 1
                    normalised = self.store._normalise(POINT_TABLE, values)
                    columns = self.store.schema(POINT_TABLE)["columns"]
                    names = [column["eng_name"] for column in columns if column["eng_name"] in normalised]
                    con.execute(
                        f"INSERT INTO {POINT_TABLE} ({', '.join(names)}) "
                        f"VALUES ({', '.join('?' for _ in names)})",
                        [normalised[name] for name in names],
                    )
                rows = [self._point_response(self.store._read(
                    con, POINT_TABLE, [cid, values["pnt_no"], values["kind"]]
                )) for values in prepared]
                con.commit()
        return rows

    def get_point(self, channel_id: Any, key: list[Any]) -> dict[str, Any]:
        self.get_channel(channel_id)
        return self._point_response(self.store.get_row(POINT_TABLE, key))

    def update_point(self, channel_id: Any, key: list[Any], body: dict[str, Any], if_match: int | None = None) -> dict[str, Any]:
        cid = self._channel_key(channel_id)[0]
        with self._control(cid):
            current = self.get_point(cid, key)
            values = dict(body)
            runtime_fields = set(values) & POINT_RUNTIME_FIELDS
            if runtime_fields:
                raise ApiError(400, "转发点实时状态只能由转发进程更新",
                               details={"runtime_fields": sorted(runtime_fields)})
            legacy_type = "source_kind" in values
            if legacy_type and values.pop("source_kind") != current["kind"]:
                raise ApiError(400, "点映射 kind 必须与 source_kind 一致")
            for name in self._schema(POINT_TABLE)["primary_keys"]:
                if name in values:
                    if str(values[name]) != str(current[name]):
                        raise ApiError(400, "主键字段不能修改")
                    values.pop(name)
            if legacy_type and not values:
                self.store._check_version(current, if_match)
                return current
            return self._point_response(self.store.update_row(POINT_TABLE, key, values, if_match))

    def delete_point(self, channel_id: Any, key: list[Any], if_match: int | None = None) -> None:
        cid = self._channel_key(channel_id)[0]
        with self._control(cid):
            self.get_channel(cid)
            self.store.delete_row(POINT_TABLE, key, if_match)

    def _update_runtime(self, channel_id: int, **values: Any) -> None:
        allowed = self._columns(CHANNEL_TABLE)
        values = {name: value for name, value in values.items() if name in allowed}
        if values:
            self.store.update_row(CHANNEL_TABLE, [channel_id], values)

    def _command(self, channel_id: int) -> list[str]:
        return [runtime_python(), "-B", "-m", "scdfe_t", "--db", str(self.db_path),
                "--schema-dir", str(self.schema_dir), "--channel-id", str(channel_id), "--managed-start"]

    def start(self, channel_id: Any) -> dict[str, Any]:
        cid = self._channel_key(channel_id)[0]
        with self._control(cid):
            row = self.get_channel(cid)
            launch_time = int(row.get("process_start_time") or 0)
            owned = self._owned(cid)
            if owned:
                if not row.get("process_enable") or row.get("process_status") == 3:
                    raise ApiError(409, "转发通道正在停止，请稍后重试", code="trans_stopping")
                if row.get("process_status") == 2:
                    return row
            process = self._children.get(cid)
            if process is not None and process.poll() is None and not row.get("process_enable"):
                # A different Web instance can observe the lease release
                # just before this instance's child handle reports exit.
                # Reap that previous launch before re-enabling the channel.
                try:
                    process.wait(timeout=STOP_TIMEOUT)
                except subprocess.TimeoutExpired as exc:
                    raise ApiError(409, "上次转发进程尚未退出，请稍后重试", code="trans_stopping") from exc
            own_starting = owned or process is not None and process.poll() is None
            # Another Web instance may have spawned the child just before it
            # acquired the runtime lease. Treat only a recent STARTING intent
            # as pending; an arbitrary live PID never proves a running lease.
            pending = (row.get("process_status") == 1 and row.get("process_enable")
                       and 0 <= _now_ms() - int(row.get("process_start_time") or 0) < START_TIMEOUT * 1000)
            if not own_starting and not pending:
                started = _now_ms()
                launch_time = started
                self._update_runtime(cid, process_enable=1, process_status=1, process_pid=0,
                                     process_start_time=started, process_stop_time=0,
                                     process_error="", process_exit_code=0)
                try:
                    project_root = Path(__file__).resolve().parents[2]
                    process = subprocess.Popen(self._command(cid), cwd=str(project_root),
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, close_fds=True)
                except OSError as exc:
                    self._update_runtime(cid, process_enable=0, process_status=5, process_pid=0,
                                         process_stop_time=_now_ms(), process_exit_code=-1, process_error=str(exc))
                    raise ApiError(409, f"转发进程启动失败: {exc}", code="trans_start_failed") from exc
                self._children[cid] = process
                # The runtime may already have published RUNNING. Do not
                # overwrite that state with the parent's launch metadata.
                with self.store.connection() as con:
                    con.execute(f"UPDATE {CHANNEL_TABLE} SET process_pid=?, _version=_version+1 "
                                "WHERE channel_id=? AND process_status=1 AND process_pid=0 AND process_start_time=?",
                                (process.pid, cid, started))
                    con.commit()
        return self._wait_started(cid, launch_time=launch_time)

    def _wait_started(self, cid: int, *, launch_time: int) -> dict[str, Any]:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            row = self.get_channel(cid)
            if row.get("process_status") == 2 and self._owned(cid):
                return row
            if not row.get("process_enable") or row.get("process_status") == 3:
                raise ApiError(409, "启动请求已取消", code="trans_start_cancelled")
            if row.get("process_status") == 5:
                raise ApiError(409, row.get("process_error") or "转发进程启动失败，请查看日志",
                               code="trans_start_failed")
            process = self._children.get(cid)
            if process is not None and process.poll() is not None and not self._owned(cid):
                try:
                    with self._control(cid):
                        current = self.get_channel(cid)
                        # An old waiter may outlive a stop followed by a
                        # different Web instance's new start. Only this exact
                        # parent's unchanged launch record may be failed.
                        if (current["_version"] == row["_version"] and
                                current.get("process_pid") == process.pid and
                                current.get("process_start_time") == launch_time and
                                self._children.get(cid) is process and not self._owned(cid)):
                            message = "转发进程启动后退出，请查看日志"
                            self._update_runtime(cid, process_enable=0, process_status=5, process_pid=0,
                                                 process_stop_time=_now_ms(), process_error=message,
                                                 process_exit_code=process.returncode)
                            raise ApiError(409, message, code="trans_start_failed")
                except ApiError as exc:
                    if exc.code != "trans_busy":
                        raise
            time.sleep(.05)
        raise ApiError(409, "转发进程未在规定时间内就绪，请查看进程状态和日志", code="trans_start_timeout")

    def stop(self, channel_id: Any) -> dict[str, Any]:
        cid = self._channel_key(channel_id)[0]
        with self._control(cid):
            return self._stop_locked(cid)

    def _stop_locked(self, cid: int) -> dict[str, Any]:
        self.get_channel(cid)
        process = self._children.get(cid)
        self._update_runtime(cid, process_enable=0, process_status=3)
        deadline = time.monotonic() + STOP_TIMEOUT
        while self._owned(cid) or process is not None and process.poll() is None:
            if time.monotonic() >= deadline:
                message = "转发进程尚未退出，已请求停止，请稍后重试"
                self._update_runtime(cid, process_status=3, process_error=message)
                raise ApiError(409, message, code="trans_stop_timeout")
            time.sleep(.05)
        self._children.pop(cid, None)
        self._update_runtime(cid, process_status=0, process_pid=0, process_stop_time=_now_ms(), process_error="")
        return self.get_channel(cid)

    def processes(self) -> list[dict[str, Any]]:
        rows = []
        for row in self.channels():
            cid = int(row["channel_id"])
            alive = self._owned(cid)
            item = dict(row)
            item["alive"] = alive
            process = self._children.get(cid)
            pending = row.get("process_status") == 1 and (
                process is not None and process.poll() is None or
                0 <= _now_ms() - int(row.get("process_start_time") or 0) < START_TIMEOUT * 1000
            )
            if not alive and not pending and row.get("process_status") in {1, 2, 3, 4}:
                # Recheck under the control lock: a fresh start must not be
                # overwritten by a stale monitor request.
                try:
                    with self._control(cid):
                        current = self.get_channel(cid)
                        if current["_version"] == row["_version"] and not self._owned(cid):
                            self._update_runtime(cid, process_status=5, process_pid=0, process_error="进程已退出")
                            item.update(process_status=5, process_pid=0, process_error="进程已退出", alive=False)
                except ApiError as exc:
                    if exc.code != "trans_busy":
                        raise
            rows.append(item)
        return rows

    def logs(self, channel_id: Any | None = None, limit: int = 200, *, record: str = "runtime") -> dict[str, Any]:
        # Keep the historical ``TransStore.logs(stub)`` call usable for
        # read-only log checks.  The stub only needs a resolved db_path; the
        # normal instance path still dispatches through its bound method.
        return TransStore._logs_for_record(self, channel_id, limit, record)

    def _logs_for_record(self, channel_id: Any | None, limit: int, record: str) -> dict[str, Any]:
        if record not in {"runtime", "packet"}:
            raise ApiError(400, "记录类型必须是 runtime 或 packet")
        from core.log_paths import packet_log_directory
        log_root = self.db_path.parent / "logs"
        packet_root = packet_log_directory(log_root)
        active_root = packet_root if record == "packet" else log_root
        if not active_root.is_dir():
            return {"items": [], "total": 0, "record": record, "directory": str(active_root)}
        from core.web_logs import log_files
        query = {"process": f"scdfe_t-channel-{int(channel_id)}"} if channel_id is not None else {}
        query["record"] = record
        files = [{"filename": item["name"], "size": item["size"], "mtime": item["modified_at"]}
                 for item in log_files(log_root, "t", query, packet_directory=packet_root)["items"]]
        files.sort(key=lambda item: item["mtime"], reverse=True)
        return {"items": files[:max(1, min(int(limit), 1000))], "total": len(files),
                "record": record, "directory": str(active_root)}

    def read_log(self, filename: str, tail: int = 300, *, record: str = "runtime") -> dict[str, Any]:
        from scdfe_c.web.log_query import LOG_PATTERNS, _relative_filename
        if record not in {"runtime", "packet"}:
            raise ApiError(400, "记录类型必须是 runtime 或 packet")
        from core.log_paths import packet_log_directory
        runtime_root = (self.db_path.parent / "logs").resolve()
        root = (packet_log_directory(runtime_root) if record == "packet" else runtime_root).resolve()
        try:
            path = root / _relative_filename(root, filename, LOG_PATTERNS["t"])
            if path.is_symlink():
                raise ValueError("不读取链接")
        except (ValueError, OSError, RuntimeError) as exc:
            raise ApiError(400, "非法日志路径") from exc
        count = max(1, min(int(tail), 5000))
        try:
            # Small files promise an exact total; large files return total=None
            # and need only enough complete lines for the requested tail.
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                if size <= 2_000_000:
                    start = 0
                    stream.seek(0)
                    data = stream.read(size)
                else:
                    floor, start = size - 2_000_000, size
                    chunks, newlines = [], 0
                    while start > floor and newlines <= count:
                        block = min(16_384, start - floor)
                        start -= block
                        stream.seek(start)
                        chunk = stream.read(block)
                        chunks.append(chunk)
                        newlines += chunk.count(b"\n")
                    data = b"".join(reversed(chunks))
            if start:
                data = data.partition(b"\n")[2]
            lines = data.decode("utf-8", errors="replace").splitlines()
        except FileNotFoundError as exc:
            raise ApiError(404, "日志文件不存在", code="not_found") from exc
        return {"filename": filename, "lines": lines[-count:],
                "total": None if start else len(lines), "truncated": bool(start),
                "record": record, "directory": str(root)}

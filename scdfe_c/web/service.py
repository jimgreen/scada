"""SQLite-backed SCADA schema and row service.

The service deliberately keeps the JSON definitions authoritative.  SQLite is
used for durable rows in ``D:\\codex\\scada\\db\\scada.sqlite3`` by default;
each operation owns an exclusive connection. Web servers may retain a bounded
number of idle connections; other callers use short-lived connections.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "dbdef" / "json"
DEFAULT_DB = ROOT / "db" / "scada.sqlite3"
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KIND_TABLES = {
    "yc": "fes_yc_define",
    "yx": "fes_yx_define",
    "yt": "fes_yt_define",
    "yk": "fes_yk_define",
}
RTU_VERSION_DDL = ('CREATE TABLE IF NOT EXISTS _scada_rtu_version ('
                   'rtu_id INTEGER PRIMARY KEY CHECK (rtu_id > 0), '
                   'definition_version INTEGER NOT NULL DEFAULT 0, command_version INTEGER NOT NULL DEFAULT 0)')
# Runtime columns are deliberately excluded from the definition version.  A
# communication worker updates these fields continuously and those writes must
# never cause a protocol reconnect/reload.
_DEFINITION_RUNTIME_COLUMNS = frozenset({
    '_version', 'raw_value', 'raw_status', 'raw_time', 'value', 'status',
    'refresh_time', 'refresh_time1', 'refresh_time2', 'refresh_time3',
    'cmd_time', 'cmd_handle_time', 'cmd_check_id',
    'man_disable_last_tm', 'lock_begintime', 'lock_holdtime',
    'process_enable', 'process_status', 'process_pid', 'process_start_time',
    'process_stop_time', 'process_heartbeat_time', 'process_exit_code',
    'process_error',
})
CHANNEL_PARAMETER_TABLES = {
    "iec104": "fes_channel_iec104_define",
    "modbus-rtu": "fes_channel_modbus_rtu_define",
    "modbus-tcp": "fes_channel_modbus_tcp_define",
    "can": "fes_channel_can_define",
    "bacnet": "fes_channel_bacnet_define",
    "opc": "fes_channel_opc_define",
}
CHANNEL_TYPES = frozenset(CHANNEL_PARAMETER_TABLES) | {"custom"}
POINT_KINDS = frozenset({"yc", "yx", "yk", "yt"})
class ApiError(Exception):
    def __init__(self, status: int, message: str, *, details: Any = None, code: str = "bad_request"):
        super().__init__(message)
        self.status, self.message, self.details, self.code = status, message, details, code


def quote_identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid SQL identifier: {value}")
    return '"' + value.replace('"', '""') + '"'


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


class SchemaCatalog:
    def __init__(self, directory: Path | str = SCHEMA_DIR):
        self.directory = Path(directory).resolve()
        if not self.directory.is_dir():
            raise RuntimeError(f"schema directory does not exist: {self.directory}")
        self.schemas: dict[str, dict[str, Any]] = {}
        for path in sorted(self.directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"invalid schema {path}: {exc}") from exc
            self._validate(payload, path.stem)
            self.schemas[path.stem] = payload
        if not self.schemas:
            raise RuntimeError(f"no JSON schemas in {self.directory}")

    @staticmethod
    def _validate(payload: dict[str, Any], filename: str) -> None:
        if not isinstance(payload, dict) or not IDENTIFIER.fullmatch(filename):
            raise RuntimeError(f"invalid table schema: {filename}")
        head = payload.get("head")
        columns = payload.get("columns")
        if not isinstance(head, dict) or head.get("eng_name") != filename:
            raise RuntimeError(f"schema {filename}: head.eng_name must match filename")
        if not isinstance(columns, list) or not columns:
            raise RuntimeError(f"schema {filename}: columns must be a non-empty list")
        names: list[str] = []
        for column in columns:
            if not isinstance(column, dict):
                raise RuntimeError(f"schema {filename}: column must be an object")
            name = column.get("eng_name")
            if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
                raise RuntimeError(f"schema {filename}: invalid column {name!r}")
            if name in names or name == '_version':
                raise RuntimeError(f"schema {filename}: duplicate column {name!r}")
            if column.get("data_type") not in {"int", "float", "str", "bool"}:
                raise RuntimeError(f"schema {filename}: unsupported data_type for {name}")
            if not isinstance(column.get('primary_key'), bool):
                raise RuntimeError(f"schema {filename}: primary_key must be boolean")
            if column.get('required') is not None and not isinstance(column.get('required'), bool):
                raise RuntimeError(f"schema {filename}: required must be boolean")
            if 'default' in column:
                default = column['default']
                valid_default = default is None
                if column.get('data_type') == 'int':
                    valid_default = isinstance(default, int) and not isinstance(default, bool)
                elif column.get('data_type') == 'float':
                    valid_default = isinstance(default, (int, float)) and not isinstance(default, bool)
                elif column.get('data_type') == 'str':
                    valid_default = isinstance(default, str)
                elif column.get('data_type') == 'bool':
                    valid_default = isinstance(default, (bool, int)) and default in (0, 1)
                if not valid_default:
                    raise RuntimeError(f"schema {filename}: invalid default for {name}")
            allowed_values = column.get('allowed_values')
            if allowed_values is not None and (
                not isinstance(allowed_values, list) or not allowed_values
                or any(not isinstance(value, str) for value in allowed_values)
            ):
                raise RuntimeError(f"schema {filename}: allowed_values must be a non-empty string list")
            names.append(name)
        if not any(column.get("primary_key") for column in columns):
            raise RuntimeError(f"schema {filename}: primary key is required")
        unique_keys = payload.get('unique_keys', [])
        if not isinstance(unique_keys, list):
            raise RuntimeError(f"schema {filename}: unique_keys must be a list")
        for unique_key in unique_keys:
            if (
                not isinstance(unique_key, list) or not unique_key
                or len(set(unique_key)) != len(unique_key)
                or any(name not in names for name in unique_key)
            ):
                raise RuntimeError(f"schema {filename}: invalid unique key {unique_key!r}")
        indexes = payload.get('indexes', [])
        if not isinstance(indexes, list):
            raise RuntimeError(f"schema {filename}: indexes must be a list")
        for index in indexes:
            if (
                not isinstance(index, list) or not index
                or len(set(index)) != len(index)
                or any(name not in names for name in index)
            ):
                raise RuntimeError(f"schema {filename}: invalid index {index!r}")

    def get(self, table: str) -> dict[str, Any]:
        try:
            return self.schemas[table]
        except KeyError as exc:
            raise ApiError(404, f"未找到表定义: {table}", code="not_found") from exc

    def names(self) -> list[str]:
        return sorted(self.schemas)

    def public(self, table: str) -> dict[str, Any]:
        payload = self.get(table)
        columns = payload["columns"]
        return {
            "head": payload["head"],
            "columns": columns,
            "primary_keys": [c["eng_name"] for c in columns if c["primary_key"]],
            "unique_keys": payload.get("unique_keys", []),
            "indexes": payload.get("indexes", []),
        }


class ScadaStore:
    # SQLite header identity. C/T databases use 0; the simulator has
    # the same schema but a different owner and must never be opened as C/T.
    application_id = 0

    def _check_database_role(self, connection):
        actual = connection.execute("PRAGMA application_id").fetchone()[0]
        if actual == self.application_id:
            return
        if self.application_id and actual == 0 and not connection.execute(
                "SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            return  # An empty file may be claimed by S in the schema transaction.
        raise ValueError(f"数据库用途不匹配：{self.db_path}；scdfe_s 必须使用独立数据库，不能与 C/T 共用")

    def __init__(self, db_path: Path | str = DEFAULT_DB, schema_dir: Path | str = SCHEMA_DIR,
                 *, connection_pool_size: int = 0):
        if type(connection_pool_size) is not int or not 0 <= connection_pool_size <= 64:
            raise ValueError('connection_pool_size 必须在 0..64 之间')
        self.connection_pool_size = connection_pool_size
        self._connection_pool = []
        self._pool_lock = threading.Lock()
        self._pool_closed = False
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog = SchemaCatalog(schema_dir)
        # Derived table metadata is immutable for the lifetime of a store.
        # Cache the identifier map and text projection used by every Web list
        # request; rebuilding them for each 5-second monitor refresh is pure
        # Python overhead and does not improve schema safety.
        self._table_meta_cache: dict[str, tuple[str, list[dict[str, Any]], list[str], dict[str, dict[str, Any]], list[str]]] = {}
        if self.db_path.exists() and self.db_path.stat().st_size:
            # Reject a database with the wrong role before enabling WAL.
            with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)) as existing:
                self._check_database_role(existing)
        try:
            self._initialise()
        except BaseException:
            self.close()
            raise

    def open_connection(self) -> sqlite3.Connection:
        """Open a configured SQLite connection owned by the caller.

        Most web operations still use :meth:`connection` for short-lived
        transactions. Long-running communication threads may keep one read
        connection and close it when their thread exits, avoiding repeated
        PRAGMA and connection setup work on every command poll.
        """
        return self._open_connection()

    def _open_connection(self, *, check_same_thread=True):
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None,
                                     check_same_thread=check_same_thread)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA mmap_size=1073741824")
            connection.execute("PRAGMA temp_store=MEMORY")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if not self.connection_pool_size:
            connection = self.open_connection()
            try:
                yield connection
            finally:
                connection.close()
            return
        with self._pool_lock:
            connection = self._connection_pool.pop() if self._connection_pool else None
        if connection is None:
            connection = self._open_connection(check_same_thread=False)
        try:
            yield connection
        finally:
            retained = False
            try:
                # Closing the old per-call connection rolled back implicitly.
                # Reused handles only need the rollback when a caller actually
                # left a transaction open; skip the no-op syscall on the
                # common autocommit read path.
                if connection.in_transaction:
                    connection.rollback()
                connection.row_factory = sqlite3.Row
                with self._pool_lock:
                    if not self._pool_closed and len(self._connection_pool) < self.connection_pool_size:
                        self._connection_pool.append(connection)
                        retained = True
            except sqlite3.Error:
                pass  # A damaged/explicitly closed connection is never pooled.
            finally:
                if not retained:
                    connection.close()

    def close(self):
        """Drain idle handles; in-flight leases close when returned.

        Late shutdown callbacks can still use short-lived connections, but
        cannot repopulate the pool after server_close().
        """
        with self._pool_lock:
            self._pool_closed = True
            connections, self._connection_pool = self._connection_pool, []
        for connection in connections:
            connection.close()

    @staticmethod
    def _sqlite_type(data_type: str) -> str:
        return {"int": "INTEGER", "float": "REAL", "str": "TEXT", "bool": "INTEGER"}[data_type]

    @staticmethod
    def _sql_literal(value: Any) -> str:
        if value is None:
            return 'NULL'
        if isinstance(value, bool):
            return '1' if value else '0'
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        raise RuntimeError(f'unsupported SQL default value: {value!r}')

    @classmethod
    def _column_definition(cls, column: dict[str, Any], *, include_required: bool = True) -> str:
        name = column['eng_name']
        part = f"{quote_identifier(name)} {cls._sqlite_type(column['data_type'])}"
        if include_required and (column['primary_key'] or name == 'rtu_id' or column.get('required', False)):
            part += ' NOT NULL'
        if name == 'rtu_id':
            part += ' CHECK (rtu_id > 0)'
        elif name in {'id', 'pnt_no'}:
            part += f" CHECK ({quote_identifier(name)} >= 0)"
        if 'default' in column:
            part += f" DEFAULT {cls._sql_literal(column['default'])}"
        return part

    def _initialise(self) -> None:
        # Retry only the initial lock acquisition; no application callback is
        # repeated after it has started.
        for attempt in range(8):
            try:
                with self.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._check_database_role(connection)
                    if self.application_id and connection.execute("PRAGMA application_id").fetchone()[0] == 0:
                        connection.execute(f"PRAGMA application_id={self.application_id}")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS _scada_schema (table_name TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)"
                    )
                    expected_tables = set(self.catalog.names())
                    actual_tables = {
                        row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if actual_tables.intersection({'_scada_command_version', '_scada_definition_version'}):
                        raise RuntimeError('数据库表结构不符合要求，需要显式迁移：python -m tools.simplify_schema --db <数据库> --apply')
                    connection.execute(RTU_VERSION_DDL)
                    unexpected_tables = actual_tables - expected_tables - {'_scada_schema', '_scada_rtu_version'}
                    if unexpected_tables:
                        raise RuntimeError(
                            '数据库包含当前 schema 未定义的表；请使用当前 schema 重新初始化数据库: '
                            f'{sorted(unexpected_tables)}'
                        )
                    for name in self.catalog.names():
                        payload = self.catalog.get(name)
                        fingerprint = hashlib.sha256(
                            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                        ).hexdigest()
                        old = connection.execute(
                            "SELECT fingerprint FROM _scada_schema WHERE table_name=?", (name,)
                        ).fetchone()
                        exists = connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                        ).fetchone() is not None
                        if not exists:
                            columns = payload["columns"]
                            primary_keys = [c["eng_name"] for c in columns if c["primary_key"]]
                            parts = ["_version INTEGER NOT NULL DEFAULT 1"]
                            for column in columns:
                                parts.append(self._column_definition(column))
                            parts.append("PRIMARY KEY (" + ", ".join(quote_identifier(k) for k in primary_keys) + ")")
                            for unique_key in payload.get('unique_keys', []):
                                parts.append("UNIQUE (" + ", ".join(quote_identifier(k) for k in unique_key) + ")")
                            if name != 'fes_rtu_define' and any(c['eng_name'] == 'rtu_id' for c in columns):
                                parts.append('FOREIGN KEY (rtu_id) REFERENCES fes_rtu_define(rtu_id) ON DELETE RESTRICT ON UPDATE RESTRICT')
                            connection.execute(
                                f"CREATE TABLE {quote_identifier(name)} ({', '.join(parts)})"
                                + (" WITHOUT ROWID" if name in KIND_TABLES.values() else "")
                            )
                            self._ensure_indexes(connection, name, payload)
                            connection.execute(
                                "INSERT INTO _scada_schema(table_name, fingerprint) VALUES (?,?)",
                                (name, fingerprint),
                            )
                            if any(c['eng_name'] == 'rtu_id' for c in columns) and primary_keys[0] != 'rtu_id':
                                connection.execute(f'CREATE INDEX {quote_identifier(name + "_rtu")} ON {quote_identifier(name)} (rtu_id)')
                        else:
                            self._validate_existing_table(connection, name, payload)
                            if not old:
                                raise RuntimeError(
                                    f'数据库表 {name} 缺少当前 schema 指纹；'
                                    '请重新初始化数据库'
                                )
                            if old[0] != fingerprint:
                                raise RuntimeError(
                                    f'数据库表 {name} 的 schema 指纹不一致；'
                                    '请重新初始化数据库'
                                )
                            self._ensure_indexes(connection, name, payload)
                    self._ensure_definition_version_triggers(connection, self.catalog)
                    self._ensure_command_version_triggers(connection)
                    connection.execute(
                        'INSERT OR IGNORE INTO _scada_rtu_version(rtu_id) SELECT rtu_id FROM fes_rtu_define'
                    )
                    metadata_tables = {
                        row[0] for row in connection.execute(
                            'SELECT table_name FROM _scada_schema'
                        )
                    }
                    if metadata_tables != expected_tables:
                        raise RuntimeError(
                            '_scada_schema 与当前 schema 不一致；'
                            '请重新初始化数据库'
                        )
                    connection.commit()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt == 7:
                    raise
                time.sleep(0.02 * (attempt + 1))

    def _validate_existing_table(self, connection, name, payload):
        """Require an existing table to match the current schema exactly."""
        expected = {column['eng_name'] for column in payload['columns']} | {'_version'}
        actual = {row[1] for row in connection.execute(
            f'PRAGMA table_info({quote_identifier(name)})'
        )}
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise RuntimeError(
                f'表 {name} 与当前 schema 不一致；请使用当前 schema 创建数据库，'
                f'缺少字段={missing}，多余字段={extra}'
            )

    @staticmethod
    def _ensure_indexes(connection, name, payload):
        for index, unique_key in enumerate(payload.get('unique_keys', []), 1):
            suffix = '_'.join(unique_key)
            index_name = f'{name}_uq_{index}_{suffix}'
            # A database created with a table-level UNIQUE clause already has
            # an equivalent SQLite auto-index.  Also tolerate an index made
            # by an earlier schema migration: creating another index with the
            # same columns only wastes space and slows writes.
            existing = False
            for row in connection.execute(f'PRAGMA index_list({quote_identifier(name)})'):
                if not row[2]:
                    continue
                columns = tuple(info[2] for info in connection.execute(
                    f'PRAGMA index_info({quote_identifier(row[1])})'
                ))
                if columns == tuple(unique_key):
                    existing = True
                    break
            if existing:
                continue
            try:
                connection.execute(
                    f'CREATE UNIQUE INDEX IF NOT EXISTS {quote_identifier(index_name)} '
                    f'ON {quote_identifier(name)} ('
                    + ', '.join(quote_identifier(key) for key in unique_key) + ')'
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError(
                    f'表 {name} 中存在违反当前唯一约束 {unique_key} 的重复数据'
                ) from exc
        for index, keys in enumerate(payload.get('indexes', []), 1):
            suffix = '_'.join(keys)
            index_name = f'{name}_idx_{index}_{suffix}'
            connection.execute(
                f'CREATE INDEX IF NOT EXISTS {quote_identifier(index_name)} '
                f'ON {quote_identifier(name)} ('
                + ', '.join(quote_identifier(key) for key in keys) + ')'
            )

    @staticmethod
    def _ensure_definition_version_triggers(connection, catalog) -> None:
        """Maintain a cheap per-RTU version for static definition changes.

        Definition fingerprints used to serialize every static cell from every
        point table on each worker check.  SQLite triggers move that work to
        the write that actually changes a definition, leaving the hot read to
        one indexed lookup.  Runtime value/quality/heartbeat fields are
        excluded so normal acquisition never asks a protocol to reconnect.
        """
        def bump(alias: str) -> str:
            return (
                f'INSERT INTO _scada_rtu_version(rtu_id, definition_version) VALUES ({alias}.rtu_id, 1) '
                'ON CONFLICT(rtu_id) DO UPDATE SET definition_version=definition_version+1;'
            )

        for name in catalog.names():
            columns = catalog.get(name)['columns']
            if not any(column['eng_name'] == 'rtu_id' for column in columns):
                continue
            static_columns = [
                column['eng_name'] for column in columns
                if column['eng_name'] not in _DEFINITION_RUNTIME_COLUMNS
                and column['eng_name'] != 'rtu_id'
            ]
            table = quote_identifier(name)
            trigger_prefix = f'_scada_defver_{name}'
            connection.execute(
                f'CREATE TRIGGER IF NOT EXISTS {quote_identifier(trigger_prefix + "_ins")} '
                f'AFTER INSERT ON {table} BEGIN {bump("NEW")} END'
            )
            connection.execute(
                f'CREATE TRIGGER IF NOT EXISTS {quote_identifier(trigger_prefix + "_del")} '
                f'AFTER DELETE ON {table} BEGIN {bump("OLD")} END'
            )
            if static_columns:
                # Auxiliary rows keyed by id can be reassigned through the Web API.
                # Both workers must reload the mapping from the committed generation.
                movable = any(c['eng_name'] == 'rtu_id' and not c['primary_key'] for c in columns)
                if movable:
                    static_columns.append('rtu_id')
                old_owner = (
                    'INSERT INTO _scada_rtu_version(rtu_id, definition_version) '
                    'SELECT OLD.rtu_id, 1 WHERE OLD.rtu_id IS NOT NEW.rtu_id '
                    'ON CONFLICT(rtu_id) DO UPDATE SET definition_version=definition_version+1;'
                ) if movable else ''
                update_columns = ', '.join(quote_identifier(column) for column in static_columns)
                changed = ' OR '.join(
                    f'OLD.{quote_identifier(column)} IS NOT NEW.{quote_identifier(column)}'
                    for column in static_columns
                )
                trigger_name = trigger_prefix + '_upd'
                sql = (
                    f'CREATE TRIGGER {quote_identifier(trigger_name)} '
                    f'AFTER UPDATE OF {update_columns} ON {table} '
                    f'WHEN {changed} BEGIN {old_owner}{bump("NEW")} END'
                )
                existing = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (trigger_name,),
                ).fetchone()
                # Upgrade only this owned trigger, inside the initialization
                # transaction. Reopening an unchanged schema performs no DDL.
                if existing is None or existing[0] != sql:
                    connection.execute(f'DROP TRIGGER IF EXISTS {quote_identifier(trigger_name)}')
                    connection.execute(sql)

    @staticmethod
    def _ensure_command_version_triggers(connection) -> None:
        """Maintain one cheap command-change token per RTU.

        Polling the command tables used to aggregate ``_version`` for every
        YK/YT row on every communication cycle.  Status/acknowledgement writes
        also changed that aggregate, so an idle channel repeatedly scanned and
        materialised the same command rows.  These triggers advance a compact
        token only when command input changes or rows are added/removed.
        """
        def bump(alias: str) -> str:
            return (
                f'INSERT INTO _scada_rtu_version(rtu_id, command_version) VALUES ({alias}.rtu_id, 1) '
                'ON CONFLICT(rtu_id) DO UPDATE SET command_version=command_version+1;'
            )

        for table_name in ('fes_yk_define', 'fes_yt_define'):
            table = quote_identifier(table_name)
            prefix = f'_scada_cmdver_{table_name}'
            connection.execute(
                f'CREATE TRIGGER IF NOT EXISTS {quote_identifier(prefix + "_ins")} '
                f'AFTER INSERT ON {table} BEGIN {bump("NEW")} END'
            )
            connection.execute(
                f'CREATE TRIGGER IF NOT EXISTS {quote_identifier(prefix + "_del")} '
                f'AFTER DELETE ON {table} BEGIN {bump("OLD")} END'
            )
            connection.execute(
                f'CREATE TRIGGER IF NOT EXISTS {quote_identifier(prefix + "_upd")} '
                f'AFTER UPDATE OF "value", "cmd_time" ON {table} '
                f'WHEN OLD."value" IS NOT NEW."value" OR OLD."cmd_time" IS NOT NEW."cmd_time" '
                f'BEGIN {bump("NEW")} END'
            )

    def schema(self, table: str) -> dict[str, Any]:
        return self.catalog.get(table)

    def schemas(self) -> list[dict[str, Any]]:
        return [self.catalog.public(name) for name in self.catalog.names()]

    def _table_parts(self, table: str) -> tuple[str, list[dict[str, Any]], list[str]]:
        canonical, columns, primary_keys, _by_name, _text_columns = self._table_metadata(table)
        return canonical, columns, primary_keys

    def _table_metadata(self, table: str) -> tuple[str, list[dict[str, Any]], list[str], dict[str, dict[str, Any]], list[str]]:
        """Return cached metadata used to validate and project table requests."""
        cached = self._table_meta_cache.get(table)
        if cached is not None:
            return cached
        payload = self.schema(table)
        columns = payload["columns"]
        primary_keys = [column["eng_name"] for column in columns if column["primary_key"]]
        by_name = {column["eng_name"]: column for column in columns}
        text_columns = [
            column["eng_name"] for column in columns
            if column["data_type"] == "str" or column["primary_key"]
        ]
        cached = (payload["head"]["eng_name"], columns, primary_keys, by_name, text_columns)
        self._table_meta_cache[table] = cached
        return cached

    @staticmethod
    def _coerce(value: Any, column: dict[str, Any], *, required: bool = False) -> Any:
        name, kind = column["eng_name"], column["data_type"]
        if value is None:
            if required:
                raise ApiError(400, f"字段 {name} 不能为空")
            return None
        if kind == "int":
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ApiError(400, f"字段 {name} 必须是整数")
            try:
                if isinstance(value, str) and not re.fullmatch(r'[+-]?[0-9]+', value.strip()):
                    raise ValueError
                number = int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ApiError(400, f"字段 {name} 必须是整数") from exc
            if not -(2**63) <= number <= 2**63 - 1:
                raise ApiError(400, f"字段 {name} 超出 SQLite 整数范围")
            if name == 'rtu_id' and number <= 0:
                raise ApiError(400, 'rtu_id 必须是正整数')
            if name in {'id', 'pnt_no', 'channel_id', 'source_pnt_no', 'address', 'io_address'} and number < 0:
                raise ApiError(400, f'{name} 不能为负数')
            if name == 'source_rtu_id' and number <= 0:
                raise ApiError(400, 'source_rtu_id 必须是正整数')
            if name == 'process_enable' and number not in {0, 1}:
                raise ApiError(400, f'{name} 必须是 0 或 1')
            if name == 'log_retention_hours' and not 1 <= number <= 87600:
                raise ApiError(400, f'{name} 必须在 1 到 87600 之间')
            if name == 'worker_start_delay_ms' and not 0 <= number <= 60000:
                raise ApiError(400, f'{name} 必须在 0 到 60000 之间')
            if name == 'retry_count' and not 0 <= number <= 10:
                raise ApiError(400, f'字段 {name} 必须在 0 到 10 之间')
            if name in {'unit_id', 'irtuaddr'} and not 1 <= number <= 247:
                raise ApiError(400, f'字段 {name} 必须在 1 到 247 之间')
            if name == 'baudrate' and not 1 <= number <= 10000000:
                raise ApiError(400, f'字段 {name} 必须在 1 到 10000000 之间')
            if name == 'bytesize' and number != 8:
                raise ApiError(400, 'Modbus RTU bytesize 必须为 8')
            if name == 'stopbits' and number not in {1, 2}:
                raise ApiError(400, 'Modbus RTU stopbits 必须为 1 或 2')
            if name in {'scan_interval_ms', 'poll_interval_ms', 'retry_interval_ms', 'report_interval_ms'} and not 50 <= number <= 60000:
                raise ApiError(400, f'{name} 必须在 50 到 60000 之间')
            if name == 'bind_port' and not 0 <= number <= 65535:
                raise ApiError(400, '字段 bind_port 必须在 0 到 65535 之间')
            if name == 'common_address' and not 1 <= number <= 65534:
                raise ApiError(400, 'IEC104 公共地址必须在 1 到 65534 之间')
            if name == 'remote_port' and not 1 <= number <= 65535:
                raise ApiError(400, '字段 remote_port 必须在 1 到 65535 之间')
            if name in {'keep_alive', 'if_yx', 'if_reverse', 'ifyk', 'ifreverse',
                         'send_ff_replace_01'} and number not in {0, 1}:
                raise ApiError(400, f'字段 {name} 必须是 0 或 1')
            if name == 'trans_type' and not 1 <= number <= 11:
                raise ApiError(400, '字段 trans_type 必须在 1 到 11 之间')
            if name == 'ytyk_type' and not 1 <= number <= 7:
                raise ApiError(400, '字段 ytyk_type 必须在 1 到 7 之间')
            if name in {'iplace', 'reg_addr'} and not 0 <= number <= 65535:
                raise ApiError(400, f'字段 {name} 必须在 0 到 65535 之间')
            if name == 'inum' and not 1 <= number <= 2000:
                raise ApiError(400, '字段 inum 必须在 1 到 2000 之间')
            if name in {'offset_bit', 'special_yk_bit'} and not 0 <= number <= 15:
                raise ApiError(400, f'字段 {name} 必须在 0 到 15 之间')
            if name == 'all_bit' and not 1 <= number <= 16:
                raise ApiError(400, '字段 all_bit 必须在 1 到 16 之间')
            if name in {'twobytedata_byteseq', 'fourbytedata_byteseq'} and not 0 <= number <= 3:
                raise ApiError(400, f'字段 {name} 必须在 0 到 3 之间')
            if name == 'special_yk_yccnt' and number < 0:
                raise ApiError(400, '字段 special_yk_yccnt 不能为负数')
            return number
        if kind == "float":
            if isinstance(value, bool):
                raise ApiError(400, f"字段 {name} 必须是数字")
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ApiError(400, f"字段 {name} 必须是数字") from exc
            if number != number or number in (float("inf"), float("-inf")):
                raise ApiError(400, f"字段 {name} 必须是有限数字")
            if name in {'connect_timeout', 'response_timeout'} and number <= 0:
                raise ApiError(400, f"字段 {name} 必须是正数")
            return number
        if kind == "bool":
            if not isinstance(value, bool):
                raise ApiError(400, f"字段 {name} 必须是布尔值")
            return int(value)
        if not isinstance(value, str):
            raise ApiError(400, f"字段 {name} 必须是字符串")
        if name in {'remote_host', 'serial_port'} and not value.strip():
            raise ApiError(400, f'字段 {name} 不能为空')
        if name == 'channel_type':
            if value not in CHANNEL_TYPES:
                raise ApiError(400, f"字段 {name} 必须是受支持的通道类型")
        allowed_values = column.get('allowed_values')
        if allowed_values is not None and value not in allowed_values:
            raise ApiError(400, f"字段 {name} 的值不在允许范围内")
        if name == "kind" and value not in POINT_KINDS:
            raise ApiError(400, "字段 kind 必须是 yc、yx、yk 或 yt")
        if name == "command_mode" and value.upper() not in {"DIRECT", "SELECT_AND_EXECUTE", "SELECT-AND-EXECUTE", "SELECT"}:
            raise ApiError(400, "字段 command_mode 必须是 DIRECT 或 SELECT_AND_EXECUTE")
        if name == "value_type" and value.lower() not in {"bool", "uint", "int", "float"}:
            raise ApiError(400, "字段 value_type 必须是 bool、uint、int 或 float")
        if name == "byte_order" and value.lower() not in {"big", "little"}:
            raise ApiError(400, "字段 byte_order 必须是 big 或 little")
        return value

    @staticmethod
    def _validate_modbus_row(table: str, values: dict[str, Any], *, partial: bool) -> None:
        """Validate Modbus auxiliary rows at the API boundary.

        The JSON schema carries field types and menus, while these constraints
        describe the relationship between fields in a Modbus frame.  Keeping
        them here prevents a row that is individually well typed from becoming
        an unusable poll/command definition.
        """
        if not table.startswith('fes_channel_modbus_'):
            return

        def present(name: str) -> bool:
            return name in values and values[name] is not None

        def integer_field(name: str, lo: int, hi: int | None = None) -> None:
            if not present(name):
                return
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < lo or (hi is not None and value > hi):
                bound = f'{lo}' if hi is None else f'{lo} 到 {hi}'
                raise ApiError(400, f'字段 {name} 必须在 {bound} 之间')

        if table in {'fes_channel_modbus_define', 'fes_channel_modbus_define_call'}:
            integer_field('irtuaddr', 1, 247)
            integer_field('iplace', 0, 65535)
            integer_field('inum', 1, 2000)
            if present('icode'):
                code = values['icode']
                if code not in {1, 2, 3, 4}:
                    raise ApiError(400, '召唤功能码必须是 1、2、3 或 4')
                if present('inum') and code >= 3 and values['inum'] > 125:
                    raise ApiError(400, '寄存器召唤的 inum 不能超过 125')
            integer_field('ioffset', 0, 65535)
            integer_field('ibitnum', 1, 16)
        elif table == 'fes_channel_modbus_define_data':
            integer_field('if_yx', 0, 1)
            integer_field('cnt', 0, 2**31 - 1)
            integer_field('trans_type', 1, 11)
            integer_field('reg_addr', 0, 65535)
            integer_field('offset_bit', 0, 15)
            integer_field('all_bit', 1, 16)
            integer_field('if_reverse', 0, 1)
            integer_field('twobytedata_byteseq', 0, 3)
            integer_field('fourbytedata_byteseq', 0, 3)
            if present('trans_type') and values['trans_type'] == 9:
                if not present('offset_bit') or not present('all_bit'):
                    raise ApiError(400, '多位数据必须提供 offset_bit 和 all_bit')
        elif table == 'fes_channel_modbus_define_ytyk':
            integer_field('ifyk', 0, 1)
            integer_field('cnt', 0, 2**31 - 1)
            integer_field('irtuaddr', 1, 247)
            integer_field('reg_addr', 0, 65535)
            integer_field('ifreverse', 0, 1)
            integer_field('twobytedata_byteseq', 0, 3)
            integer_field('fourbytedata_byteseq', 0, 3)
            integer_field('special_yk_yccnt', 0, 2**31 - 1)
            integer_field('special_yk_bit', 0, 15)
            integer_field('send_ff_replace_01', 0, 1)
            if present('ytyk_type') and values['ytyk_type'] not in {1, 2, 3, 4, 5, 6, 7}:
                raise ApiError(400, '字段 ytyk_type 必须在 1 到 7 之间')
            if present('icode'):
                code = values['icode']
                if code not in {5, 6, 16}:
                    raise ApiError(400, '遥控/遥调功能码必须是 5、6 或 16')

    @staticmethod
    def _validate_trans_row(table: str, values: dict[str, Any], *, partial: bool) -> None:
        """Validate protocol-neutral four-remote trans mappings."""
        if table == 'fes_scdfe_t_define':
            if 'protocol' in values and values['protocol'] not in {'iec104', 'modbus-tcp'}:
                raise ApiError(400, '转发规约只能是 iec104 或 modbus-tcp')
            if 'channel_id' in values and values['channel_id'] <= 0:
                raise ApiError(400, 'channel_id 必须是正整数')
            return
        if table != 'fes_scdfe_t_point_define':
            return
        if 'kind' in values and values['kind'] not in POINT_KINDS:
            raise ApiError(400, '转发点类型必须是 yc、yx、yk 或 yt')
        if 'enabled' in values and values['enabled'] not in {0, 1}:
            raise ApiError(400, 'enabled 必须是 0 或 1')
        if 'address' in values and not 0 <= values['address'] <= 65535:
            raise ApiError(400, 'Modbus 地址必须在 0 到 65535 之间')
        if 'io_address' in values and not 0 <= values['io_address'] <= 16777215:
            raise ApiError(400, 'IEC 信息体地址必须在 0 到 16777215 之间')
        if 'scale' in values and values['scale'] == 0:
            raise ApiError(400, '比例不能为 0')

    def _normalise(self, table: str, values: dict[str, Any], *, partial: bool = False,
                   validate_row: bool = True, generated_key: str | None = None) -> dict[str, Any]:
        canonical, columns, primary_keys = self._table_parts(table)
        if not isinstance(values, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        allowed = {c["eng_name"] for c in columns}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ApiError(400, "包含未知字段", details={"unknown_fields": unknown})
        result: dict[str, Any] = {}
        for column in columns:
            name = column["eng_name"]
            if name in values:
                result[name] = self._coerce(
                    values[name], column,
                    required=column["primary_key"] or name == 'rtu_id' or column.get('required', False),
                )
            elif name == generated_key:
                continue
            elif not partial and (column["primary_key"] or name == 'rtu_id' or column.get('required', False)):
                raise ApiError(400, f"缺少必填字段: {name}")
            elif not partial and 'default' in column:
                result[name] = self._coerce(column['default'], column)
        if validate_row:
            self._validate_modbus_row(canonical, result, partial=partial)
            self._validate_trans_row(canonical, result, partial=partial)
        return result

    def _key_values(self, table: str, raw: str | list[Any] | tuple[Any, ...]) -> dict[str, Any]:
        canonical, columns, primary_keys = self._table_parts(table)
        values = raw if isinstance(raw, (list, tuple)) else raw.split(",")
        if len(values) != len(primary_keys):
            raise ApiError(400, f"主键需要 {len(primary_keys)} 个值: {','.join(primary_keys)}")
        by_name = {c["eng_name"]: c for c in columns}
        return {name: self._coerce(values[index], by_name[name], required=True) for index, name in enumerate(primary_keys)}

    @staticmethod
    def _public_row(row: sqlite3.Row, columns: list[dict[str, Any]]) -> dict[str, Any]:
        result = {}
        for column in columns:
            value = row[column["eng_name"]]
            if column["data_type"] == "bool" and value is not None:
                value = bool(value)
            result[column["eng_name"]] = _json_value(value)
        result["_version"] = int(row["_version"])
        return result

    @staticmethod
    def _public_rows(rows: list[sqlite3.Row], columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert a page of SQLite rows without repeating name lookups.

        ``list_rows`` is the main high-volume Web read path.  The old
        per-row converter looked up every column by name and rebuilt the
        schema loop for every result row.  ``SELECT *`` returns the same
        physical column order for a table, so resolve the indexes once and
        use the row slice for the common layout (``_version`` first).  The
        indexed fallback keeps the method correct for databases whose column
        order was created externally, and retains the existing bool/bytes
        JSON conversion semantics.
        """
        if not rows:
            return []
        names = tuple(column["eng_name"] for column in columns)
        bool_indexes = tuple(
            index for index, column in enumerate(columns)
            if column["data_type"] == "bool"
        )
        row_keys = rows[0].keys()
        direct_layout = bool(row_keys and row_keys[0] == "_version"
                             and tuple(row_keys[1:]) == names)
        value_indexes = (None if direct_layout else
                         tuple(row_keys.index(name) for name in names))
        version_index = 0 if direct_layout else row_keys.index("_version")
        result: list[dict[str, Any]] = []
        for row in rows:
            values = row[1:] if direct_layout else tuple(row[index] for index in value_indexes)
            if bool_indexes:
                values = list(values)
                for index in bool_indexes:
                    if values[index] is not None:
                        values[index] = bool(values[index])
            if any(isinstance(value, bytes) for value in values):
                values = list(values)
                for index, value in enumerate(values):
                    if isinstance(value, bytes):
                        values[index] = _json_value(value)
            item = dict(zip(names, values))
            item["_version"] = int(row[version_index])
            result.append(item)
        return result

    def list_rows(self, table: str, *, where: dict[str, Any] | None = None, search: str = "", limit: int = 50,
                  offset: int = 0, sort: str | None = None, order: str = "asc",
                  fields: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        canonical, columns, primary_keys, by_name, text_columns = self._table_metadata(table)
        if limit < 1 or limit > 500 or offset < 0:
            raise ApiError(400, "limit 必须在 1 到 500 之间，offset 不能为负数")
        # List views often show only a small projection of a wide point or
        # protocol auxiliary table.  Keep the full schema authoritative while
        # allowing callers to avoid reading/serialising hidden columns.  Keys
        # and the optimistic-concurrency token are always returned so row
        # actions remain safe even when a caller requests a narrow projection.
        if fields is None:
            selected_columns = columns
        else:
            requested = list(fields)
            if not requested or any(not isinstance(name, str) or not name for name in requested):
                raise ApiError(400, "fields 必须是非空字段名列表")
            unknown_fields = sorted(set(requested) - set(by_name))
            if unknown_fields:
                raise ApiError(400, "未知投影字段", details={"unknown_fields": unknown_fields})
            selected_names = list(dict.fromkeys([*primary_keys, *requested]))
            selected_columns = [by_name[name] for name in selected_names]
        where = where or {}
        unknown = sorted(set(where) - set(by_name))
        if unknown:
            raise ApiError(400, "未知过滤字段", details={"unknown_fields": unknown})
        conditions: list[str] = []
        params: list[Any] = []
        for name, value in where.items():
            conditions.append(f"{quote_identifier(name)}=?")
            params.append(self._coerce(value, by_name[name], required=True))
        if search:
            if text_columns:
                conditions.append("(" + " OR ".join(f"CAST({quote_identifier(name)} AS TEXT) LIKE ? ESCAPE '\\'" for name in text_columns) + ")")
                escaped = search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                params.extend([f"%{escaped}%"] * len(text_columns))
        predicate = " WHERE " + " AND ".join(conditions) if conditions else ""
        if sort is None:
            sort = primary_keys[0]
        if sort not in by_name:
            raise ApiError(400, f"不允许按字段排序: {sort}")
        if order not in {"asc", "desc"}:
            raise ApiError(400, "order 只能是 asc 或 desc")
        with self.connection() as connection:
            connection.execute('BEGIN')
            ordering = ', '.join(f'{quote_identifier(name)} {order.upper()}' for name in [sort] + [k for k in primary_keys if k != sort])
            projection = ', '.join([quote_identifier('_version'), *(
                quote_identifier(column["eng_name"]) for column in selected_columns
            )])
            rows = connection.execute(
                f"SELECT {projection} FROM {quote_identifier(canonical)}{predicate} ORDER BY {ordering} LIMIT ? OFFSET ?",
                [*params, limit + 1, offset],
            ).fetchall()
            # A short final page already gives the exact count. A full page
            # with a successor, or an empty page beyond the end, still needs
            # COUNT from this same read snapshot.
            if len(rows) > limit or (offset and not rows):
                total = connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(canonical)}{predicate}", params).fetchone()[0]
                rows = rows[:limit]
            else:
                total = offset + len(rows)
        return {"items": self._public_rows(rows, selected_columns), "total": int(total), "limit": limit, "offset": offset}

    def get_row(self, table: str, key: str | list[Any] | tuple[Any, ...]) -> dict[str, Any]:
        canonical, columns, primary_keys = self._table_parts(table)
        keys = self._key_values(table, key)
        condition = " AND ".join(f"{quote_identifier(name)}=?" for name in primary_keys)
        with self.connection() as connection:
            row = connection.execute(f"SELECT * FROM {quote_identifier(canonical)} WHERE {condition}", list(keys.values())).fetchone()
        if row is None:
            raise ApiError(404, "记录不存在", code="not_found")
        return self._public_row(row, columns)

    def create_row(self, table: str, values: dict[str, Any], *, allocate_key=False) -> dict[str, Any]:
        canonical, columns, primary_keys = self._table_parts(table)
        # Only designated channel endpoints can omit a single SQLite INTEGER
        # PRIMARY KEY. Generic table CRUD continues to require explicit keys.
        generated = primary_keys[0] if allocate_key else None
        if generated and (len(primary_keys) != 1 or generated in values or
                          canonical not in (set(CHANNEL_PARAMETER_TABLES.values()) | {'fes_scdfe_t_define'}) or
                          generated not in {'channel_id', 'id'}):
            raise ValueError("不支持自动分配该主键")
        normalised = self._normalise(table, values, generated_key=generated)
        names = [c["eng_name"] for c in columns]
        insert_names = [name for name in names if name in normalised]
        placeholders = ", ".join("?" for _ in insert_names)
        sql = f"INSERT INTO {quote_identifier(canonical)} ({', '.join(quote_identifier(n) for n in insert_names)}) VALUES ({placeholders})"
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(sql, [normalised[n] for n in insert_names])
                if generated:
                    normalised[generated] = cursor.lastrowid
                row = self._read(connection, table, [normalised[n] for n in primary_keys])
                connection.commit()
                return row
        except sqlite3.IntegrityError as exc:
            if "foreign key" in str(exc).lower():
                raise ApiError(409, "引用的 RTU 不存在", code="conflict") from exc
            raise ApiError(409, "主键已存在或数据违反约束", code="conflict") from exc

    def _read(self, connection, table, key):
        canonical, columns, primary_keys = self._table_parts(table)
        keys = self._key_values(table, key)
        predicate = ' AND '.join(f'{quote_identifier(k)}=?' for k in keys)
        row = connection.execute(f'SELECT * FROM {quote_identifier(canonical)} WHERE {predicate}', list(keys.values())).fetchone()
        if row is None:
            raise ApiError(404, '记录不存在', code='not_found')
        return self._public_row(row, columns)

    @staticmethod
    def _check_version(row, if_match):
        if if_match is not None and row['_version'] != if_match:
            raise ApiError(409, '记录已被其他操作修改，请关闭并重新打开编辑窗口', code='version_conflict')

    def _rtu_reference_tables(self) -> list[tuple[str, str]]:
        """Return every schema that is owned by an RTU.

        The relationship is defined by the schema itself, rather than by a
        hard-coded protocol list.  This keeps cascade deletion complete when a
        new protocol auxiliary table is added with an ``rtu_id`` column.
        """
        references = []
        for name in self.catalog.names():
            if name == 'fes_rtu_define':
                continue
            columns = self.schema(name)['columns']
            if any(column['eng_name'] == 'rtu_id' for column in columns):
                references.append((name, self.schema(name)['head']['chn_name']))
        return references

    def describe_rtu_cascade(self, rtu_id: int | str) -> dict[str, Any]:
        """Describe the records that deleting one RTU will remove."""
        keys = self._key_values('fes_rtu_define', [rtu_id])
        with self.connection() as connection:
            row = connection.execute(
                'SELECT * FROM fes_rtu_define WHERE rtu_id=?',
                (keys['rtu_id'],),
            ).fetchone()
            if row is None:
                raise ApiError(404, '记录不存在', code='not_found')
            rtu = self._public_row(row, self.schema('fes_rtu_define')['columns'])
            references = [{
                'table': 'fes_rtu_define',
                'name': self.schema('fes_rtu_define')['head']['chn_name'],
                'count': 1,
            }]
            for name, label in self._rtu_reference_tables():
                count = connection.execute(
                    f'SELECT COUNT(*) FROM {quote_identifier(name)} WHERE rtu_id=?',
                    (keys['rtu_id'],),
                ).fetchone()[0]
                if count:
                    references.append({'table': name, 'name': label, 'count': int(count)})
        return {
            'rtu_id': keys['rtu_id'],
            'rtu': rtu,
            'references': references,
            'total': sum(item['count'] for item in references),
        }

    def delete_rtu_cascade(self, rtu_id: int | str, if_match: int | None = None) -> dict[str, Any]:
        """Delete an RTU and all of its owned rows in one SQLite transaction."""
        keys = self._key_values('fes_rtu_define', [rtu_id])
        deleted_rows: dict[str, int] = {}
        try:
            with self.connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                parent = self._read(connection, 'fes_rtu_define', [keys['rtu_id']])
                self._check_version(parent, if_match)
                for name, _label in self._rtu_reference_tables():
                    count = connection.execute(
                        f'SELECT COUNT(*) FROM {quote_identifier(name)} WHERE rtu_id=?',
                        (keys['rtu_id'],),
                    ).fetchone()[0]
                    if count:
                        connection.execute(
                            f'DELETE FROM {quote_identifier(name)} WHERE rtu_id=?',
                            (keys['rtu_id'],),
                        )
                        deleted_rows[name] = int(count)
                connection.execute(
                    'DELETE FROM fes_rtu_define WHERE rtu_id=?',
                    (keys['rtu_id'],),
                )
                # The RTU delete trigger bumps this row before the parent is
                # removed.  Drop both internal tokens so recreating the same
                # RTU starts from a clean definition/command generation.
                connection.execute(
                    'DELETE FROM _scada_rtu_version WHERE rtu_id=?',
                    (keys['rtu_id'],),
                )
                deleted_rows['fes_rtu_define'] = 1
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, 'RTU 仍被其他数据引用，不能删除', details=str(exc), code='conflict') from exc
        return {
            'deleted': True,
            'rtu_id': keys['rtu_id'],
            'deleted_rows': deleted_rows,
            'total_deleted': sum(deleted_rows.values()),
        }

    def submit_command(self, rtu_id, kind, point_no, body, if_match=None):
        """Atomically publish one explicit command for the channel worker.

        A point has one command slot. Do not replace an unhandled command;
        cmd_time is strictly increasing even for repeated identical values.
        """
        if kind not in {'yk', 'yt'}:
            raise ApiError(400, '只支持遥控和遥调指令')
        if set(body) != {'value'}:
            raise ApiError(400, '指令只接受 value 字段')
        if if_match is None:
            raise ApiError(428, '请先读取测点并提交 If-Match 版本', code='version_required')
        table = KIND_TABLES[kind]
        key = [rtu_id, point_no]
        columns = {column['eng_name']: column for column in self.schema(table)['columns']}
        value = self._coerce(body['value'], columns['value'], required=True)
        if kind == 'yk' and value not in {0, 1}:
            raise ApiError(400, '遥控值只能为 0 或 1')
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = self._read(connection, table, key)
            self._check_version(row, if_match)
            rtu = self._read(connection, 'fes_rtu_define', [rtu_id])
            # Every client adapter that exposes ``send_yk_point``/
            # ``send_yt_point`` can consume a queued command.  CAN was
            # accidentally omitted from this Web-side capability gate even
            # though ``client.can.CanMaster`` implements both operations;
            # that made CAN commands fail before they reached scdfe_c.
            if rtu['channel_type'] not in {'iec104', 'modbus-tcp', 'modbus-rtu', 'can', 'bacnet', 'opc'}:
                raise ApiError(409, '该规约尚未配置按点遥控/遥调下发接口', code='command_unsupported')
            if row.get('man_disable') or row.get('auto_disable'):
                raise ApiError(409, '测点已闭锁，禁止下发', code='command_disabled')
            if int(row.get('cmd_time') or 0) > int(row.get('cmd_handle_time') or 0):
                raise ApiError(409, '该测点还有待处理指令', code='command_pending')
            if kind == 'yt':
                low, high = row.get('eng_min'), row.get('eng_max')
                if low is not None and high is not None and low > high:
                    raise ApiError(409, '遥调工程上下限配置错误', code='command_range')
                if (low is not None and value < low) or (high is not None and value > high):
                    raise ApiError(400, '遥调值超出工程上下限', code='command_range')
            command_time = max(int(time.time() * 1000),
                               int(row.get('cmd_time') or 0) + 1,
                               int(row.get('cmd_handle_time') or 0) + 1)
            keys = self._key_values(table, key)
            connection.execute(
                f'UPDATE {quote_identifier(table)} SET value=?, cmd_time=?, status=0, '
                '_version=_version+1 WHERE rtu_id=? AND pnt_no=?',
                (value, command_time, keys['rtu_id'], keys['pnt_no']),
            )
            item = self._read(connection, table, key)
            connection.commit()
        return item

    def _prepare_update(self, table, key, values):
        canonical, columns, primary_keys = self._table_parts(table)
        keys = self._key_values(table, key)
        normalised = self._normalise(table, values, partial=True, validate_row=False)
        if set(normalised).intersection(primary_keys):
            raise ApiError(400, "主键字段不能修改")
        if not normalised:
            raise ApiError(400, "至少需要修改一个字段")
        assignments = ", ".join(f"{quote_identifier(name)}=?" for name in normalised)
        predicate = " AND ".join(f"{quote_identifier(name)}=?" for name in primary_keys)
        params = [normalised[n] for n in normalised]
        params.extend(keys.values())
        sql = f"UPDATE {quote_identifier(canonical)} SET {assignments}, _version=_version+1 WHERE {predicate}"
        return canonical, key, normalised, sql, params

    def _apply_update(self, connection, change, if_match=None):
        table, key, values, sql, params = change
        # Only the Modbus/trans validators merge changed fields with the old
        # row. Other schemas can enforce existence/version in the UPDATE.
        if not table.startswith('fes_channel_modbus_') and table not in {'fes_scdfe_t_define', 'fes_scdfe_t_point_define'}:
            if if_match is not None:
                sql += ' AND _version=?'
                params = [*params, if_match]
            if connection.execute(sql, params).rowcount == 0:
                self._check_version(self._read(connection, table, key), if_match)
                raise ApiError(409, '更新未写入', code='conflict')
            return
        current = self._read(connection, table, key)
        self._check_version(current, if_match)
        # Validate partial edits against the full row under the write lock.
        merged = {**current, **values}
        self._validate_modbus_row(table, merged, partial=False)
        self._validate_trans_row(table, merged, partial=False)
        connection.execute(sql, params)

    def update_rows(self, updates, *, return_rows=False):
        """Commit one validated batch; optionally return its exact row versions."""
        changes = [self._prepare_update(*item) for item in updates]
        if not changes:
            return [] if return_rows else 0
        try:
            with self.connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                for change in changes:
                    self._apply_update(connection, change)
                result = ([self._read(connection, change[0], change[1]) for change in changes]
                          if return_rows else len(changes))
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, '引用的 RTU 不存在或数据违反约束', code='conflict') from exc
        return result

    def update_row(self, table: str, key: str | list[Any] | tuple[Any, ...], values: dict[str, Any], if_match: int | None = None) -> dict[str, Any]:
        change = self._prepare_update(table, key, values)
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._apply_update(connection, change, if_match)
                row = self._read(connection, table, key)
                connection.commit()
                return row
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, '引用的 RTU 不存在或数据违反约束', code='conflict') from exc

    def delete_row(self, table: str, key: str | list[Any] | tuple[Any, ...], if_match: int | None = None) -> None:
        canonical, columns, primary_keys = self._table_parts(table)
        if canonical == 'fes_rtu_define':
            # Keep every RTU deletion path consistent, including callers that
            # use the store directly instead of the HTTP route.
            self.delete_rtu_cascade(key, if_match)
            return
        keys = self._key_values(table, key)
        predicate = " AND ".join(f"{quote_identifier(name)}=?" for name in primary_keys)
        params = list(keys.values())
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._check_version(self._read(connection, table, key), if_match)
                connection.execute(f"DELETE FROM {quote_identifier(canonical)} WHERE {predicate}", params)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "记录仍被其他表引用，不能删除", details=str(exc), code="conflict") from exc

    def overview(self, rtu_id=None, *, summary=False):
        names = ('fes_rtu_define', 'fes_yc_define', 'fes_yx_define', 'fes_yt_define', 'fes_yk_define') if summary else self.catalog.names()
        count_queries = []
        count_params = []
        for name in names:
            scoped = rtu_id is not None and any(
                column['eng_name'] == 'rtu_id' for column in self.schema(name)['columns']
            )
            count_queries.append(
                'SELECT ? AS table_name, COUNT(*) AS row_count FROM '
                + quote_identifier(name)
                + (' WHERE rtu_id=?' if scoped else '')
            )
            count_params.append(name)
            if scoped:
                count_params.append(rtu_id)
        with self.connection() as connection:
            connection.execute('BEGIN')
            if rtu_id is not None and connection.execute(
                    'SELECT 1 FROM fes_rtu_define WHERE rtu_id=?', (rtu_id,)).fetchone() is None:
                raise ApiError(404, '记录不存在', code='not_found')
            count_rows = connection.execute(
                ' UNION ALL '.join(count_queries), count_params,
            ).fetchall()
            channel_rows = () if summary else connection.execute(
                "SELECT COALESCE(NULLIF(channel_type, ''), 'custom'), COUNT(*) "
                "FROM fes_rtu_define GROUP BY 1"
            ).fetchall()
        counts = {row['table_name']: int(row['row_count']) for row in count_rows}
        result = {'counts': counts, 'rtu_id': rtu_id, 'database': str(self.db_path)}
        if not summary:
            result['channel_types'] = dict(channel_rows)
        return result

    def channel_parameter_table(self, channel_type):
        return CHANNEL_PARAMETER_TABLES.get(channel_type)

    @staticmethod
    def _channel_fields() -> set[str]:
        # Packet recording is part of the channel definition.  Keeping it in
        # this allow-list makes the channel editor the single write path for
        # the setting, alongside the channel identity and protocol.
        return {"channel_id", "channel_type", "packet_recording"}

    def create_channel(self, rtu_id, values: dict[str, Any]) -> dict[str, Any]:
        """Create the single channel attached to an existing RTU.

        Channel identity is stored on ``fes_rtu_define``.  The schema currently
        requires those columns for every RTU, so this endpoint is mainly useful
        for rows created by an older/import workflow where the fields are NULL.
        It deliberately refuses to replace an existing channel.
        """
        row = self.get_row("fes_rtu_define", [rtu_id])
        if row.get("channel_id") is not None or row.get("channel_type") is not None:
            raise ApiError(409, "该 RTU 已经配置通道，请使用通道维护", code="channel_exists")
        unknown = sorted(set(values) - self._channel_fields())
        if unknown:
            raise ApiError(400, "通道请求包含未知字段", details={"unknown_fields": unknown})
        if "channel_id" not in values or "channel_type" not in values:
            raise ApiError(400, "新建通道必须提供 channel_id 和 channel_type")
        if values.get("channel_id") is None or values.get("channel_type") is None:
            raise ApiError(400, "channel_id 和 channel_type 不能为空")
        return self.update_row("fes_rtu_define", [rtu_id], values)

    def update_channel(self, rtu_id, values: dict[str, Any], if_match: int | None = None) -> dict[str, Any]:
        """Update channel columns while preserving the one-channel relation."""
        row = self.get_row("fes_rtu_define", [rtu_id])
        unknown = sorted(set(values) - self._channel_fields())
        if unknown:
            raise ApiError(400, "通道请求包含未知字段", details={"unknown_fields": unknown})
        if not values:
            raise ApiError(400, "至少需要修改一个通道字段")
        if "channel_type" in values and values["channel_type"] != row.get("channel_type"):
            old_table = self.channel_parameter_table(row.get("channel_type"))
            if old_table:
                existing = self.list_rows(old_table, where={"rtu_id": rtu_id}, limit=2)["items"]
                if existing:
                    raise ApiError(
                        409,
                        "当前通道已有协议参数，请先删除旧协议参数再切换规约",
                        code="channel_parameter_exists",
                    )
        return self.update_row("fes_rtu_define", [rtu_id], values, if_match)

    def create_channel_parameter(self, rtu_id, values: dict[str, Any]) -> dict[str, Any]:
        """Create a protocol parameter row and allocate id-style keys safely."""
        info = self.channel_for_rtu(rtu_id)
        table = info["parameter_table"]
        if not table:
            raise ApiError(409, "该通道类型没有参数定义表", code="unsupported_channel_type")
        if info["parameter"] is not None:
            raise ApiError(409, "该 RTU 已存在此通道类型的参数定义", code="conflict")
        body = dict(values)
        body["rtu_id"] = rtu_id
        primary_keys = self.catalog.public(table)["primary_keys"]
        if primary_keys == ["id"] and "id" in body:
            raise ApiError(400, "协议参数的 id 由服务端自动分配，不能提交", code="immutable_primary_key")
        return self.create_row(table, body, allocate_key=primary_keys == ["id"])

    def channel_for_rtu(self, rtu_id):
        canonical, columns, _ = self._table_parts('fes_rtu_define')
        with self.connection() as connection:
            row = connection.execute(
                f'SELECT * FROM {quote_identifier(canonical)} WHERE rtu_id=?',
                (rtu_id,),
            ).fetchone()
            if row is None:
                raise ApiError(404, '记录不存在', code='not_found')
            item = self._public_row(row, columns)
            if item.get('channel_id') is None or item.get('channel_type') is None:
                raise ApiError(404, '该 RTU 尚未配置通道', code='not_found')
            table = self.channel_parameter_table(item.get('channel_type'))
            parameter = None
            parameter_schema = None
            if table:
                parameter_schema = self.catalog.public(table)
                parameter_canonical, parameter_columns, primary_keys = self._table_parts(table)
                ordering = ', '.join(
                    f'{quote_identifier(name)} ASC' for name in primary_keys
                )
                parameter_rows = connection.execute(
                    f'SELECT * FROM {quote_identifier(parameter_canonical)} '
                    f'WHERE rtu_id=? ORDER BY {ordering} LIMIT 2',
                    (rtu_id,),
                ).fetchall()
                if len(parameter_rows) > 1:
                    raise ApiError(
                        409,
                        f'{table} 对同一 RTU 存在多个参数记录',
                        code='relationship_conflict',
                    )
                if parameter_rows:
                    parameter = self._public_row(parameter_rows[0], parameter_columns)
        return {
            'item': item,
            'parameter_table': table,
            'parameter': parameter,
            'parameter_schema': parameter_schema,
        }


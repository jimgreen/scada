"""SQLite-backed SCADA schema and row service.

The service deliberately keeps the JSON definitions authoritative.  SQLite is
used for durable rows in ``D:\\codex\\scada\\db\\scada.sqlite3`` by default;
each operation opens its own short-lived connection so multiple web workers
can safely use the same database file.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "dbdef" / "json"
DEFAULT_DB = ROOT / "db" / "scada.sqlite3"
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KIND_TABLES = {
    "yc": "fes_yc_define",
    "yx": "fes_yx_define",
    "yt": "fes_yt_define",
    "yk": "fes_yk_define",
}
CHANNEL_PARAMETER_TABLES = {
    "iec104": "fes_channel_iec104_define",
    "modbus-rtu": "fes_channel_modbus_rtu_define",
    "modbus-tcp": "fes_channel_modbus_tcp_define",
    "can": "fes_channel_can_define",
    "bacnet": "fes_channel_bacnet_define",
    "opc": "fes_channel_opc_define",
}
CHANNEL_TYPES = frozenset(CHANNEL_PARAMETER_TABLES) | {"custom"}
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
        }


class ScadaStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB, schema_dir: Path | str = SCHEMA_DIR):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog = SchemaCatalog(schema_dir)
        self._initialise()

    def open_connection(self) -> sqlite3.Connection:
        """Open a configured SQLite connection owned by the caller.

        Most web operations still use :meth:`connection` for short-lived
        transactions. Long-running communication threads may keep one read
        connection and close it when their thread exits, avoiding repeated
        PRAGMA and connection setup work on every command poll.
        """
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA mmap_size=1073741824")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.open_connection()
        try:
            yield connection
        finally:
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
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS _scada_schema (table_name TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)"
                    )
                    expected_tables = set(self.catalog.names())
                    actual_tables = {
                        row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    unexpected_tables = actual_tables - expected_tables - {'_scada_schema'}
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
                            )
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

    def schema(self, table: str) -> dict[str, Any]:
        return self.catalog.get(table)

    def schemas(self) -> list[dict[str, Any]]:
        return [self.catalog.public(name) for name in self.catalog.names()]

    def _table_parts(self, table: str) -> tuple[str, list[dict[str, Any]], list[str]]:
        payload = self.schema(table)
        columns = payload["columns"]
        return payload["head"]["eng_name"], columns, [c["eng_name"] for c in columns if c["primary_key"]]

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
            if name in {'id', 'pnt_no'} and number < 0:
                raise ApiError(400, f'{name} 不能为负数')
            if name in {'process_enable', 'process_restart'} and number not in {0, 1}:
                raise ApiError(400, f'{name} 必须是 0 或 1')
            if name == 'log_retention_hours' and not 1 <= number <= 87600:
                raise ApiError(400, f'{name} 必须在 1 到 87600 之间')
            if name == 'worker_start_delay_ms' and not 0 <= number <= 60000:
                raise ApiError(400, f'{name} 必须在 0 到 60000 之间')
            if name in {'scan_interval_ms', 'poll_interval_ms', 'retry_interval_ms'} and not 50 <= number <= 60000:
                raise ApiError(400, f'{name} 必须在 50 到 60000 之间')
            if name in {'chan_port', 'bind_port'} and not 0 <= number <= 65535:
                raise ApiError(400, f'{name} 必须在 0 到 65535 之间')
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
            return number
        if kind == "bool":
            if not isinstance(value, bool):
                raise ApiError(400, f"字段 {name} 必须是布尔值")
            return int(value)
        if not isinstance(value, str):
            raise ApiError(400, f"字段 {name} 必须是字符串")
        if name == 'channel_type':
            if value not in CHANNEL_TYPES:
                raise ApiError(400, f"字段 {name} 必须是受支持的通道类型")
        allowed_values = column.get('allowed_values')
        if allowed_values is not None and value not in allowed_values:
            raise ApiError(400, f"字段 {name} 的值不在允许范围内")
        return value

    def _normalise(self, table: str, values: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
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
            elif not partial and (column["primary_key"] or name == 'rtu_id' or column.get('required', False)):
                raise ApiError(400, f"缺少必填字段: {name}")
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

    def list_rows(self, table: str, *, where: dict[str, Any] | None = None, search: str = "", limit: int = 50,
                  offset: int = 0, sort: str | None = None, order: str = "asc") -> dict[str, Any]:
        canonical, columns, primary_keys = self._table_parts(table)
        if limit < 1 or limit > 500 or offset < 0:
            raise ApiError(400, "limit 必须在 1 到 500 之间，offset 不能为负数")
        by_name = {c["eng_name"]: c for c in columns}
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
            text_columns = [c["eng_name"] for c in columns if c["data_type"] == "str" or c['primary_key']]
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
            total = connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(canonical)}{predicate}", params).fetchone()[0]
            ordering = ', '.join(f'{quote_identifier(name)} {order.upper()}' for name in [sort] + [k for k in primary_keys if k != sort])
            rows = connection.execute(
                f"SELECT * FROM {quote_identifier(canonical)}{predicate} ORDER BY {ordering} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return {"items": [self._public_row(row, columns) for row in rows], "total": int(total), "limit": limit, "offset": offset}

    def get_row(self, table: str, key: str | list[Any] | tuple[Any, ...]) -> dict[str, Any]:
        canonical, columns, primary_keys = self._table_parts(table)
        keys = self._key_values(table, key)
        condition = " AND ".join(f"{quote_identifier(name)}=?" for name in primary_keys)
        with self.connection() as connection:
            row = connection.execute(f"SELECT * FROM {quote_identifier(canonical)} WHERE {condition}", list(keys.values())).fetchone()
        if row is None:
            raise ApiError(404, "记录不存在", code="not_found")
        return self._public_row(row, columns)

    def create_row(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        canonical, columns, primary_keys = self._table_parts(table)
        normalised = self._normalise(table, values)
        names = [c["eng_name"] for c in columns]
        insert_names = [name for name in names if name in normalised]
        placeholders = ", ".join("?" for _ in insert_names)
        sql = f"INSERT INTO {quote_identifier(canonical)} ({', '.join(quote_identifier(n) for n in insert_names)}) VALUES ({placeholders})"
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(sql, [normalised[n] for n in insert_names])
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

    def update_row(self, table: str, key: str | list[Any] | tuple[Any, ...], values: dict[str, Any], if_match: int | None = None) -> dict[str, Any]:
        canonical, columns, primary_keys = self._table_parts(table)
        keys = self._key_values(table, key)
        normalised = self._normalise(table, values, partial=True)
        if set(normalised).intersection(primary_keys):
            raise ApiError(400, "主键字段不能修改")
        if not normalised:
            raise ApiError(400, "至少需要修改一个字段")
        assignments = ", ".join(f"{quote_identifier(name)}=?" for name in normalised)
        predicate = " AND ".join(f"{quote_identifier(name)}=?" for name in primary_keys)
        params = [normalised[n] for n in normalised]
        params.extend(keys.values())
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._check_version(self._read(connection, table, key), if_match)
                connection.execute(
                    f"UPDATE {quote_identifier(canonical)} SET {assignments}, _version=_version+1 WHERE {predicate}", params
                )
                row = self._read(connection, table, key)
                connection.commit()
                return row
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, '引用的 RTU 不存在或数据违反约束', code='conflict') from exc

    def delete_row(self, table: str, key: str | list[Any] | tuple[Any, ...], if_match: int | None = None) -> None:
        canonical, columns, primary_keys = self._table_parts(table)
        keys = self._key_values(table, key)
        predicate = " AND ".join(f"{quote_identifier(name)}=?" for name in primary_keys)
        params = list(keys.values())
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._check_version(self._read(connection, table, key), if_match)
                if canonical == 'fes_rtu_define':
                    references = {}
                    for name in self.catalog.names():
                        if name != canonical and any(c['eng_name'] == 'rtu_id' for c in self.schema(name)['columns']):
                            count = connection.execute(f'SELECT COUNT(*) FROM {quote_identifier(name)} WHERE rtu_id=?', [keys['rtu_id']]).fetchone()[0]
                            if count:
                                references[name] = count
                    if references:
                        raise ApiError(409, '该 RTU 仍有关联点表或通道参数记录，请先删除关联记录', details=references, code='referenced')
                connection.execute(f"DELETE FROM {quote_identifier(canonical)} WHERE {predicate}", params)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "记录仍被其他表引用，不能删除", details=str(exc), code="conflict") from exc

    def overview(self, rtu_id=None):
        if rtu_id is not None:
            self.get_row('fes_rtu_define', [rtu_id])
        counts = {}
        with self.connection() as connection:
            connection.execute('BEGIN')
            for name in self.catalog.names():
                scoped = rtu_id is not None and any(c['eng_name'] == 'rtu_id' for c in self.schema(name)['columns'])
                counts[name] = connection.execute(f'SELECT COUNT(*) FROM {quote_identifier(name)}' + (' WHERE rtu_id=?' if scoped else ''), [rtu_id] if scoped else []).fetchone()[0]
        channel_types = {}
        for row in self.list_rows('fes_rtu_define', limit=500)['items']:
            channel_type = row.get('channel_type') or 'custom'
            channel_types[channel_type] = channel_types.get(channel_type, 0) + 1
        return {
            'counts': counts, 'rtu_id': rtu_id, 'database': str(self.db_path),
            'channel_types': channel_types,
        }

    def channel_parameter_table(self, channel_type):
        return CHANNEL_PARAMETER_TABLES.get(channel_type)

    def channel_for_rtu(self, rtu_id):
        row = self.get_row('fes_rtu_define', [rtu_id])
        if row.get('channel_id') is None or row.get('channel_type') is None:
            raise ApiError(404, '该 RTU 尚未配置通道', code='not_found')
        table = self.channel_parameter_table(row.get('channel_type'))
        parameter = None
        if table:
            parameter_rows = self.list_rows(table, where={'rtu_id': rtu_id}, limit=2)['items']
            if len(parameter_rows) > 1:
                raise ApiError(409, f'{table} 对同一 RTU 存在多个参数记录', code='relationship_conflict')
            parameter = parameter_rows[0] if parameter_rows else None
        return {
            'item': row,
            'parameter_table': table,
            'parameter': parameter,
            'parameter_schema': self.catalog.public(table) if table else None,
        }


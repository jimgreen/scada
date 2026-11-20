"""Publish SCADA's actual SQLite schema in the RTDB embedded format."""

import hashlib
import json
import math
from pathlib import Path
import re


RTDB_METADATA_TABLES = frozenset({
    'rtdb_schema_database', 'rtdb_schema_tables', 'rtdb_schema_columns',
})
IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def _literal(sql, kind):
    """Describe literal defaults, leaving SQL expressions in the physical DDL."""
    if sql is None:
        return None
    sql = sql.strip()
    while sql.startswith('(') and sql.endswith(')'):
        sql = sql[1:-1].strip()
    if re.fullmatch(r"'(?:[^']|'')*'", sql):
        value = sql[1:-1].replace("''", "'")
    elif re.fullmatch(r'[+-]?\d+', sql):
        value = int(sql)
        if not -(2**63) <= value < 2**63:
            return None
    elif re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', sql):
        value = float(sql)
        if not math.isfinite(value):
            return None
    else:
        return None
    if kind == 'bool' and type(value) is int and value in (0, 1):
        return bool(value)
    if kind == 'str':
        return str(value)
    if kind == 'int' and type(value) is int:
        return value
    if kind == 'float' and isinstance(value, (int, float)):
        return value
    return None


def _data_type(declared):
    declared = declared.upper()
    if 'INT' in declared:
        return 'int', 'INT'
    if any(token in declared for token in ('CHAR', 'CLOB', 'TEXT')):
        return 'str', 'STRING'
    if any(token in declared for token in ('REAL', 'FLOA', 'DOUB', 'NUMERIC', 'DECIMAL')):
        return 'float', 'FLOAT'
    if declared == 'BOOLEAN':
        return 'bool', '单选菜单'
    return 'blob', 'STRING'


def _indexes(connection, table):
    indexes = set()
    for index, origin, partial in connection.execute(
        'SELECT name, origin, partial FROM pragma_index_list(?)', (table,)
    ):
        if origin == 'pk' or partial:
            continue
        fields = tuple(row[0] for row in connection.execute(
            'SELECT name FROM pragma_index_xinfo(?) WHERE "key"=1 ORDER BY seqno', (index,)
        ))
        if 1 <= len(fields) <= 8 and all(fields) and len(set(fields)) == len(fields):
            indexes.add(fields)
    return [list(fields) for fields in sorted(indexes)[:64]]


def sync_embedded_schema(connection, catalog=None, *, allow_keyless=False, include_tables=None):
    """Update metadata inside the caller's DDL transaction, without copying rows.

    Schema comes from SQLite. A SCADA catalog, when supplied, contributes only
    labels and display hints. A store can limit its public catalog to entity tables.
    """
    if not connection.in_transaction:
        raise RuntimeError('RTDB schema sync requires an active SQLite transaction')
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
    )}
    existing = tables & RTDB_METADATA_TABLES
    if existing:
        if (
            existing != RTDB_METADATA_TABLES
            or not connection.execute(
                "SELECT 1 FROM pragma_table_info('rtdb_schema_database') WHERE name='storage_format'"
            ).fetchone()
            or not connection.execute(
                "SELECT 1 FROM rtdb_schema_database WHERE version=1 AND storage_format='native'"
            ).fetchone()
        ):
            raise ValueError('数据库包含非 SCADA 原生格式的 RTDB 内置结构，不能覆盖')
    connection.execute('''CREATE TABLE IF NOT EXISTS rtdb_schema_database (
        version INTEGER PRIMARY KEY CHECK(version=1), eng_name TEXT NOT NULL,
        chn_name TEXT NOT NULL, storage_format TEXT NOT NULL CHECK(storage_format='native'))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS rtdb_schema_tables (
        table_name TEXT PRIMARY KEY, chn_name TEXT NOT NULL, ordinal INTEGER NOT NULL,
        fingerprint TEXT NOT NULL, indexes_json TEXT)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS rtdb_schema_columns (
        table_name TEXT NOT NULL REFERENCES rtdb_schema_tables(table_name) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL, eng_name TEXT NOT NULL, chn_name TEXT NOT NULL,
        data_type TEXT NOT NULL, primary_key INTEGER NOT NULL CHECK(primary_key IN (0,1)),
        disp_type TEXT NOT NULL, disp_menu TEXT NOT NULL, has_default INTEGER NOT NULL
        CHECK(has_default IN (0,1)), default_json TEXT,
        PRIMARY KEY(table_name, ordinal), UNIQUE(table_name, eng_name))''')
    filename = connection.execute("SELECT file FROM pragma_database_list WHERE name='main'").fetchone()[0]
    database_label = Path(filename).stem or 'scada'
    database_name = re.sub(r'[^A-Za-z0-9_]', '_', database_label)
    if not IDENTIFIER.fullmatch(database_name):
        database_name = 'scada_' + database_name
    connection.execute(
        '''INSERT INTO rtdb_schema_database VALUES(1,?,?,'native')
           ON CONFLICT(version) DO UPDATE SET eng_name=excluded.eng_name, chn_name=excluded.chn_name
           WHERE eng_name IS NOT excluded.eng_name OR chn_name IS NOT excluded.chn_name''',
        (database_name, database_label),
    )
    old_tables = {row[0]: tuple(row[1:]) for row in connection.execute(
        'SELECT table_name,chn_name,ordinal,fingerprint,indexes_json FROM rtdb_schema_tables'
    )}
    old_columns = {}
    old_values = {}
    for row in connection.execute('''SELECT table_name, ordinal, eng_name, chn_name, data_type,
            primary_key, disp_type, disp_menu, has_default, default_json
            FROM rtdb_schema_columns ORDER BY table_name, ordinal'''):
        old_values.setdefault(row[0], []).append(tuple(row))
        old_columns[row[0], row[2]] = (row[3], row[4], row[6], row[7])
    catalog_names = set(catalog.names()) if catalog is not None else set()
    public_tables = tables - RTDB_METADATA_TABLES
    if include_tables is not None:
        included = set(include_tables)
        if included - public_tables:
            raise ValueError(f'RTDB 内置结构引用了不存在的设备表：{sorted(included - public_tables)}')
        public_tables = included
    physical = {}
    for name in public_tables:
        columns = connection.execute(
            'SELECT name,type,dflt_value,pk FROM pragma_table_xinfo(?) WHERE hidden<>1 ORDER BY cid', (name,)
        ).fetchall()
        if not any(row[3] for row in columns):
            if allow_keyless:
                continue
            raise ValueError(f'RTDB 内置结构要求设备或业务表具有主键：{name}')
        physical[name] = columns
    for name in old_tables.keys() - physical.keys():
        connection.execute('DELETE FROM rtdb_schema_columns WHERE table_name=?', (name,))
        connection.execute('DELETE FROM rtdb_schema_tables WHERE table_name=?', (name,))
    for ordinal, name in enumerate(sorted(physical)):
        actual = physical[name]
        if not IDENTIFIER.fullmatch(name) or any(not IDENTIFIER.fullmatch(row[0]) for row in actual):
            raise ValueError(f'RTDB 不支持表或字段名称：{name}')
        payload = catalog.get(name) if name in catalog_names else {}
        columns_by_name = {col['eng_name']: col for col in payload.get('columns', ())}
        label = str(payload.get('head', {}).get('chn_name') or old_tables.get(name, (name,))[0]).strip() or name
        columns = []
        values = []
        for position, (field, declared, sql_default, pk) in enumerate(actual):
            kind, display = _data_type(declared)
            prior_label, prior_kind, prior_display, prior_menu = old_columns.get(
                (name, field), (field, kind, display, '')
            )
            hint = columns_by_name.get(field, {})
            hinted_kind = hint.get('data_type', prior_kind)
            if hinted_kind == kind or (kind == 'int' and hinted_kind == 'bool'):
                kind = hinted_kind
                display = hint.get('disp_type') or prior_display
                menu = str(hint.get('disp_menu', prior_menu) or '').strip()
            else:
                menu = ''
            column_label = str(hint.get('chn_name') or prior_label).strip() or field
            display = str(display).strip() or 'STRING'
            default = _literal(sql_default, kind)
            column = dict(eng_name=field, chn_name=column_label, primary_key=bool(pk),
                          data_type=kind, disp_type=display, disp_menu=menu)
            if default is not None:
                column['default_value'] = default
            columns.append(column)
            values.append((name, position, field, column_label, kind, int(bool(pk)), display, menu,
                           int(default is not None), json.dumps(default, ensure_ascii=False) if default is not None else None))
        indexes = _indexes(connection, name)
        definition = dict(head=dict(eng_name=name, chn_name=label), columns=columns, indexes=indexes)
        fingerprint = hashlib.sha256(json.dumps(definition, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        indexes_json = json.dumps(indexes)
        same_columns = values == old_values.get(name)
        if old_tables.get(name) == (label, ordinal, fingerprint, indexes_json) and same_columns:
            continue
        connection.execute('''INSERT INTO rtdb_schema_tables VALUES(?,?,?,?,?)
            ON CONFLICT(table_name) DO UPDATE SET chn_name=excluded.chn_name, ordinal=excluded.ordinal,
            fingerprint=excluded.fingerprint, indexes_json=excluded.indexes_json''',
            (name, label, ordinal, fingerprint, indexes_json))
        if not same_columns:
            connection.execute('DELETE FROM rtdb_schema_columns WHERE table_name=?', (name,))
            connection.executemany('INSERT INTO rtdb_schema_columns VALUES(?,?,?,?,?,?,?,?,?,?)', values)

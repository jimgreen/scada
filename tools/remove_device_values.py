"""One-time, backed-up removal of redundant YC/YX device value columns.

Stop scdfe_c and the Web server before running. No legacy access is provided.
Use --refresh-test-values only to reset identified protocol fixture values;
station/point definitions and command timestamps are preserved.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scdfe_control import InstanceLock
from web.seed_protocol_test_data import _base_rtu_code, _point_values, expanded_protocols
from web.service import SCHEMA_DIR, ScadaStore, SchemaCatalog, quote_identifier


REMOVED = tuple(base + suffix for suffix in ("", "_1", "_2", "_3")
                for base in ("value_dev", "status_dev", "refresh_time_dev"))
TABLES = ("fes_yc_define", "fes_yx_define")


def retained_digest(connection, table, columns):
    digest = hashlib.sha256()
    count = 0
    projection = ','.join(map(quote_identifier, columns))
    for row in connection.execute(f'SELECT {projection} FROM {quote_identifier(table)} ORDER BY rtu_id,pnt_no'):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode('utf-8') + b'\n')
        count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def refresh_fixtures(connection):
    """Only explicit test codes; never alter pending or issued YT commands."""
    specs = {_base_rtu_code(spec): spec for spec in expanded_protocols(100)}
    stations = [(r[0], specs[r[1]]) for r in connection.execute('SELECT rtu_id,code FROM fes_rtu_define')
                if r[1] in specs]
    changed = {}
    now = int(time.time() * 1000)
    for kind in ('yc', 'yx', 'yt'):
        table = f'fes_{kind}_define'
        fields = ('value', 'status', 'refresh_time') if kind == 'yx' else (
            'raw_base', 'raw_scale', 'raw_value', 'raw_status', 'raw_time',
            'value', 'status', 'refresh_time')
        params = []
        for rtu_id, spec in stations:
            for row in connection.execute(f'SELECT * FROM {table} WHERE rtu_id=?', (rtu_id,)):
                number = row['pnt_no']
                values = _point_values(spec, rtu_id, kind, number, now)
                codes = {values['code']}
                if spec['key'] == 'iec104' and spec['station_index'] == 1:
                    codes.add(f'TEST_IEC104_{kind.upper()}_{number:03d}')
                if row['code'] not in codes:
                    continue
                if kind == 'yt' and (row['cmd_time'] or row['cmd_handle_time']):
                    continue
                params.append((*(values[f] for f in fields), rtu_id, number))
        connection.executemany(
            f'UPDATE {table} SET ' + ','.join(f'{f}=?' for f in fields) +
            ',_version=_version+1 WHERE rtu_id=? AND pnt_no=?', params)
        changed[table] = len(params)
    return changed


def migrate(db_path, *, refresh_test_values=False, schema_dir=SCHEMA_DIR):
    db = Path(db_path).resolve(strict=True)
    catalog = SchemaCatalog(schema_dir)
    lock = InstanceLock(db, 'main')
    if not lock.acquire():
        raise RuntimeError('请先停止 scdfe_c 再迁移数据库')
    report = {'database': str(db), 'backup': None, 'removed': {}, 'retained': {}, 'test_rows_updated': {}}
    try:
        with closing(sqlite3.connect(db, isolation_level=None, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
            try:
                if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('迁移前数据库完整性检查失败')
                projections = {}
                for table in TABLES:
                    expected = ['_version', *(c['eng_name'] for c in catalog.public(table)['columns'])]
                    actual = [r[1] for r in connection.execute(f'PRAGMA table_info({table})')]
                    removed = [name for name in actual if name in REMOVED]
                    if [name for name in actual if name not in REMOVED] != expected:
                        raise RuntimeError(f'{table} 有超出本次迁移范围的 schema 差异')
                    projections[table] = expected
                    report['removed'][table] = removed
                    report['retained'][table] = retained_digest(connection, table, expected)
                if any(report['removed'].values()) or refresh_test_values:
                    backup = db.parent / 'backups' / (
                        db.name + '.before_remove_device_values.' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.bak')
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    # A separate reader backs up the committed snapshot while this
                    # connection holds the reserved write lock (no backup deadlock).
                    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(backup)) as target:
                        source.backup(target)
                        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise RuntimeError('备份完整性检查失败')
                    report['backup'] = str(backup)
                for table in TABLES:
                    for name in report['removed'][table]:
                        connection.execute(f'ALTER TABLE {table} DROP COLUMN {quote_identifier(name)}')
                    if retained_digest(connection, table, projections[table]) != report['retained'][table]:
                        raise RuntimeError(f'{table} 保留字段校验失败')
                    if report['removed'][table]:
                        fingerprint = hashlib.sha256(json.dumps(catalog.get(table), ensure_ascii=False,
                                                               sort_keys=True).encode('utf-8')).hexdigest()
                        connection.execute('UPDATE _scada_schema SET fingerprint=? WHERE table_name=?',
                                           (fingerprint, table))
                if refresh_test_values:
                    report['test_rows_updated'] = refresh_fixtures(connection)
                report['integrity_check'] = connection.execute('PRAGMA integrity_check').fetchone()[0]
                report['foreign_key_violations'] = len(connection.execute('PRAGMA foreign_key_check').fetchall())
                if report['integrity_check'] != 'ok' or report['foreign_key_violations']:
                    raise RuntimeError('迁移后完整性或外键检查失败')
                # Validate all physical tables before committing, without opening
                # a second writing connection through ScadaStore.__init__.
                validator = ScadaStore.__new__(ScadaStore)
                for table in catalog.names():
                    validator._validate_existing_table(connection, table, catalog.get(table))
                    expected = hashlib.sha256(json.dumps(catalog.get(table), ensure_ascii=False,
                                                        sort_keys=True).encode('utf-8')).hexdigest()
                    row = connection.execute('SELECT fingerprint FROM _scada_schema WHERE table_name=?', (table,)).fetchone()
                    if row is None or row[0] != expected:
                        raise RuntimeError(f'{table} schema 指纹不匹配')
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        ScadaStore(db, schema_dir)
        return report
    finally:
        lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--refresh-test-values', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    output = json.dumps(migrate(args.db, refresh_test_values=args.refresh_test_values), ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + '\n', encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()

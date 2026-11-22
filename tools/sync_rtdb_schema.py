"""Backfill embedded RTDB definitions in existing SCADA SQLite databases."""

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

from core.rtdb_schema import sync_embedded_schema
from scdfe_c.web.service import SCHEMA_DIR, SchemaCatalog
from scdfe_d.service import APPLICATION_ID as DEVICE_APP_ID, discover_device_tables


def sync_file(path, catalog):
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=30)) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            business = {'_scada_schema', 'fes_rtu_define'} <= tables
            dev_tables = [t for t, _ in discover_device_tables(connection)]
            app_id = connection.execute('PRAGMA application_id').fetchone()[0]
            devices = bool(dev_tables) or app_id == DEVICE_APP_ID
            connections = {'connection_session', 'connection_event', 'connection_writer'} <= tables
            if not (business or devices or connections):
                raise ValueError(f'不是 SCADA 业务库、设备库或连接记录库：{path}')
            public_tables = set(dev_tables) if (devices and not business) else None
            sync_embedded_schema(connection, catalog if business else None, include_tables=public_tables)
            return connection.execute('SELECT count(*) FROM rtdb_schema_tables').fetchone()[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', action='append', required=True, type=Path)
    parser.add_argument('--schema-dir', type=Path, default=SCHEMA_DIR)
    args = parser.parse_args(argv)
    catalog = SchemaCatalog(args.schema_dir)
    for path in args.db:
        try:
            count = sync_file(path, catalog)
        except (OSError, ValueError, sqlite3.Error) as exc:
            parser.exit(1, f'{exc}\n')
        print(f'{path.resolve()} · {count} tables')


if __name__ == '__main__':
    main()

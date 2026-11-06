"""Plan or explicitly apply the RTU token, point type and point-key migration."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from scdfe_c.web.service import (DEFAULT_DB, KIND_TABLES, RTU_VERSION_DDL,
                                 ScadaStore, SchemaCatalog, quote_identifier as q)


MAPPING = 'fes_scdfe_t_point_define'
OLD_MAPPING_HASH = 'bedbb10461dfd92657d49489601ce8ba14ab0c9a48aa2f93492826492d58d609'
NEW = '_scada_rtu_version'
OLD = ('_scada_definition_version', '_scada_command_version')
VERSION_COLS = ('definition_version', 'command_version')


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def owned_triggers(catalog):
    return {f'_scada_defver_{t}_{op}' for t in catalog.names()
            if any(c['eng_name']=='rtu_id' for c in catalog.get(t)['columns'])
            for op in ('ins','del','upd')} | {
                f'_scada_cmdver_{t}_{op}' for t in ('fes_yk_define','fes_yt_define') for op in ('ins','del','upd')}


def plan(con, catalog):
    tables = {r[1]:r[4] for r in con.execute('PRAGMA main.table_list') if r[2]=='table' and not r[1].startswith('sqlite_')}
    legacy = set(OLD).issubset(tables) and NEW not in tables
    if not legacy and not (NEW in tables and not set(OLD).intersection(tables)):
        raise ValueError('内部版本表缺失或新旧表并存，不执行迁移')
    if legacy:
        for table in OLD:
            fields = [(r[1],r[5]) for r in con.execute(f'PRAGMA table_xinfo({q(table)})')]
            if fields != [('rtu_id',1),('version',0)]:
                raise ValueError(f'{table} 含未识别的字段或主键，不自动合并')
    expected = set(catalog.names()) | {'_scada_schema'} | (set(OLD) if legacy else {NEW})
    if set(tables) != expected:
        raise ValueError(f'数据库与当前业务表目录不匹配：{sorted(set(tables)^expected)}')
    existing = dict(con.execute('SELECT table_name,fingerprint FROM _scada_schema'))
    hashes = {t:fingerprint(catalog.get(t)) for t in catalog.names()}
    has_type = 'source_kind' in {r[1] for r in con.execute(f'PRAGMA table_info({MAPPING})')}
    if has_type:
        hashes[MAPPING] = OLD_MAPPING_HASH
    if existing != hashes:
        raise ValueError('存在未识别的表定义指纹；请先迁移相应旧版本')
    if has_type and con.execute(f'SELECT 1 FROM {MAPPING} WHERE kind IS NOT source_kind LIMIT 1').fetchone():
        raise ValueError('转发点 kind 与 source_kind 不同，拒绝丢弃数据')
    rebuild = [t for t in KIND_TABLES.values() if not tables[t]]
    for t in tables:
        if any(f[2] in rebuild for f in con.execute(f'PRAGMA foreign_key_list({q(t)})')):
            raise ValueError(f'{t} 有外键引用待重建的点表，需要专门迁移')
    if legacy or has_type:
        retired = list(OLD) if legacy else []
        if has_type:
            retired.append('source_kind')
        known = owned_triggers(catalog) | {MAPPING+'_idx_1_source_rtu_id_source_kind_source_pnt_no'}
        for name, sql in con.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('view','index','trigger') AND sql IS NOT NULL"):
            if name not in known and any(re.search(r'\b'+x+r'\b',sql,re.I) for x in retired):
                raise ValueError(f'{name} 依赖即将移除的字段或表，需要专门迁移')
    return {'merge_versions':legacy, 'remove_source_kind':has_type, 'cluster_points':rebuild}


def rebuild_point(con, table):
    old_sql = con.execute('SELECT sql FROM sqlite_schema WHERE type=\'table\' AND name=?',(table,)).fetchone()[0]
    # Preserve the actual column types, defaults, constraints and local indexes.
    objects = list(con.execute("SELECT sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
                               (table,)))
    temp = '_scada_rebuild_'+table
    con.execute(f'CREATE TABLE {q(temp)} '+old_sql[old_sql.index('('):]+' WITHOUT ROWID')
    con.execute(f'INSERT INTO {q(temp)} SELECT * FROM {q(table)} ORDER BY rtu_id,pnt_no')
    con.execute(f'DROP TABLE {q(table)}')
    con.execute(f'ALTER TABLE {q(temp)} RENAME TO {q(table)}')
    for (sql,) in objects:
        con.execute(sql)


def apply(db=DEFAULT_DB, *, write=False, compact=False):
    db=Path(db).resolve(strict=True)
    catalog=SchemaCatalog()
    with closing(sqlite3.connect(db.as_uri()+('?mode=rw' if write else '?mode=ro'),
                                 uri=True, timeout=15, isolation_level=None)) as con:
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        actions=plan(con,catalog)
        result={'db':str(db),**actions,'changed':False,'backup':None}
        if not write or not any(actions.values()):
            return result
        # A second connection can back up the committed database while this
        # transaction excludes competing writers. Never overwrite a backup.
        directory=db.parent/'backups'
        directory.mkdir(exist_ok=True)
        dest=directory/(db.name+'.before_simplify.'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.bak')
        with dest.open('xb'):
            pass
        with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(dest)) as backup:
            src.backup(backup)
            if backup.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('备份校验失败')
        if actions['merge_versions']:
            for trigger in owned_triggers(catalog):
                con.execute(f'DROP TRIGGER IF EXISTS {q(trigger)}')
            con.execute(RTU_VERSION_DDL)
            for t,column in zip(OLD,VERSION_COLS):
                con.execute(f'INSERT INTO {NEW}(rtu_id,{column}) SELECT rtu_id,version FROM {t} WHERE 1 '
                            f'ON CONFLICT(rtu_id) DO UPDATE SET {column}=excluded.{column}')
                con.execute(f'DROP TABLE {t}')
            con.execute(f'INSERT OR IGNORE INTO {NEW}(rtu_id) SELECT rtu_id FROM fes_rtu_define')
        if actions['remove_source_kind']:
            con.execute(f'DROP INDEX IF EXISTS {MAPPING}_idx_1_source_rtu_id_source_kind_source_pnt_no')
            con.execute(f'ALTER TABLE {MAPPING} DROP COLUMN source_kind')
            ScadaStore._ensure_indexes(con,MAPPING,catalog.get(MAPPING))
            con.execute('UPDATE _scada_schema SET fingerprint=? WHERE table_name=?',(fingerprint(catalog.get(MAPPING)),MAPPING))
        for table in actions['cluster_points']:
            rebuild_point(con,table)
        ScadaStore._ensure_definition_version_triggers(con,catalog)
        ScadaStore._ensure_command_version_triggers(con)
        if any(plan(con,catalog).values()) or con.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('迁移后的结构或外键校验失败')
        if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('迁移后的数据库完整性校验失败')
        con.commit()
        result.update(changed=True,backup=str(dest))
        if compact:
            # VACUUM must run outside the migration transaction. A failure
            # here does not undo the already committed, verified migration.
            try:
                con.execute('VACUUM')
            except sqlite3.Error as exc:
                raise RuntimeError(f'结构迁移已提交；文件压缩未完成，可单独执行 VACUUM。备份：{dest}') from exc
            result['compacted']=True
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=DEFAULT_DB)
    parser.add_argument('--apply',action='store_true',help='默认只读；指定此选项才备份并迁移')
    parser.add_argument('--compact',action='store_true',help='迁移提交后执行 VACUUM 归还空闲文件页')
    args=parser.parse_args()
    if args.compact and not args.apply:
        parser.error('--compact 需要 --apply')
    print(json.dumps(apply(args.db,write=args.apply,compact=args.compact),ensure_ascii=False,indent=2))

"""Read-only validation of a SCADA database before selection or Web startup."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from core.rtdb_schema import RTDB_METADATA_TABLES


MAX_VALIDATION_ERRORS = 50


def validate_database(path, role, catalog):
    path = Path(path).resolve()
    errors, counts = [], {}
    user_version = 0

    def add_error(msg: str) -> None:
        if len(errors) < MAX_VALIDATION_ERRORS and msg not in errors:
            errors.append(msg)

    try:
        if not path.is_file():
            raise ValueError('文件不存在，请选择已有数据库')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
            con.execute('BEGIN')
            if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('SQLite 文件损坏或完整性检查失败')
            user_version = int(con.execute('PRAGMA user_version').fetchone()[0] or 0)
            expected_role = int.from_bytes(b'SDFS', 'big') if role == 's' else 0
            if con.execute('PRAGMA application_id').fetchone()[0] != expected_role:
                raise ValueError('库类型错误：S需要独立模拟器库，C/T需要业务库')
            from core.iec104_soe import TABLES as SOE_TABLES
            from core.monitor_changes import TABLES as MONITOR_TABLES
            from core.command_trace import TABLES as COMMAND_TABLES
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected = set(catalog.names())
            allowed = (expected | {'_scada_schema', '_scada_rtu_version'} | SOE_TABLES
                       | MONITOR_TABLES | COMMAND_TABLES | RTDB_METADATA_TABLES)
            if tables - allowed:
                add_error('包含非SCADA表：' + ', '.join(sorted(tables - allowed)[:5]))
            if expected - tables or '_scada_schema' not in tables:
                raise ValueError('缺少SCADA必需表：' + ', '.join(sorted(expected - tables)[:5]))
            schemas = dict(con.execute('SELECT table_name,fingerprint FROM _scada_schema'))
            if set(schemas) != expected:
                add_error('SCADA结构字典不完整或包含多余表')
            type_map = {'int': 'INTEGER', 'bool': 'INTEGER', 'float': 'REAL', 'str': 'TEXT'}
            for name in sorted(expected):
                schema = catalog.get(name)
                fingerprint = hashlib.sha256(json.dumps(schema, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                if schemas.get(name) != fingerprint:
                    add_error(f'{name} 结构版本不匹配，请先迁移数据库')
                actual = {r[1]: r for r in con.execute(f'PRAGMA table_info("{name}")')}
                fields = {c['eng_name'] for c in schema['columns']} | {'_version'}
                if set(actual) != fields:
                    add_error(f'{name} 实际字段与定义不一致（缺少或多余字段）')
                    continue
                for column in schema['columns']:
                    if actual[column['eng_name']][2].upper() != type_map[column['data_type']]:
                        add_error(f'{name}.{column["eng_name"]} 字段类型错误')
                keys = [r[1] for r in sorted(actual.values(), key=lambda r: r[5]) if r[5]]
                if keys != [c['eng_name'] for c in schema['columns'] if c['primary_key']]:
                    add_error(f'{name} 主键定义错误')
            if con.execute('PRAGMA foreign_key_check').fetchone():
                add_error('点表包含无效外键或孤立设备关联')
            for kind in ('yc', 'yx', 'yk', 'yt'):
                counts[kind] = con.execute(f'SELECT count(*) FROM fes_{kind}_define').fetchone()[0]
                if con.execute(f'SELECT 1 FROM fes_{kind}_define p LEFT JOIN fes_rtu_define r '
                               'ON p.rtu_id=r.rtu_id WHERE r.rtu_id IS NULL LIMIT 1').fetchone():
                    add_error(f'{kind} 点表引用了不存在的RTU')
            counts['rtu'] = con.execute('SELECT count(*) FROM fes_rtu_define').fetchone()[0]
            # 显式 IEC104 点映射必须引用有效四遥点并使用合法 ASDU（已统一禁用 CP24Time2a：4/6/10/12/14）。
            valid_types = {'yc': {1, 2, 3, 9, 11, 13, 21, 34, 35, 36},
                           'yx': {1, 2, 3, 5, 30, 31, 32}, 'yk': {1, 2, 45, 46, 58, 59},
                           'yt': {1, 2, 3, 48, 49, 50, 61, 62, 63}}
            seen = set()
            present_kinds = set()
            for channel, kind, no, address, typ in con.execute(
                'SELECT channel_id,kind,pnt_no,io_address,type_id FROM fes_channel_iec104_point_define'):
                if kind not in valid_types or (typ is not None and typ not in valid_types[kind]):
                    add_error(f'IEC104通道{channel} 点{kind}/{no} 类型标识无效')
                else:
                    present_kinds.add(kind)
                if address is not None:
                    if not isinstance(address, int) or not 1 <= address <= 0xffffff:
                        add_error(f'IEC104通道{channel} 信息体地址越界')
                    elif (channel, address) in seen:
                        add_error(f'IEC104通道{channel} 信息体地址{address}重复')
                    else:
                        seen.add((channel, address))
            for kind in sorted(present_kinds):
                orphan = con.execute(
                    f'SELECT m.channel_id, m.pnt_no FROM fes_channel_iec104_point_define m '
                    f'WHERE m.kind=? AND NOT EXISTS ('
                    f'  SELECT 1 FROM fes_{kind}_define p '
                    f'  JOIN fes_rtu_define r ON r.rtu_id=p.rtu_id '
                    f"  WHERE r.channel_type='iec104' AND r.channel_id=m.channel_id AND p.pnt_no=m.pnt_no"
                    f') LIMIT 1',
                    (kind,),
                ).fetchone()
                if orphan is not None:
                    add_error(f'IEC104通道{orphan[0]} 映射{kind}/{orphan[1]}没有对应四遥点')
            if con.execute("SELECT 1 FROM fes_rtu_define r LEFT JOIN fes_channel_iec104_define c ON r.channel_id=c.channel_id "
                           "WHERE r.channel_type='iec104' AND c.channel_id IS NULL LIMIT 1").fetchone():
                add_error('IEC104 RTU缺少通道参数')
            if con.execute('SELECT 1 FROM fes_channel_iec104_define WHERE rtu_addr IS NOT NULL AND '
                           '(rtu_addr<1 OR rtu_addr>65534) OR remote_port IS NOT NULL AND '
                           '(remote_port<1 OR remote_port>65535) LIMIT 1').fetchone():
                add_error('IEC104公共地址或TCP端口越界')
    except (ValueError, sqlite3.Error, OSError) as exc:
        add_error(str(exc))
    return dict(valid=not errors, errors=errors, counts=counts, user_version=user_version,
                message='数据库校验通过' if not errors else '数据库不合法：' + errors[0])

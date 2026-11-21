"""Read the current values of the points selected in a workbench table."""

from collections import defaultdict
from math import isfinite
from time import time_ns

from scdfe_c.web.service import ApiError, quote_identifier

MAX_POINTS = 24
POINT_TABLES = {kind: f'fes_{kind}_define' for kind in ('yc', 'yx', 'yk', 'yt')}


def _identifier(value, field, minimum=1):
    if isinstance(value, str) and 1 <= len(value) <= 19 and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ApiError(400, f'{field} 必须是有效的整数编号')
    return value


def _samples_for_points(con, role, points):
    owner = 'channel_id' if role == 't' else 'rtu_id'
    expected = {'kind', owner, 'pnt_no'}
    requests = defaultdict(list)
    for slot, point in enumerate(points):
        if not isinstance(point, dict) or point.keys() != expected:
            raise ApiError(400, f'每个点需要 kind、{owner} 和 pnt_no')
        kind = point['kind']
        if not isinstance(kind, str) or kind not in POINT_TABLES:
            raise ApiError(400, 'kind 必须是 yc、yx、yk 或 yt')
        requests[kind].append((slot, _identifier(point[owner], owner),
                               _identifier(point['pnt_no'], 'pnt_no', minimum=0)))
    for kind, keys in requests.items():
        table = 'fes_scdfe_t_point_define' if role == 't' else POINT_TABLES[kind]
        condition = 'AND p.kind=?' if role == 't' else ''
        values = ', '.join(['(?, ?, ?)'] * len(keys))
        args = [value for key in keys for value in key]
        if role == 't':
            args.append(kind)
        yield from con.execute(
            f'''WITH requested(slot, owner, point) AS (VALUES {values})
                SELECT r.slot, p.value, p.status, p.refresh_time
                FROM requested r LEFT JOIN {table} p
                ON p.{owner}=r.owner AND p.pnt_no=r.point {condition}''', args)


def _samples_for_devices(store, con, points):
    ids = defaultdict(list)
    for slot, point in enumerate(points):
        if not isinstance(point, dict) or point.keys() != {'measurement_id'}:
            raise ApiError(400, '每个量测需要 measurement_id')
        ids[_identifier(point['measurement_id'], 'measurement_id')].append(slot)
    placeholders = ', '.join(['?'] * len(ids))
    devices = defaultdict(list)
    for mid, metric, table, idx in con.execute(
        f'''SELECT m.measurement_id, m.metric, m.quantity_table, b.idx
            FROM device_measurement m JOIN d_device_members b
            ON b.device_id=m.device_id AND b.table_name=m.quantity_table
            WHERE m.catalog_active=1 AND m.measurement_id IN ({placeholders})''', tuple(ids)):
        devices[table, idx].append((mid, metric))
    for (table, idx), measurements in devices.items():
        cols = ', '.join(quote_identifier(metric + suffix)
                         for _, metric in measurements for suffix in ('', '_qual', '_time'))
        row = con.execute(f'SELECT {cols} FROM {store._table_name(table)} WHERE idx=?', (idx,)).fetchone()
        if row is not None:
            for offset, (mid, _) in enumerate(measurements):
                for slot in ids[mid]:
                    yield (slot, *row[3 * offset:3 * offset + 3])


def read_point_trend(store, role, body):
    if body.keys() != {'points'} or not isinstance(body['points'], list):
        raise ApiError(400, '需要 points 测点数组')
    points = body['points']
    if len(points) > MAX_POINTS:
        raise ApiError(400, f'最多同时查询 {MAX_POINTS} 个测点')
    values = [dict(value=None, quality=None, time_ms=None) for _ in points]
    if points:
        with store.connection() as con:
            con.execute('BEGIN')
            rows = (_samples_for_devices(store, con, points) if role == 'd'
                    else _samples_for_points(con, role, points))
            for slot, value, quality, stamp in rows:
                values[slot] = dict(
                    value=value if isinstance(value, (int, float)) and isfinite(value) else None,
                    quality=quality if isinstance(quality, int) else None,
                    time_ms=stamp if isinstance(stamp, int) and stamp > 0 else None,
                )
    return dict(time_ms=time_ns() // 1_000_000, values=values)

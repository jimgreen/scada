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
    by_table = defaultdict(lambda: defaultdict(list))
    for slot, point in enumerate(points):
        if not isinstance(point, dict):
            raise ApiError(400, '每个量测需要 measurement_id 或 table/idx/metric')
        if point.keys() == {'measurement_id'}:
            mid = _identifier(point['measurement_id'], 'measurement_id')
            resolved = store.resolve_measurement_id(con, mid)
            if resolved is None:
                continue
            table, idx, metric = resolved
        elif point.keys() == {'table', 'idx', 'metric'}:
            table = str(point['table'])
            idx = _identifier(point['idx'], 'idx', minimum=0)
            metric = str(point['metric'])
            metrics = store.table_metrics(con, table)
            if metrics is None or all(m != metric for m, _ in metrics):
                continue
        else:
            raise ApiError(400, '每个量测需要 measurement_id 或 table/idx/metric')
        by_table[table][idx].append((slot, metric))
    for table, idx_map in by_table.items():
        # 按表合并为单条 WHERE idx IN (...) 查询，直接从设备宽表读取值、质量和时标。
        metrics = list(dict.fromkeys(metric for rows in idx_map.values() for _, metric in rows))
        metric_pos = {metric: pos for pos, metric in enumerate(metrics)}
        cols = ', '.join(quote_identifier(metric + suffix)
                         for metric in metrics for suffix in ('', '_qual', '_time'))
        idx_list = list(idx_map.keys())
        idx_placeholders = ', '.join(['?'] * len(idx_list))
        for row in con.execute(
            f'SELECT idx, {cols} FROM {store._table_name(table)} WHERE idx IN ({idx_placeholders})',
            tuple(idx_list),
        ):
            row_idx = row[0]
            for slot, metric in idx_map.get(row_idx, ()):
                base = 1 + 3 * metric_pos[metric]
                yield (slot, row[base], row[base + 1], row[base + 2])


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

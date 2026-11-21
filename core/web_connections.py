"""Shared S/T connection pages and read-only history API."""
from contextlib import closing
import sqlite3

from core.connection_history import history_path, read_history
from core.web_workbench import ASSET_DIR, render_workbench

PROTOCOL_NAMES = {'iec104':'IEC104', 'modbus-tcp':'Modbus TCP', 'modbus-rtu':'Modbus RTU',
                  'can':'CAN', 'opc':'OPC UA', 'bacnet':'BACnet/IP'}


def connection_targets(store, component, ids=None):
    """Read only names and bindings; one batch query, never one query per row."""
    key = 'rtu_id' if component == 's' else 'channel_id'
    table = 'fes_rtu_define' if component == 's' else 'fes_scdfe_t_define'
    columns = 'rtu_id,channel_id,name,channel_type AS protocol' if component == 's' else 'channel_id,name,protocol'
    if ids is not None and not ids:
        return {}
    where = '' if ids is None else f' WHERE {key} IN ({",".join("?" for _ in ids)})'
    with store.connection() as con:
        rows = [dict(row) for row in con.execute(f'SELECT {columns} FROM {table}{where} ORDER BY name,{key}',
                                               () if ids is None else tuple(ids))]
    return {row[key]:row for row in rows}


def target_name(row, component, *, missing=False):
    name = str(row.get('name') or '').strip()
    if name:
        return name
    noun = 'RTU' if component == 's' else '转发通道'
    ident = row.get('rtu_id') if component == 's' else row.get('channel_id')
    return f'{"已删除的" if missing else "未命名"}{noun}（{ident}）' if ident is not None else f'未关联{noun}'


def connection_options(store, component):
    key = 'rtu_id' if component == 's' else 'channel_id'
    targets = connection_targets(store,component)
    current_ids = set(targets)
    path = history_path(store.db_path,component)
    if path.exists():
        # Keep deleted definitions selectable while their connection history exists.
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as con:
            con.row_factory = sqlite3.Row
            for row in con.execute(f'SELECT {key}, MIN(channel_id) AS linked_channel, MIN(protocol) AS protocol '
                                   f'FROM connection_session WHERE {key} IS NOT NULL GROUP BY {key}'):
                targets.setdefault(row[key],{key:row[key],'channel_id':row['linked_channel'],'protocol':row['protocol']})
    items = []
    for ident,row in targets.items():
        name = target_name(row,component,missing=ident not in current_ids)
        protocol = PROTOCOL_NAMES.get(str(row.get('protocol') or '').lower(),row.get('protocol') or '未配置规约')
        items.append(dict(value=f'{key}:{ident}',name=name,label=f'{name} · {protocol}',
                          channel_id=row.get('channel_id'),rtu_id=row.get('rtu_id'),query={key:ident}))
    counts = {}
    for item in items:
        counts[item['label']] = counts.get(item['label'],0)+1
    for item in items:
        if counts[item['label']] > 1:
            item['label'] += f'（{"RTU" if component=="s" else "通道"} {item["query"][key]}）'
    items.sort(key=lambda item:(item['name'],item['value']))
    return {'items':items,'component':component}


def attach_connection_names(result, store, component, detail=False):
    rows = [result['item']] if detail else result['items']
    key = 'rtu_id' if component == 's' else 'channel_id'
    targets = connection_targets(store,component,{row[key] for row in rows if row.get(key) is not None})
    for row in rows:
        target = targets.get(row.get(key))
        row['display_name'] = target_name(target or row,component,missing=target is None)


def handle_connections(handler, parts, query, method, store, component):
    from scdfe_c.web.service import ApiError
    page = parts[:1] == ['connections'] and len(parts) <= 2
    api = parts[:2] == ['api','connections']
    if not page and not api:
        return False
    if method != 'GET':
        raise ApiError(405,'连接历史只支持 GET 查询')
    if page:
        from core.web_http import send_static
        return send_static(
            handler,
            ASSET_DIR / 'connections.html',
            'text/html; charset=utf-8',
            transform=lambda raw: render_workbench(raw, component, 'connections.html'),
            cache_key=f'workbench:{component}:connections.html',
        )
    if parts == ['api','connections','options']:
        if query:
            raise ApiError(400,'名称选项接口不接受查询参数')
        return handler._json(200,connection_options(store,component))
    ident = parts[2] if len(parts)>=3 else None
    if len(parts)>4 or (len(parts)==4 and parts[3]!='events'):
        raise ApiError(404,'资源不存在')
    if ident and (len(ident)!=32 or any(ch not in '0123456789abcdef' for ch in ident)):
        raise ApiError(400,'连接编号无效')
    allowed = {'limit','offset','channel_id','rtu_id','state'} if not ident else (
        {'limit','before','after','kind'} if len(parts)==4 else set())
    if set(query)-allowed:
        raise ApiError(400,'未知连接查询参数')
    result = read_history(store.db_path,component,ident,events=len(parts)==4,query=query)
    if result is None:
        raise ApiError(404,'连接历史不存在或已超出保留期限')
    if len(parts) != 4:
        attach_connection_names(result,store,component,detail=ident is not None)
    result['component'] = component
    return handler._json(200,result)

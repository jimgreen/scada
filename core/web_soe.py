"""Shared, read-only SOE query endpoint for C/S/T."""
from pathlib import Path


def handle_soe(handler, parts, query, method, store, component):
    from scdfe_c.web.service import ApiError
    if parts not in (['soe'],['api','soe'],['static','soe.css'],['static','soe.js']): return False
    if method != 'GET': raise ApiError(405,'SOE 历史只支持 GET 查询')
    if parts[0]=='static':
        kind='text/css' if parts[1].endswith('.css') else 'application/javascript'
        return handler._send(200,kind+'; charset=utf-8',
                             Path(__file__).with_name('web_static').joinpath(parts[1]).read_bytes())
    if parts == ['soe']:
        if query: raise ApiError(400,'页面不接受查询参数')
        return handler._send(200,'text/html; charset=utf-8',
                             Path(__file__).with_name('web_static').joinpath('soe.html').read_bytes())
    if set(query)-{'rtu_id','after_id','limit','tail'}:
        raise ApiError(400,'未知 SOE 查询参数')
    values={}
    for key,value in query.items():
        try:
            values[key]=int(value)
        except (TypeError,ValueError): raise ApiError(400,f'{key} 必须是单个整数')
    limit=values.get('limit',100)
    after=values.get('after_id',0)
    rtu=values.get('rtu_id')
    tail=values.get('tail',0)
    if not 1<=limit<=1000 or not 0<=after<=2**63-1 or tail not in (0,1) or (rtu is not None and not 1<=rtu<=2**63-1):
        raise ApiError(400,'limit 必须为 1..1000，after_id 非负，rtu_id 为正，tail 为 0/1')
    if tail and after: raise ApiError(400,'tail 与 after_id 不能同时使用')
    origin='S' if component=='s' else 'C'
    where,args='origin=? AND event_id>?',[origin,after]
    if rtu is not None: where+=' AND rtu_id=?';args.append(rtu)
    with store.connection() as con:
        rows=[dict(r) for r in con.execute(f'SELECT * FROM _scada_iec104_soe WHERE {where} '
              f'ORDER BY event_id {"DESC" if tail else "ASC"} LIMIT ?',(*args,limit+1))]
        more=len(rows)>limit;rows=rows[:limit]
        if tail: rows.reverse()
        cursors=[dict(r) for r in con.execute('SELECT * FROM _scada_iec104_soe_cursor ORDER BY consumer')]
    return handler._json(200,dict(items=rows,next_after_id=rows[-1]['event_id'] if rows else after,
        has_more=more and not tail,limit=limit,component=component,cursors=cursors,
        time_convention='CP56 calendar displayed as UTC; raw bytes retained',
        delivery='at_least_once_apci_ack'))

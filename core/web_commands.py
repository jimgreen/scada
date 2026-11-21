"""Shared command history, feedback configuration and audited review APIs."""
import hashlib
import json
from pathlib import Path
from core.command_feedback import LABELS


def integer(query,key,*,default=None,allow_zero=False):
    value=query.get(key,default)
    if value is None:return None
    try:number=int(value)
    except (ValueError,TypeError):raise ValueError(f'{key} 必须为整数')
    if not (0 if allow_zero else 1)<=number<=2**63-1:raise ValueError(f'{key} 超出范围')
    return number


def handle_commands(handler,parts,query,method,store,component):
    from scdfe_c.web.service import ApiError
    is_review=len(parts)==4 and parts[:2]==['api','command-traces'] and parts[3]=='review'
    is_resolve=len(parts)==4 and parts[:2]==['api','command-traces'] and parts[3]=='resolve'
    is_detail=len(parts)==3 and parts[:2]==['api','command-traces']
    allowed=(['command-traces'],['api','command-traces'],['static','command-traces.js'],
             ['api','command-feedback'],['api','command-options'])
    if parts not in allowed and not (is_review or is_resolve or is_detail):return False
    try:
        if is_resolve:
            if component=='s':raise ApiError(409,'S 只保存接收记录；请在 C/T 处理派发结案')
            if method!='POST':raise ApiError(405,'人工结案只支持 POST')
            if query:raise ValueError('结案操作不接受查询参数')
            from core.command_trace import resolve_uncertain
            return handler._json(200,dict(item=resolve_uncertain(store,integer({'id':parts[2]},'id'),handler._body())))
        if is_detail:
            if method!='GET':raise ApiError(405,'指令详情只支持 GET')
            if query:raise ValueError('详情不接受查询参数')
            with store.connection() as con:
                row=con.execute(history_sql(False)+' WHERE h.command_id=?',(integer({'id':parts[2]},'id'),)).fetchone()
            if row is None:raise ApiError(404,'指令记录不存在')
            item=decorate(dict(row))
            from core.command_trace import resolution_blocker
            item['resolution_blocker']='S 只保存接收记录' if component=='s' else resolution_blocker(item)
            return handler._json(200,dict(item=item,component=component))
        if is_review:
            if method!='POST':raise ApiError(405,'核对记录只支持 POST')
            if query:raise ValueError('核对操作不接受查询参数')
            from core.command_feedback import save_review
            return handler._json(200,dict(item=save_review(store,integer({'id':parts[2]},'id'),handler._body())))
        if parts==['api','command-feedback']:
            if component=='s':raise ApiError(409,'S 保存独立接收记录；请在 C/T 配置回采规则')
            if method=='PUT':
                if query:raise ValueError('保存规则不接受查询参数')
                from core.command_feedback import save_rule
                return handler._json(200,dict(item=save_rule(store,handler._body())))
            if method!='GET':raise ApiError(405,'回采规则只支持 GET/PUT')
            if set(query)!={'rtu_id','kind','pnt_no'} or query['kind'] not in ('yk','yt'):raise ValueError('请指定命令点')
            with store.connection() as con:
                row=con.execute('SELECT * FROM _scada_command_feedback_rule WHERE rtu_id=? AND kind=? AND pnt_no=?',
                                (integer(query,'rtu_id'),query['kind'],integer(query,'pnt_no'))).fetchone()
            return handler._json(200,dict(item=dict(row) if row else None))
        if method!='GET':raise ApiError(405,'指令追踪只支持查询')
        if parts==['api','command-options']:
            if set(query)-{'rtu_id','kind','q'}:raise ValueError('未知查询字段')
            with store.connection() as con:
                if not query:
                    items=[dict(row) for row in con.execute('SELECT rtu_id,name FROM fes_rtu_define ORDER BY rtu_id')]
                else:
                    kind=query.get('kind')
                    if kind not in ('yk','yt','yc','yx') or 'rtu_id' not in query:raise ValueError('请选择 RTU 和点类型')
                    keyword=query.get('q','').strip()
                    if len(keyword)>120:raise ValueError('检索文本过长')
                    items=[dict(row) for row in con.execute(f'''SELECT pnt_no,name FROM fes_{kind}_define WHERE rtu_id=?
                        AND (instr(COALESCE(name,''),?)>0 OR CAST(pnt_no AS TEXT)=?) ORDER BY pnt_no LIMIT 1001''',
                        (integer(query,'rtu_id'),keyword,keyword))]
            return handler._json(200,dict(items=items[:1000],has_more=len(items)>1000,component=component))
        assets=Path(__file__).with_name('web_static')
        from core.web_http import send_static
        if parts[0]=='static':
            return send_static(handler,assets/'command-traces.js','application/javascript; charset=utf-8')
        if parts==['command-traces']:
            if component in ('s','c','t'):
                from core.web_workbench import render_workbench
                return send_static(
                    handler,
                    assets/'command-traces.html',
                    'text/html; charset=utf-8',
                    transform=lambda raw: render_workbench(raw,component,'command-traces.html'),
                    cache_key=f'workbench:{component}:command-traces.html',
                )
            return send_static(handler,assets/'command-traces.html','text/html; charset=utf-8')
        return history(handler,query,store,component)
    except ValueError as exc:
        raise ApiError(400,str(exc)) from exc


def history_sql(summary):
    columns=('h.command_id,h.rtu_id,h.kind,h.pnt_no,h.cmd_time,h.value,h.stage,h.confirmation_scope,'
             'h.finish_time,h.feedback_state,h.review_time,h.resolution_time,length(h.error)>0 AS has_error' if summary else 'h.*')
    sql=f'''SELECT {columns},r.name AS rtu_name,r.channel_id,COALESCE(k.name,t.name) AS point_name'''
    if not summary:sql+=',f.name AS feedback_rtu_name,COALESCE(y.name,c.name) AS feedback_point_name'
    sql+=''' FROM _scada_command_trace h
        LEFT JOIN fes_rtu_define r ON r.rtu_id=h.rtu_id
        LEFT JOIN fes_yk_define k ON h.kind='yk' AND k.rtu_id=h.rtu_id AND k.pnt_no=h.pnt_no
        LEFT JOIN fes_yt_define t ON h.kind='yt' AND t.rtu_id=h.rtu_id AND t.pnt_no=h.pnt_no'''
    if not summary:sql+=''' LEFT JOIN fes_rtu_define f ON f.rtu_id=h.feedback_rtu_id
        LEFT JOIN fes_yx_define y ON h.feedback_kind='yx' AND y.rtu_id=h.feedback_rtu_id AND y.pnt_no=h.feedback_pnt_no
        LEFT JOIN fes_yc_define c ON h.feedback_kind='yc' AND c.rtu_id=h.feedback_rtu_id AND c.pnt_no=h.feedback_pnt_no'''
    return sql


def decorate(row):
    row['device_execution_confirmed']=None
    row['feedback_matches_expected']=row['feedback_state']=='matched'
    row['feedback_label']=LABELS.get(row['feedback_state'],row['feedback_state'])
    return row


def history(handler,query,store,component):
    from scdfe_c.web.server import browser_safe
    if set(query)-{'rtu_id','kind','pnt_no','before','limit','stage','from','to','q','view'}:raise ValueError('未知查询字段')
    if query.get('view','full') not in ('full','summary'):raise ValueError('view 只支持 full/summary')
    summary=query.get('view')=='summary'
    where,args=[],[]
    for key,column in [('rtu_id','h.rtu_id=?'),('pnt_no','h.pnt_no=?'),('before','h.command_id<?'),
                       ('from','h.cmd_time>=?'),('to','h.cmd_time<=?')]:
        if key in query:where.append(column);args.append(integer(query,key))
    if 'from' in query and 'to' in query and int(query['from'])>int(query['to']):raise ValueError('起始时刻晚于结束时刻')
    if 'kind' in query:
        if query['kind'] not in ('yk','yt'):raise ValueError('kind 只支持 yk/yt')
        where.append('h.kind=?');args.append(query['kind'])
    if 'stage' in query:
        if query['stage'] not in {'queued','dispatching','select_sent','select_confirmed','execute_sent','protocol_confirmed','driver_completed','failed','received'}:
            raise ValueError('未知指令阶段')
        where.append('h.stage=?');args.append(query['stage'])
    keyword=query.get('q','').strip()
    if len(keyword)>120:raise ValueError('检索文本过长')
    if keyword:
        where.append("(instr(COALESCE(r.name,''),?)>0 OR instr(COALESCE(k.name,t.name,''),?)>0)");args.extend((keyword,keyword))
    limit=integer(query,'limit',default=50)
    if limit>200:raise ValueError('每页最多 200 条')
    with store.connection() as con:
        con.execute('BEGIN')
        revision=con.execute('SELECT revision FROM _scada_command_revision WHERE id=1').fetchone()[0]
        tag='"'+hashlib.sha256(json.dumps([component,revision,query],sort_keys=True).encode()).hexdigest()+'"'
        if handler.headers.get('If-None-Match')==tag:
            con.commit()
            return handler._send(304,'application/json; charset=utf-8',b'',{'ETag':tag})
        rows=[dict(row) for row in con.execute(history_sql(summary)+
            (' WHERE '+' AND '.join(where) if where else '')+' ORDER BY h.command_id DESC LIMIT ?',(*args,limit+1))]
        con.commit()
    more=len(rows)>limit;rows=rows[:limit]
    for row in rows:decorate(row)
    payload=dict(items=rows,has_more=more,next_before=rows[-1]['command_id'] if rows else None,component=component)
    return handler._send(200,'application/json; charset=utf-8',json.dumps(browser_safe(payload),ensure_ascii=False,allow_nan=False).encode(),{'ETag':tag})

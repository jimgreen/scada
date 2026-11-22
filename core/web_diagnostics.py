"""Shared read-only diagnostics: persisted stage timings and config preflight."""
from pathlib import Path
import time


def handle_diagnostics(handler, parts, query, method, store, component):
    from scdfe_c.web.service import ApiError
    allowed = (['diagnostics'],['api','diagnostics'],['static','diagnostics.css'],['static','diagnostics.js'])
    if parts not in allowed:
        return False
    if method != 'GET':
        raise ApiError(405,'运行诊断只支持 GET 查询')
    assets=Path(__file__).with_name('web_static')
    from core.web_http import send_static
    if parts[0]=='static':
        mime='text/css' if parts[1].endswith('.css') else 'application/javascript'
        return send_static(handler,assets/parts[1],mime+'; charset=utf-8')
    if parts==['diagnostics']:
        if component in ('s','c','t','d'):
            from core.web_workbench import render_workbench
            return send_static(
                handler,
                assets/'diagnostics.html',
                'text/html; charset=utf-8',
                transform=lambda raw: render_workbench(raw,component,'diagnostics.html'),
                cache_key=f'workbench:{component}:diagnostics.html',
            )
        return send_static(handler,assets/'diagnostics.html','text/html; charset=utf-8')
    if set(query)-{'checks'} or query.get('checks','0') not in ('0','1'):
        raise ApiError(400,'checks 只能为 0 或 1')
    now=int(time.time()*1000)
    if component=='d':
        collector = getattr(handler.server, 'collector', None)
        col_status = collector.status() if collector is not None else {}
        history = store.history().get('refreshes', []) if hasattr(store, 'history') else []
        durations = [float(r['duration_ms']) for r in history if r.get('duration_ms') is not None]
        rows = []
        if durations or col_status:
            sorted_d = sorted(durations)
            def _pct(p):
                if not sorted_d:
                    return 0.0
                idx = min(len(sorted_d) - 1, max(0, int(round((len(sorted_d) - 1) * p))))
                return round(sorted_d[idx], 3)
            latency = {}
            if sorted_d:
                latency['d_collect_ms'] = dict(
                    count=len(sorted_d),
                    window_count=len(sorted_d),
                    last_ms=round(durations[0], 3),
                    p50_ms=_pct(0.5),
                    p95_ms=_pct(0.95),
                    p99_ms=_pct(0.99),
                )
            rows.append(dict(
                service='scdfe_d',
                channel_id=0,
                name='设备量测采集器',
                running=int(bool(col_status.get('running'))),
                sampled_at=now,
                stale=False,
                latency=latency,
                source_delta={},
                soe_progress=None,
                database_wait=None,
                slow_batches=None,
                command_write=None,
                modbus_requests=None,
                point_runtime=None,
                pending_messages=0,
            ))
        payload = dict(
            component='d',
            items=rows,
            window=100,
            note='统计来自 scdfe_d 最近 100 次采集刷新批次。',
        )
        if query.get('checks') == '1':
            checks = []
            source_db = store.default_source_database() if hasattr(store, 'default_source_database') else ''
            if not source_db:
                checks.append(dict(severity='warning', name='源 SCADA 数据库', channels=[], message='未配置或未发现默认源 SCADA 数据库'))
            else:
                try:
                    resolved = store.source_path(source_db)
                    if not resolved.is_file():
                        checks.append(dict(severity='error', name='源 SCADA 数据库', channels=[], message=f'源库文件不存在：{source_db}'))
                except Exception as exc:
                    checks.append(dict(severity='error', name='源 SCADA 数据库', channels=[], message=str(exc)))
            if col_status.get('last_error'):
                checks.append(dict(severity='error', name='设备量测采集器', channels=[], message=str(col_status['last_error'])))
            payload['checks'] = checks
            payload['checked_at'] = now
        return handler._json(200, payload)
    services=None
    if component=='s':
        services=handler._channel_service_map()
        running={key:service for key,service in services.items() if service.running}
        handler._monitor_statuses(running,{})
    from core.runtime_status import runtime_status_rows
    prefix='scdfe_'+component
    now=int(time.time()*1000)
    rows=[]
    # 按 service_name 走主键前缀索引查询，避免全表扫描无关组件的运行态 JSON。
    status_rows = runtime_status_rows(store, service_name=prefix) + runtime_status_rows(store, service_name=prefix + '_writer')
    for row in status_rows:
        timing=row['diagnostic'].get('latency',{})
        command_write=row['diagnostic'].get('command_write') or {}
        timing={**timing,**{'command_'+key:value for key,value in command_write.get('latency',{}).items()}}
        requests=row['diagnostic'].get('modbus_requests') or (row['diagnostic'].get('engine') or {}).get('modbus_requests')
        point_runtime=row['diagnostic'].get('point_runtime')
        if timing or requests or point_runtime:
            rows.append(dict(service=row['service_name'],channel_id=row['channel_id'],
                name=row['channel_name'],running=row['running'],sampled_at=row['sampled_at'],
                stale=now-row['sampled_at']>20000,latency=timing,
                source_delta=row['diagnostic'].get('source_delta',{}),
                soe_progress=row['diagnostic'].get('soe_progress'),
                database_wait=row['diagnostic'].get('database_wait'),
                slow_batches=row['diagnostic'].get('slow_batches'),
                command_write=row['diagnostic'].get('command_write'),
                modbus_requests=requests,
                point_runtime=point_runtime,
                pending_messages=row['diagnostic'].get('pending_messages')))
    payload=dict(component=component,items=rows,window=256,
        note='分位数来自最近 256 个阶段样本，不可相加作为端到端时延；完整链路需用同一变化的关联验收记录。')
    if query.get('checks')=='1':
        from core.startup_checks import configuration_checks
        payload['checks']=configuration_checks(store,component,services)
        payload['checked_at']=now
    return handler._json(200,payload)

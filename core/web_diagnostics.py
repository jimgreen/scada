"""Shared read-only diagnostics for scdfe_d."""
from pathlib import Path
import time

from core.common import ApiError


def handle_diagnostics(handler, parts, query, method, store, component):
    allowed = (['diagnostics'], ['api', 'diagnostics'], ['static', 'diagnostics.css'], ['static', 'diagnostics.js'])
    if parts not in allowed:
        return False
    if method != 'GET':
        raise ApiError(405, '运行诊断只支持 GET 查询')
    assets = Path(__file__).with_name('web_static')
    from core.web_http import send_static
    if parts[0] == 'static':
        mime = 'text/css' if parts[1].endswith('.css') else 'application/javascript'
        return send_static(handler, assets / parts[1], mime + '; charset=utf-8')
    if parts == ['diagnostics']:
        if component == 'd':
            from core.web_workbench import render_workbench
            return send_static(
                handler,
                assets / 'diagnostics.html',
                'text/html; charset=utf-8',
                transform=lambda raw: render_workbench(raw, component, 'diagnostics.html'),
                cache_key=f'workbench:{component}:diagnostics.html',
            )
        return send_static(handler, assets / 'diagnostics.html', 'text/html; charset=utf-8')
    if set(query) - {'checks'} or query.get('checks', '0') not in ('0', '1'):
        raise ApiError(400, 'checks 只能为 0 或 1')
    now = int(time.time() * 1000)
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
            checks.append(dict(severity='warning', name='源 FES 数据库', channels=[], message='未配置或未发现默认源 FES 数据库'))
        else:
            try:
                resolved = store.source_path(source_db)
                if not resolved.is_file():
                    checks.append(dict(severity='error', name='源 FES 数据库', channels=[], message=f'源库文件不存在：{source_db}'))
            except Exception as exc:
                checks.append(dict(severity='error', name='源 FES 数据库', channels=[], message=str(exc)))
        if col_status.get('last_error'):
            checks.append(dict(severity='error', name='设备量测采集器', channels=[], message=str(col_status['last_error'])))
        payload['checks'] = checks
        payload['checked_at'] = now
    return handler._json(200, payload)

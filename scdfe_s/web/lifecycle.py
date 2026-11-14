"""Request-local lifecycle causes; no query/body credentials enter the logs."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import sys
from urllib.parse import urlsplit
from uuid import uuid4
from core import operation_audit as audit


_cause = ContextVar('scdfe_s_lifecycle_cause', default='')


@contextmanager
def lifecycle_cause(reason):
    parent = _cause.get()
    token = _cause.set(reason + ('；触发来源=' + parent if parent else ''))
    try:
        yield
    finally:
        _cause.reset(token)


def stop_cause():
    cause = _cause.get() or '程序直接调用 stop，未提供上游来源'
    return cause + (('；操作ID=' + audit.trace_id()) if audit.trace_id() and '操作ID=' not in cause else '')


def traced_lifecycle(method):
    """Also attribute non-HTTP start/stop/reconfigure calls to their call site."""
    @wraps(method)
    def wrapped(service, *args, **kwargs):
        caller = sys._getframe(1)
        location = f'{Path(caller.f_code.co_filename).name}:{caller.f_lineno}:{caller.f_code.co_name}'
        del caller
        reason = _cause.get() or ('程序调用 ' + location)
        context = nullcontext() if audit.active() else audit.scope(
            's', service.store.db_path.parent / 'logs', source='程序内部', reason=reason, caller=location)
        with context, (nullcontext() if _cause.get() else lifecycle_cause(reason)):
            details = {'action': method.__name__, 'service': type(service).__name__,
                       'config': dict(service._config), 'caller': location}
            audit.emit('服务动作开始', **details, arguments=args, options=kwargs)
            try:
                result = method(service, *args, **kwargs)
            except Exception as exc:
                audit.emit('服务动作失败', **details, error_type=type(exc).__name__, error=str(exc))
                raise
            audit.emit('服务动作完成', **details,
                       channel_id=service._file_log.channel_id, running=service.running)
            return result
    return wrapped


def _path(value):
    try:
        return ' '.join(urlsplit(value).path.split())[:240] or '未提供'
    except ValueError:
        return '无效路径'


def controlled_call(handler, service, action, operation):
    """Record control requests and their results, correlated with stop logs.

    ContextVar isolates concurrent HTTP handlers; internal reload/close calls
    can supply their own cause without changing protocol method signatures.
    """
    request_id = audit.trace_id() or uuid4().hex[:12]
    detail = (f'操作={action} 操作ID={request_id} 来源IP={handler.client_address[0]} '
              f'来源页面={_path(handler.headers.get("Referer", ""))} '
              f'请求={handler.command} {_path(handler.path)}')
    service._record('INFO', '收到服务控制请求 ' + detail)
    with lifecycle_cause('HTTP 服务控制请求 ' + detail):
        try:
            result = operation()
        except Exception as exc:
            service._record('ERROR', f'服务控制失败 {detail} 异常={type(exc).__name__}: {exc}')
            raise
    service._record('INFO', '服务控制完成 ' + detail)
    return result

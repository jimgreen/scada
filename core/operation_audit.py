"""Bounded operation receipts shared by C/S/T; never used for polling data.

The HTTP trace is generated here, not trusted from the client. A receipt is
written only after its database transaction commits. Contexts can be copied
into control-job threads without sharing mutable request completion state.
"""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit, unquote
from uuid import uuid4


_current = ContextVar('scada_operation_audit', default=None)
_lock = threading.Lock()
_secret_key = re.compile(r'password|passwd|pwd|secret|token|authorization|cookie|credential|private.?key|api.?key|access.?key', re.I)
_credential = re.compile(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+@')
_assignment = re.compile(r'(?i)((?:password|passwd|pwd|secret|token|authorization|cookie)[\w-]*\s*[:=]\s*)[^\s,;]+')
MAX_SAMPLES = 20
LOG = logging.getLogger(__name__)


def active():
    return _current.get() is not None


def trace_id():
    state = _current.get()
    return state['trace_id'] if state else ''


def path_only(value):
    try:
        return ' '.join(urlsplit(str(value)).path.split())[:240]
    except ValueError:
        return '<invalid-path>'


def _text(value):
    text = _assignment.sub(r'\1[REDACTED]', _credential.sub(r'\1[REDACTED]@', str(value)))
    state = _current.get()
    for secret in (tuple(state.get('secrets', ())) if state else ()):
        text = text.replace(secret, '[REDACTED]')
    return text[:1000] + ('…[truncated]' if len(text) > 1000 else '')


def safe(value, depth=0):
    if depth > 6:
        return '<depth-limit>'
    if isinstance(value, dict):
        items = list(value.items())
        result = {str(k): '[REDACTED]' if _secret_key.search(str(k)) else safe(v, depth + 1)
                  for k, v in items[:80]}
        if len(items) > 80:
            result['_omitted_fields'] = len(items) - 80
        return result
    if isinstance(value, (tuple, list)):
        sample = [safe(item, depth + 1) for item in value[:MAX_SAMPLES]]
        return sample if len(value) <= MAX_SAMPLES else {'count': len(value), 'sample': sample, 'truncated': True}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _text(value)


def _remember_secrets(value, depth=0):
    state = _current.get()
    if state is None or depth > 6:
        return
    if isinstance(value, dict):
        for key, item in list(value.items())[:80]:
            if _secret_key.search(str(key)) and isinstance(item, str) and item:
                state['secrets'].add(item)
            else:
                _remember_secrets(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value[:MAX_SAMPLES]:
            _remember_secrets(item, depth + 1)


def emit(phase, **details):
    state = _current.get()
    if state is None:
        return
    try:
        _remember_secrets(details)
        now = datetime.now().astimezone()
        record = {'time': now.isoformat(timespec='milliseconds'),
                  'trace_id': state['trace_id'], 'component': state['component'],
                  'phase': phase, 'pid': os.getpid(), 'thread_id': threading.get_ident(),
                  **state['origin'], **safe(details)}
        data = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len(data.encode('utf-8')) > 60000:
            record = {**{k: record[k] for k in ('time', 'trace_id', 'component', 'phase', 'pid', 'thread_id')},
                      'truncated': True, 'detail_preview': data[:10000]}
            data = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
        root = state['directory']
        directory = root / now.strftime('%Y/%m/%d/%H')
        with _lock:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f'scdfe_{state["component"]}-operations-{os.getpid()}-{now:%Y%m%d-%H}.log'
            with target.open('a', encoding='utf-8', newline='\n') as stream:
                stream.write(f'{now:%H:%M:%S} [日志] 操作审计 {data}\n')
    except Exception as exc:
        # Never report a committed write as failed and provoke a duplicate retry.
        # The fallback is explicit and contains neither request data nor paths.
        LOG.error('操作审计写入失败 trace_id=%s phase=%s error_type=%s',
                  state['trace_id'], phase, type(exc).__name__)


@contextmanager
def scope(component, directory, **origin):
    token = _current.set({'trace_id': uuid4().hex, 'component': component,
                          'directory': Path(directory).resolve(),
                          'origin': safe(origin), 'secrets': set()})
    try:
        yield trace_id()
    finally:
        _current.reset(token)


def request_body(value):
    emit('请求参数已解析', parameters=value)


def response(handler, status, payload):
    if active():
        handler._audit_status = int(status)
        # Capture before writing the socket: a disconnected client does not
        # undo the action. Keep business result separate from HTTP delivery.
        emit('业务返回', http_status=status, result=payload)


def exception():
    """Attach the caught failure to the trace, without source code or locals."""
    if not active():
        return
    error = sys.exc_info()[1]
    if error is not None:
        frames = traceback.extract_tb(error.__traceback__)[-5:]
        emit('执行异常', error_type=type(error).__name__, error=str(error),
             frames=[{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                     for frame in frames])


def change(table, key, before, after, **details):
    if not active():
        return
    if isinstance(before, dict) and isinstance(after, dict):
        details['changed_fields'] = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
    emit('数据库已提交', table=table, key=key, before=before, after=after, **details)


def control_action(component):
    """Cold-path programmatic control calls also get an origin and a trace."""
    def decorate(method):
        @wraps(method)
        def wrapped(owner, *args, **kwargs):
            caller = sys._getframe(1)
            location = f'{Path(caller.f_code.co_filename).name}:{caller.f_lineno}:{caller.f_code.co_name}'
            del caller
            root = owner.store.db_path.parent / 'logs'
            if component == 'c' and not active():
                try:
                    from core.scdfe_control import log_directory
                    root = log_directory(owner.store)
                except Exception:
                    pass
            context = nullcontext() if active() else scope(component, root, source='程序内部', caller=location)
            with context:
                details = {'action': method.__name__, 'owner': type(owner).__name__,
                           'arguments': args, 'options': kwargs, 'caller': location}
                emit('控制调用开始', **details)
                try:
                    result = method(owner, *args, **kwargs)
                except Exception as exc:
                    emit('控制调用失败', **details, error_type=type(exc).__name__, error=str(exc))
                    raise
                emit('控制调用返回', **details, result=result)
                return result
        return wrapped
    return decorate


def audit_request(component):
    def decorate(method):
        @wraps(method)
        def wrapped(handler, *args, **kwargs):
            if handler.command in {'GET', 'HEAD', 'OPTIONS'}:
                return method(handler, *args, **kwargs)
            store = handler.service.store if component == 't' else handler.store
            root = store.db_path.parent / 'logs'
            if component == 'c':
                try:
                    from core.scdfe_control import log_directory
                    root = log_directory(store)
                except Exception:
                    pass
            handler._audit_status = None
            handler._audit_delivered = False
            started = time.monotonic()
            with scope(component, root, source='HTTP', method=handler.command,
                       path=path_only(handler.path), peer_ip=handler.client_address[0],
                       peer_port=handler.client_address[1],
                       page=path_only(handler.headers.get('Referer', '')),
                       user_agent=handler.headers.get('User-Agent', '')[:240],
                       client_tab=handler.headers.get('X-Scada-Tab', '')[:80],
                       client_action=unquote(handler.headers.get('X-Scada-Action', '')[:480])[:160],
                       client_request=handler.headers.get('X-Scada-Request', '')[:80],
                       actor='未认证；页面及客户端标记为客户端声明'):
                emit('请求到达', if_match=handler.headers.get('If-Match', '')[:80])
                try:
                    return method(handler, *args, **kwargs)
                except Exception as exc:
                    emit('请求异常', error_type=type(exc).__name__)
                    raise
                finally:
                    status = handler._audit_status
                    outcome = ('已受理' if status == 202 else '成功' if status and 200 <= status < 300
                               else '失败' if status else '未返回结果')
                    emit('请求结束', outcome=outcome, http_status=status,
                         response_delivered=handler._audit_delivered,
                         duration_ms=round((time.monotonic() - started) * 1000, 3))
        return wrapped
    return decorate

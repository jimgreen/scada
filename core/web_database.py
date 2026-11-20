"""Select an existing database without mixing requests or active runtimes across stores."""
from contextlib import ExitStack, contextmanager
from functools import wraps
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from scdfe_c.web.service import ApiError, ScadaStore
from core.database_validation import validate_database

LOG = logging.getLogger(__name__)
SERVICE_NAMES = ('iec104_service', 'modbus_tcp_service', 'modbus_tcp_server_service',
                 'modbus_rtu_service', 'modbus_rtu_server_service', 'can_service',
                 'can_server_service', 'opc_server_service', 'bacnet_server_service')


class RequestGate:
    """Normal requests stay concurrent; a switch drains them and excludes new ones."""
    def __init__(self):
        self.condition = threading.Condition()
        self.readers = self.waiting = 0
        self.writer = False

    @contextmanager
    def hold(self, exclusive=False):
        with self.condition:
            if exclusive:
                self.waiting += 1
                try:
                    self.condition.wait_for(lambda: not self.writer and not self.readers)
                    self.writer = True
                finally:
                    self.waiting -= 1
            else:
                self.condition.wait_for(lambda: not self.writer and not self.waiting)
                self.readers += 1
        try:
            yield
        finally:
            with self.condition:
                if exclusive:
                    self.writer = False
                else:
                    self.readers -= 1
                self.condition.notify_all()


def database_request(method):
    @wraps(method)
    def wrapped(handler, *args, **kwargs):
        exclusive = unquote(urlsplit(handler.path).path).strip('/') == 'api/database' and handler.command == 'POST'
        with handler.server._database_gate.hold(exclusive):
            return method(handler, *args, **kwargs)
    return wrapped


def selection_file(role, host, port):
    key = hashlib.sha256(f'{host}:{port}'.encode()).hexdigest()[:12]
    return Path(__file__).resolve().parents[1] / 'db' / f'web_database_{role}_{key}.json'


def selected_database(path, explicit, default, *, base_directory=None):
    if explicit is not None:
        explicit = Path(explicit)
        return (Path(base_directory) / explicit).resolve() if base_directory and not explicit.is_absolute() else explicit
    if path.is_file():
        # A broken remembered selection must be visible, not silently open a different DB.
        data = json.loads(path.read_text(encoding='utf-8'))
        saved = Path(data['database'])
        if not saved.is_absolute():
            saved = Path(base_directory or data['base_directory']) / saved
        if not saved.is_file():
            raise ValueError(f'已选数据库不存在：{saved}；请用 --db 指定数据库')
        return saved.resolve()
    return default


def prepare_database(role, path, schema_dir):
    resources = ExitStack()
    try:
        if role == 't':
            from scdfe_t.web.service import TransStore
            trans = TransStore(path, schema_dir, connection_pool_size=4)
            resources.callback(trans.store.close)
            return resources, {'trans_store': trans}
        if role == 'c':
            from scdfe_c.web.process_control import ProcessController
            store = ScadaStore(path, schema_dir, connection_pool_size=4)
            resources.callback(store.close)
            return resources, {'store': store, 'process_controller': ProcessController(store)}
        from scdfe_s.database import SimulatorStore
        from scdfe_s.web.iec104_service import IEC104Service
        from scdfe_s.web.protocol_service import (ModbusTcpService, ModbusTcpServerService,
            ModbusRtuService, ModbusRtuServerService, CanService, CanServerService,
            OpcServerService, BacnetServerService)
        from core.service_log import ServiceFileLog
        store = SimulatorStore(path, schema_dir, connection_pool_size=4)
        resources.callback(store.close)
        history = ServiceFileLog.startup_catalog(store.db_path.parent / 'logs')
        context = {'store': store, '_channel_service_lock': threading.RLock(),
                   '_channel_service_cache': {}, '_channel_service_history': history}
        for name, cls in zip(SERVICE_NAMES, (IEC104Service, ModbusTcpService, ModbusTcpServerService,
                ModbusRtuService, ModbusRtuServerService, CanService, CanServerService,
                OpcServerService, BacnetServerService)):
            service = cls(store, startup_catalog=history)
            context[name] = service
            resources.callback(service.close)
        return resources, context
    except BaseException:
        resources.close()
        raise


class DatabaseSelectionMixin:
    def _init_database(self, role, path, schema_dir, selection_path=None):
        self.database_role = role
        self._database_gate = RequestGate()
        self._database_token = uuid4().hex
        self._database_selection_path = selection_path
        self._database_base_directory = Path(__file__).resolve().parents[1] / f'scdfe_{role}' / 'web'
        self._resources, context = prepare_database(role, path, schema_dir)
        self.__dict__.update(context)

    def database_store(self):
        return self.trans_store.store if self.database_role == 't' else self.store

    def database_info(self):
        current = self.database_store().db_path
        relative = self.relative_database_path(current)
        return dict(database=str(current), role=self.database_role, token=self._database_token,
                    relative_database=relative, base_directory=str(self._database_base_directory),
                    directory=Path(relative).parent.as_posix() if relative else '.',
                    remembered=self._database_selection_path is not None)

    def relative_database_path(self, path):
        try:
            return Path(os.path.relpath(path, self._database_base_directory)).as_posix()
        except ValueError:
            return None  # Another drive cannot be expressed as a relative path.

    def resolve_database_path(self, value):
        if not isinstance(value, str) or not value.strip() or '\x00' in value:
            raise ApiError(400, '请选择数据库文件或填写相对于Web服务目录的路径')
        path = Path(value.strip()).expanduser()
        return (path if path.is_absolute() else self._database_base_directory / path).resolve()

    def database_tree(self, directory):
        folder = self.resolve_database_path(directory or '.')
        if not folder.is_dir():
            raise ApiError(400, '目录不存在或不可访问')
        entries = []
        try:
            for path in folder.iterdir():
                if path.name.startswith('.') or path.name in {'node_modules', '__pycache__'}:
                    continue
                if path.is_dir() or (path.is_file() and path.suffix.lower() in {'.sqlite3', '.sqlite', '.db'}):
                    relative = self.relative_database_path(path)
                    if relative is not None:
                        entries.append(dict(name=path.name, path=relative, directory=path.is_dir()))
        except OSError as exc:
            raise ApiError(400, '无法读取此目录') from exc
        entries.sort(key=lambda row: (not row['directory'], row['name'].lower()))
        return dict(directory=self.relative_database_path(folder),
                    parent=self.relative_database_path(folder.parent) if folder.parent != folder else None,
                    entries=entries)

    def check_database(self, path):
        return validate_database(path, self.database_role, self.database_store().catalog)

    def create_database_entry(self, body, *, directory=False):
        if not isinstance(body, dict) or set(body) != {'directory', 'name'}:
            raise ApiError(400, '需要目录和名称')
        parent = self.resolve_database_path(body['directory'])
        name = body['name']
        if (not isinstance(name, str) or not name.strip() or name != name.strip() or
                name in {'.', '..'} or any(c in name for c in '/\\\x00<>:"|?*') or name.endswith('.')):
            raise ApiError(400, '名称无效，请只输入文件名或目录名')
        if not parent.is_dir():
            raise ApiError(400, '父目录不存在')
        target = parent / name
        if not directory and target.suffix.lower() not in {'.sqlite3', '.sqlite', '.db'}:
            raise ApiError(400, '数据库扩展名必须为 .sqlite3、.sqlite 或 .db')
        created = False
        try:
            if directory:
                target.mkdir()
                return dict(path=self.relative_database_path(target), directory=True)
            # Exclusive creation prevents accidental replacement, including races.
            with target.open('xb'):
                created = True
            from scdfe_s.database import SimulatorStore
            store_type = SimulatorStore if self.database_role == 's' else ScadaStore
            created_store = store_type(target, self.database_store().catalog.directory)
            created_store.close()
            validation = self.require_valid_database(target)
            return dict(path=self.relative_database_path(target), validation=validation)
        except FileExistsError as exc:
            raise ApiError(409, '同名文件或目录已存在，请更换名称', code='already_exists') from exc
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            raise ApiError(400, f'创建失败：{exc}') from exc
        finally:
            if created and 'validation' not in locals():
                target.unlink(missing_ok=True)

    def require_valid_database(self, path):
        result = self.check_database(path)
        if not result['valid']:
            raise ApiError(400, result['message'], code='invalid_database', details=result)
        return result

    def _assert_database_idle(self):
        from core.process_registry import process_is_alive
        from core.scdfe_control import InstanceLock
        store = self.database_store()
        busy = False
        if self.database_role == 's':
            services = [getattr(self, name) for name in SERVICE_NAMES]
            services += list(self._channel_service_cache.values())
            busy = any(service.running for service in services)
        elif self.database_role == 'c':
            controller = self.process_controller
            with controller._mutex:
                busy = bool(controller._operation and controller._operation['status'] == 'running')
                busy |= any(child.poll() is None for child in controller._children.values())
            with store.connection() as con:
                busy |= any(process_is_alive(int(r[0] or 0)) for r in con.execute(
                    'SELECT process_pid FROM fes_process_define'))
            busy |= InstanceLock.held(store.db_path, 'process-control')
        else:
            busy = any(child.poll() is None for child in self.trans_store._children.values())
            with store.connection() as con:
                channels = [r[0] for r in con.execute('SELECT channel_id FROM fes_scdfe_t_define')]
            busy |= any(InstanceLock.held(store.db_path, f'trans-{cid}') or
                        InstanceLock.held(store.db_path, f'trans-control-{cid}') for cid in channels)
        if busy:
            raise ApiError(409, '当前服务仍在运行或处理启停，请先在进程/通信监控中停止，再切换数据库', code='database_busy')

    def switch_database(self, body):
        if not isinstance(body, dict) or set(body) != {'database', 'expected_database'}:
            raise ApiError(400, '需要 database 和 expected_database 两个路径字段')
        current = self.database_store()
        if body['expected_database'] != str(current.db_path):
            raise ApiError(409, '数据库已被其他页面切换，请刷新页面', code='database_changed')
        target = self.resolve_database_path(body['database'])
        self.require_valid_database(target)
        if target.samefile(current.db_path):
            self._remember_database(target)
            return self.database_info()
        self._assert_database_idle()
        try:
            resources, context = prepare_database(self.database_role, target, current.catalog.directory)
        except (ValueError, RuntimeError, sqlite3.Error, OSError) as exc:
            raise ApiError(400, f'无法使用该数据库：{exc}', code='invalid_database') from exc
        try:
            self._remember_database(target)
        except ApiError:
            resources.close()
            raise
        old = self._resources
        self.__dict__.update(context)
        self._resources = resources
        self._database_token = uuid4().hex
        try:
            old.close()
        except Exception:
            LOG.exception('closing previous database resources')
        return self.database_info()

    def _remember_database(self, target):
        if self._database_selection_path is None:
            return
        path = Path(self._database_selection_path)
        temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({'database': self.relative_database_path(target) or str(target),
                                            'base_directory': str(self._database_base_directory)}, ensure_ascii=False), encoding='utf-8')
            temporary.replace(path)
        except OSError as exc:
            raise ApiError(500, '保存数据库选择失败，仍使用原数据库') from exc
        finally:
            temporary.unlink(missing_ok=True)


def handle_database(handler, parts, query, method):
    server = handler.server
    token = handler.headers.get('X-Scada-Database')
    if method not in {'GET', 'HEAD', 'OPTIONS'} and token and token != server._database_token:
        raise ApiError(409, '数据库已切换，请刷新页面后再操作', code='database_changed')
    if parts in (['api', 'database', 'create'], ['api', 'database', 'mkdir']) and method == 'POST':
        if query:
            raise ApiError(400, '创建接口不接受查询参数')
        return handler._json(201, server.create_database_entry(handler._body(), directory=parts[-1] == 'mkdir'))
    if parts == ['api', 'database', 'tree'] and method == 'GET':
        if set(query) - {'directory'}:
            raise ApiError(400, '未知目录查询参数')
        return handler._json(200, server.database_tree(query.get('directory', '.')))
    if parts == ['api', 'database', 'validate'] and method == 'GET':
        if set(query) != {'path'}:
            raise ApiError(400, '需要数据库相对路径path')
        target = server.resolve_database_path(query['path'])
        return handler._json(200, dict(server.check_database(target), path=server.relative_database_path(target)))
    if parts == ['api', 'database']:
        if query:
            raise ApiError(400, '数据库接口不接受查询参数')
        if method == 'GET':
            return handler._json(200, server.database_info())
        if method == 'POST':
            return handler._json(200, server.switch_database(handler._body()))
        raise ApiError(405, '数据库接口支持GET和POST')
    if parts in (['static', 'database-selector.js'], ['static', 'database-selector.css']) and method == 'GET':
        path = Path(__file__).with_name('web_static') / parts[-1]
        content_type = 'text/css' if path.suffix == '.css' else 'application/javascript'
        return handler._send(200, content_type + '; charset=utf-8', path.read_bytes())
    if method in {'POST', 'PATCH', 'PUT'} and parts[:1] == ['api']:
        start = parts[-1] == 'start'
        if parts == ['api', 'processes', 'control']:
            start = handler._body().get('action') == 'start'
        if parts == ['api', 'scdfe_c', 'control']:
            start = bool(handler._body().get('process_enable'))
        if start:
            server.require_valid_database(server.database_store().db_path)
    return False

"""Database selection is isolated per Web and never accepts a model/other-role DB."""
import http.client
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.parse import quote

from scdfe_c.web.server import make_server as make_c
from scdfe_s.web.server import make_server as make_s
from scdfe_t.web.server import make_server as make_t
from scdfe_c.web.service import ScadaStore
from scdfe_s.database import SimulatorStore


class DatabaseSelectionTests(unittest.TestCase):
    def request(self, server, method, path, body=None, token=None):
        con = http.client.HTTPConnection(*server.server_address, timeout=10)
        try:
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['X-Scada-Database'] = token
            con.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            response = con.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
        finally:
            con.close()

    def test_switch_all_roles_health_data_and_stale_tab(self):
        for role, factory, store_cls in [('c', make_c, ScadaStore), ('s', make_s, SimulatorStore), ('t', make_t, ScadaStore)]:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                first, second = root / 'first.sqlite3', root / '中文 空格.sqlite3'
                target = store_cls(second)
                target.create_row('fes_rtu_define', {'rtu_id': 17, 'name': '第二库', 'channel_id': 17, 'channel_type': 'iec104'})
                target.create_row('fes_channel_iec104_define', {'id': 17, 'channel_id': 17})
                target.close()
                preference = root / 'selection.json'
                server = factory('127.0.0.1', 0, db_path=first, selection_path=preference)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    status, before = self.request(server, 'GET', '/api/database')
                    self.assertEqual(status, 200)
                    status, switched = self.request(server, 'POST', '/api/database',
                        {'database': str(second), 'expected_database': str(first.resolve())}, before['token'])
                    self.assertEqual(status, 200, switched)
                    self.assertEqual(switched['database'], str(second.resolve()))
                    self.assertNotEqual(switched['token'], before['token'])
                    from core.web_database import selected_database
                    self.assertEqual(selected_database(preference, None, first), second.resolve())
                    self.assertEqual(selected_database(preference, first, second), first)
                    status, health = self.request(server, 'GET', '/api/health')
                    self.assertEqual(health['database'], str(second.resolve()))
                    actual = server.trans_store.store if role == 't' else server.store
                    self.assertEqual(actual.get_row('fes_rtu_define', [17])['name'], '第二库')
                    status, error = self.request(server, 'POST', '/api/database',
                        {'database': str(first), 'expected_database': str(second)}, before['token'])
                    self.assertEqual(status, 409)
                    self.assertEqual(error['error']['code'], 'database_changed')
                finally:
                    server.shutdown(); server.server_close(); thread.join(3)

    def test_invalid_targets_leave_current_database_and_target_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wrong = root / 's.sqlite3'; s = SimulatorStore(wrong); s.close()
            model = root / 'model.sqlite3'
            with closing(sqlite3.connect(model)) as con:
                con.execute('CREATE TABLE model(id INTEGER)')
            for bad in [wrong, model, root / 'missing.sqlite3']:
                server = make_c('127.0.0.1', 0, db_path=root / 'c.sqlite3')
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                previous = bad.read_bytes() if bad.exists() else None
                try:
                    _, current = self.request(server, 'GET', '/api/database')
                    status, error = self.request(server, 'POST', '/api/database',
                        {'database': str(bad), 'expected_database': current['database']})
                    self.assertEqual(status, 400, error)
                    self.assertEqual(str(server.store.db_path), current['database'])
                    self.assertEqual(bad.read_bytes() if bad.exists() else None, previous)
                finally:
                    server.shutdown(); server.server_close(); thread.join(3)

    def test_running_s_service_prevents_switch(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = SimulatorStore(root / 'second.sqlite3'); target.close()
            server = make_s('127.0.0.1', 0, db_path=root / 'first.sqlite3')
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                server._channel_service_cache['test-running'] = Mock(running=True)
                _, before = self.request(server, 'GET', '/api/database')
                status, error = self.request(server, 'POST', '/api/database',
                    {'database': str(root / 'second.sqlite3'), 'expected_database': before['database']})
                self.assertEqual(status, 409, error)
                self.assertEqual(error['error']['code'], 'database_busy')
            finally:
                server.shutdown(); server.server_close(); thread.join(3)

    def test_c_async_operation_and_t_runtime_lock_prevent_switch(self):
        from core.scdfe_control import InstanceLock
        for role, factory in [('c', make_c), ('t', make_t)]:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                other = ScadaStore(root / 'second.sqlite3'); other.close()
                server = factory('127.0.0.1', 0, db_path=root / 'first.sqlite3')
                lease = None
                if role == 'c':
                    server.process_controller._operation = {'status': 'running'}
                else:
                    server.trans_store.store.create_row('fes_scdfe_t_define', {'channel_id': 1, 'protocol': 'iec104'})
                    lease = InstanceLock(server.trans_store.db_path, 'trans-1')
                    self.assertTrue(lease.acquire())
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    _, before = self.request(server, 'GET', '/api/database')
                    status, error = self.request(server, 'POST', '/api/database',
                        {'database': str(root / 'second.sqlite3'), 'expected_database': before['database']})
                    self.assertEqual(status, 409, error)
                    self.assertEqual(error['error']['code'], 'database_busy')
                finally:
                    if lease: lease.release()
                    server.shutdown(); server.server_close(); thread.join(3)

    def test_switch_waits_for_an_inflight_read_including_encoded_route(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            other = ScadaStore(root / 'second.sqlite3'); other.close()
            server = make_c('127.0.0.1', 0, db_path=root / 'first.sqlite3')
            server.store.create_row('fes_rtu_define', {'rtu_id': 1, 'name': '旧库', 'channel_id': 1, 'channel_type': 'iec104'})
            entered, release = threading.Event(), threading.Event()
            original = server.store.get_row
            def slow(*args, **kwargs):
                entered.set(); release.wait(5)
                return original(*args, **kwargs)
            server.store.get_row = slow
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    read = pool.submit(self.request, server, 'GET', '/api/rtus/1')
                    self.assertTrue(entered.wait(3))
                    switch = pool.submit(self.request, server, 'POST', '/api/%64atabase/',
                        {'database': str(root / 'second.sqlite3'), 'expected_database': str(server.store.db_path)})
                    self.assertFalse(switch.done())
                    release.set()
                    self.assertEqual(read.result()[1]['item']['name'], '旧库')
                    self.assertEqual(switch.result()[0], 200)
            finally:
                release.set(); server.shutdown(); server.server_close(); thread.join(3)

    def test_relative_directory_creation_and_database_creation_all_roles(self):
        from core.web_database import selected_database
        for role, factory in [('c', make_c), ('s', make_s), ('t', make_t)]:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                base = root / ('scdfe_' + role) / 'web'; base.mkdir(parents=True)
                preference = root / 'selection.json'
                server = factory('127.0.0.1', 0, db_path=root / 'first.sqlite3', selection_path=preference)
                self.assertEqual(server._database_base_directory, Path(__file__).resolve().parents[1] / ('scdfe_' + role) / 'web')
                server._database_base_directory = base
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    _, before = self.request(server, 'GET', '/api/database')
                    self.assertEqual(before['relative_database'], '../../first.sqlite3')
                    body = {'directory': '../..', 'name': '新目录 空格'}
                    status, created = self.request(server, 'POST', '/api/database/mkdir', body)
                    self.assertEqual(status, 201, created)
                    directory = created['path']
                    self.assertEqual(directory, '../../新目录 空格')
                    self.assertEqual(self.request(server, 'POST', '/api/database/mkdir', body)[0], 409)
                    self.assertEqual(self.request(server, 'POST', '/api/database/mkdir', {'directory': directory, 'name': '../escape'})[0], 400)
                    status, result = self.request(server, 'POST', '/api/database/create', {'directory': directory, 'name': '空白.sqlite3'})
                    self.assertEqual(status, 201, result)
                    self.assertTrue(result['validation']['valid'])
                    target = root / '新目录 空格' / '空白.sqlite3'
                    previous = target.read_bytes()
                    self.assertEqual(self.request(server, 'POST', '/api/database/create', {'directory': directory, 'name': '空白.sqlite3'})[0], 409)
                    self.assertEqual(target.read_bytes(), previous)
                    _, tree = self.request(server, 'GET', '/api/database/tree?directory=' + quote(directory))
                    self.assertEqual(tree['entries'], [{'name': '空白.sqlite3', 'path': result['path'], 'directory': False}])
                    _, checked = self.request(server, 'GET', '/api/database/validate?path=' + quote(result['path']))
                    self.assertTrue(checked['valid'], checked)
                    status, switched = self.request(server, 'POST', '/api/database', {'database': result['path'], 'expected_database': before['database']})
                    self.assertEqual(status, 200, switched)
                    self.assertEqual(selected_database(preference, None, None), target.resolve())
                    self.assertEqual(selected_database(preference, result['path'], None, base_directory=base), target.resolve())
                    self.assertEqual(json.loads(preference.read_text(encoding='utf-8'))['database'], result['path'])
                finally:
                    server.shutdown(); server.server_close(); thread.join(3)

    def test_actual_schema_corruption_blocks_start_for_all_roles(self):
        cases = [('c', make_c, '/api/processes/control', {'target': 'main', 'action': 'start'}),
                 ('s', make_s, '/api/rtus/1/service/start', {}),
                 ('t', make_t, '/api/channels/1/start', {})]
        for role, factory, endpoint, body in cases:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'db.sqlite3'
                server = factory('127.0.0.1', 0, db_path=path)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    with closing(sqlite3.connect(path)) as con:
                        con.execute('ALTER TABLE fes_yc_define ADD COLUMN unexpected INTEGER'); con.commit()
                    _, checked = self.request(server, 'GET', '/api/database/validate?path=' + quote(str(path)))
                    self.assertFalse(checked['valid'])
                    self.assertIn('实际字段', checked['message'])
                    status, error = self.request(server, 'POST', endpoint, body)
                    self.assertEqual(status, 400, error)
                    self.assertEqual(error['error']['code'], 'invalid_database')
                finally:
                    server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__':
    unittest.main()

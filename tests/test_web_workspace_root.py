"""The Web file chooser stays within its configured workspace, for every role."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import quote

from core.web_database import selected_database
from tests.test_web_database_selection import DatabaseSelectionTests
from scdfe_c.web.server import make_server as make_c
from scdfe_s.web.server import make_server as make_s
from scdfe_t.web.server import make_server as make_t


class WorkspaceRootTests(unittest.TestCase):
    request = DatabaseSelectionTests.request

    @contextmanager
    def server(self, factory, root):
        database = root / 'realtime' / 'qinling' / 'first.sqlite3'
        database.parent.mkdir(parents=True, exist_ok=True)
        server = factory('127.0.0.1', 0, db_path=database, workspace_root=root,
                         selection_path=root / 'selection.json')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_relative_paths_and_root_parent_for_all_roles(self):
        for factory in (make_c, make_s, make_t):
            with self.subTest(factory=factory.__module__), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                with self.server(factory, root) as server:
                    status, info = self.request(server, 'GET', '/api/database')
                    self.assertEqual(status, 200)
                    self.assertEqual(info['database'], 'realtime/qinling/first.sqlite3')
                    self.assertEqual(info['relative_database'], info['database'])
                    self.assertEqual(info['workspace_root'], str(root.resolve()))
                    _, tree = self.request(server, 'GET', '/api/database/tree?directory=.')
                    self.assertIsNone(tree['parent'])
                    self.assertEqual(tree['directory'], '.')
                    self.assertEqual(tree['entries'][0]['path'], 'realtime')
                    _, health = self.request(server, 'GET', '/api/health')
                    self.assertEqual(health['database'], info['database'])
                    _, created = self.request(server, 'POST', '/api/database/mkdir',
                                              {'directory': '.', 'name': 'simulation'})
                    self.assertEqual(created['path'], 'simulation')
                    status, created = self.request(server, 'POST', '/api/database/create',
                        {'directory': 'simulation', 'name': '中文 空格.sqlite3'})
                    self.assertEqual(status, 201, created)
                    status, switched = self.request(server, 'POST', '/api/database',
                        {'database': created['path'], 'expected_database': info['database']})
                    self.assertEqual(status, 200, switched)
                    self.assertEqual(switched['database'], 'simulation/中文 空格.sqlite3')
                    self.assertEqual(selected_database(root / 'selection.json', None, None,
                        workspace_root=root), (root / switched['database']).resolve())

    def test_read_write_and_switch_reject_outside_paths_for_all_roles(self):
        for factory in (make_c, make_s, make_t):
            with self.subTest(factory=factory.__module__), tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / 'workspace'
                root.mkdir()
                outside = Path(folder) / 'outside'
                outside.mkdir()
                with self.server(factory, root) as server:
                    _, info = self.request(server, 'GET', '/api/database')
                    bad_paths = ['..', '../outside', 'realtime/../../outside', str(outside),
                                 '/outside', 'C:/outside', 'C:outside', r'..\outside',
                                 r'\\server\share', 'realtime/.. /outside']
                    for bad in bad_paths:
                        for endpoint in ('tree?directory=', 'validate?path='):
                            status, error = self.request(server, 'GET', '/api/database/' + endpoint + quote(bad, safe=''))
                            self.assertEqual(status, 400, (bad, error))
                        for endpoint, name in [('mkdir', 'leak'), ('create', 'leak.sqlite3')]:
                            status, error = self.request(server, 'POST', '/api/database/' + endpoint,
                                                        {'directory': bad, 'name': name})
                            self.assertEqual(status, 400, (bad, error))
                        status, error = self.request(server, 'POST', '/api/database',
                            {'database': bad, 'expected_database': info['database']})
                        self.assertEqual(status, 400, (bad, error))
                    self.assertEqual(list(outside.iterdir()), [])
                    self.assertEqual(self.request(server, 'GET', '/api/database')[1]['database'], info['database'])

    def test_external_directory_link_is_hidden_and_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, outside = Path(folder) / 'workspace', Path(folder) / 'outside'
            root.mkdir(); outside.mkdir()
            link = root / 'escape'
            try:
                if os.name == 'nt':
                    import _winapi
                    _winapi.CreateJunction(str(outside), str(link))
                else:
                    link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(str(exc))
            try:
                with self.server(make_c, root) as server:
                    _, tree = self.request(server, 'GET', '/api/database/tree?directory=.')
                    self.assertNotIn('escape', [row['name'] for row in tree['entries']])
                    self.assertEqual(self.request(server, 'GET', '/api/database/tree?directory=escape')[0], 400)
                    self.assertEqual(self.request(server, 'POST', '/api/database/create',
                        {'directory': 'escape', 'name': 'leak.sqlite3'})[0], 400)
                    self.assertFalse((outside / 'leak.sqlite3').exists())
            finally:
                if os.name == 'nt':
                    link.rmdir()
                else:
                    link.unlink()

    def test_startup_rejects_outside_database_before_creating_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'workspace'; root.mkdir()
            outside = Path(folder) / 'outside.sqlite3'
            with self.assertRaises(ValueError):
                make_c('127.0.0.1', 0, db_path=outside, workspace_root=root)
            self.assertFalse(outside.exists())

    def test_remembered_legacy_path_is_contained(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'workspace'; root.mkdir()
            database = root / 'first.sqlite3'; database.touch()
            preference = Path(folder) / 'selection.json'
            preference.write_text(json.dumps({'database': 'workspace/first.sqlite3',
                                              'base_directory': folder}), encoding='utf-8')
            self.assertEqual(selected_database(preference, None, None, workspace_root=root), database)
            outside = Path(folder) / 'outside.sqlite3'; outside.touch()
            preference.write_text(json.dumps({'database': str(outside)}), encoding='utf-8')
            with self.assertRaises(ValueError):
                selected_database(preference, None, None, workspace_root=root)


if __name__ == '__main__':
    unittest.main()

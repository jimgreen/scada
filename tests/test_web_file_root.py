"""The configured Web file root bounds both reads and writes for C/S/T."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import quote

from tests import test_web_database_selection as selection_tests
from scdfe_c.web.server import make_server as make_c
from scdfe_s.web.server import make_server as make_s
from scdfe_t.web.server import make_server as make_t
from scdfe_c.web.service import ApiError


class WebFileRootTests(unittest.TestCase):
    # Reuse the HTTP helper without inheriting the existing test cases.
    request = selection_tests.DatabaseSelectionTests.request

    def test_all_roles_bound_tree_validation_selection_and_creation(self):
        for role, factory in [('c', make_c), ('s', make_s), ('t', make_t)]:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as folder:
                parent = Path(folder)
                root = parent / 'workspace'; root.mkdir()
                outside = parent / 'workspace-other'; outside.mkdir()
                marker = outside / 'do-not-touch.sqlite3'; marker.write_bytes(b'unchanged')
                link = root / 'outside-link'
                if os.name == 'nt':
                    subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                   check=True, capture_output=True)
                else:
                    link.symlink_to(outside, target_is_directory=True)
                db = root / f'scdfe_{role}.sqlite3'
                server = factory('127.0.0.1', 0, db_path=db, workspace_root=root)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    _, info = self.request(server, 'GET', '/api/database')
                    self.assertEqual(info['relative_database'], db.name)
                    self.assertEqual(info['workspace_root'], str(root.resolve()))
                    _, tree = self.request(server, 'GET', '/api/database/tree')
                    self.assertEqual(tree['directory'], '.')
                    self.assertIsNone(tree['parent'])
                    self.assertNotIn('outside-link', [r['name'] for r in tree['entries']])
                    for path in ['..', '../workspace-other', str(outside), 'outside-link']:
                        for method, url, body in [
                            ('GET', '/api/database/tree?directory=' + quote(path), None),
                            ('GET', '/api/database/validate?path=' + quote(path + '/do-not-touch.sqlite3'), None),
                            ('POST', '/api/database', {'database': path + '/do-not-touch.sqlite3', 'expected_database': info['database']}),
                            ('POST', '/api/database/mkdir', {'directory': path, 'name': 'new-folder'}),
                            ('POST', '/api/database/create', {'directory': path, 'name': 'new.sqlite3'}),
                        ]:
                            code, body = self.request(server, method, url, body)
                            self.assertEqual(code, 400, (path, url, body))
                    self.assertEqual(marker.read_bytes(), b'unchanged')
                    self.assertEqual(sorted(p.name for p in outside.iterdir()), [marker.name])
                    self.assertEqual(server.database_store().db_path, db.resolve())
                    log_route = '/api/scdfe_c/logs' if role == 'c' else '/api/log-files'
                    code, logs = self.request(server, 'GET', log_route)
                    self.assertEqual(code, 200, logs)
                    self.assertEqual(logs['directory'], 'logs')
                    if role == 'c':
                        code, body = self.request(server, 'PATCH', '/api/scdfe_c/control', {'log_dir': '../workspace-other'})
                        self.assertEqual(code, 400, body)
                        code, body = self.request(server, 'PATCH', '/api/scdfe_c/control', {'log_dir': 'nested/logs'})
                        self.assertEqual(code, 200, body)
                        self.assertEqual(body['item']['log_dir'], 'nested/logs')
                        _, logs = self.request(server, 'GET', log_route)
                        self.assertEqual(logs['directory'], 'nested/logs')
                        # Legacy DB configuration must not expose files outside the root.
                        server.store.update_row('fes_scdfe_c_define', [1], {'log_dir': str(outside)})
                        self.assertEqual(self.request(server, 'GET', log_route)[0], 400)
                finally:
                    server.shutdown(); server.server_close(); thread.join(3)
                    if os.name == 'nt':
                        link.rmdir()
                    else:
                        link.unlink()

    def test_startup_rejects_outside_database_before_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder); root = parent / 'root'; root.mkdir()
            for factory in [make_c, make_s, make_t]:
                with self.assertRaises(ValueError):
                    factory(port=0, db_path=parent / 'outside.sqlite3', workspace_root=root)
                self.assertFalse((parent / 'outside.sqlite3').exists())

    def test_startup_rejects_external_log_directory_link(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder); root = parent / 'workspace'; root.mkdir()
            outside = parent / 'external'; outside.mkdir()
            link = root / 'logs'
            if os.name == 'nt':
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
            else:
                link.symlink_to(outside, target_is_directory=True)
            try:
                for factory in [make_c, make_s, make_t]:
                    with self.assertRaises(ApiError):
                        factory(port=0, db_path=root / 'new.sqlite3', workspace_root=root)
                    self.assertFalse((root / 'new.sqlite3').exists())
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                if os.name == 'nt':
                    link.rmdir()
                else:
                    link.unlink()

    def test_saved_web_relative_path_migrates_without_changing_database(self):
        from core.web_database import selected_database
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder); root = parent / 'workspace'; root.mkdir()
            web = parent / 'scdfe_c' / 'web'; web.mkdir(parents=True)
            db = root / 'existing.sqlite3'; db.touch()
            preference = parent / 'selection.json'
            preference.write_text(json.dumps({'database': '../../workspace/existing.sqlite3',
                                              'base_directory': str(web)}))
            self.assertEqual(selected_database(preference, None, None, workspace_root=root), db.resolve())
            self.assertEqual(selected_database(preference, 'existing.sqlite3', None, workspace_root=root), db.resolve())
            with self.assertRaises(ValueError):
                selected_database(preference, '../outside.sqlite3', None, workspace_root=root)
            preference.write_text(json.dumps({'database': '../outside.sqlite3', 'workspace_root': str(root)}))
            with self.assertRaises(ValueError):
                selected_database(preference, None, None, workspace_root=root)

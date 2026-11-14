"""Attribution, committed changes, redaction and isolated asynchronous traces."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from core import operation_audit as audit
from core.web_logs import log_files
from scdfe_c.web.log_query import search_logs
from scdfe_c.web.server import make_server as make_c
from scdfe_s.web.server import make_server as make_s
from scdfe_t.web.server import make_server as make_t
from scdfe_c.web.service import ApiError, ScadaStore


def records(root, trace=None):
    result = []
    for file in Path(root).rglob('*-operations-*.log'):
        for line in file.read_text(encoding='utf-8').splitlines():
            item = json.loads(line.split('操作审计 ', 1)[1])
            if trace is None or item['trace_id'] == trace:
                result.append(item)
    return result


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def server(self, factory):
        server = factory('127.0.0.1', 0, db_path=self.root / factory.__module__.split('.')[0] / 'test.sqlite3')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        return server

    def call(self, server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request(method, path, None if body is None else json.dumps(body), {
            'Content-Type': 'application/json', 'Referer': 'http://u:pass@localhost/edit?token=URL_SECRET',
            'X-Scada-Tab': 'tab-1', 'X-Scada-Request': 'client-id',
            'X-Scada-Action': '%E4%BF%9D%E5%AD%98', **(headers or {}),
        })
        response = connection.getresponse()
        data = json.loads(response.read())
        trace = response.getheader('X-Scada-Trace')
        connection.close()
        # The final receipt follows the socket write by a few instructions.
        deadline = time.monotonic() + 2
        while trace and not any(r['phase'] == '请求结束' for r in records(self.root, trace)):
            if time.monotonic() > deadline:
                self.fail('missing request completion receipt')
            time.sleep(.01)
        return response.status, data, trace

    def test_crud_receipts_and_search_visible_in_all_three_webs(self):
        for component, factory in [('c', make_c), ('s', make_s), ('t', make_t)]:
            with self.subTest(component=component):
                server = self.server(factory)
                path = '/api/trans/channels' if component == 't' else '/api/rtus'
                body = {'channel_id': 81, 'protocol': 'modbus-tcp', 'bind_host': '127.0.0.1', 'bind_port': 22502} if component == 't' else {'rtu_id': 81, 'channel_id': 81, 'channel_type': 'iec104'}
                status, _, created = self.call(server, 'POST', path, {**body, 'name': 'before'})
                self.assertEqual(status, 201)
                status, _, changed = self.call(server, 'PATCH', path + '/81', {'name': 'after'})
                self.assertEqual(status, 200)
                receipt = next(r for r in records(self.root, changed) if r['phase'] == '数据库已提交')
                self.assertEqual(receipt['before']['name'], 'before')
                self.assertEqual(receipt['after']['name'], 'after')
                self.assertEqual(receipt['client_action'], '保存')
                self.assertEqual(receipt['page'], '/edit')
                self.assertEqual(receipt['peer_ip'], '127.0.0.1')
                self.assertNotEqual(changed, created)
                status, _, deleted = self.call(server, 'DELETE', path + '/81')
                self.assertEqual(status, 200)
                self.assertTrue(any(r['phase'] == '数据库已提交' and r.get('after') is None
                                    for r in records(self.root, deleted)))
                directory = self.root / factory.__module__.split('.')[0] / 'logs'
                self.assertTrue(log_files(directory, component, {})['items'])
                result = search_logs(directory, {'q': changed}, family=component)
                self.assertIn(changed, json.dumps(result))
                self.assertNotIn('URL_SECRET', json.dumps(records(self.root)))

    def test_failures_rejections_and_read_only_polling(self):
        for factory in (make_c, make_s, make_t):
            server = self.server(factory)
            for method, path, extra, expected in [
                ('POST', '/api/not-a-route', {}, 404),
                ('PUT', '/api/health', {}, (404, 405)),
                ('POST', '/api/health', {'Origin': 'http://attacker.invalid'}, 403),
            ]:
                status, _, trace = self.call(server, method, path, {}, extra)
                self.assertIn(status, expected if isinstance(expected, tuple) else (expected,))
                self.assertEqual(records(self.root, trace)[-1]['outcome'], '失败')
                self.assertFalse(any(r['phase'] == '数据库已提交' for r in records(self.root, trace)))
            count = len(records(self.root))
            self.assertEqual(self.call(server, 'GET', '/api/health')[0], 200)
            self.assertEqual(len(records(self.root)), count)

    def test_redaction_bounds_concurrency_and_context_reset(self):
        barrier = threading.Barrier(8)
        def write(index):
            with audit.scope('c', self.root, source='test', index=index) as trace:
                barrier.wait(3)
                audit.request_body({'password': 'VERY_SECRET', 'auth_token': 'TOKEN_SECRET',
                                    'nested': {'private_key': 'PRIVATE_SECRET'},
                                    'items': list(range(300)), 'description': 'https://u:p@example.test'})
                audit.emit('失败', error='bad password=VERY_SECRET and TOKEN_SECRET')
            self.assertFalse(audit.active())
            return trace
        with ThreadPoolExecutor(8) as pool:
            traces = list(pool.map(write, range(8)))
        self.assertEqual(len(set(traces)), 8)
        for index, trace in enumerate(traces):
            rows = records(self.root, trace)
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(r['index'] == index for r in rows))
            self.assertEqual(rows[0]['parameters']['items']['count'], 300)
            self.assertEqual(len(rows[0]['parameters']['items']['sample']), 20)
        data = json.dumps(records(self.root))
        for secret in ('VERY_SECRET', 'TOKEN_SECRET', 'PRIVATE_SECRET', 'u:p@'):
            self.assertNotIn(secret, data)
        audit.emit('outside')
        self.assertEqual(len(records(self.root)), 16)

    def test_batch_commit_and_rollback_are_distinguished(self):
        store = ScadaStore(self.root / 'batch.sqlite3')
        for rid in range(1, 26):
            store.create_row('fes_rtu_define', {'rtu_id': rid, 'channel_id': rid, 'channel_type': 'iec104', 'name': 'old'})
        with audit.scope('c', self.root, source='test') as success:
            store.update_rows([('fes_rtu_define', [rid], {'name': 'new'}) for rid in range(1, 26)])
        receipt = records(self.root, success)[0]
        self.assertEqual((receipt['count'], receipt['sampled'], receipt['truncated']), (25, 20, True))
        with audit.scope('c', self.root, source='test') as failure:
            with self.assertRaises(ApiError):
                store.update_rows([('fes_rtu_define', [1], {'name': 'rollback'}),
                                   ('fes_rtu_define', [999], {'name': 'missing'})])
        self.assertEqual(records(self.root, failure), [])
        self.assertEqual(store.get_row('fes_rtu_define', [1])['name'], 'new')

    def test_async_control_records_final_failure_with_same_trace(self):
        server = self.server(make_c)
        controller = server.process_controller
        entered, release = threading.Event(), threading.Event()
        def fail(_):
            controller._stage('测试停止阶段')
            entered.set()
            release.wait(4)
            raise RuntimeError('isolated failure')
        with patch.object(controller, '_stop', side_effect=fail):
            status, result, trace = self.call(server, 'POST', '/api/processes/control', {'target': 'all', 'action': 'stop'})
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(2))
            self.assertEqual(records(self.root, trace)[-1]['outcome'], '已受理')
            release.set()
            controller._thread.join(4)
        rows = records(self.root, trace)
        self.assertTrue(any(r['phase'] == '任务步骤' for r in rows))
        self.assertTrue(any(r['phase'] == '任务失败' for r in rows))
        self.assertFalse(any(r['phase'] == '任务完成' for r in rows))

    def test_audit_disk_failure_does_not_turn_committed_write_into_retry(self):
        store = ScadaStore(self.root / 'disk.sqlite3')
        with audit.scope('c', self.root / 'logs', source='test'):
            with patch.object(Path, 'open', side_effect=OSError('disk full')), self.assertLogs(audit.LOG, 'ERROR') as log:
                store.create_row('fes_rtu_define', {'rtu_id': 1, 'channel_id': 1, 'channel_type': 'iec104'})
            self.assertIn('操作审计写入失败', log.output[0])
        self.assertEqual(store.get_row('fes_rtu_define', [1])['rtu_id'], 1)

    def test_keepalive_request_context_and_trace_header_do_not_leak(self):
        server = self.server(make_c)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        self.addCleanup(connection.close)
        traces = []
        for method in ('POST', 'GET', 'POST'):
            connection.request(method, '/api/health', headers={'Content-Length': '0'})
            response = connection.getresponse()
            response.read()
            traces.append(response.getheader('X-Scada-Trace'))
        self.assertIsNone(traces[1])
        self.assertTrue(traces[0] and traces[2])
        self.assertNotEqual(traces[0], traces[2])

    def test_command_token_update_redacts_request_old_row_new_row_and_response(self):
        server = self.server(make_c)
        _, initial, _ = self.call(server, 'GET', '/api/scdfe_c/command-server')
        old = initial['item']['auth_token']
        secret = 'test-auth-token-that-must-not-enter-audit'
        status, _, trace = self.call(server, 'PATCH', '/api/scdfe_c/command-server', {'auth_token': secret})
        self.assertEqual(status, 200)
        data = json.dumps(records(self.root, trace))
        self.assertNotIn(old, data)
        self.assertNotIn(secret, data)
        receipt = next(r for r in records(self.root, trace) if r['phase'] == '数据库已提交')
        self.assertIn('auth_token', receipt['changed_fields'])
        self.assertEqual(receipt['before']['auth_token'], '[REDACTED]')
        self.assertEqual(receipt['after']['auth_token'], '[REDACTED]')

    def test_internal_failure_retains_diagnostic_call_site(self):
        server = self.server(make_c)
        with patch.object(server.store, 'create_rtu', side_effect=RuntimeError('isolated failure')):
            with self.assertLogs('scada.web', 'ERROR') as output:
                status, _, trace = self.call(server, 'POST', '/api/rtus', {'rtu_id': 1})
        self.assertEqual(status, 500)
        self.assertIn(trace, output.output[0])
        failure = next(r for r in records(self.root, trace) if r['phase'] == '执行异常')
        self.assertEqual(failure['error_type'], 'RuntimeError')
        self.assertEqual(failure['error'], 'isolated failure')
        self.assertTrue(failure['frames'])
        self.assertFalse(any(r['phase'] == '数据库已提交' for r in records(self.root, trace)))

    def test_all_pages_load_client_audit_asset_before_application_scripts(self):
        for factory in (make_c, make_s, make_t):
            server = self.server(factory)
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            self.addCleanup(connection.close)
            connection.request('GET', '/')
            response = connection.getresponse()
            html = response.read().decode('utf-8')
            self.assertEqual(response.status, 200)
            self.assertIn('/static/audit-client.js', html)
            self.assertLess(html.index('/static/audit-client.js'), html.index('</head>'))
            connection.request('GET', '/static/audit-client.js')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'X-Scada-Request', response.read())


if __name__ == '__main__':
    unittest.main()

"""HTTP regression tests; all data lives in temporary directories."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import web.server as server_module
from web.server import _process_is_alive, _scdfe_popen_kwargs, make_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'scada.sqlite3'
        self.server = make_server('127.0.0.1', 0, db_path=self.db)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def call(self, path, method='GET', data=None, headers=None):
        h = {'Content-Type': 'application/json'}
        h.update(headers or {})
        request = Request(self.url + path, method=method, headers=h,
                          data=json.dumps(data).encode('utf-8') if data is not None else None)
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            return response.status, json.loads(body) if 'application/json' in response.headers.get('Content-Type', '') else body

    def rtu(self, number=1):
        status, body = self.call('/api/rtus', 'POST', {
            'rtu_id': number, 'channel_id': number, 'channel_type': 'custom',
            'name': f'测试站 {number}',
        })
        self.assertEqual(status, 201, body)

    def test_process_probe_is_non_destructive(self):
        self.assertTrue(_process_is_alive(os.getpid()))
        self.assertFalse(_process_is_alive(2**31 - 1))

    def test_process_launch_options_are_platform_specific(self):
        with patch.object(server_module.os, "name", "posix"):
            options = _scdfe_popen_kwargs()
        self.assertEqual(options, {"start_new_session": True, "close_fds": True})

        with patch.object(server_module.os, "name", "nt"), \
                patch.object(server_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200):
            options = _scdfe_popen_kwargs()
        self.assertEqual(options, {"creationflags": 0x200, "close_fds": True})

    def test_process_probe_posix_branch_uses_kill_zero(self):
        with patch.object(server_module.os, "name", "posix"), \
                patch.object(server_module.os, "kill") as kill:
            self.assertTrue(_process_is_alive(1234))
            kill.assert_called_once_with(1234, 0)

    def test_catalog_and_empty_application(self):
        status, body = self.call('/api/schemas')
        self.assertEqual(status, 200)
        self.assertEqual(len(body['items']), 18)
        self.assertEqual(sum(len(s['columns']) for s in body['items']), 353)
        rtu_schema = next(s for s in body['items'] if s['head']['eng_name'] == 'fes_rtu_define')
        rtu_columns = {column['eng_name'] for column in rtu_schema['columns']}
        self.assertNotIn('proto_type', rtu_columns)
        self.assertNotIn('max_yc_num', rtu_columns)
        self.assertNotIn('max_yx_num', rtu_columns)
        self.assertNotIn('max_yt_num', rtu_columns)
        self.assertNotIn('max_yk_num', rtu_columns)
        self.assertNotIn('channel_status', rtu_columns)
        self.assertIn('status', rtu_columns)
        self.assertEqual(self.call('/')[0], 200)
        self.assertEqual(self.call('/api/health')[1]['status'], 'ok')
        self.assertEqual(self.call('/api/rtus')[1]['total'], 0)

    def test_all_four_types_scoped_crud_and_persistence(self):
        self.rtu(7)
        self.rtu(42)
        for kind, value in [('yc', 12.5), ('yx', 1), ('yt', 22.75), ('yk', 0)]:
            base = f'/api/rtus/7/points/{kind}'
            status, body = self.call(base, 'POST', {'pnt_no': 3, 'name': '测试点', 'value': value})
            self.assertEqual(status, 201, body)
            self.assertEqual(body['item']['rtu_id'], 7)
            self.assertEqual(self.call(base)[1]['total'], 1)
            self.assertEqual(self.call(f'/api/rtus/42/points/{kind}')[1]['total'], 0)
            self.assertEqual(self.call(f'/api/rtus/42/points/{kind}/3')[0], 404)
            status, body = self.call(base + '/3', 'PATCH', {'name': '已更新', 'value': value + 1})
            self.assertEqual(status, 200, body)
            self.assertEqual(self.call(base + '/3')[1]['item']['name'], '已更新')
            self.assertEqual(self.call(f'/api/points/{kind}')[1]['total'], 1)
        # A newly opened service observes committed data from another instance.
        from web.service import ScadaStore
        reopened = ScadaStore(self.db)
        self.assertEqual(reopened.list_rows('fes_yc_define')['total'], 1)
        for kind in ('yc', 'yx', 'yt', 'yk'):
            self.assertEqual(self.call(f'/api/rtus/7/points/{kind}/3', 'DELETE')[0], 200)
        self.assertEqual(self.call('/api/rtus/7', 'DELETE')[0], 200)

    def test_relations_duplicate_keys_and_immutable_primary_key(self):
        self.rtu()
        self.assertEqual(self.call('/api/rtus', 'POST', {
            'rtu_id': 1, 'channel_id': 2, 'channel_type': 'modbus-tcp',
        })[0], 409)
        status, body = self.call('/api/rtus/1', 'DELETE')
        self.assertEqual(status, 200, body)

    def test_validation_scope_and_large_integer_round_trip(self):
        self.rtu()
        rtu = self.call('/api/rtus/1')[1]['item']
        self.assertEqual((rtu['process_enable'], rtu['process_restart'],
                          rtu['process_status'], rtu['process_pid']), (1, 0, 0, 0))
        self.assertEqual(self.call('/api/rtus/1', 'PATCH', {'process_enable': 2})[0], 400)
        self.assertEqual(self.call('/api/rtus/1', 'PATCH', {'process_restart': -1})[0], 400)
        self.assertEqual(self.call('/api/rtus/1', 'PATCH', {
            'process_enable': 0, 'process_restart': 1,
        })[0], 200)
        base = '/api/rtus/1/points/yc'
        for data in ({'pnt_no': True}, {'pnt_no': 1.5}, {'pnt_no': -1}, {'pnt_no': 1, 'value': 'NaN'},
                     {'pnt_no': 1, 'typo': 1}, {'pnt_no': 1, 'rtu_id': 2}, {'pnt_no': None}):
            self.assertEqual(self.call(base, 'POST', data)[0], 400, data)
        large = '9223372036854775807'
        self.assertEqual(self.call(base, 'POST', {'pnt_no': 0, 'yc_id': large})[0], 201)
        self.assertEqual(self.call(base + '/0')[1]['item']['yc_id'], large)
        self.assertEqual(self.call(base, 'POST', {'pnt_no': 2, 'yc_id': '9223372036854775808'})[0], 400)
        self.assertEqual(self.call('/api/rtus/999/points/yc')[0], 404)
        self.assertEqual(self.call('/api/rtus/1/points/unknown')[0], 404)

    def test_pagination_search_order_and_unknown_fields(self):
        for number in range(1, 5):
            self.rtu(number)
        body = self.call('/api/rtus?limit=2&offset=1&sort=rtu_id&order=desc')[1]
        self.assertEqual(body['total'], 4)
        self.assertEqual([r['rtu_id'] for r in body['items']], [3, 2])
        self.assertEqual(self.call('/api/rtus?q=3')[1]['total'], 1)
        for query in ('limit=-1', 'offset=-2', 'sort=bad', 'order=bad', 'unknown=1', 'rtu_id=1&rtu_id=2'):
            self.assertEqual(self.call('/api/rtus?' + query)[0], 400, query)
        self.assertEqual(self.call('/api/tables/not_a_table/rows')[0], 404)

    def test_every_schema_crud_including_four_part_key(self):
        self.rtu()
        schemas = self.call('/api/schemas')[1]['items']
        for schema in schemas:
            name = schema['head']['eng_name']
            if name == 'fes_rtu_define':
                continue
            row = {c['eng_name']: 1 for c in schema['columns'] if c['primary_key']}
            if any(c['eng_name'] == 'rtu_id' for c in schema['columns']):
                row['rtu_id'] = 1
            row['name'] = name
            route = f'/api/tables/{name}/rows'
            self.assertEqual(self.call(route, 'POST', row)[0], 201, name)
            key = ','.join(str(row[k]) for k in schema['primary_keys'])
            self.assertEqual(self.call(route + '/' + key)[1]['item']['name'], name)
            self.assertEqual(self.call(route + '/' + key, 'PATCH', {'name': '修改'})[0], 200)
            self.assertEqual(self.call(route + '/' + key, 'DELETE')[0], 200)

    def test_versions_and_concurrent_create(self):
        self.rtu()
        self.assertEqual(self.call('/api/rtus/1', 'PATCH', {'name': '新值'}, {'If-Match': '1'})[0], 200)
        self.assertEqual(self.call('/api/rtus/1', 'PATCH', {'name': '旧值'}, {'If-Match': '1'})[0], 409)
        self.assertEqual(self.call('/api/rtus/1', 'DELETE', headers={'If-Match': '1'})[0], 409)
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda _: self.call('/api/tables/fes_channel_modbus_tcp_define/rows', 'POST', {'rtu_id': 1})[0], range(8)))
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(statuses.count(409), 7)

    def test_origin_guard_and_static_traversal(self):
        self.assertEqual(self.call('/api/rtus', 'POST', {'rtu_id': 1}, {'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(self.call('/api/rtus', 'POST', {
            'rtu_id': 1, 'channel_id': 1, 'channel_type': 'custom',
        }, {'Origin': self.url})[0], 201)
        self.assertEqual(self.call('/static/../../db/scada.sqlite3')[0], 404)
        self.assertEqual(self.call('/db/scada.sqlite3')[0], 404)

    def test_scope_cannot_be_overridden_and_null_can_clear(self):
        self.rtu(1)
        self.rtu(2)
        self.call('/api/rtus/2/points/yc', 'POST', {'pnt_no': 8, 'name': 'other', 'value': 5})
        for method, data in [('GET', None), ('PATCH', {'value': 2}), ('DELETE', None)]:
            self.assertEqual(self.call('/api/rtus/1/points/yc/2,8', method, data)[0], 400)
            self.assertEqual(self.call('/api/rtus/1/points/yc/8', method, data)[0], 404)
        self.assertEqual(self.call('/api/rtus/1/points/yc?rtu_id=2')[0], 400)
        self.assertEqual(self.call('/api/rtus/2/points/yc/8', 'PATCH', {'value': None, 'name': ''})[0], 200)
        item = self.call('/api/rtus/2/points/yc/8')[1]['item']
        self.assertIsNone(item['value'])
        self.assertEqual(item['name'], '')
        self.assertEqual(self.call('/api/rtus/2/points/yc/8', 'PATCH', {'rtu_id': 1})[0], 400)

    def test_filtered_rtu_channels_and_invalid_update_relation(self):
        self.rtu(1)
        self.rtu(2)
        self.assertEqual(self.call('/api/rtus?channel_type=custom')[1]['total'], 2)
        self.assertEqual(self.call('/api/rtus/1', 'PATCH', {'rtu_id': 999})[0], 400)
        self.assertEqual(self.call('/api/rtus/1')[1]['item']['channel_id'], 1)
        self.assertEqual(self.call('/api/tables/fes_channel_iec104_define/schema')[0], 200)
        self.assertEqual(self.call('/api/overview?rtu_id=1')[1]['counts']['fes_rtu_define'], 1)

    def test_one_channel_per_rtu_and_protocol_parameter_endpoint(self):
        status, _ = self.call('/api/rtus', 'POST', {
            'rtu_id': 6, 'channel_id': 60, 'channel_type': 'modbus_tcp',
        })
        self.assertEqual(status, 400)
        status, body = self.call('/api/rtus', 'POST', {
            'rtu_id': 7, 'channel_id': 70, 'channel_type': 'modbus-tcp',
            'chan_addr': '127.0.0.1', 'chan_port': 1502,
        })
        self.assertEqual(status, 201, body)
        self.assertEqual(body['item']['channel_type'], 'modbus-tcp')
        info = self.call('/api/rtus/7/channel')[1]
        self.assertEqual(info['parameter_table'], 'fes_channel_modbus_tcp_define')
        self.assertIsNone(info['parameter'])
        status, body = self.call('/api/rtus/7/channel/parameters', 'POST', {
            'remote_host': '127.0.0.1', 'remote_port': 1502, 'unit_id': 3,
        })
        self.assertEqual(status, 201, body)
        self.assertEqual(self.call('/api/rtus/7/channel/parameters')[1]['item']['unit_id'], 3)
        status, body = self.call('/api/tables/fes_channel_modbus_tcp_define/rows', 'POST', {'rtu_id': 7})
        self.assertEqual(status, 409, body)
        self.assertEqual(self.call('/api/rtus/7', 'PATCH', {'channel_type': 'can'})[0], 200)
        self.assertEqual(self.call('/api/rtus/7/channel')[1]['parameter_table'], 'fes_channel_can_define')

    def test_bad_json_content_type_methods_and_unexpected_paths(self):
        for data in (b'{"rtu_id":1,"rtu_id":2}', b'{"rtu_id":NaN}', b'[1]', b'{', b''):
            request = Request(self.url + '/api/rtus', data=data, headers={'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=10)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        self.assertEqual(self.call('/api/rtus', 'POST', {'rtu_id': 3}, {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.call('/api/rtus', 'DELETE')[0], 405)
        self.assertEqual(self.call('/not-api/rtus/1', 'DELETE')[0], 404)
        self.assertEqual(self.call('/api/rtus/1/points/yc/1/extra')[0], 404)

    def test_removed_channel_routes_and_legacy_table_names_are_not_aliases(self):
        # RTU and channel are a single resource now.  Removed routes and the
        # former protocol table names must not resolve through compatibility
        # aliases.
        for path in (
            '/api/channels',
            '/api/channels/1',
            '/api/tables/fes_channel_define/schema',
            '/api/tables/fes_iec104_define/schema',
            '/api/tables/fes_modbus_define/schema',
        ):
            self.assertEqual(self.call(path)[0], 404, path)

    def test_row_change_does_not_modify_schema_files(self):
        from web.service import SCHEMA_DIR
        sources = {p: p.read_bytes() for p in SCHEMA_DIR.glob('*.json')}
        self.rtu()
        self.call('/api/rtus/1', 'PATCH', {'name': 'new'})
        self.call('/api/rtus/1', 'DELETE')
        self.assertEqual(sources, {p: p.read_bytes() for p in sources})

    def test_scdfe_control_status_and_log_routes(self):
        self.rtu()
        status, body = self.call('/api/scdfe_c/status')
        self.assertEqual(status, 200)
        self.assertEqual(body['item']['id'], 1)
        self.assertEqual(body['rtus'][0]['thread_id'], 0)
        self.assertNotIn('process_pid', body['rtus'][0])
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'process_pid': 1})[0], 400)
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'worker_start_delay_ms': 60001})[0], 400)
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'log_retention_hours': 87601})[0], 400)
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'log_retention_hours': 720})[0], 200)
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'worker_start_delay_ms': 500})[0], 200)
        self.assertEqual(self.call('/api/scdfe_c/status')[1]['item']['process_pid'], 0)
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'process_enable': 0})[0], 200)
        from scdfe_c import ScdfeLogger
        logger = ScdfeLogger(self.db.parent / 'logs', process_name='scdfe_c-main')
        logger.process('api test')
        status, body = self.call('/api/scdfe_c/logs?process=scdfe_c-main')
        self.assertEqual(status, 200)
        self.assertEqual(body['total'], 1)
        self.assertIn('hour', body['items'][0])
        filename = body['items'][0]['name']
        status, body = self.call('/api/scdfe_c/logs/' + filename + '?tail=1')
        self.assertEqual(status, 200)
        self.assertIn('api test', body['item']['content'])
        self.assertEqual(self.call('/api/scdfe_c/logs/..%2Fsecret.log')[0], 404)

    def test_scdfe_http_start_and_cooperative_stop(self):
        status, body = self.call('/api/scdfe_c/control', 'PATCH', {
            'process_enable': 1, 'worker_start_delay_ms': 0,
        })
        self.assertEqual(status, 200, body)
        deadline = time.monotonic() + 8
        running = None
        while time.monotonic() < deadline:
            running = self.call('/api/scdfe_c/status')[1]['item']
            if running['process_status'] == 2 and int(running['process_pid'] or 0) > 0:
                break
            time.sleep(0.1)
        try:
            self.assertIsNotNone(running)
            self.assertEqual(running['process_status'], 2)
            self.assertGreater(int(running['process_pid'] or 0), 0)

            status, body = self.call('/api/scdfe_c/control', 'PATCH', {'process_enable': 0})
            self.assertEqual(status, 200, body)
            deadline = time.monotonic() + 8
            stopped = None
            while time.monotonic() < deadline:
                stopped = self.call('/api/scdfe_c/status')[1]['item']
                if stopped['process_status'] == 0 and int(stopped['process_pid'] or 0) == 0:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(stopped)
            self.assertEqual((stopped['process_status'], int(stopped['process_pid'] or 0)), (0, 0))
        finally:
            # Keep the test isolated if an assertion or process startup race
            # interrupts the normal stop path.
            current = self.call('/api/scdfe_c/status')[1]['item']
            if int(current.get('process_pid') or 0):
                self.call('/api/scdfe_c/control', 'PATCH', {'process_enable': 0})


if __name__ == '__main__':
    unittest.main()

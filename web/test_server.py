"""HTTP regression tests; all data lives in temporary directories."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import web.process_control as process_module
import core.process_registry as registry_module
from web.server import make_server
from core.process_registry import process_is_alive
from web.process_control import popen_options
from web.process_control import runtime_python


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'scada.sqlite3'
        self.server = make_server('127.0.0.1', 0, db_path=self.db)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        if self.server.process_controller._thread:
            self.server.process_controller._thread.join(15)
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
        self.assertTrue(process_is_alive(os.getpid()))
        self.assertFalse(process_is_alive(2**31 - 1))

    def test_process_launch_options_are_platform_specific(self):
        with patch.object(registry_module.os, "name", "posix"):
            options = popen_options()
        self.assertEqual(options, {"start_new_session": True, "close_fds": True})

        with patch.object(registry_module.os, "name", "nt"), \
                patch.object(process_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200):
            options = popen_options()
        self.assertEqual(options, {"creationflags": 0x200, "close_fds": True})

    def test_child_runtime_prefers_project_virtual_environment(self):
        with patch.object(process_module, "ROOT", Path(self.temp.name)), \
                patch.object(process_module, "sys") as process_sys:
            process_sys.executable = "system-python"
            # No project venv in the temporary root: the Web interpreter is
            # the portable fallback.
            self.assertEqual(runtime_python(), "system-python")

        expected = Path(process_module.ROOT) / ".venv" / (
            "Scripts" if os.name == "nt" else "bin"
        ) / ("python.exe" if os.name == "nt" else "python")
        expected_runtime = str(expected.resolve()) if expected.is_file() else sys.executable
        self.assertEqual(runtime_python(), expected_runtime)

    def test_process_probe_posix_branch_uses_kill_zero(self):
        with patch.object(registry_module.os, "name", "posix"), \
                patch.object(registry_module.os, "kill") as kill:
            self.assertTrue(process_is_alive(1234))
            kill.assert_called_once_with(1234, 0)

    def test_catalog_and_empty_application(self):
        status, body = self.call('/api/schemas')
        self.assertEqual(status, 200)
        self.assertEqual(len(body['items']), 19)
        self.assertEqual(sum(len(s['columns']) for s in body['items']), 335)
        rtu_schema = next(s for s in body['items'] if s['head']['eng_name'] == 'fes_rtu_define')
        rtu_columns = {column['eng_name'] for column in rtu_schema['columns']}
        self.assertNotIn('proto_type', rtu_columns)
        self.assertNotIn('max_yc_num', rtu_columns)
        self.assertNotIn('max_yx_num', rtu_columns)
        self.assertNotIn('max_yt_num', rtu_columns)
        self.assertNotIn('max_yk_num', rtu_columns)
        self.assertNotIn('channel_status', rtu_columns)
        for removed in ('rdfid', 'st_id', 'oper_area', 'rtu_104code', 'chan_addr', 'chan_port'):
            self.assertNotIn(removed, rtu_columns)
        self.assertIn('status', rtu_columns)
        self.assertEqual(self.call('/')[0], 200)
        self.assertEqual(self.call('/api/health')[1]['status'], 'ok')
        self.assertEqual(self.call('/api/rtus')[1]['total'], 0)

    def test_removed_device_fields_are_absent_and_rejected(self):
        self.rtu()
        removed = [base + suffix for suffix in ('', '_1', '_2', '_3')
                   for base in ('value_dev', 'status_dev', 'refresh_time_dev')]
        for kind in ('yc', 'yx'):
            schema = self.call(f'/api/tables/fes_{kind}_define/schema')[1]['schema']
            self.assertTrue(set(removed).isdisjoint(c['eng_name'] for c in schema['columns']))
            base = f'/api/rtus/1/points/{kind}'
            self.assertEqual(self.call(base, 'POST', {'pnt_no': 1, 'value': 1})[0], 201)
            for field in removed:
                self.assertEqual(self.call(base + '/1', 'PATCH', {field: 5})[0], 400)
                self.assertEqual(self.call(base, 'POST', {'pnt_no': 2, field: 5})[0], 400)
            self.assertTrue(set(removed).isdisjoint(self.call(base + '/1')[1]['item']))

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
        self.assertEqual((rtu['process_enable'], rtu['process_status'], rtu['process_pid']), (1, 0, 0))
        self.assertEqual(self.call('/api/rtus/1', 'PATCH', {'process_enable': 2})[0], 400)
        for field, value in (('process_restart', 1), ('process_restart_time', 123), ('process_restart_count', 1)):
            self.assertEqual(self.call('/api/rtus/1', 'PATCH', {field: value})[0], 400)
        self.assertEqual(self.call('/api/rtus/1', 'PATCH', {
            'process_enable': 0,
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
            if name in {'fes_rtu_define', 'fes_process_define', 'fes_scdfe_c_define'}:
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

    def test_dedicated_channel_maintenance_and_id_allocation(self):
        self.rtu(8)
        current = self.call('/api/rtus/8')[1]['item']
        status, body = self.call('/api/rtus/8/channel', 'PATCH', {
            'channel_type': 'iec104',
        }, {'If-Match': str(current['_version'])})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['channel_type'], 'iec104')
        status, body = self.call('/api/rtus/8/channel/parameters', 'POST', {
            'rtu_addr': 8,
        })
        self.assertEqual(status, 201, body)
        self.assertGreater(body['item']['id'], 0)
        self.assertEqual(body['item']['rtu_id'], 8)
        self.assertEqual(self.call('/api/rtus/8/channel', 'PATCH', {
            'channel_type': 'modbus-tcp',
        })[0], 409)
        status, body = self.call('/api/rtus/8/channel/parameters', 'PATCH', {
            'call_alldata_time': 30,
        }, {'If-Match': str(body['item']['_version'])})
        self.assertEqual(status, 200, body)

    def test_channel_route_rejects_duplicate_and_unknown_fields(self):
        self.rtu(9)
        self.assertEqual(self.call('/api/rtus/9/channel', 'POST', {
            'channel_id': 90, 'channel_type': 'can',
        })[0], 409)
        self.assertEqual(self.call('/api/rtus/9/channel', 'PATCH', {
            'not_a_channel_field': 1,
        })[0], 400)

    def test_id_protocol_parameter_tables_allocate_internal_id(self):
        cases = [
            (10, 'bacnet', {'bind_ip': '0.0.0.0', 'bind_port': 47808, 'dev_id': 10}),
            (11, 'opc', {'opc_interval': 1000, 'opc_name': 'demo'}),
        ]
        for rtu_id, channel_type, values in cases:
            status, body = self.call('/api/rtus', 'POST', {
                'rtu_id': rtu_id, 'channel_id': rtu_id * 10,
                'channel_type': channel_type,
            })
            self.assertEqual(status, 201, body)
            status, body = self.call(f'/api/rtus/{rtu_id}/channel/parameters', 'POST', values)
            self.assertEqual(status, 201, body)
            self.assertIsInstance(body['item']['id'], int)
            self.assertEqual(body['item']['rtu_id'], rtu_id)
        self.call('/api/rtus', 'POST', {
            'rtu_id': 12, 'channel_id': 120, 'channel_type': 'opc',
        })
        self.assertEqual(self.call('/api/rtus/12/channel/parameters', 'POST', {'id': 999})[0], 400)

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
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'process_restart': 1})[0], 400)
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'worker_start_delay_ms': 60001})[0], 400)
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'log_retention_hours': 87601})[0], 400)
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'log_retention_hours': 720})[0], 200)
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'worker_start_delay_ms': 500})[0], 200)
        self.assertEqual(self.call('/api/scdfe_c/status')[1]['item']['process_pid'], 0)
        status_item = self.call('/api/scdfe_c/status')[1]['item']
        for field in ('process_restart', 'process_restart_time', 'process_restart_count'):
            self.assertNotIn(field, status_item)
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'process_enable': 0})[0], 202)
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
        debug = ScdfeLogger(self.db.parent / 'logs', process_name='scdfe_c-debug-rtu-7')
        debug.process('debug route test')
        status, body = self.call('/api/scdfe_c/logs?rtu_id=7')
        self.assertEqual((status, body['total']), (200, 1))
        self.assertEqual(body['items'][0]['process'], 'scdfe_c-debug-rtu-7')
        self.assertNotIn('channel_id', body['items'][0])
        status, body = self.call('/api/scdfe_c/logs/' + body['items'][0]['name'])
        self.assertEqual(status, 200)
        self.assertEqual(body['item']['rtu_id'], 7)
        self.assertIn('debug route test', body['item']['content'])
        self.assertNotRegex(body['item']['content'], r'(?:pid|rtu|channel|chanel)=\d+')
        self.assertNotIn('channel_id', body['item'])
        self.assertEqual(self.call('/api/scdfe_c/logs?channel_id=70')[0], 400)

    def test_main_and_writer_hourly_files_can_be_listed_and_searched(self):
        from datetime import datetime
        import scdfe_c as runtime

        for role in ('main', 'db-writer'):
            process = f'scdfe_c-{role}'
            logger = runtime.ScdfeLogger(self.db.parent / 'logs', process_name=process)
            with patch.object(runtime, 'datetime') as clock:
                for hour in (9, 10):
                    clock.now.return_value = datetime(2026, 11, 1, hour).astimezone()
                    logger.normal(f'{role} hourly summary {hour}')
            status, body = self.call('/api/scdfe_c/logs?process=' + process)
            self.assertEqual((status, body['total']), (200, 2))
            self.assertEqual({item['hour'] for item in body['items']}, {'20261101-09', '20261101-10'})
            for item in body['items']:
                status, result = self.call('/api/scdfe_c/logs/' + item['name'])
                self.assertEqual(status, 200)
                self.assertIn(f'{role} hourly summary', result['item']['content'])
            status, result = self.call('/api/scdfe_c/logs/search?level=INFO&q=summary&process=' + process)
            self.assertEqual(status, 200)
            self.assertEqual(len(result['items']), 2)

    def test_nested_log_files_can_be_listed_read_and_searched(self):
        from scdfe_c import ScdfeLogger
        logger = ScdfeLogger(self.db.parent / 'logs', process_name='scdfe_c-db-writer')
        logger.normal('nested web log')
        status, body = self.call('/api/scdfe_c/logs?process=scdfe_c-db-writer')
        self.assertEqual(status, 200)
        filename = body['items'][0]['name']
        self.assertGreaterEqual(filename.count('/'), 4)
        status, result = self.call('/api/scdfe_c/logs/' + filename)
        self.assertEqual(status, 200)
        self.assertIn('nested web log', result['item']['content'])
        status, result = self.call('/api/scdfe_c/logs/search?process=scdfe_c-db-writer&level=INFO&q=nested')
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result['items']), 1)

    def test_scdfe_status_pagination_and_filters_include_all_rtus(self):
        with self.server.store.connection() as connection:
            connection.executemany(
                "INSERT INTO fes_rtu_define (rtu_id, channel_id, channel_type, name, "
                "process_status, process_enable) VALUES (?, ?, 'iec104', ?, ?, ?)",
                [(n, n, f'测试站 {n}', 5 if n % 2 else 2, n % 2) for n in range(1, 506)],
            )
            connection.commit()
        status, body = self.call('/api/scdfe_c/status?limit=5&offset=500')
        self.assertEqual(status, 200)
        self.assertEqual(body['total'], 505)
        self.assertEqual([row['rtu_id'] for row in body['rtus']], list(range(501, 506)))
        self.assertEqual(body['counts'], {'total': 505, 'running': 252, 'errors': 253, 'automatic': 253})
        self.assertIn('_version', body['rtus'][0])
        status, body = self.call('/api/scdfe_c/status?process_status=5&process_enable=1&q=505')
        self.assertEqual((status, body['total'], body['rtus'][0]['rtu_id']), (200, 1, 505))
        for query in ('limit=501', 'offset=-1', 'limit=no', 'process_status=bad', 'unknown=1'):
            self.assertEqual(self.call('/api/scdfe_c/status?' + query)[0], 400)

    def test_log_search_combines_keyword_time_type_and_cursor(self):
        from scdfe_c import ScdfeLogger
        logger = ScdfeLogger(self.db.parent / 'logs', process_name='scdfe_c-main')
        logger.normal('电压正常 keyword-a')
        logger.alarm('链路告警 keyword-a')
        logger.error('连接错误 keyword-a')
        logger.process('通信重载 keyword-a')
        base = '/api/scdfe_c/logs/search?q=keyword-a&level=ERROR&process=scdfe_c-main&start=2020-01-01T00%3A00%3A00%2B08%3A00&end=2030-01-01T00%3A00%3A00%2B08%3A00&limit=1'
        status, body = self.call(base)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body['items']), 1)
        self.assertEqual(body['items'][0]['level'], 'ERROR')
        self.assertTrue(body['has_more'])
        status, body = self.call(base + '&cursor=' + body['next_cursor'])
        self.assertEqual((status, body['items'], body['has_more']), (200, [], False))
        for query in ('level=BAD', 'start=2030-01-01T00%3A00%3A00%2B08%3A00&end=2020-01-01T00%3A00%3A00%2B08%3A00', 'q=' + 'x' * 300):
            self.assertEqual(self.call('/api/scdfe_c/logs/search?' + query)[0], 400)

    def test_scdfe_http_rejects_server_owner_without_changing_control(self):
        from core.scdfe_control import ServerInstanceLock

        before = self.call('/api/scdfe_c/status')[1]['item']
        lock = ServerInstanceLock()
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        with patch.object(process_module.subprocess, 'Popen') as spawn:
            status, body = self.call('/api/scdfe_c/control', 'PATCH', {'process_enable': 1})
            self.assertEqual(status, 409, body)
            self.assertEqual(body['error']['code'], 'process_already_running')
            spawn.assert_not_called()
        self.assertEqual(self.call('/api/scdfe_c/status')[1]['item'], before)

    def test_scdfe_http_start_and_cooperative_stop(self):
        from core.scdfe_control import CONTROL_TABLE
        self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'worker_start_delay_ms': 0, 'scan_interval_ms': 100})[0], 200)
        def wait_operation():
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                result = self.call('/api/processes')[1]
                if not result['busy']:
                    self.assertEqual(result['operation']['status'], 'completed', result)
                    return result
                time.sleep(0.1)
            self.fail('process operation timed out')
        try:
            self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'process_enable': 1})[0], 202)
            wait_operation()
            running = self.call('/api/scdfe_c/status')[1]['item']
            self.assertEqual(running['process_status'], 2)
            self.assertGreater(running['process_pid'], 0)
            self.assertEqual(self.call('/api/scdfe_c/control', 'PATCH', {'process_enable': 0})[0], 202)
            wait_operation()
            stopped = self.call('/api/scdfe_c/status')[1]['item']
            self.assertEqual((stopped['process_status'], stopped['process_pid']), (0, 0))
        finally:
            self.server.store.update_row(CONTROL_TABLE, [1], {'process_enable': 0})
            thread = self.server.process_controller._thread
            if thread:
                thread.join(12)

    def test_process_routes_and_runtime_tables_are_read_only(self):
        status, body = self.call('/api/processes')
        self.assertEqual(status, 200)
        self.assertEqual([r['process_key'] for r in body['items']], ['main', 'writer'])
        self.assertNotIn('process_restart_count', body['items'][0])
        self.assertNotIn('owner_token', body['items'][0])
        for table in ('fes_process_define', 'fes_scdfe_c_define'):
            path = f'/api/tables/{table}/rows'
            self.assertEqual(self.call(path, 'POST', {})[0], 405)
            key = 'main' if table == 'fes_process_define' else '1'
            self.assertEqual(self.call(path + '/' + key, 'PATCH', {'process_pid': 1})[0], 405)
            self.assertEqual(self.call(path + '/' + key, 'DELETE')[0], 405)
        for body in ({}, {'target': 'bad', 'action': 'start'}, {'target': 'main', 'action': []},
                     {'target': 'all', 'action': 'configure'}, {'target': 'writer', 'action': 'start', 'unknown': 1}):
            self.assertEqual(self.call('/api/processes/control', 'POST', body)[0], 400, body)


if __name__ == '__main__':
    unittest.main()

"""S services stop only on control actions and retain the action's origin."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from scdfe_s.web.lifecycle import controlled_call, lifecycle_cause, stop_cause
from scdfe_s.web.server import make_server


class LifecycleContextTests(unittest.TestCase):
    def handler(self, path):
        return SimpleNamespace(client_address=('127.0.0.1', 1234), command='POST', path=path,
            headers={'Referer': 'http://user:password@localhost/test?token=secret'})

    def test_concurrent_requests_keep_separate_causes_and_strip_url_secrets(self):
        barrier = threading.Barrier(2)
        def run(rid):
            service = SimpleNamespace(_record=Mock())
            def operation():
                barrier.wait(timeout=2)
                with lifecycle_cause('运行参数变更'):
                    return stop_cause()
            reason = controlled_call(self.handler(f'/api/rtus/{rid}/service/stop'),
                                     service, 'stop', operation)
            self.assertNotIn('HTTP', stop_cause())
            self.assertNotIn('secret', str(service._record.call_args_list))
            self.assertNotIn('password', reason)
            self.assertIn('来源页面=/test', reason)
            self.assertIn('运行参数变更', reason)
            return reason
        with ThreadPoolExecutor(2) as pool:
            first, second = list(pool.map(run, (8, 9)))
        self.assertIn('/rtus/8/', first)
        self.assertNotIn('/rtus/9/', first)
        self.assertIn('/rtus/9/', second)
        self.assertNotIn('/rtus/8/', second)

    def test_failed_operation_is_recorded_and_context_is_released(self):
        service = SimpleNamespace(_record=Mock())
        with self.assertRaisesRegex(RuntimeError, 'stop failed'):
            controlled_call(self.handler('/api/iec104/stop'), service, 'stop',
                            Mock(side_effect=RuntimeError('stop failed')))
        self.assertEqual(service._record.call_args.args[0], 'ERROR')
        self.assertIn('服务控制失败', service._record.call_args.args[1])
        self.assertNotIn('HTTP', stop_cause())


class ServiceStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.web = make_server('127.0.0.1', 0, db_path=self.root / 'scdfe_s.sqlite3')
        self.thread = threading.Thread(target=self.web.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_web)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.web.store.create_row('fes_channel_iec104_define', {
            'id': 1, 'channel_id': 8, 'remote_host': '127.0.0.1',
            'remote_port': self.port, 'rtu_addr': 1,
        })
        self.web.store.create_row('fes_rtu_define', {
            'rtu_id': 9, 'channel_id': 8, 'channel_type': 'iec104', 'name': '独立生命周期测试站',
        })

    def close_web(self):
        self.web.shutdown()
        self.thread.join(3)
        self.web.server_close()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.web.server_port, timeout=5)
        headers = {'Content-Type': 'application/json',
                   'Referer': 'http://127.0.0.1/test?rtu_id=9&token=secret'}
        connection.request(method, path, None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        self.last_trace = response.getheader('X-Scada-Trace')
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200, payload)
        return payload

    def channel_log(self):
        return '\n'.join(p.read_text(encoding='utf-8')
                         for p in (self.root / 'logs').rglob('scdfe_s-channel-8-*.log'))

    def test_client_disconnect_and_monitor_poll_do_not_stop_service_after_ten_seconds(self):
        self.request('POST', '/api/rtus/9/service/start', {})
        service = self.web.channel_services(rtu_id=9)['iec104-rtu-9']
        for _ in range(2):
            with socket.create_connection(('127.0.0.1', self.port), timeout=2):
                pass
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            result = self.request('GET', '/api/monitor/communications?rtu_id=9')
            self.assertTrue(result['items'][0]['running'])
            self.assertIs(self.web.channel_services(rtu_id=9)['iec104-rtu-9'], service)
            time.sleep(.4)
        self.assertNotIn('服务已停止', self.channel_log())
        self.request('POST', '/api/rtus/9/service/stop', {})
        self.assertFalse(service.running)
        line = next(line for line in self.channel_log().splitlines() if '服务已停止' in line)
        self.assertIn('操作ID=' + self.last_trace, line)
        for value in ('停止原因=HTTP', '来源IP=127.0.0.1', '来源页面=/test',
                      '请求=POST /api/rtus/9/service/stop', 'RTU编号=9', '通道编号=8',
                      f'监听=127.0.0.1:{self.port}', '操作ID='):
            self.assertIn(value, line)
        self.assertNotIn('secret', self.channel_log())

    def test_monitor_control_and_resource_cleanup_have_distinct_causes(self):
        self.request('POST', '/api/monitor/communications/iec104-rtu-9/start', {})
        self.request('POST', '/api/monitor/communications/iec104-rtu-9/stop', {})
        self.assertIn('/api/monitor/communications/iec104-rtu-9/stop', self.channel_log())
        self.request('POST', '/api/rtus/9/service/start', {})
        service = self.web.channel_services(rtu_id=9)['iec104-rtu-9']
        service.close()
        self.assertIn('停止原因=Web服务关闭或资源释放', self.channel_log())

    def test_protocol_config_reload_retains_request_origin_and_restarts_service(self):
        self.web.iec104_service.configure({'rtu_id': 9, 'port': self.port})
        self.request('POST', '/api/iec104/start', {})
        self.request('PATCH', '/api/iec104/config', {'t3': 25})
        self.assertTrue(self.web.iec104_service.running)
        text = self.channel_log()
        self.assertIn('停止原因=运行参数变更，停止旧实例后重载', text)
        self.assertIn('请求=PATCH /api/iec104/config', text)
        self.request('POST', '/api/iec104/stop', {})
        self.assertIn('请求=POST /api/iec104/stop', self.channel_log())

    def test_modbus_protocol_api_records_stop_origin_too(self):
        self.web.modbus_tcp_service.configure({'mode': 'server', 'listen_port': self.port})
        self.request('POST', '/api/modbus-tcp/start', {})
        self.request('POST', '/api/modbus-tcp/stop', {})
        records = self.web.modbus_tcp_service.logs()
        message = next(item['message'] for item in records if '服务已停止' in item['message'])
        self.assertIn('停止原因=HTTP 服务控制请求', message)
        self.assertIn('请求=POST /api/modbus-tcp/stop', message)
        self.assertIn('来源页面=/test', message)


if __name__ == '__main__':
    unittest.main()

"""Communication lifecycle messages retain enough context for the main log UI."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import scdfe_c as runtime
from scdfe_c.web.log_query import search_logs
from scdfe_c.web.service import ScadaStore


class CommunicationLifecycleLogsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ScadaStore(self.root / 'scada.sqlite3')
        self.addCleanup(self.store.close)
        self.logs = self.root / 'logs'
        self.store.create_row('fes_channel_modbus_tcp_define', {
            'channel_id': 70, 'name': '测试通道', 'remote_host': '127.0.0.1',
            'remote_port': 1502, 'unit_id': 3,
        })
        self.store.create_row('fes_rtu_define', {
            'rtu_id': 7, 'channel_id': 70, 'channel_type': 'modbus-tcp', 'name': '测试站',
        })
        self.spec = runtime.ChannelSpec(7, 70, 'modbus-tcp', '测试站')

    def content(self, process):
        return ''.join(p.read_text(encoding='utf-8') for p in self.logs.rglob(process + '-*.log'))

    def test_main_start_stop_and_search_identify_rtu_channel_and_old_endpoint(self):
        thread = Mock(native_id=12345, ident=12345)
        thread.is_alive.return_value = True
        supervisor = runtime.ScdfeSupervisor(
            self.store.db_path, log_dir=self.logs, thread_factory=Mock(return_value=thread),
        )
        self.addCleanup(supervisor.store.close)
        lock = Mock()
        self.assertTrue(supervisor._start_worker(self.spec, channel_lock=lock))
        # A channel edit must not relabel the old thread's shutdown destination.
        self.store.update_row('fes_channel_modbus_tcp_define', [70], {'remote_port': 1503})
        thread.is_alive.return_value = False
        self.assertTrue(supervisor._stop_worker(self.spec.key, reason='配置已修改，重新加载'))
        lock.release.assert_called_once()
        content = self.content('scdfe_c-main')
        for event in ('启动通信线程', '停止通信线程', '通信线程已停止'):
            line = next(line for line in content.splitlines() if event in line)
            for expected in ('RTU编号=7', '测试站', '通道类型=modbus-tcp', '通道编号=70',
                             '127.0.0.1:1502', '站号=3', '线程ID=12345'):
                self.assertIn(expected, line)
        self.assertNotIn('1503', content)
        result = search_logs(self.logs, {'q': 'RTU编号=7', 'limit': '50'}, family='c')
        self.assertEqual(len(result['items']), 3)
        self.assertTrue(all('通道编号=70' in row['text'] for row in result['items']))

    def test_start_failure_and_stop_timeout_identify_owner(self):
        thread = Mock(native_id=321, ident=321)
        thread.start.side_effect = RuntimeError('cannot start thread')
        supervisor = runtime.ScdfeSupervisor(
            self.store.db_path, log_dir=self.logs, thread_factory=Mock(return_value=thread),
        )
        self.addCleanup(supervisor.store.close)
        failed_lock = Mock()
        self.assertFalse(supervisor._start_worker(self.spec, channel_lock=failed_lock))
        failed_lock.release.assert_called_once()
        thread.start.side_effect = None
        thread.is_alive.return_value = True
        lock = Mock()
        self.assertTrue(supervisor._start_worker(self.spec, channel_lock=lock, recovering=True))
        self.assertFalse(supervisor._stop_worker(self.spec.key, reason='停止测试'))
        lock.release.assert_not_called()
        content = self.content('scdfe_c-main')
        for line in content.splitlines():
            self.assertIn('RTU编号=7', line)
            self.assertIn('通道类型=modbus-tcp', line)
        self.assertIn('启动原因=异常恢复', content)
        self.assertIn('通信线程停止超时', content)

    def test_main_log_distinguishes_same_channel_number_in_different_protocols(self):
        self.store.create_row('fes_channel_iec104_define', {
            'id': 1, 'channel_id': 70, 'remote_host': '127.0.0.2', 'remote_port': 2404,
        })
        self.store.create_row('fes_rtu_define', {
            'rtu_id': 8, 'channel_id': 70, 'channel_type': 'iec104', 'name': '104站',
        })
        threads = [Mock(native_id=100, ident=100), Mock(native_id=200, ident=200)]
        supervisor = runtime.ScdfeSupervisor(
            self.store.db_path, log_dir=self.logs, thread_factory=Mock(side_effect=threads),
            worker_start_delay=0,
        )
        self.addCleanup(supervisor.store.close)
        for spec in (self.spec, runtime.ChannelSpec(8, 70, 'iec104', '104站')):
            self.assertTrue(supervisor._start_worker(spec, channel_lock=Mock()))
        lines = self.content('scdfe_c-main').splitlines()
        self.assertEqual(len(lines), 2)
        for expected in ('RTU编号=7', '通道类型=modbus-tcp', '目标=127.0.0.1:1502', '线程ID=100'):
            self.assertIn(expected, lines[0])
        for expected in ('RTU编号=8', '通道类型=iec104', '目标=127.0.0.2:2404', '线程ID=200'):
            self.assertIn(expected, lines[1])

    def test_worker_retries_record_stage_attempt_and_elapsed_without_poll_queries(self):
        failing = Mock()
        failing.connect.side_effect = TimeoutError('连接超时\n无法到达对端')
        # Avoid Mock inventing optional adapter methods/properties.
        class Master:
            def connect(self): pass
            def collect_once(self): return []
            def close(self): pass

        worker = runtime.ChannelWorker(
            self.store, self.spec, log_dir=self.logs, poll_interval=0, retry_interval=.001,
            master_factory=Mock(side_effect=[failing, Master()]),
        )
        from scdfe_c.log_context import channel_context
        with patch.object(runtime, 'channel_context', wraps=channel_context) as context:
            stats = worker.run(max_cycles=2)
        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.cycles, 2)
        self.assertEqual(context.call_count, 1)
        content = self.content('scdfe_c-rtu-7')
        for expected in ('RTU编号=7', '目标=127.0.0.1:1502', '阶段=建立连接',
                         'TimeoutError', '连接尝试=2', '连续失败=1', '重试间隔=0.001s',
                         '连接耗时=', '停止原因=达到指定采集周期数'):
            self.assertIn(expected, content)
        self.assertTrue(all(line[:2].isdigit() for line in content.splitlines()))
        failing.close.assert_called_once()

    def test_collection_failure_is_not_reported_as_connection_failure(self):
        stop = threading.Event()
        class Master:
            def connect(self): pass
            def close(self): pass
            def collect_once(self):
                stop.set()
                raise ValueError('非法响应')
        worker = runtime.ChannelWorker(
            self.store, self.spec, log_dir=self.logs, stop_event=stop,
            master_factory=lambda *_: Master(), retry_interval=.001,
        )
        worker.run()
        content = self.content('scdfe_c-rtu-7')
        self.assertIn('阶段=采集遥测/遥信', content)
        self.assertIn('异常=ValueError: 非法响应', content)
        self.assertNotIn('阶段=建立连接', content)

    def test_protocol_context_is_specific_and_does_not_expose_opc_credentials(self):
        from scdfe_c.log_context import channel_context
        cases = [
            ('iec104', {'id': 1, 'remote_host': '::1', 'remote_port': 2404, 'rtu_addr': 2},
             ('目标=[::1]:2404', '公共地址CA=2')),
            ('modbus-rtu', {'serial_port': 'COM8', 'baudrate': 19200, 'bytesize': 8,
                            'parity': 'E', 'stopbits': 1, 'unit_id': 4},
             ('串口=COM8', '串口参数=19200/8E1', '站号=4')),
            ('can', {'interface': 'virtual', 'channel': 'can0', 'bitrate': 500000,
                     'data_bitrate': 2000000, 'fd': 1},
             ('CAN接口=virtual/can0', '仲裁波特率=500000', '数据波特率=2000000', 'CAN-FD=1')),
            ('opc', {'id': 1, 'opc_name': 'opc.tcp://user:secret@localhost:4840/path?token=secret',
                     'opc_user': 'user', 'opc_pwd': 'secret'},
             ('端点=opc.tcp://localhost:4840',)),
            ('bacnet', {'id': 1, 'remote_host': '127.0.0.2', 'remote_port': 47808,
                        'bind_ip': '0.0.0.0', 'bind_port': 47809, 'dev_id': 22},
             ('目标=127.0.0.2:47808', '本端=0.0.0.0:47809', '设备ID=22')),
        ]
        for kind, values, expected in cases:
            with self.subTest(protocol=kind):
                self.store.create_row('fes_channel_' + kind.replace('-', '_') + '_define',
                                      {'channel_id': 70, **values})
                context = channel_context(self.store, runtime.ChannelSpec(7, 70, kind, '测试站'))
                self.assertIn('通道类型=' + kind, context)
                for part in expected:
                    self.assertIn(part, context)
                self.assertNotIn('secret', context)
                self.assertNotIn('user:', context)

    def test_missing_configuration_keeps_identity_and_does_not_block_logging(self):
        from scdfe_c.log_context import channel_context
        context = channel_context(self.store, runtime.ChannelSpec(8, 80, 'iec104', '未配置站'))
        self.assertIn('RTU编号=8', context)
        self.assertIn('通道编号=80', context)
        self.assertIn('连接参数=未配置', context)
        with patch.object(self.store, 'connection', side_effect=RuntimeError('database unavailable')):
            context = channel_context(self.store, self.spec)
        self.assertIn('RTU编号=7', context)
        self.assertIn('连接参数=读取失败(RuntimeError)', context)


if __name__ == '__main__':
    unittest.main()

"""Regressions for command-process lifetime and acquisition recovery."""
from pathlib import Path
from queue import Queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import scdfe_c as runtime
from core.process_registry import PROCESS_TABLE
from scdfe_c.command_server import COMMAND_TABLE, run_process
from scdfe_c.web.service import ScadaStore


class ReviewRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ScadaStore(self.root / 'review.sqlite3')

    def supervisor(self):
        return runtime.ScdfeSupervisor(self.store.db_path, log_dir=self.root / 'logs',
                                       retry_interval=.01)

    def test_enabled_command_process_stays_alive_until_disabled(self):
        self.store.create_row(COMMAND_TABLE, {
            'id': 1, 'bind_host': '127.0.0.1', 'bind_port': 0,
            'auth_token': 'review-test-only', 'process_enable': 1,
        })
        event = threading.Event()
        results = []
        thread = threading.Thread(target=lambda: results.append(run_process(
            self.store.db_path, self.store.catalog.directory, stop_event=event,
        )))
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self.store.get_row(COMMAND_TABLE, [1])['process_status'] == 2:
                    break
                time.sleep(.01)
            thread.join(1.2)
            self.assertTrue(thread.is_alive(), 'enabled command service exited by itself')
            self.assertEqual(self.store.get_row(PROCESS_TABLE, ['command-server'])['process_enable'], 1)
            self.store.update_row(COMMAND_TABLE, [1], {'process_enable': 0})
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [0])
        finally:
            event.set()
            thread.join(3)

    def test_failed_connection_exits_before_retrying_old_protocol(self):
        self.store.create_row('fes_rtu_define', {
            'rtu_id': 27101, 'channel_id': 710, 'channel_type': 'modbus-tcp',
        })
        event = threading.Event()
        calls = []

        def factory(store, spec):
            calls.append(spec.channel_type)
            def connect():
                if len(calls) == 1:
                    store.update_row('fes_rtu_define', [27101], {'channel_type': 'iec104'})
                else:
                    event.set()  # Bound execution on the unfixed implementation.
                raise ConnectionError('offline test device')
            return SimpleNamespace(connect=connect, close=lambda: None)

        worker = runtime.ChannelWorker(
            self.store, runtime.ChannelSpec(27101, 710, 'modbus-tcp'),
            log_dir=self.root / 'logs', retry_interval=.01,
            master_factory=factory, stop_event=event,
        )
        worker.run()
        self.assertEqual(calls, ['modbus-tcp'])
        self.assertFalse(event.is_set(), 'worker should request a definition reload')

    def test_supervisor_replaces_live_worker_when_protocol_changes(self):
        self.store.create_row('fes_rtu_define', {
            'rtu_id': 27101, 'channel_id': 710, 'channel_type': 'iec104',
        })
        supervisor = self.supervisor()
        old_spec = runtime.ChannelSpec(27101, 710, 'modbus-tcp')
        supervisor._workers[old_spec.key] = SimpleNamespace(
            spec=old_spec, thread=SimpleNamespace(is_alive=lambda: True),
        )
        def stop(key, **kwargs):
            supervisor._workers.pop(key)
            return True
        with patch.object(supervisor, '_start_writer'), \
                patch.object(supervisor, '_apply_control_config', return_value=False), \
                patch.object(supervisor, '_stop_worker', side_effect=stop) as stopped, \
                patch.object(supervisor, '_start_worker', return_value=True) as started:
            result = supervisor.reconcile()
        stopped.assert_called_once()
        self.assertEqual(started.call_args.args[0].channel_type, 'iec104')
        self.assertEqual(result['reloaded'], 1)

    def test_dead_or_timed_out_writer_never_creates_join_waiter_threads(self):
        supervisor = self.supervisor()
        queue = Queue()
        queue.put('unacknowledged')
        supervisor._writer_queue = queue
        supervisor._writer_drain_ack = runtime.mp.get_context('spawn').Value('Q', 0)
        supervisor._writer_process = SimpleNamespace(is_alive=lambda: False)
        before = {t.ident for t in threading.enumerate()}
        try:
            for _ in range(3):
                self.assertFalse(supervisor._drain_writer(timeout=.03))
            supervisor._writer_process = SimpleNamespace(is_alive=lambda: True)
            for _ in range(2):
                self.assertFalse(supervisor._drain_writer(timeout=.03))
            leaked = [t for t in threading.enumerate() if t.ident not in before]
            self.assertEqual(leaked, [])
        finally:
            # Release any waiters created by the unfixed code.
            while True:
                try:
                    queue.get_nowait()
                except runtime.Empty:
                    break
                queue.task_done()

    def test_drain_waits_for_current_barrier_not_an_old_ack(self):
        supervisor = self.supervisor()
        ack = runtime.mp.get_context('spawn').Value('Q', 0)
        messages = []
        def put(message, timeout=None):
            messages.append(message)
            ack.value = message[1] - 1
        supervisor._writer_queue = SimpleNamespace(put=put)
        supervisor._writer_process = SimpleNamespace(is_alive=lambda: True)
        supervisor._writer_drain_ack = ack
        self.assertFalse(supervisor._drain_writer(timeout=.02))
        self.assertFalse(supervisor._drain_writer(timeout=.02))
        self.assertEqual([m[1] for m in messages], [1, 2])

    def test_drain_barrier_acknowledges_committed_data_in_real_writer_process(self):
        self.store.create_row('fes_rtu_define', {
            'rtu_id': 27101, 'channel_id': 710, 'channel_type': 'custom',
        })
        self.store.create_row('fes_yc_define', {'rtu_id': 27101, 'pnt_no': 1, 'value': 0})
        supervisor = self.supervisor()
        try:
            supervisor._start_writer()
            supervisor._writer_queue.put({'op': 'batch', 'groups': [{
                'table': 'fes_yc_define', 'key_names': ('rtu_id', 'pnt_no'),
                'value_names': ('value',), 'rows': [(42.5, 27101, 1)],
            }]})
            self.assertTrue(supervisor._drain_writer(timeout=3))
            self.assertEqual(self.store.get_row('fes_yc_define', [27101, 1])['value'], 42.5)
            # _stop_writer is a bounded poll: a slow process/OS teardown is
            # retained for the next supervisor scan, not forcibly terminated.
            # The drain/commit assertions above do not promise process exit
            # within a single two-second poll.
            deadline = time.monotonic() + 10
            while not supervisor._stop_writer():
                self.assertLess(time.monotonic(), deadline, 'writer did not finish cooperative shutdown')
        finally:
            supervisor._stop_writer()

    def test_writer_stop_retains_slow_process_and_sends_only_one_stop(self):
        supervisor = self.supervisor()
        process = Mock(exitcode=0)
        process.is_alive.return_value = True
        queue = Mock()
        supervisor._writer_process = process
        supervisor._writer_queue = queue
        self.assertFalse(supervisor._stop_writer())
        self.assertFalse(supervisor._stop_writer())
        self.assertIs(supervisor._writer_process, process)
        self.assertIs(supervisor._writer_queue, queue)
        queue.put.assert_called_once()
        self.assertEqual(queue.put.call_args.args, (runtime.BATCH_WRITER_STOP,))
        queue.close.assert_not_called()
        process.is_alive.return_value = False
        self.assertTrue(supervisor._stop_writer())
        self.assertIsNone(supervisor._writer_process)
        queue.close.assert_called_once()
        queue.join_thread.assert_called_once()
        process.close.assert_called_once()

    def test_writer_death_during_drain_returns_without_background_waiters(self):
        supervisor = self.supervisor()
        alive = iter([True, False])
        supervisor._writer_process = SimpleNamespace(is_alive=lambda: next(alive))
        supervisor._writer_queue = Queue()
        supervisor._writer_drain_ack = runtime.mp.get_context('spawn').Value('Q', 0)
        before = {t.ident for t in threading.enumerate()}
        self.assertFalse(supervisor._drain_writer(timeout=1))
        self.assertEqual({t.ident for t in threading.enumerate()}, before)

    def test_stuck_ack_lock_cannot_exceed_drain_timeout(self):
        supervisor = self.supervisor()
        ack = runtime.mp.get_context('spawn').Value('Q', 0)
        supervisor._writer_drain_ack = ack
        supervisor._writer_queue = Queue()
        supervisor._writer_process = SimpleNamespace(is_alive=lambda: True)
        ready, release = threading.Event(), threading.Event()
        def hold_lock():
            with ack.get_lock():
                ready.set()
                release.wait(2)
        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            self.assertTrue(ready.wait(1))
            started = time.monotonic()
            self.assertFalse(supervisor._drain_writer(timeout=.03))
            self.assertLess(time.monotonic() - started, .5)
        finally:
            release.set()
            holder.join(1)

    def test_invalid_yx_preserves_persisted_and_cached_value(self):
        self.store.create_row('fes_rtu_define', {
            'rtu_id': 27101, 'channel_id': 710, 'channel_type': 'iec104',
        })
        self.store.create_row('fes_yx_define', {
            'rtu_id': 27101, 'pnt_no': 1, 'value': 1,
        })
        worker = runtime.ChannelWorker(self.store, runtime.ChannelSpec(27101, 710, 'iec104'),
                                       log_dir=self.root / 'logs')
        self.addCleanup(worker._close_command_connection)
        for value, quality, expected in [(None, 128, 1), (False, 0, 0), (None, 128, 0)]:
            worker._sync_measurements([SimpleNamespace(kind='yx', point_no=1,
                                                       value=value, quality=quality)])
            row = self.store.get_row('fes_yx_define', [27101, 1])
            self.assertEqual((row['value'], row['status']), (expected, quality))


if __name__ == '__main__':
    unittest.main()

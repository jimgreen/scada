"""python-can virtual 总线验证应用主站，不访问硬件 CAN 驱动。"""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from pathlib import Path
import tempfile

from api import CanMaster
from api.can import CanError, CanTimeout
from web.service import ScadaStore


class CanMasterTests(unittest.TestCase):
    def setUp(self):
        self.channel = 'scada-master-' + uuid4().hex
        self.master = CanMaster(interface='virtual', channel=self.channel)
        self.addCleanup(self.master.close)
        self.peer = CanMaster(interface='virtual', channel=self.channel, auto_start=True)
        self.addCleanup(self.peer.close)
        self.master.connect()

    def test_request_matcher_and_unmatched_frames_preserved(self):
        def respond():
            request = self.peer.recv(1)
            self.assertEqual((request.arbitration_id, request.data), (0x100, b'\x02'))
            self.peer.send(0x222, b'other')
            self.peer.send(0x180, b'\x02', is_extended_id=True)
            self.peer.send(0x180, b'\x01')
            self.peer.send(0x180, b'\x02\xff')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(respond)
            result = self.master.request(0x100, b'\x02', response_id=0x180,
                                         matcher=lambda frame: frame.data[:1] == b'\x02')
            future.result(timeout=2)
        self.assertEqual(result.data, b'\x02\xff')
        saved = self.master.collect_once(timeout=0, max_frames=10)
        self.assertEqual([frame.data for frame in saved], [b'other', b'\x02', b'\x01'])
        self.assertTrue(saved[1].is_extended_id)

    def test_request_timeout_sends_only_once(self):
        started = time.monotonic()
        with self.assertRaises(CanTimeout):
            self.master.request(0x100, response_id=0x180, timeout=0.03)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(list(self.peer.receive(timeout=0.01))), 1)
        with self.assertRaises(ValueError):
            self.master.request(0x100, response_id=0x800)
        self.assertIsNone(self.peer.recv(0))

    def test_matcher_error_preserves_frame(self):
        def respond():
            self.peer.recv(1)
            self.peer.send(0x180, b'bad')
        def fail(_):
            raise RuntimeError('bad matcher')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(respond)
            with self.assertRaisesRegex(RuntimeError, 'bad matcher'):
                self.master.request(0x100, response_id=0x180, matcher=fail)
            future.result(timeout=2)
        self.assertEqual(self.master.recv(0).data, b'bad')

    def test_fd_extended_remote_dlc_and_validation(self):
        sent = self.master.send(0x12345, bytes(range(12)), is_extended_id=True,
                                is_fd=True, bitrate_switch=True)
        received = self.peer.recv(0.1)
        self.assertEqual(received.data, sent.data)
        self.assertTrue(received.is_fd and received.bitrate_switch)
        self.master.send(0x100, is_remote_frame=True, dlc=8)
        remote = self.peer.recv(0.1)
        self.assertTrue(remote.is_remote_frame)
        self.assertEqual(remote.dlc, 8)
        for kwargs in ({'is_fd': True, 'is_remote_frame': True},
                       {'is_fd': False, 'bitrate_switch': True}, {'dlc': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.master.send(0x100, **kwargs)
        self.assertIsNone(self.peer.recv(0))

    def test_filters_survive_reconnect_and_batches_are_bounded(self):
        self.master.set_filters([{'can_id': 0x180, 'can_mask': 0x7ff}])
        self.master.close()
        self.master.connect()
        self.peer.send(0x100, b'filtered')
        for i in range(3):
            self.peer.send(0x180, bytes([i]))
        self.assertEqual([f.data for f in self.master.collect_once(timeout=0.1, max_frames=2)],
                         [b'\x00', b'\x01'])
        self.assertEqual(self.master.recv(0.1).data, b'\x02')
        self.assertIsNone(self.master.recv(0))

    def test_periodic_send_and_stop(self):
        task = self.master.send_periodic(0x101, b'\x01', period=0.01)
        try:
            frames = list(self.peer.receive(timeout=1, count=3))
            self.assertEqual(len(frames), 3)
            self.assertTrue(all(f.arbitration_id == 0x101 for f in frames))
        finally:
            task.stop()
        while self.peer.recv(0) is not None:
            pass
        self.assertIsNone(self.peer.recv(0.03))

    def test_poll_stop_callback_error_and_lifecycle(self):
        self.peer.send(0x120, b'one')
        stop = threading.Event()
        batches = []
        def on_frames(frames):
            batches.append(frames)
            stop.set()
        self.assertEqual(self.master.poll_loop(on_frames, stop_event=stop, interval=30,
                                               timeout=0.01), 1)
        self.assertEqual(batches[0][0].data, b'one')
        self.assertEqual(self.master.poll_loop(on_frames, stop_event=stop), 0)
        self.assertEqual(self.master.poll_loop(batches.append, timeout=0.001, max_cycles=2), 2)
        def fail(_):
            raise RuntimeError('callback failed')
        with self.assertRaisesRegex(RuntimeError, 'callback failed'):
            self.master.poll_loop(fail, timeout=0.001)
        self.master.close()
        with self.assertRaises(CanError):
            self.master.collect_once(timeout=0)
        with self.assertRaises(CanError):
            self.master.send_periodic(0x100)

    def test_invalid_poll_parameters(self):
        for kwargs in ({'timeout': float('nan')}, {'interval': float('inf')},
                       {'timeout': 0, 'interval': 0}, {'max_frames': 0}, {'max_cycles': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.master.poll_loop(print, **kwargs)


class CanMasterConstructionTests(unittest.TestCase):
    def test_channel_constructor_defers_opening_hardware(self):
        with patch('api.can.can.Bus') as bus:
            master = CanMaster.from_channel({'rtu_id': 7, 'channel': 'PCAN_USBBUS1'},
                                            interface='pcan', bitrate=250000)
            self.assertEqual(master.rtu_id, 7)
            self.assertFalse(master.connected)
            bus.assert_not_called()
            with master:
                bus.assert_called_once()
                self.assertEqual(bus.call_args.kwargs['channel'], 'PCAN_USBBUS1')
                self.assertEqual(bus.call_args.kwargs['bitrate'], 250000)
            bus.return_value.shutdown.assert_called_once()

    def test_store_constructor_uses_can_parameter_table(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ScadaStore(Path(directory) / 'scada.sqlite3')
            store.create_row('fes_rtu_define', {
                'rtu_id': 7, 'channel_id': 70, 'channel_type': 'can',
                            })
            store.create_row('fes_channel_can_define', {
                'rtu_id': 7, 'interface': 'virtual', 'channel': 'configured',
                'bitrate': 250000, 'fd': 1,
            })
            with patch('api.can.can.Bus') as bus:
                master = CanMaster.from_store(store, 7)
                self.assertFalse(master.connected)
                self.assertEqual((master.interface, master.channel, master.bitrate),
                                 ('virtual', 'configured', 250000))
                self.assertTrue(master.fd)
                master.close()
                bus.assert_not_called()


if __name__ == '__main__':
    unittest.main()

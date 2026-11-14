"""Never read a native monitoring shared_ptr while its receiver replaces it."""
import socket
import time
import unittest
from unittest.mock import PropertyMock, patch

import c104
from client.iec104 import IEC104Master
from server.iec104_server import IEC104Server


class ReceiveSnapshotTests(unittest.TestCase):
    def test_live_value_and_poll_only_read_detached_receive_snapshot(self):
        peer = IEC104Master('127.0.0.1', rtu_id=3, init='NONE')
        self.addCleanup(peer.close)
        point = peer.add_point('yc', 1)
        point.info = c104.ShortInfo(actual=4324, quality=c104.Quality.Blocked)
        receive = peer._make_receive_callback('yc', 1)
        receive(point, point.info, None)

        class UnsafeNativePoint:
            @property
            def info(self):
                raise AssertionError('polling touched concurrently replaced native info')

        peer._points[('yc', 1)] = UnsafeNativePoint()
        peer._configured_entries = [('yc', 1, peer._points[('yc', 1)])]
        with patch.object(peer, '_started', True), patch.object(
                IEC104Master, 'connected', new_callable=PropertyMock, return_value=True):
            first = peer.value('yc', 1)
            self.assertEqual((first.value, first.quality), (4324, 16))
            self.assertIs(peer.collect_once()[0], first)
            self.assertIs(peer.collect_once()[0], first)
            point.info = c104.ShortInfo(actual=99, quality=c104.Quality.Invalid)
            # A callback from a replaced mapping must not overwrite its successor.
            receive(point, point.info, None)
            self.assertEqual(peer.value('yc', 1).value, 4324)
            peer._points[('yc', 1)] = point
            receive(point, point.info, None)
            peer._points[('yc', 1)] = UnsafeNativePoint()
            self.assertEqual((peer.value('yc', 1).value, peer.value('yc', 1).quality), (99, 128))
            self.assertEqual((first.value, first.quality), (4324, 16))

    def test_started_point_without_received_data_is_invalid_not_native_default(self):
        peer = IEC104Master('127.0.0.1', init='NONE')
        self.addCleanup(peer.close)
        peer.add_point('yc', 1)
        with patch.object(peer, '_started', True):
            sample = peer.value('yc', 1)
        self.assertIsNone(sample.value)
        self.assertEqual(sample.quality, 128)

    def test_four_clients_do_not_mix_point_identity_during_batch_receive(self):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
        server = IEC104Server(ip='127.0.0.1', port=port, tick_rate_ms=50)
        self.addCleanup(server.stop)
        clients = [IEC104Master('127.0.0.1', port, init='NONE') for _ in range(4)]
        for no in range(1, 65):
            server.add_point('yc', no, value=no)
        for peer in clients:
            self.addCleanup(peer.close)
            peer.configure([dict(kind='yc', pnt_no=no) for no in range(1, 65)])
        server.start()
        for peer in clients: peer.connect(timeout=3)
        for step in range(1, 33):
            server.update_monitoring([('yc', no, step*1000+no, 0) for no in range(1, 65)])
            deadline = time.monotonic()+3
            while True:
                samples = [sample for peer in clients for sample in peer.collect_once()]
                for sample in samples:
                    if sample.value is not None:
                        self.assertEqual(sample.value % 1000, sample.point_no)
                if all(s.value == step*1000+s.point_no for s in samples): break
                self.assertLess(time.monotonic(), deadline, 'all clients receive every batch')
                time.sleep(.002)

    def test_large_burst_and_gi_reach_both_clients_without_queue_loss(self):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
        server = IEC104Server(ip='127.0.0.1', port=port, tick_rate_ms=50)
        self.addCleanup(server.stop)
        count = 8192
        for no in range(1, count + 1):
            server.add_point('yc', no, value=0)
        clients = [IEC104Master('127.0.0.1', port, init='NONE') for _ in range(2)]
        for peer in clients:
            self.addCleanup(peer.close)
            peer.configure([dict(kind='yc', pnt_no=no) for no in range(1, count + 1)])
        server.start()
        for peer in clients:
            peer.connect(timeout=4)

        def await_values(value_offset, quality, previous=None):
            deadline = time.monotonic() + 60
            progress_deadline = time.monotonic() + 5
            last_count = -1
            while True:
                received = [peer.collect_once() for peer in clients]
                counts = [sum(
                    value.value == value.point_no + value_offset and value.quality == quality
                    and (previous is None or value is not previous[index][point_index])
                    for point_index, value in enumerate(values))
                    for index, values in enumerate(received)]
                if counts == [count, count]:
                    return received
                if sum(counts) > last_count:
                    last_count = sum(counts)
                    progress_deadline = time.monotonic() + 5
                self.assertLess(time.monotonic(), min(deadline, progress_deadline),
                                f'8192 points must reach each client; received {counts}')
                # Poll at the production cadence; avoid starving native Python
                # callbacks while validating thousands of received objects.
                time.sleep(.05)

        server.update_monitoring([('yc', no, float(no), 0) for no in range(1, count + 1)])
        retained = await_values(0, 0)
        server.update_monitoring([('yc', no, float(no + 1), 128) for no in range(1, count + 1)])
        before_gi = await_values(1, 128)
        self.assertTrue(all(v.value == v.point_no and v.quality == 0 for row in retained for v in row))
        for peer in clients:
            self.assertTrue(peer.general_interrogation())
        await_values(1, 128, previous=before_gi)


if __name__ == '__main__': unittest.main()

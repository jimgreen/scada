"""Snapshot immutability, serialization and coherent native IEC104 reads."""
from copy import deepcopy
from datetime import datetime, timezone
import pickle
import socket
import time
from types import SimpleNamespace
import unittest

import c104

from client.iec104 import IEC104Master, IEC104Value
from server.iec104_server import IEC104Server
from client.modbus_tcp import ModbusValue


class ProtocolSnapshotTests(unittest.TestCase):
    def test_records_keep_named_fields_and_are_detached_and_serializable(self):
        now = datetime(2026, 11, 2, 1, 2, 3, tzinfo=timezone.utc)
        samples = [
            (ModbusValue, dict(rtu_id=7, call_seq=2, point_no=3, kind='yc',
                               value=12.5, received_at=now)),
            (IEC104Value, dict(rtu_id=7, point_no=3, kind='yc', value=12.5,
                               quality=128, recorded_at=now, received_at=now)),
        ]
        for cls, fields in samples:
            with self.subTest(cls=cls.__name__):
                record = cls(**fields)
                self.assertEqual(record, cls(*fields.values()))
                for restored in (deepcopy(record), pickle.loads(pickle.dumps(record))):
                    self.assertIs(type(restored), cls)
                    self.assertEqual({k: getattr(restored, k) for k in fields}, fields)
                for field in fields:
                    with self.assertRaises(AttributeError):
                        setattr(record, field, None)

    def test_native_information_types_quality_time_and_retained_snapshots(self):
        master = IEC104Master('127.0.0.1', rtu_id=7)
        self.addCleanup(master.close)
        stamp = datetime(2026, 11, 2, 1, 2, 3, tzinfo=timezone.utc)
        cases = [
            ('yc', 1, c104.ShortInfo(actual=12.5, quality=c104.Quality.Invalid, recorded_at=stamp), 12.5, 128),
            ('yc', 2, c104.ScaledInfo(actual=c104.Int16(-123), quality=c104.Quality.Blocked), -123, 16),
            ('yc', 3, c104.NormalizedInfo(actual=c104.NormalizedFloat(0.25)), 0.25, 0),
            ('yx', 1, c104.SingleInfo(on=True), True, 0),
            ('yx', 2, c104.DoubleInfo(state=c104.Double.ON), 2, 0),
            ('yk', 1, c104.SingleCmd(on=True), True, None),
            ('yk', 2, c104.DoubleCmd(state=c104.Double.OFF), 1, None),
            ('yt', 1, c104.NormalizedCmd(target=c104.NormalizedFloat(0.5)), 0.5, None),
            ('yt', 2, c104.ScaledCmd(target=c104.Int16(321)), 321, None),
            ('yt', 3, c104.ShortCmd(target=4.5), 4.5, None),
        ]
        for number, (kind, data_type, info, value, quality) in enumerate(cases, 1):
            with self.subTest(kind=kind, data_type=data_type):
                point = master.add_point(kind, number, data_type=data_type)
                point.info = info
                result = master.value(kind, number)
                self.assertIs(type(result.value), type(value))
                self.assertEqual((result.rtu_id, result.value, result.quality), (7, value, quality))
                self.assertEqual(result.recorded_at, info.recorded_at)
                self.assertIsNotNone(result.received_at.tzinfo)
                view = master.snapshot()
                view.clear()
                self.assertIs(master.snapshot()[(kind, number)], result)
        point = master._points[('yc', 1)]
        first = master.snapshot()[('yc', 1)]
        point.info = c104.ShortInfo(actual=99, quality=c104.Quality.Blocked)
        second = master.value('yc', 1)
        self.assertEqual((first.value, first.quality, first.recorded_at), (12.5, 128, None))
        self.assertEqual((second.value, second.quality, second.recorded_at), (99.0, 16, None))
        timed = master._station.add_point(io_address=master.point_address('yc', 50),
                                          type=c104.Type.M_ME_TF_1)
        timed.info = c104.ShortInfo(actual=3.5, quality=c104.Quality.Invalid, recorded_at=stamp)
        master._points[('yc', 50)] = timed
        result = master.value('yc', 50)
        self.assertIsNotNone(result.recorded_at)
        self.assertEqual((result.value, result.quality, result.recorded_at), (3.5, 128, timed.recorded_at))

    def test_point_update_during_read_does_not_mix_value_quality_and_timestamp(self):
        stamp = datetime(2026, 11, 2, tzinfo=timezone.utc)
        first = SimpleNamespace(value=12.5, quality=SimpleNamespace(value=128), recorded_at=stamp)
        second = SimpleNamespace(value=99.0, quality=SimpleNamespace(value=16), recorded_at=None)

        class UpdatingPoint:
            active = first

            @property
            def info(self):
                captured = self.active
                self.active = second
                return captured

            @property
            def value(self):
                captured = self.active.value
                self.active = second
                return captured

            @property
            def quality(self):
                return self.active.quality

            @property
            def recorded_at(self):
                return self.active.recorded_at

        master = IEC104Master('127.0.0.1', rtu_id=7)
        self.addCleanup(master.close)
        master._points[('yc', 1)] = UpdatingPoint()
        result = master.value('yc', 1)
        self.assertEqual((result.value, result.quality, result.recorded_at), (12.5, 128, stamp))
        result = master.value('yc', 1)
        self.assertEqual((result.value, result.quality, result.recorded_at), (99.0, 16, None))

    def test_poll_fast_paths_keep_configured_scope_and_order(self):
        master = IEC104Master('127.0.0.1', rtu_id=7)
        self.addCleanup(master.close)
        yc = master.add_point('yc', 1)
        yx = master.add_point('yx', 1)
        yc.info = c104.ShortInfo(actual=1.0)
        yx.info = c104.SingleInfo(on=True)

        # An unconfigured client keeps an empty configured entry list, while
        # the low-level _poll() API still reads all points explicitly added.
        self.assertEqual(master._configured_entries, [])
        self.assertEqual([(value.kind, value.point_no) for value in master._poll()],
                         [('yc', 1), ('yx', 1)])

        master.configure([dict(kind='yx', pnt_no=1), dict(kind='yc', pnt_no=1)])
        self.assertEqual([(value.kind, value.point_no)
                          for value in master._poll_entries(master._configured_entries)],
                         [('yx', 1), ('yc', 1)])

    def test_loopback_quality_updates_and_command_results(self):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        commands = []
        server = IEC104Server(ip='127.0.0.1', port=port)
        self.addCleanup(server.stop)
        yc = server.add_point('yc', 1, value=0.0)
        yx = server.add_point('yx', 1, value=False)
        for kind in ('yk', 'yt'):
            server.add_point(kind, 1, command_handler=lambda event: commands.append(event) or True)
        master = IEC104Master('127.0.0.1', port, rtu_id=7, init='NONE')
        self.addCleanup(master.close)
        master.configure([dict(kind='yc', pnt_no=1), dict(kind='yx', pnt_no=1)])
        server.start()
        master.connect(timeout=3)
        master.general_interrogation()
        for cycle, (yc_quality, yx_quality) in enumerate(((128, 16), (1, 32), (0, 0), (16, 128)), 1):
            number, binary = cycle * 1.25, bool(cycle % 2)
            yc.info = c104.ShortInfo(actual=number, quality=c104.Quality(yc_quality))
            yx.info = c104.SingleInfo(on=binary, quality=c104.Quality(yx_quality))
            self.assertTrue(yc.transmit(c104.Cot.SPONTANEOUS))
            self.assertTrue(yx.transmit(c104.Cot.SPONTANEOUS))
            expected = [('yc', number, yc_quality), ('yx', binary, yx_quality)]
            deadline = time.monotonic() + 3
            while True:
                values = master.collect_once()
                actual = [(v.kind, v.value, v.quality) for v in values]
                if actual == expected or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertEqual(actual, expected)
        yk_result = master.send_yk_point(1, True)
        yt_result = master.send_yt_point(1, 7.5)
        self.assertEqual((yk_result.value, yk_result.quality), (True, None))
        self.assertEqual((yt_result.value, yt_result.quality), (7.5, None))
        self.assertEqual([(event.kind, event.value) for event in commands], [('yk', True), ('yt', 7.5)])
        self.assertIsNone(server.last_error)


if __name__ == '__main__':
    unittest.main()

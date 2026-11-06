"""Regression checks for IEC104 double-point business values and commands."""
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest

import c104

from client.iec104 import IEC104Master
from server.iec104_server import IEC104Server
from scdfe_c.web.service import ScadaStore
from scdfe_s.web.iec104_service import IEC104Service, IEC104ServiceError
from scdfe_t import TransRuntime


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


class IEC104DoubleRegressionTests(unittest.TestCase):
    def test_master_normalizes_double_states_and_marks_unknown_quality(self):
        master = IEC104Master('127.0.0.1', rtu_id=7, init='NONE')
        self.addCleanup(master.close)
        point = master.add_point('yx', 1, data_type=2)
        for state, expected, quality in (
            (c104.Double.OFF, False, 16),
            (c104.Double.ON, True, 16),
            (c104.Double.INTERMEDIATE, None, 144),
            (c104.Double.INDETERMINATE, None, 144),
        ):
            with self.subTest(state=state):
                point.info = c104.DoubleInfo(state=state, quality=c104.Quality.Blocked)
                value = master.value('yx', 1)
                self.assertIs(value.value, expected)
                self.assertEqual(value.quality, quality)

    def test_server_updates_and_snapshots_keep_off_and_on_distinct(self):
        server = IEC104Server(ip='127.0.0.1', port=free_port())
        self.addCleanup(server.stop)
        server.add_point('yx', 1, data_type=2, value=True)
        server.add_point('yk', 1, data_type=2, value=True)
        for expected in (False, True, False):
            self.assertIs(server.update('yx', 1, expected, transmit=False), expected)
            self.assertIs(server.set_value('yk', 1, expected), expected)
            self.assertEqual(server.snapshot(), {('yx', 1): expected, ('yk', 1): expected})

    def test_invalid_double_commands_are_rejected_before_dispatch(self):
        server = IEC104Server(ip='127.0.0.1', port=free_port())
        self.addCleanup(server.stop)
        commands = []
        server.add_point('yk', 1, data_type=2, value=False,
                         command_handler=lambda event: commands.append(event) or True)
        callback = server._make_command_callback('yk', 1)
        message = SimpleNamespace(is_select_command=False, cot=c104.Cot.ACTIVATION,
                                  originator_address=1)
        for state in (c104.Double.INTERMEDIATE, c104.Double.INDETERMINATE):
            self.assertEqual(callback(SimpleNamespace(value=state), None, message),
                             c104.ResponseState.FAILURE)
        self.assertEqual(commands, [])
        self.assertIsNone(server.command('yk', 1))

    def test_monitoring_quality_can_change_without_changing_value(self):
        server = IEC104Server(ip='127.0.0.1', port=free_port())
        self.addCleanup(server.stop)
        point = server.add_point('yx', 1, data_type=2, value=True)
        self.assertIs(server.set_value('yx', 1, True, quality=128), True)
        self.assertEqual(point.quality.value, 128)
        server.set_value('yx', 1, True)
        self.assertEqual(point.quality.value, 128)
        self.assertIs(server.update('yx', 1, True, quality=0, transmit=False), True)
        self.assertEqual(point.quality.value, 0)
        server.set_value('yx', 1, True, quality=c104.Quality.Blocked)
        self.assertEqual(point.quality.value, 16)

    def test_web_persists_normalized_double_updates_and_preserves_unknown_state(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ScadaStore(Path(folder) / 'review.sqlite3')
            store.create_row('fes_rtu_define', {
                'rtu_id': 7, 'channel_id': 7, 'channel_type': 'iec104',
                'process_enable': 0,
            })
            for kind in ('yx', 'yk'):
                store.create_row(f'fes_{kind}_define', {'rtu_id': 7, 'pnt_no': 1, 'value': 1})
            service = IEC104Service(store)
            service.configure({'rtu_id': 7, 'port': free_port()})
            server = IEC104Server(ip='127.0.0.1', port=free_port())
            self.addCleanup(server.stop)
            for kind in ('yx', 'yk'):
                server.add_point(kind, 1, data_type=2, value=True)
                for state in (False, True):
                    actual = server.set_value(kind, 1, state)
                    row = service._persist_point(kind, 1, actual)
                    self.assertEqual((row['value'], row['status']), (int(state), 0))
            row = service._persist_point('yx', 1, None)
            self.assertEqual((row['value'], row['status']), (1, 128))
            with self.assertRaises(IEC104ServiceError):
                service._persist_point('yk', 1, None)
            self.assertEqual(store.get_row('fes_yk_define', [7, 1])['value'], 1)

    def test_real_double_point_monitoring_and_commands_round_trip(self):
        port = free_port()
        commands = []
        server = IEC104Server(ip='127.0.0.1', port=port)
        self.addCleanup(server.stop)
        server.add_point('yx', 1, data_type=2, value=False)
        server.add_point('yk', 1, data_type=2, value=False,
                         command_handler=lambda event: commands.append(event.value) or True)
        master = IEC104Master('127.0.0.1', port=port, init='NONE')
        self.addCleanup(master.close)
        master.add_point('yx', 1, data_type=2)
        master.add_point('yk', 1, data_type=2)
        server.start()
        master.connect(timeout=4)
        for expected in (False, True, False):
            server.update('yx', 1, expected)
            deadline = time.monotonic() + 3
            while master.value('yx', 1).value is not expected and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIs(master.value('yx', 1).value, expected)
            result = master.send_yk_point(1, expected, double=True)
            self.assertIs(result.value, expected)
            self.assertIs(commands[-1], expected)
        # Native OFF is a protocol enum, not Python truthy business ON.
        result = master.send_yk_point(1, c104.Double.OFF, double=True)
        self.assertIs(result.value, False)
        self.assertEqual(commands, [False, True, False, False])
        before = len(commands)
        for state in (c104.Double.INTERMEDIATE, c104.Double.INDETERMINATE, 2, -1):
            with self.assertRaises(ValueError):
                master.send_yk_point(1, state, double=True)
        self.assertEqual(len(commands), before)

    def test_transmission_double_commands_preserve_source_off_on(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'review.sqlite3'
            store = ScadaStore(db)
            store.create_row('fes_rtu_define', {
                'rtu_id': 7, 'channel_id': 7, 'channel_type': 'iec104',
                'process_enable': 0,
            })
            store.create_row('fes_yk_define', {'rtu_id': 7, 'pnt_no': 4, 'value': 0})
            port = free_port()
            store.create_row('fes_scdfe_t_define', {
                'channel_id': 1, 'code': 'DOUBLE_TEST', 'name': 'Double command test',
                'protocol': 'iec104', 'bind_host': '127.0.0.1', 'bind_port': port,
            })
            store.create_row('fes_scdfe_t_point_define', {
                'channel_id': 1, 'kind': 'yk', 'pnt_no': 1,
                'source_rtu_id': 7, 'source_pnt_no': 4,
                'io_address': 0x6001, 'type_id': 46, 'enabled': 1,
            })
            calls = []
            runtime = TransRuntime(db, channel_id=1)
            runtime.writer = SimpleNamespace(
                submit_command=lambda *args, **kwargs: calls.append(args) or True)
            spec, points, _ = runtime.load()
            server = runtime.build_server(spec, points)
            client = IEC104Master('127.0.0.1', port=port, init='NONE')
            try:
                server.start()
                client.add_point('yk', 1, data_type=2)
                client.connect(timeout=4)
                for value in (False, True, False):
                    client.send_yk_point(1, value, double=True)
                self.assertEqual(calls, [(7, 'yk', 4, 0), (7, 'yk', 4, 1), (7, 'yk', 4, 0)])
            finally:
                client.close()
                server.stop()


if __name__ == '__main__':
    unittest.main()

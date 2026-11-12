"""C/S engineering mapping contracts over real loopback Modbus TCP."""
from contextlib import ExitStack
from pathlib import Path
import asyncio
import tempfile
import threading
import unittest
from unittest.mock import patch

from client.modbus_tcp import ModbusTcpMaster, ModbusError
from scdfe_c.web.service import ScadaStore
from server.modbus_mapped_server import ModbusMappedServer, simulator_mappings
from tests.test_protocol_interface_contracts import free_port, eventually


class SpecialMappings(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = ScadaStore(root / 'scdfe_s.sqlite3')
        self.stack.callback(self.store.close)
        self.store.create_row('fes_rtu_define', dict(rtu_id=1, channel_id=1, channel_type='modbus-tcp'))
        self.store.create_row('fes_channel_modbus_tcp_define', dict(channel_id=1, remote_host='127.0.0.1', remote_port=502, unit_id=1))

    def point(self, kind, no, value):
        self.store.create_row(f'fes_{kind}_define', dict(rtu_id=1, pnt_no=no, value=value))

    def start(self, points=None):
        port = free_port()
        self.engine = ModbusMappedServer(store=self.store, rtu_id=1, port=port,
            points=simulator_mappings(self.store, 1, 1) if points is None else points)
        self.stack.callback(self.engine.close)
        self.engine.start()
        self.peer = ModbusTcpMaster.from_store(self.store, 1, transport_overrides=dict(remote_port=port))
        self.stack.callback(self.peer.close)
        self.peer.connect()

    def test_shared_slices_byte_order_inversion_and_database_sync(self):
        self.store.create_row('fes_channel_modbus_define_call', dict(id=1, channel_id=1, call_seq=1, irtuaddr=1, icode=4, iplace=30001, address_base=30001, inum=3))
        definitions = [
            ('yx', 1, 0, 8, 0, dict(offset_bit=0, if_reverse=1)),
            ('yc', 1, 5, 9, 0, dict(offset_bit=1, all_bit=3)),
            ('yc', 2, -2, 7, 0, {}),
            ('yc', 3, 171, 6, 1, {}),
            ('yc', 4, 205, 6, 1, dict(twobytedata_byteseq=1)),
            ('yx', 2, 1, 8, 2, dict(offset_bit=1, twobytedata_byteseq=1)),
        ]
        for ident, (kind, no, value, typ, address, extra) in enumerate(definitions, 1):
            self.point(kind, no, value)
            self.store.create_row('fes_channel_modbus_define_data', dict(id=ident, channel_id=1, call_seq=1, if_yx=int(kind == 'yx'), cnt=no, reg_addr=30001+address, trans_type=typ, **extra))
        self.store.create_row('fes_channel_modbus_define_call', dict(id=2, channel_id=1, call_seq=2, irtuaddr=1, icode=2, iplace=0, inum=1))
        self.point('yx', 3, 1)
        self.store.create_row('fes_channel_modbus_define_data', dict(id=7, channel_id=1, call_seq=2, if_yx=1, cnt=3, reg_addr=0, trans_type=8, if_reverse=1))
        self.start()
        self.assertEqual(self.peer.read_input_registers(0, 3), [0xFE0B, 0xCDAB, 0x0200])
        self.assertEqual(self.peer.read_discrete_inputs(0), [False])
        self.assertEqual({(p.kind, p.point_no): p.value for p in self.peer.collect_once()},
                         {(k, n): v for k, n, v, *_ in definitions} | {('yx', 3): 1})
        self.store.update_rows([
            ('fes_yx_define', [1, 1], dict(value=1)),
            ('fes_yc_define', [1, 1], dict(value=2)),
            ('fes_yc_define', [1, 4], dict(value=18)),
        ])
        self.assertTrue(eventually(lambda: self.peer.read_input_registers(0, 3) == [0xFE04, 0x12AB, 0x0200]))
        for kind, no, invalid in [('yc', 1, 8), ('yc', 2, -129), ('yc', 3, 1.5), ('yx', 1, 2)]:
            with self.subTest(kind=kind, no=no), self.assertRaises((ValueError, OverflowError)):
                self.engine.prepare_updates([(kind, no, invalid)])
        self.assertEqual(self.peer.read_input_registers(0, 2), [0xFE04, 0x12AB])

    def test_register_controls_and_inverted_coils_match_client_encoder(self):
        definitions = [
            dict(icode=5, ytyk_type=6, ifreverse=1),
            dict(icode=15, ytyk_type=6, ifreverse=1),
            dict(icode=6, ytyk_type=1),
            dict(icode=16, ytyk_type=2, twobytedata_byteseq=1),
            dict(icode=6, ytyk_type=6, special_yk_bit=4),
            dict(icode=16, ytyk_type=6, send_ff_replace_01=1, ifreverse=1),
            dict(icode=6, ytyk_type=7, special_yk_bit=3),
            dict(icode=16, ytyk_type=7, special_yk_bit=9),
        ]
        for no, definition in enumerate(definitions, 1):
            self.point('yk', no, 0)
            address = 20 if no >= 7 else no
            self.store.create_row('fes_channel_modbus_define_ytyk', dict(id=no, channel_id=1, ifyk=1, cnt=no, irtuaddr=1, reg_addr=address, **definition))
        self.start()
        for no, definition in enumerate(definitions[:6], 1):
            for value in (True, False):
                with self.subTest(no=no, value=value):
                    self.peer.send_yk_point(no, value)
                    row = self.store.get_row('fes_yk_define', [1, no])
                    self.assertEqual(row['value'], int(value))
                    self.assertEqual(row['status'], 1)
                    self.assertGreater(row['cmd_handle_time'], 0)
        self.peer.write_single_register(20, 0x8000)
        self.peer.send_yk_point(7, True, current_value=0x8000)
        self.peer.send_yk_point(8, True, current_value=self.peer.read_holding_registers(20)[0])
        self.assertEqual(self.peer.read_holding_registers(20), [0x8208])
        self.assertEqual([self.store.get_row('fes_yk_define', [1, n])['value'] for n in (7, 8)], [1, 1])
        self.engine.update_point('yk', 7, False)
        self.assertEqual(self.peer.read_holding_registers(20), [0x8200])
        # An invalid register command is rejected before any DB or wire mutation.
        with self.assertRaises(ModbusError):
            self.peer.write_single_register(3, 123)
        self.assertEqual(self.peer.read_holding_registers(3), [0])

    def test_multi_register_write_is_one_transaction_and_rolls_back_on_late_failure(self):
        points = []
        for no in (1, 2):
            self.point('yc', no, no * 10)
            points.append(dict(kind='yc', pnt_no=no, register_area='holding', address=no-1, data_type='uint16'))
        self.start(points)
        with self.store.connection() as con:
            con.execute("CREATE TRIGGER reject_second BEFORE UPDATE OF value ON fes_yc_define WHEN NEW.pnt_no=2 AND NEW.value=99 BEGIN SELECT RAISE(ABORT,'test late failure'); END")
            con.commit()
        with patch.object(self.store, 'update_rows', wraps=self.store.update_rows) as batch:
            with self.assertRaises(ModbusError) as failure:
                self.peer.write_multiple_registers(0, [88, 99])
            self.assertEqual(failure.exception.exception_code, 4)
            self.assertEqual(batch.call_count, 1)
        self.assertEqual([self.store.get_row('fes_yc_define', [1, n])['value'] for n in (1, 2)], [10, 20])
        self.assertEqual(self.peer.read_holding_registers(0, 2), [10, 20])
        with patch.object(self.store, 'update_rows', wraps=self.store.update_rows) as batch:
            self.peer.write_multiple_registers(0, [33, 44])
            self.assertEqual(batch.call_count, 1)
        self.assertEqual(self.peer.read_holding_registers(0, 2), [33, 44])
        self.assertEqual([self.store.get_row('fes_yc_define', [1, n])['value'] for n in (1, 2)], [33, 44])

    def test_overlapping_masks_are_rejected_before_listen(self):
        for no in (1, 2):
            self.point('yc', no, 0)
        points = [dict(kind='yc', pnt_no=n, address=0, register_area='holding',
                       data_type='uint16', slice_shift=0, slice_width=8) for n in (1, 2)]
        with self.assertRaisesRegex(ValueError, '冲突'):
            ModbusMappedServer(store=self.store, rtu_id=1, points=points)

    def test_shared_holding_write_updates_all_slices_in_one_commit(self):
        self.point('yc', 1, 0)
        self.point('yx', 1, 0)
        points = [dict(kind=kind, pnt_no=1, address=0, register_area='holding', data_type='uint16',
                       slice_shift=shift, slice_width=width, reverse=reverse)
                  for kind, shift, width, reverse in [('yc', 0, 8, False), ('yx', 15, 1, True)]]
        self.start(points)
        with patch.object(self.store, 'update_rows', wraps=self.store.update_rows) as batch:
            self.peer.write_single_register(0, 0x400A)
            self.assertEqual(batch.call_count, 1)
        self.assertEqual(self.store.get_row('fes_yc_define', [1, 1])['value'], 10)
        self.assertEqual(self.store.get_row('fes_yx_define', [1, 1])['value'], 1)
        self.store.update_row('fes_yc_define', [1, 1], dict(value=11))
        self.assertTrue(eventually(lambda: self.peer.read_holding_registers(0) == [0x400B]))

    def test_database_scan_cannot_overwrite_newer_remote_write(self):
        self.point('yc', 1, 10)
        self.start([dict(kind='yc', pnt_no=1, address=0, register_area='holding', data_type='uint16')])
        reached, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = self.engine._on_datastore
        def pause_scan(operation):
            if operation.__name__ == 'publish_if_current' and not reached.is_set():
                reached.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release scan')
                try:
                    return original(operation)
                finally:
                    finished.set()
            return original(operation)
        with patch.object(self.engine, '_on_datastore', pause_scan), patch.object(
                self.engine, 'publish_updates', wraps=self.engine.publish_updates) as publish:
            self.store.update_row('fes_yc_define', [1, 1], dict(value=55))
            try:
                self.assertTrue(reached.wait(3))
                self.peer.write_single_register(0, 66)
            finally:
                release.set()
            self.assertTrue(finished.wait(3))
            self.assertEqual(self.peer.read_holding_registers(0), [66])
            self.assertFalse(any(sample[2] == 55 for call in publish.call_args_list for sample in call.args[0]))
        self.assertEqual(self.store.get_row('fes_yc_define', [1, 1])['value'], 66)

    def test_transmission_callback_failure_returns_exception_04(self):
        from pymodbus.constants import ExcCodes
        from server.modbus_trans_server import ModbusTcpTransServer
        for failure in (False, RuntimeError('writer unavailable')):
            def handler(*args):
                if isinstance(failure, Exception):
                    raise failure
                return failure
            server = ModbusTcpTransServer(points=[dict(kind='yt', pnt_no=1, address=0)], command_handler=handler)
            try:
                result = asyncio.run(server._action(6, 0, 0, 1, [0], [23]))
                self.assertEqual(result, ExcCodes.DEVICE_FAILURE)
                self.assertEqual(server._values[('yt', 1)], 0)
            finally:
                server.close()

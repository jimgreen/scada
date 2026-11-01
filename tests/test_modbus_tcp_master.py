"""主站的真实回环TCP通信、点表隔离、完整加载及轮询生命周期测试。"""
from dataclasses import make_dataclass
from pathlib import Path
import tempfile
import threading
import time
import unittest

from pyModbusTCP.server import DataBank, ModbusServer

from api.modbus_tcp import ModbusError, ModbusTcpMaster
from web.service import ScadaStore


def point_tables():
    return ([dict(rtu_id=7, call_seq=0, irtuaddr=9, iplace=10, inum=2, icode=3)],
            [dict(rtu_id=7, call_seq=0, cnt=1, reg_addr=10, trans_type=2)])


class MasterLoopbackTests(unittest.TestCase):
    def setUp(self):
        bank = DataBank(coils_size=100, d_inputs_size=100,
                        h_regs_size=100, i_regs_size=100)
        self.server = ModbusServer(host='127.0.0.1', port=0, no_block=True, data_bank=bank)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.master = ModbusTcpMaster(
            '127.0.0.1', self.server._service.server_address[1], rtu_id=7, timeout=0.5,
        )
        self.addCleanup(self.master.close)
        self.master.configure(*point_tables())
        self.master.connect()
        self.bank = bank

    def test_all_eight_function_codes_over_tcp(self):
        self.bank.set_discrete_inputs(0, [True, False, True])
        self.bank.set_input_registers(0, [400, 5000])
        self.assertEqual(self.master.read_discrete_inputs(0, 3), [True, False, True])
        self.assertEqual(self.master.read_input_registers(0, 2), [400, 5000])
        self.assertTrue(self.master.write_single_coil(0, True))
        self.assertTrue(self.master.write_multiple_coils(1, [False, True, True]))
        self.assertEqual(self.master.read_coils(0, 4), [True, False, True, True])
        self.assertEqual(self.bank.get_coils(0, 4), [True, False, True, True])
        self.assertTrue(self.master.write_single_register(0, 123))
        self.assertTrue(self.master.write_multiple_registers(1, [456, 65535]))
        self.assertEqual(self.master.read_holding_registers(0, 3), [123, 456, 65535])
        self.assertEqual(self.bank.get_holding_registers(0, 3), [123, 456, 65535])

    def test_float_collection_and_point_commands(self):
        calls, points = point_tables()
        points[0].update(trans_type=5, fourbytedata_byteseq=3)
        definitions = [
            dict(rtu_id=7, cnt=1, ifyk=1, irtuaddr=9, reg_addr=2, icode=5),
            dict(rtu_id=7, cnt=1, ifyk=0, irtuaddr=9, reg_addr=10,
                 icode=16, ytyk_type=5, fourbytedata_byteseq=3),
        ]
        self.master.configure(calls, points, definitions)
        self.assertTrue(self.master.send_yk_point(1, True))
        self.assertEqual(self.bank.get_coils(2), [True])
        self.assertTrue(self.master.send_yt_point(1, 12.5))
        self.assertEqual(self.bank.get_holding_registers(10, 2), [0x4148, 0])
        self.assertEqual(self.master.collect_once()[0].value, 12.5)
        self.assertEqual(self.master.native.unit_id, 9)

    def test_configuration_copy_filter_and_failed_replacement(self):
        calls, points = point_tables()
        Call = make_dataclass('Call', [(key, object) for key in calls[0]])
        self.master.configure([Call(**calls[0]), {**calls[0], 'rtu_id': 8}], points)
        points[0]['reg_addr'] = 0
        self.bank.set_holding_registers(10, [123])
        self.assertEqual(self.master.collect_once()[0].value, 123)
        with self.assertRaises(ValueError):
            self.master.configure(calls, points)
        self.assertEqual(self.master.collect_once()[0].value, 123)
        duplicate = dict(point_tables()[1][0])
        with self.assertRaisesRegex(ValueError, '重复'):
            self.master.configure(calls, [duplicate, duplicate])

    def test_commands_filter_other_rtu_and_special_bit(self):
        row = dict(rtu_id=7, cnt=1, ifyk=1, irtuaddr=9, reg_addr=3,
                   icode=6, ytyk_type=7, special_yk_bit=2)
        definitions = [row, {**row, 'rtu_id': 8}]
        self.assertTrue(self.master.send_yk_point(1, True, definitions, current_value=2))
        self.assertEqual(self.bank.get_holding_registers(3), [6])
        with self.assertRaises(ValueError):
            self.master.send_yk_point(1, True, [row, row])
        with self.assertRaises(ValueError):
            self.master.send_yk_point(1, True, [{**row, 'rtu_id': 8}])
        self.assertEqual(self.bank.get_holding_registers(3), [6])

    def test_loop_interval_stop_and_callback_error(self):
        batches = []
        started = time.monotonic()
        self.assertEqual(self.master.poll_loop(batches.append, interval=0.03, max_cycles=3), 3)
        self.assertGreaterEqual(time.monotonic() - started, 0.05)
        self.assertEqual(len(batches), 3)
        stop = threading.Event()
        self.assertEqual(self.master.poll_loop(lambda _: stop.set(), interval=30, stop_event=stop), 1)
        self.assertEqual(self.master.poll_loop(batches.append, stop_event=stop), 0)
        for interval in (float('nan'), float('inf'), -1, True):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.master.poll_loop(batches.append, interval=interval)

        def fail(_):
            raise RuntimeError('callback failed')
        with self.assertRaisesRegex(RuntimeError, 'callback failed'):
            self.master.poll_loop(fail, max_cycles=1)

    def test_slave_exception_and_closed_connection(self):
        with self.assertRaises(ModbusError) as caught:
            self.master.read_holding_registers(100, 1)
        self.assertEqual(caught.exception.exception_code, 2)
        self.assertEqual(self.master.read_holding_registers(0), [0])
        self.master.close()
        with self.assertRaises(ModbusError):
            self.master.collect_once()


class MasterStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ScadaStore(Path(self.temp.name) / 'scada.sqlite3')
        self.store.create_row('fes_rtu_define', {
            'rtu_id': 7, 'channel_id': 70, 'channel_type': 'modbus-tcp',
                    })
        self.store.create_row('fes_channel_modbus_define', {'id': 1, 'rtu_id': 7, 'irtuaddr': 9})
        self.store.create_row('fes_channel_modbus_tcp_define', {
            'rtu_id': 7, 'remote_host': '127.0.0.1', 'remote_port': 1503,
            'unit_id': 5, 'response_timeout': 0.7,
        })

    def test_loads_more_than_500_points_and_commands_without_writes(self):
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            con.executemany('INSERT INTO fes_channel_modbus_define_call '
                            '(id,rtu_id,irtuaddr,call_seq,iplace,inum,icode) VALUES (?,7,9,?,?,?,3)',
                            [(seq + 1, seq, seq * 125, min(125, 501 - seq * 125)) for seq in range(5)])
            con.executemany('INSERT INTO fes_channel_modbus_define_data '
                            '(id,rtu_id,call_seq,cnt,reg_addr,trans_type) VALUES (?,7,?,?,?,2)',
                            [(i + 1, i // 125, i + 1, i) for i in range(501)])
            con.commit()
        self.store.create_row('fes_channel_modbus_define_ytyk', {
            'id': 1, 'rtu_id': 7, 'ifyk': 1, 'cnt': 1, 'reg_addr': 0,
        })
        before = self.store.overview()
        master = ModbusTcpMaster.from_store(self.store, 7)
        self.addCleanup(master.close)
        self.assertEqual((master.host, master.port, master.unit_id), ('127.0.0.1', 1503, 5))
        self.assertEqual(len(master._calls), 5)
        self.assertEqual(len(master._points), 501)
        self.assertEqual(len(master._definitions), 1)
        self.assertFalse(master.connected)
        self.assertEqual(self.store.overview(), before)

    def test_rejects_ambiguous_or_wrong_channels_and_protocol(self):
        with self.assertRaises(ValueError):
            ModbusTcpMaster.from_store(self.store, 999)
        with self.assertRaises(TypeError):
            ModbusTcpMaster.from_store(self.store, 7, channel_id=70)
        self.store.update_row('fes_rtu_define', [7], {'channel_type': 'iec104'})
        with self.assertRaisesRegex(ValueError, '通道'):
            ModbusTcpMaster.from_store(self.store, 7)


if __name__ == '__main__':
    unittest.main()

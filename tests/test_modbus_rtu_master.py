"""用 pyserial socket:// 承载真实 RTU 帧/CRC；不访问物理串口。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from pymodbus import FramerType
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from api import ModbusRtuMaster
from api.modbus_rtu import ModbusError
from web.service import ScadaStore


class RtuSimulator:
    """pymodbus RTU framer 在回环字节流上模拟两个独立从站。"""

    def __init__(self):
        self.ready = threading.Event()
        self.error = None
        self.packets = []
        self.corrupt = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise TimeoutError('RTU simulator startup')
        if self.error:
            raise self.error

    def trace(self, sending, data):
        self.packets.append((sending, data))
        if sending and self.corrupt:
            return data[:-1] + bytes([data[-1] ^ 0xff])
        return data

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        devices = [SimDevice(number, (
            [SimData(0, count=64, values=False, datatype=DataType.BITS)],
            [SimData(0, count=64, values=True, datatype=DataType.BITS)],
            [SimData(0, count=100, values=number, datatype=DataType.REGISTERS)],
            [SimData(0, count=100, values=400, datatype=DataType.REGISTERS)],
        )) for number in (9, 10)]
        self.server = ModbusTcpServer(devices, framer=FramerType.RTU,
                                      address=('127.0.0.1', 0), trace_packet=self.trace)
        await self.server.serve_forever(background=True)
        self.port = self.server.transport.sockets[0].getsockname()[1]
        self.ready.set()
        await self.server.serving

    def _run(self):
        try:
            asyncio.run(self.serve())
        except BaseException as exc:
            self.error = exc
            self.ready.set()

    def close(self):
        asyncio.run_coroutine_threadsafe(self.server.shutdown(), self.loop).result(3)
        self.thread.join(3)
        if self.thread.is_alive():
            raise TimeoutError('RTU simulator shutdown')


class RtuWireTests(unittest.TestCase):
    def setUp(self):
        self.sim = RtuSimulator()
        self.addCleanup(self.sim.close)
        self.master = ModbusRtuMaster(f'socket://127.0.0.1:{self.sim.port}',
                                     unit_id=9, rtu_id=7, parity='N', timeout=0.15)
        self.addCleanup(self.master.close)
        self.master.connect()

    def test_eight_functions_and_known_crc_frame(self):
        self.assertEqual(self.master.read_holding_registers(0, 2), [9, 9])
        self.assertIn((False, bytes.fromhex('090300000002c543')), self.sim.packets)
        self.assertEqual(self.master.read_input_registers(0, 2), [400, 400])
        self.assertEqual(self.master.read_discrete_inputs(0, 3), [True] * 3)
        self.assertTrue(self.master.write_single_coil(0, True))
        self.assertTrue(self.master.write_multiple_coils(1, [False, True, True]))
        self.assertEqual(self.master.read_coils(0, 4), [True, False, True, True])
        self.assertTrue(self.master.write_single_register(0, 123))
        self.assertTrue(self.master.write_multiple_registers(1, [456, 65535]))
        self.assertEqual(self.master.read_holding_registers(0, 3), [123, 456, 65535])

    def test_points_float_commands_polling_and_stop(self):
        calls = [dict(rtu_id=7, call_seq=0, irtuaddr=9, iplace=0, inum=2, icode=3)]
        points = [dict(rtu_id=7, call_seq=0, cnt=1, reg_addr=0,
                       trans_type=5, fourbytedata_byteseq=3)]
        definitions = [
            dict(rtu_id=7, cnt=1, ifyk=0, reg_addr=0, ytyk_type=5,
                 fourbytedata_byteseq=3, icode=16),
            dict(rtu_id=7, cnt=1, ifyk=1, reg_addr=0, icode=5),
        ]
        self.master.configure(calls, points, definitions)
        self.assertTrue(self.master.send_yt_point(1, 12.5))
        self.assertTrue(self.master.send_yk_point(1, True))
        self.assertEqual(self.master.collect_once()[0].value, 12.5)
        self.assertEqual(self.master.read_coils(0), [True])
        stop = threading.Event()
        self.assertEqual(self.master.poll_loop(lambda _: stop.set(), stop_event=stop, interval=30), 1)
        self.assertEqual(self.master.poll_loop(print, stop_event=stop), 0)
        points[0]['call_seq'] = 99
        with self.assertRaises(ValueError):
            self.master.configure(calls, points)
        self.assertEqual(self.master.collect_once()[0].value, 12.5)

    def test_multi_slave_serialized_requests(self):
        def read(number):
            for _ in range(3):
                self.assertEqual(self.master.read_holding_registers(0, unit_id=number), [number])
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(read, number) for number in (9, 10)]
            for future in futures:
                future.result(timeout=5)

    def test_exception_and_crc_timeout_without_retry(self):
        with self.assertRaises(ModbusError) as caught:
            self.master.read_holding_registers(200)
        self.assertEqual(caught.exception.exception_code, 2)
        self.sim.packets.clear()
        self.sim.corrupt = True
        with self.assertRaises(ModbusError):
            self.master.read_holding_registers(0)
        self.assertEqual(sum(not sending for sending, _ in self.sim.packets), 1)
        self.master.close()
        with self.assertRaises(ModbusError):
            self.master.read_coils(0)


class RtuConfigTests(unittest.TestCase):
    def test_store_loading_serial_settings_and_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ScadaStore(Path(directory) / 'scada.sqlite3')
            store.create_row('fes_rtu_define', {
                'rtu_id': 7, 'channel_id': 70, 'channel_type': 'modbus-rtu',
                            })
            store.create_row('fes_channel_modbus_rtu_define', {
                'rtu_id': 7, 'serial_port': 'COM98', 'unit_id': 10,
                'response_timeout': 0.2,
            })
            store.create_row('fes_channel_modbus_define', {'id': 1, 'rtu_id': 7, 'irtuaddr': 9})
            store.create_row('fes_channel_modbus_define_call', {
                'id': 1, 'rtu_id': 7, 'call_seq': 0, 'irtuaddr': 9,
                'iplace': 0, 'inum': 1, 'icode': 3,
            })
            store.create_row('fes_channel_modbus_define_data', {
                'id': 1, 'rtu_id': 7, 'call_seq': 0, 'cnt': 1, 'reg_addr': 0, 'trans_type': 2,
            })
            before = store.overview()
            master = ModbusRtuMaster.from_store(store, 7, baudrate=19200, parity='O', stopbits=2)
            self.addCleanup(master.close)
            self.assertEqual((master.port, master.unit_id), ('COM98', 10))
            self.assertFalse(master.connected)
            self.assertEqual(master.native.comm_params.baudrate, 19200)
            self.assertEqual(master.native.comm_params.parity, 'O')
            self.assertEqual(master.native.comm_params.stopbits, 2)
            self.assertEqual(len(master._points), 1)
            self.assertEqual(store.overview(), before)
            store.update_row('fes_rtu_define', [7], {'channel_type': 'modbus-tcp'})
            with self.assertRaisesRegex(ValueError, '通道'):
                ModbusRtuMaster.from_store(store, 7)

    def test_invalid_parameters_never_open_serial(self):
        for kwargs in ({'unit_id': 0}, {'timeout': float('nan')}, {'timeout': float('inf')},
                       {'baudrate': 0}, {'parity': 'X'}, {'rtu_id': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ModbusRtuMaster('COM99', **kwargs)


if __name__ == '__main__':
    unittest.main()

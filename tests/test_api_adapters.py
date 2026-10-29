"""第三方库适配层的离线回归。

不连接用户现场设备：Modbus 调用用假对象验证转发到 pyModbusTCP/pymodbus
的 API，IEC104 验证 c104 点对象和地址映射，CAN 使用 python-can virtual 总线。
"""
import importlib.metadata
import time
import unittest

import c104
import api
from pyModbusTCP.server import ModbusServer

from api.can import CanMaster, CanFrame
from api.iec104 import IEC104Master, IEC104Value
from api.iec104_server import IEC104Server
from api.modbus_rtu import ModbusRtuMaster
from api.modbus_tcp import ModbusTcpMaster, decode_point


class FakeTcp:
    is_open = True
    unit_id = 1
    last_error_as_txt = ''
    last_except_as_txt = ''
    last_except = None

    def read_holding_registers(self, address, count):
        return [address + n for n in range(count)]

    def read_coils(self, address, count):
        return [bool((address + n) & 1) for n in range(count)]

    def write_single_coil(self, address, value):
        return value is True

    def write_single_register(self, address, value):
        return value == 123

    def write_multiple_registers(self, address, values):
        return values == [1, 2]

    def write_multiple_coils(self, address, values):
        return values == [True, False]


class FakeSerial:
    connected = True

    def read_holding_registers(self, address, count, device_id):
        class Reply:
            registers = [address + n for n in range(count)]
            bits = []
            def isError(self): return False
        return Reply()

    def write_register(self, address, value, device_id):
        class Reply:
            def isError(self): return False
        return Reply()


class ModbusAdapterTests(unittest.TestCase):
    def test_tcp_uses_pymodbustcp_client(self):
        master = ModbusTcpMaster('127.0.0.1')
        self.assertEqual(master.native.__class__.__module__, 'pyModbusTCP.client')
        master.native = FakeTcp()
        self.assertEqual(master.read_holding_registers(10, 2), [10, 11])
        self.assertEqual(master.read_coils(10, 2), [False, True])
        self.assertTrue(master.write_single_coil(0, True))
        self.assertTrue(master.write_multiple_registers(0, [1, 2]))
        with self.assertRaises(ValueError):
            master.read_holding_registers(65535, 2)

    def test_tcp_master_configured_poll_and_channel_unit(self):
        calls = [{'rtu_id': 7, 'call_seq': 0, 'irtuaddr': 9,
                  'iplace': 10, 'inum': 2, 'icode': 3}]
        points = [
            {'rtu_id': 7, 'call_seq': 0, 'cnt': 1, 'reg_addr': 10,
             'trans_type': 2},
            {'rtu_id': 7, 'call_seq': 0, 'cnt': 2, 'reg_addr': 11,
             'trans_type': 2},
        ]
        master = ModbusTcpMaster.from_channel(
            {'rtu_id': 7, 'chan_addr': '127.0.0.1', 'chan_port': 502,
             'irtuaddr': 9},
        )
        self.assertEqual(master.unit_id, 9)
        master.configure(calls, points)
        master.native = FakeTcp()
        self.assertEqual([(v.point_no, v.value) for v in master.collect_once()], [(1, 10), (2, 11)])

        with self.assertRaises(ValueError):
            master.configure(calls, [{**points[0], 'call_seq': 1}])

    def test_tcp_master_poll_loop_and_point_commands(self):
        master = ModbusTcpMaster('127.0.0.1', rtu_id=7)
        master.native = FakeTcp()
        calls = [{'rtu_id': 7, 'call_seq': 0, 'irtuaddr': 1,
                  'iplace': 0, 'inum': 1, 'icode': 3}]
        points = [{'rtu_id': 7, 'call_seq': 0, 'cnt': 1, 'reg_addr': 0,
                   'trans_type': 2}]
        master.configure(calls, points)
        batches = []
        self.assertEqual(master.poll_loop(batches.append, interval=0, max_cycles=2), 2)
        self.assertEqual(len(batches), 2)
        definitions = [
            {'rtu_id': 7, 'cnt': 1, 'ifyk': 1, 'reg_addr': 2,
             'icode': 5, 'ytyk_type': 1, 'irtuaddr': 1},
            {'rtu_id': 7, 'cnt': 3, 'ifyk': 0, 'reg_addr': 3,
             'icode': 6, 'ytyk_type': 2, 'irtuaddr': 1},
        ]
        self.assertTrue(master.send_yk_point(1, True, definitions))
        self.assertTrue(master.send_yt_point(3, 123, definitions))
        with self.assertRaises(ValueError):
            master.send_yk_point(9, True, definitions)

    def test_tcp_master_real_local_slave(self):
        server = ModbusServer(host='127.0.0.1', port=0, no_block=True)
        server.data_bank.set_holding_registers(10, [321, 654])
        server.data_bank.set_coils(3, [True, False])
        server.start()
        master = ModbusTcpMaster(
            '127.0.0.1', server._service.server_address[1], unit_id=1, rtu_id=7,
            calls=[{'rtu_id': 7, 'call_seq': 0, 'irtuaddr': 1,
                    'iplace': 10, 'inum': 2, 'icode': 3}],
            points=[
                {'rtu_id': 7, 'call_seq': 0, 'cnt': 1, 'reg_addr': 10,
                 'trans_type': 2},
                {'rtu_id': 7, 'call_seq': 0, 'cnt': 2, 'reg_addr': 11,
                 'trans_type': 2},
            ],
        )
        try:
            with master:
                values = master.collect_once()
                self.assertEqual([item.value for item in values], [321, 654])
                self.assertEqual(master.read_coils(3, 2), [True, False])
                self.assertTrue(master.write_single_coil(4, True))
                self.assertEqual(master.read_coils(4), [True])
        finally:
            server.stop()

    def test_rtu_uses_pymodbus_serial_client(self):
        master = ModbusRtuMaster('loop://', parity='N')
        self.assertEqual(master.native.__class__.__module__, 'pymodbus.client.serial')
        master.native = FakeSerial()
        self.assertEqual(master.read_holding_registers(4, 2), [4, 5])
        self.assertTrue(master.write_single_register(0, 7))

    def test_configured_point_mapping(self):
        self.assertEqual(decode_point(bytes.fromhex('00003f80'),
                         {'trans_type': 5, 'fourbytedata_byteseq': 2}), 1.0)
        self.assertEqual(decode_point(bytes.fromhex('1234'),
                         {'trans_type': 2}), 0x1234)
        self.assertEqual(decode_point(bytes.fromhex('0004'),
                         {'trans_type': 9, 'offset_bit': 2, 'all_bit': 2}), 1)


class IEC104AdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = IEC104Master('127.0.0.1', init='NONE', common_address=10,
                                   ycbegin_addr=0x4001, yxbegin_addr=1,
                                   ykbegin_addr=0x6001, ytbegin_addr=0x6201)

    def tearDown(self):
        self.client.close()

    def test_addresses_and_c104_points(self):
        self.assertEqual(self.client.point_address('yc', 1), 0x4001)
        self.assertEqual(self.client.point_number('yk', 0x6002), 2)
        point = self.client.add_point('yx', 2)
        self.assertEqual(point.type, c104.Type.M_SP_NA_1)
        point.info = c104.SingleInfo(on=True)
        current = self.client.value('yx', 2)
        self.assertIsInstance(current, IEC104Value)
        self.assertTrue(current.value)

    def test_scaled_and_float_point_types(self):
        scaled = self.client.add_point('yc', 2, data_type=2)
        short = self.client.add_point('yc', 3, data_type=1)
        self.assertEqual(scaled.type, c104.Type.M_ME_NB_1)
        self.assertEqual(short.type, c104.Type.M_ME_NC_1)
        scaled.info = c104.ScaledInfo(actual=c104.Int16(123))
        short.info = c104.ShortInfo(actual=12.5)
        self.assertEqual(self.client.value('yc', 2).value, 123)
        self.assertAlmostEqual(self.client.value('yc', 3).value, 12.5)

    def test_commands_use_c104_information_objects(self):
        yk = self.client.add_point('yk', 1)
        yk.info = c104.SingleCmd(on=True)
        self.assertTrue(yk.value)
        yt = self.client.add_point('yt', 1, data_type=3)
        yt.info = c104.ShortCmd(target=3.5)
        self.assertAlmostEqual(float(yt.value), 3.5)

    def test_schema_constructor(self):
        c = IEC104Master.from_channel({'rtu_id': 3, 'chan_addr': '127.0.0.1', 'chan_port': 2404},
                                      {'rtu_addr': 7, 'ycbegin_addr': 100})
        try:
            self.assertEqual(c.common_address, 7)
            self.assertEqual(c.point_address('yc', 1), 100)
        finally:
            c.close()


class IEC104ServerTests(unittest.TestCase):
    def test_server_interrogation_and_monitoring_update(self):
        server = IEC104Server(ip='127.0.0.1', port=25061, common_address=1)
        server.add_point('yx', 1, value=True)
        server.add_point('yc', 1, value=12.5)
        client = IEC104Master('127.0.0.1', 25061, common_address=1, init='NONE')
        try:
            server.start()
            client.add_point('yx', 1)
            client.add_point('yc', 1)
            client.configure([
                {'kind': 'yx', 'pnt_no': 1},
                {'kind': 'yc', 'pnt_no': 1},
            ])
            client.connect(timeout=3)
            self.assertTrue(client.general_interrogation())
            time.sleep(0.1)
            values = {(item.kind, item.point_no): item.value for item in client.collect_once()}
            self.assertEqual(values, {('yx', 1): True, ('yc', 1): 12.5})
            server.update('yc', 1, 15.0)
            time.sleep(0.1)
            self.assertEqual(client.value('yc', 1).value, 15.0)
        finally:
            client.close()
            server.stop()

    def test_server_receives_yk_command_callback(self):
        commands = []
        server = IEC104Server(ip='127.0.0.1', port=25062, common_address=1)
        server.add_point('yk', 1, value=False,
                         command_handler=lambda command: commands.append(command) or True)
        client = IEC104Master('127.0.0.1', 25062, common_address=1, init='NONE')
        try:
            server.start()
            client.add_point('yk', 1)
            client.connect(timeout=3)
            result = client.send_yk_point(1, True)
            self.assertTrue(result.value)
            self.assertEqual(len(commands), 1)
            self.assertEqual((commands[0].kind, commands[0].point_no, commands[0].value),
                             ('yk', 1, True))
            self.assertFalse(commands[0].selected)
            self.assertIsNone(server.last_error)
        finally:
            client.close()
            server.stop()


class CanAdapterTests(unittest.TestCase):
    def test_virtual_bus_send_receive_and_filters(self):
        channel = 'scada-api-test'
        sender = CanMaster(interface='virtual', channel=channel, auto_start=True)
        receiver = CanMaster(interface='virtual', channel=channel, auto_start=True)
        try:
            receiver.set_filters([{'can_id': 0x100, 'can_mask': 0x7ff, 'extended': False}])
            sent = sender.send(0x100, b'\x01\x02')
            received = receiver.recv(timeout=1)
            self.assertIsInstance(sent, CanFrame)
            self.assertEqual(received.arbitration_id, 0x100)
            self.assertEqual(received.data, b'\x01\x02')
            sender.send(0x101, b'\x03')
            self.assertIsNone(receiver.recv(timeout=0.05))
        finally:
            sender.close()
            receiver.close()

    def test_can_fd_and_validation(self):
        bus = CanMaster(interface='virtual', channel='scada-api-fd', fd=True, auto_start=True)
        try:
            frame = bus.send(0x123, bytes(range(12)), is_fd=True, bitrate_switch=True)
            self.assertTrue(frame.is_fd)
            with self.assertRaises(ValueError):
                bus.send(0x800, b'')
        finally:
            bus.close()


class DependencyTests(unittest.TestCase):
    def test_pinned_dependencies_are_installed(self):
        self.assertEqual(importlib.metadata.version('c104'), '2.2.1')
        self.assertEqual(importlib.metadata.version('pyModbusTCP'), '0.3.1')
        self.assertEqual(importlib.metadata.version('pymodbus'), '3.16.1')
        self.assertEqual(importlib.metadata.version('python-can'), '4.6.1')

    def test_public_protocol_surface_has_no_legacy_client_aliases(self):
        import api.can as can_api
        import api.iec104 as iec_api
        import api.modbus_rtu as rtu_api
        import api.modbus_tcp as tcp_api

        for module, names in (
                (tcp_api, ('ModbusTcpClient', 'ModbusOperations', 'ModbusMasterOperations',
                           'decode_legacy_point')),
                (rtu_api, ('ModbusRtuClient',)),
                (can_api, ('CanClient',)),
                (iec_api, ('IEC104Client',))):
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertFalse(hasattr(module, name))
        for name in ('poll_once', 'send_yk', 'send_yt'):
            self.assertFalse(hasattr(api.ModbusTcpMaster, name))
        for name in ('disconnect', 'poll', 'send_yk', 'send_yt'):
            self.assertFalse(hasattr(api.IEC104Master, name))


if __name__ == '__main__':
    unittest.main()

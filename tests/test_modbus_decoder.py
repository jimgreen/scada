"""Independent wire-value fixtures for shared TCP/RTU register decoders."""
import math
import struct
from types import SimpleNamespace
import unittest

from client.modbus_tcp import ModbusError, ModbusTcpMaster, decode_point
from client.modbus_rtu import ModbusRtuMaster


class DecoderTests(unittest.TestCase):
    @staticmethod
    def golden_cases():
        cases = [
            ('8000', dict(trans_type=1), -32768),
            ('ff7f', dict(trans_type=1, twobytedata_byteseq=1), 32767),
            ('ffff', dict(trans_type=2), 65535),
            ('1234', dict(trans_type=2, twobytedata_byteseq=1), 0x3412),
            ('1234', dict(trans_type=6), 0x34),
            ('1234', dict(trans_type=6, twobytedata_byteseq=1), 0x12),
            ('80', dict(trans_type=7), -128),
            ('7f', dict(trans_type=7), 127),
            ('8000000000000000', dict(trans_type=10, twobytedata_byteseq=1), -(2**63)),
            ('7fffffffffffffff', dict(trans_type=10), 2**63 - 1),
            ('ffffffffffffffff', dict(trans_type=11), 2**64 - 1),
            ('0123456789abcdef', dict(trans_type=11, twobytedata_byteseq=1), 0x0123456789abcdef),
        ]
        for order, wire in enumerate(('78563412', '34127856', '56781234', '12345678')):
            cases.extend((wire, dict(trans_type=typ, fourbytedata_byteseq=order), 0x12345678)
                         for typ in (3, 4))
        for order, wire in enumerate(('00000080', '00800000', '00008000', '80000000')):
            cases.append((wire, dict(trans_type=3, fourbytedata_byteseq=order), -(2**31)))
        for order, wire in enumerate(('0000803f', '803f0000', '00003f80', '3f800000')):
            cases.append((wire, dict(trans_type=5, fourbytedata_byteseq=order), 1.0))
        cases.extend([
            ('00000080', dict(trans_type=5), -0.0),
            ('0000807f', dict(trans_type=5), math.inf),
            ('000080ff', dict(trans_type=5), -math.inf),
            ('0000c07f', dict(trans_type=5), math.nan),
        ])
        return cases

    def assert_value(self, actual, expected):
        self.assertIs(type(actual), type(expected))
        if isinstance(expected, float):
            if math.isnan(expected):
                self.assertTrue(math.isnan(actual))
            else:
                self.assertEqual(struct.pack('>d', actual), struct.pack('>d', expected))
        else:
            self.assertEqual(actual, expected)

    def test_golden_frames_with_trailing_bytes_and_attribute_rows(self):
        for wire, definition, expected in self.golden_cases():
            with self.subTest(wire=wire, definition=definition):
                raw = bytes.fromhex(wire) + b'\xa5\x5a'
                for data in (raw, bytearray(raw), memoryview(raw)):
                    self.assert_value(decode_point(data, definition), expected)
                self.assert_value(decode_point(raw, SimpleNamespace(**definition)), expected)

    def test_golden_frames_in_cached_and_explicit_plans_at_nonzero_offsets(self):
        for cls, host in ((ModbusTcpMaster, '127.0.0.1'), (ModbusRtuMaster, 'loop://')):
            master = cls(host, rtu_id=7)
            self.addCleanup(master.close)
            for wire, definition, expected in self.golden_cases():
                payload = bytes.fromhex(wire)
                raw = b'\xaa\xbb' + payload + (b'\x00' if len(payload) % 2 else b'') + b'\xa5\x5a'
                words = [int.from_bytes(raw[i:i + 2], 'big') for i in range(0, len(raw), 2)]
                calls = [dict(rtu_id=7, call_seq=0, iplace=10, inum=len(words), icode=3)]
                points = [dict(rtu_id=7, call_seq=0, cnt=1, reg_addr=11, **definition)]
                master._invoke = lambda *_, words=words: words
                master.configure(calls, points)
                with self.subTest(cls=cls.__name__, wire=wire, definition=definition):
                    for result in (master.collect_once(), master.collect_once(calls, points)):
                        self.assert_value(result[0].value, expected)

    def test_every_bit_offset_width_and_reverse(self):
        for little, word in ((0, 0xa55a), (1, 0x5aa5)):
            for offset in range(16):
                for reverse in (0, 1):
                    definition = dict(trans_type=8, twobytedata_byteseq=little,
                                      offset_bit=offset, if_reverse=reverse)
                    self.assert_value(decode_point(b'\xa5\x5a', definition),
                                      bool((word >> offset) & 1) ^ bool(reverse))
                for width in range(1, 17 - offset):
                    definition = dict(trans_type=9, twobytedata_byteseq=little,
                                      offset_bit=offset, all_bit=width)
                    self.assert_value(decode_point(b'\xa5\x5a', definition),
                                      (word >> offset) & ((1 << width) - 1))

    def test_invalid_metadata_and_short_data(self):
        bad = [dict(trans_type=v) for v in (None, 0, 12, True, 1.5)]
        bad.extend(dict(trans_type=5, fourbytedata_byteseq=v) for v in (-1, 4, True))
        bad.extend(dict(trans_type=8, offset_bit=v) for v in (-1, 16, True))
        bad.extend(dict(trans_type=9, offset_bit=15, all_bit=v) for v in (None, 0, 2, True))
        for definition in bad:
            with self.subTest(definition=definition), self.assertRaises(ValueError):
                decode_point(b'\x00' * 8, definition)
        for typ, size in enumerate((2, 2, 4, 4, 4, 2, 1, 2, 2, 8, 8), 1):
            for length in range(size):
                with self.subTest(typ=typ, length=length), self.assertRaisesRegex(ValueError, '数据不足'):
                    decode_point(b'\x00' * length, dict(trans_type=typ, all_bit=1))
        # Width is checked before other metadata, including for byte/bit points.
        with self.assertRaisesRegex(ValueError, '需要 4 字节，得到 1'):
            decode_point(b'\x00', dict(trans_type=5, fourbytedata_byteseq=99))
        self.assertEqual(decode_point(b'\x12\x34', dict(trans_type=2, twobytedata_byteseq=None)), 0x1234)

    def test_partial_or_excess_response_fails_without_returning_stale_samples(self):
        for cls, host in ((ModbusTcpMaster, '127.0.0.1'), (ModbusRtuMaster, 'loop://')):
            master = cls(host, rtu_id=7)
            self.addCleanup(master.close)
            for fc in (1, 2, 3, 4):
                calls = [dict(rtu_id=7, call_seq=0, iplace=10, inum=4, icode=fc)]
                points = [dict(rtu_id=7, call_seq=0, cnt=1, reg_addr=10,
                               trans_type=8 if fc < 3 else 2)]
                master.configure(calls, points)
                for count in (0, 1, 3):
                    master._invoke = lambda *_, count=count: [1] * count
                    with self.subTest(cls=cls.__name__, fc=fc, count=count):
                        with self.assertRaisesRegex(ModbusError, '响应数量错误'):
                            master.collect_once()
                master._invoke = lambda *_: [1, 1, 1, 1]
                self.assertEqual(master.collect_once()[0].value, True if fc < 3 else 1)


if __name__ == '__main__':
    unittest.main()

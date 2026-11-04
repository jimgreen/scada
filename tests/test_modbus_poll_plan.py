"""Configured plans, temporary overrides and atomic replacements for TCP/RTU."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from client.modbus_tcp import ModbusTcpMaster, decode_point
from client.modbus_rtu import ModbusRtuMaster


class PollPlanTests(unittest.TestCase):
    def masters(self):
        for cls, address in ((ModbusTcpMaster, '127.0.0.1'), (ModbusRtuMaster, 'loop://')):
            master = cls(address, rtu_id=7)
            self.addCleanup(master.close)
            yield master

    @staticmethod
    def tables():
        return ([dict(rtu_id=7, call_seq=0, iplace=10, inum=1, icode=3)],
                [dict(rtu_id=7, call_seq=0, cnt=1, reg_addr=10, trans_type=2)])

    def test_sorted_requests_keep_point_order_scope_offsets_and_default_unit(self):
        for master in self.masters():
            with self.subTest(protocol=type(master).__name__):
                calls = [dict(rtu_id=7, call_seq=seq, iplace=seq * 10, inum=2, icode=seq,
                              irtuaddr=seq + 1) for seq in (4, 2, 3, 1)]
                points = [dict(rtu_id=7, call_seq=seq, cnt=seq * 10 + number, reg_addr=seq * 10 + offset,
                               trans_type=8 if seq < 3 else 2, if_reverse=offset)
                          for seq in (3, 1, 4, 2) for number, offset in ((2, 1), (1, 0))]
                requests = []

                def invoke(name, address, count, unit):
                    requests.append((name, address, count, unit))
                    return [True, False] if address < 30 else [100, 200]

                master._invoke = invoke
                master.configure([*calls, {**calls[0], 'rtu_id': 8}], points)
                result = master.collect_once()
                self.assertEqual(requests, [
                    ('read_coils', 10, 2, 2), ('read_discrete_inputs', 20, 2, 3),
                    ('read_holding_registers', 30, 2, 4), ('read_input_registers', 40, 2, 5)])
                self.assertEqual([(v.call_seq, v.point_no, v.kind, v.value) for v in result], [
                    (seq, seq * 10 + number, 'yx' if seq < 3 else 'yc', True if seq < 3 else value)
                    for seq in (1, 2, 3, 4) for number, value in ((2, 200), (1, 100))])
                calls, points = self.tables()
                master.configure(calls, points)
                master.unit_id = 9
                master.collect_once()
                self.assertEqual(requests[-1][-1], 9)

    def test_temporary_overrides_are_fresh_and_do_not_replace_configured_plan(self):
        for master in self.masters():
            with self.subTest(protocol=type(master).__name__):
                calls, points = self.tables()
                master._invoke = lambda _name, address, count, _unit: list(range(address, address + count))
                master.configure(calls, points)
                alternate = [{**calls[0], 'iplace': 20}]
                moved = [{**points[0], 'reg_addr': 20}]
                self.assertEqual(master.collect_once(iter(alternate), iter(moved))[0].value, 20)
                alternate[0]['iplace'] = moved[0]['reg_addr'] = 30
                self.assertEqual(master.collect_once(alternate, moved)[0].value, 30)
                self.assertEqual(master.collect_once(points=points)[0].value, 10)
                self.assertEqual(master.collect_once(calls=calls)[0].value, 10)
                self.assertEqual(master.collect_once()[0].value, 10)
                calls[0]['iplace'] = points[0]['reg_addr'] = 40
                self.assertEqual(master.collect_once()[0].value, 10)
                master.configure(calls, points)
                self.assertEqual(master.collect_once()[0].value, 40)

    def test_invalid_late_point_and_failed_replacement_do_not_send_any_request(self):
        for master in self.masters():
            with self.subTest(protocol=type(master).__name__):
                calls, points = self.tables()
                requests = []
                master._invoke = lambda *args: requests.append(args) or [123]
                master.configure(calls, points)
                invalid_calls = [*calls, {**calls[0], 'call_seq': 1, 'iplace': 20}]
                invalid_points = [*points, {**points[0], 'call_seq': 1, 'cnt': 2,
                                           'reg_addr': 20, 'trans_type': 5}]
                for operation in (master.configure, master.collect_once):
                    with self.assertRaises(ValueError):
                        operation(invalid_calls, invalid_points)
                    self.assertEqual(requests, [])
                self.assertEqual(master.collect_once()[0].value, 123)

    def test_all_decoder_types_and_byte_orders_work_with_cached_and_temporary_plans(self):
        raw = bytes.fromhex('0123456789abcdef')
        for master in self.masters():
            with self.subTest(protocol=type(master).__name__):
                calls = [dict(rtu_id=7, call_seq=0, iplace=0, inum=4, icode=3)]
                points = [dict(rtu_id=7, call_seq=0, cnt=number, reg_addr=0, trans_type=typ,
                               twobytedata_byteseq=little, fourbytedata_byteseq=order,
                               offset_bit=3, all_bit=5, if_reverse=1)
                          for number, (typ, little, order) in enumerate(
                              ((typ, little, order) for typ in range(1, 12)
                               for little in (0, 1) for order in range(4)), 1)]
                master._invoke = lambda *_: [int.from_bytes(raw[i:i+2], 'big') for i in range(0, 8, 2)]
                expected = [decode_point(raw, point) for point in points]
                master.configure(calls, points)
                for result in (master.collect_once(), master.collect_once(calls, points)):
                    self.assertEqual([v.value for v in result], expected)

    def test_reconfigure_waits_for_entire_inflight_poll(self):
        for master in self.masters():
            with self.subTest(protocol=type(master).__name__):
                calls, points = self.tables()
                calls.append({**calls[0], 'call_seq': 1, 'iplace': 11})
                points.append({**points[0], 'call_seq': 1, 'cnt': 2, 'reg_addr': 11})
                master.configure(calls, points)
                entered, release, replacing = threading.Event(), threading.Event(), threading.Event()

                def invoke(_name, address, _count, _unit):
                    if address == 10:
                        entered.set()
                        if not release.wait(3):
                            raise TimeoutError('test did not release poll')
                    return [address]

                def configure():
                    replacing.set()
                    master.configure([{**calls[0], 'iplace': 20}], [{**points[0], 'reg_addr': 20}])

                master._invoke = invoke
                with ThreadPoolExecutor(max_workers=2) as executor:
                    poll = executor.submit(master.collect_once)
                    try:
                        self.assertTrue(entered.wait(2))
                        replacement = executor.submit(configure)
                        self.assertTrue(replacing.wait(2))
                        self.assertFalse(replacement.done())
                    finally:
                        release.set()
                    self.assertEqual([v.value for v in poll.result(3)], [10, 11])
                    replacement.result(3)
                self.assertEqual([v.value for v in master.collect_once()], [20])


if __name__ == '__main__':
    unittest.main()

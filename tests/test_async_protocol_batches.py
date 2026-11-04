"""Batching must preserve order, quality, fault handling and thread isolation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import time
from types import SimpleNamespace
import unittest

from asyncua import ua

from client.bacnet import BacnetMaster, BacnetError, _LoopThread as BacnetLoop
from client.opc import OpcUaMaster, OpcError, _LoopThread as OpcLoop
from tests.test_opc import _OpcServer


def opc_point(number, *, rtu=7, node=None, kind='yc'):
    return dict(rtu_id=rtu, kind=kind, pnt_no=number,
                node_id=node or f'ns=2;s=YC_{number}')


def bacnet_point(number, *, rtu=7):
    return dict(rtu_id=rtu, kind='yc', pnt_no=number,
                object_type='analog-value', instance=number)


class NativeOpc:
    def __init__(self):
        self.requests = []
        self.limit = 10000
        self.data = {}
        self.failure = None
        self.short = False
        self.delay = 0
        self.cancelled = threading.Event()

    def get_node(self, node_id):
        return SimpleNamespace(nodeid=node_id)

    async def read_attributes(self, nodes):
        ids = [n.nodeid for n in nodes]
        self.requests.append(ids)
        if len(ids) > self.limit:
            raise ua.UaStatusCodeError(ua.StatusCodes.BadTooManyOperations)
        if self.failure:
            raise self.failure
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        result = [self.data.get(node_id, ua.DataValue(ua.Variant(index + 0.25)))
                  for index, node_id in enumerate(ids)]
        return result[:-1] if self.short else result


class BatchTests(unittest.TestCase):
    def native_opc(self, **kwargs):
        master = OpcUaMaster('opc.tcp://127.0.0.1:4840', rtu_id=7, **kwargs)
        master._loop_thread = OpcLoop('test-opc-batches')
        master._client = NativeOpc()
        master._connected = True
        self.addCleanup(master._loop_thread.close)
        return master, master._client

    def native_bacnet(self, reader, **kwargs):
        master = BacnetMaster('127.0.0.1', rtu_id=7, **kwargs)
        master._loop_thread = BacnetLoop('test-bacnet-batches')
        master._app = SimpleNamespace(read_property=reader)
        master._connected = True
        self.addCleanup(master._loop_thread.close)
        return master

    def test_opc_chunks_keep_rtu_filter_order_duplicates_and_quality(self):
        master, native = self.native_opc(read_batch_size=3)
        rows = [opc_point(i) for i in range(1, 8)]
        rows.insert(3, opc_point(99, rtu=8))
        rows.append(opc_point(8, node=rows[0]['node_id'], kind='yx'))
        for index, row in enumerate(rows):
            native.data[row['node_id']] = ua.DataValue(
                ua.Variant(index + .5), ua.StatusCode(ua.StatusCodes.GoodClamped))
        master.configure(rows)
        results = master.collect_once()
        self.assertEqual([v.point_no for v in results], list(range(1, 9)))
        self.assertEqual([len(r) for r in native.requests], [3, 3, 2])
        self.assertEqual(results[0].value, results[-1].value)
        self.assertTrue(all(v.status.value == ua.StatusCodes.GoodClamped for v in results))
        self.assertTrue(all(v.received_at.tzinfo == timezone.utc for v in results))

    def test_opc_read_nodes_supports_generator_and_empty_disconnected(self):
        master, native = self.native_opc(read_batch_size=2)
        self.assertEqual(master.read_nodes((f'ns=2;s={i}' for i in range(3))), [.25, 1.25, .25])
        self.assertEqual([len(r) for r in native.requests], [2, 1])
        master._connected = False
        self.assertEqual(master.read_nodes([]), [])
        with self.assertRaises(OpcError):
            master.read_nodes(['ns=2;s=1'])

    def test_opc_server_limit_is_learned_and_no_reads_are_duplicated(self):
        master, native = self.native_opc(read_batch_size=8)
        native.limit = 2
        master.configure([opc_point(i) for i in range(8)])
        self.assertEqual(len(master.collect_once()), 8)
        self.assertEqual([len(r) for r in native.requests], [8, 4, 2, 2, 2, 2])
        native.requests.clear()
        self.assertEqual(len(master.collect_once()), 8)
        self.assertEqual([len(r) for r in native.requests], [2, 2, 2, 2])
        self.assertEqual(len({n for r in native.requests for n in r}), 8)

    def test_opc_limit_one_fails_without_infinite_retry(self):
        master, native = self.native_opc(read_batch_size=1)
        native.limit = 0
        with self.assertRaises(OpcError):
            master.read_nodes(['ns=2;s=1'])
        self.assertEqual(len(native.requests), 1)

    def test_opc_bad_status_does_not_emit_partial_batch_or_retry(self):
        master, native = self.native_opc(read_batch_size=2)
        rows = [opc_point(i) for i in range(5)]
        native.data[rows[1]['node_id']] = ua.DataValue(
            ua.Variant(5.0), ua.StatusCode(ua.StatusCodes.BadOutOfService))
        master.configure(rows)
        with self.assertRaises(OpcError):
            master.collect_once()
        self.assertEqual(len(native.requests), 1)
        native.data.clear()
        self.assertEqual(len(master.collect_once()), 5)

    def test_opc_malformed_response_and_missing_status_are_errors(self):
        master, native = self.native_opc()
        native.short = True
        with self.assertRaisesRegex(OpcError, '数量不匹配'):
            master.read_nodes(['ns=2;s=1'])
        native.short = False
        native.data['ns=2;s=1'] = SimpleNamespace(StatusCode=None)
        with self.assertRaisesRegex(OpcError, '状态码'):
            master.read_nodes(['ns=2;s=1'])

    def test_opc_transport_error_never_retries(self):
        master, native = self.native_opc()
        native.failure = OSError('connection lost')
        with self.assertRaisesRegex(OpcError, 'connection lost'):
            master.read_nodes(['ns=2;s=1', 'ns=2;s=2'])
        self.assertEqual(len(native.requests), 1)

    def test_opc_point_bad_too_many_operations_is_not_a_service_retry(self):
        master, native = self.native_opc()
        native.data['ns=2;s=1'] = ua.DataValue(
            ua.Variant(5.0), ua.StatusCode(ua.StatusCodes.BadTooManyOperations))
        with self.assertRaises(OpcError):
            master.read_nodes(['ns=2;s=1', 'ns=2;s=2'])
        self.assertEqual(len(native.requests), 1)
        self.assertEqual(master._read_limit, master.read_batch_size)

    def test_opc_timeout_cancels_work_and_does_not_read_later_chunks(self):
        master, native = self.native_opc(timeout=.03, read_batch_size=2)
        native.delay = 10
        start = time.monotonic()
        with self.assertRaisesRegex(OpcError, '超时'):
            master.read_nodes([f'ns=2;s={i}' for i in range(5)])
        self.assertLess(time.monotonic() - start, 1)
        self.assertTrue(native.cancelled.wait(.5))
        self.assertEqual(len(native.requests), 1)

    def test_opc_batch_size_validation_and_definition_reload(self):
        for invalid in (True, 0, -1, 1.5, '100'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                OpcUaMaster('opc.tcp://127.0.0.1:4840', read_batch_size=invalid)
        master, native = self.native_opc()
        master.configure([opc_point(1)])
        master.collect_once()
        master.configure([opc_point(2, node='ns=2;s=new')])
        native.requests.clear()
        self.assertEqual(master.collect_once()[0].point_no, 2)
        self.assertEqual(native.requests, [['ns=2;s=new']])

    def test_bacnet_keeps_sequential_requests_binary_conversion_and_point_times(self):
        requests, times = [], []
        active = 0

        async def reader(address, object_id, prop, **kwargs):
            nonlocal active
            self.assertEqual(active, 0)
            active += 1
            await asyncio.sleep(0)
            requests.append((address, object_id, prop))
            times.append(datetime.now(timezone.utc))
            active -= 1
            return SimpleNamespace(attr='active') if object_id.startswith('binary') else len(requests) + .25
        master = self.native_bacnet(reader)
        rows = [bacnet_point(i) for i in range(1, 103)]
        rows.append(dict(rtu_id=7, kind='yx', pnt_no=1, object_type='binary-input', instance=103))
        rows.append(bacnet_point(999, rtu=8))
        master.configure(rows)
        values = master.collect_once()
        self.assertEqual(len(values), 103)
        self.assertEqual([n for _, n, _ in requests],
                         [f'analog-value,{i}' for i in range(1, 103)] + ['binary-input,103'])
        self.assertIs(values[-1].value, True)
        self.assertTrue(all(v.received_at >= t for v, t in zip(values, times)))
        self.assertGreater(values[-1].received_at, values[0].received_at)

    def test_bacnet_property_selection_and_reconfiguration(self):
        seen = []

        async def reader(_address, object_id, prop, **_kwargs):
            seen.append((object_id, prop))
            return 'inactive'
        master = self.native_bacnet(reader)
        row = dict(rtu_id=7, kind='yx', pnt_no=1, object_type='binary-input', instance=1)
        master.configure([row])
        self.assertIs(master.collect_once()[0].value, False)
        master.configure([dict(row, instance=2, property_identifier='description')])
        self.assertEqual(master.collect_once()[0].value, 'inactive')
        self.assertEqual(seen, [('binary-input,1', 'presentValue'), ('binary-input,2', 'description')])

    def test_bacnet_quality_property_failure_keeps_process_value(self):
        async def reader(_address, _object_id, prop, **_kwargs):
            if prop == 'statusFlags':
                raise OSError('statusFlags unsupported')
            return 17.5

        master = self.native_bacnet(reader)
        master.configure([dict(
            rtu_id=7, kind='yc', pnt_no=1, object_type='analog-value', instance=1,
            quality_property='statusFlags',
        )])
        value = master.collect_once()[0]
        self.assertEqual((value.value, value.quality), (17.5, 0))

    def test_bacnet_failure_stops_batch_and_next_collection_recovers(self):
        seen = []

        async def reader(_address, object_id, _prop, **_kwargs):
            seen.append(object_id)
            if object_id.endswith(',2'):
                raise OSError('device offline')
            return 10
        master = self.native_bacnet(reader)
        master.configure([bacnet_point(i) for i in range(1, 4)])
        with self.assertRaisesRegex(BacnetError, 'device offline'):
            master.collect_once()
        self.assertEqual(seen, ['analog-value,1', 'analog-value,2'])
        master.configure([bacnet_point(1)])
        self.assertEqual(master.collect_once()[0].value, 10)

    def test_bacnet_timeout_is_per_point_and_cancels_without_later_reads(self):
        seen = []
        cancelled = threading.Event()

        async def reader(_address, object_id, _prop, **_kwargs):
            seen.append(object_id)
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        master = self.native_bacnet(reader, timeout=.03)
        master.configure([bacnet_point(i) for i in range(3)])
        start = time.monotonic()
        with self.assertRaisesRegex(BacnetError, '超时'):
            master.collect_once()
        self.assertLess(time.monotonic() - start, 1)
        self.assertTrue(cancelled.wait(.5))
        self.assertEqual(seen, ['analog-value,0'])

    def test_bacnet_total_batch_may_exceed_single_point_timeout(self):
        async def reader(*_args, **_kwargs):
            await asyncio.sleep(.025)
            return 5.5
        master = self.native_bacnet(reader, timeout=.1)
        master.configure([bacnet_point(i) for i in range(6)])
        start = time.monotonic()
        self.assertEqual(len(master.collect_once()), 6)
        self.assertGreater(time.monotonic() - start, master.timeout)

    def test_slow_bacnet_does_not_block_another_protocol_thread(self):
        entered = threading.Event()

        async def reader(*_args, **_kwargs):
            entered.set()
            await asyncio.sleep(10)
        slow = self.native_bacnet(reader, timeout=.15)
        slow.configure([bacnet_point(1)])
        fast, _native = self.native_opc()
        fast.configure([opc_point(1)])
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(slow.collect_once)
            self.assertTrue(entered.wait(1))
            self.assertEqual(pool.submit(fast.collect_once).result(1)[0].point_no, 1)
            with self.assertRaises(BacnetError):
                future.result(1)

    def test_filtered_out_points_do_not_require_connection(self):
        for master, row in [(OpcUaMaster('opc.tcp://127.0.0.1:4840', rtu_id=7), opc_point(1, rtu=8)),
                            (BacnetMaster('127.0.0.1', rtu_id=7), bacnet_point(1, rtu=8))]:
            with self.subTest(protocol=type(master).__name__):
                master.configure([row])
                self.assertEqual(master.collect_once(), [])
                with self.assertRaises(ValueError):
                    master.collect_once([])

    def test_real_opc_batch_read_and_bad_node_error(self):
        server = _OpcServer()
        server.start()
        self.addCleanup(server.close)
        master = OpcUaMaster(server.endpoint, rtu_id=7, read_batch_size=2)
        with master:
            self.assertEqual(master.read_nodes([server.node_id] * 3), [12.5] * 3)
            master.configure([opc_point(i, node=server.node_id) for i in range(3)])
            self.assertEqual([v.value for v in master.collect_once()], [12.5] * 3)
            with self.assertRaises(OpcError):
                master.read_nodes([server.node_id, 'ns=2;s=missing'])
            master.write_node(server.node_id, 73.25)
            self.assertEqual(master.read_nodes([server.node_id]), [73.25])


if __name__ == '__main__':
    unittest.main()

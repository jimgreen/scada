"""Large diagnostic selections and Modbus command dispatch boundaries."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from core.runtime_status import RuntimeStatusStore, runtime_status_rows
from scdfe_c.web.service import ScadaStore
from server.modbus_trans_server import ModbusTcpTransServer, ExcCodes


class DiagnosticSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ScadaStore(Path(temporary.name)/'test.sqlite3', connection_pool_size=1)
        self.addCleanup(self.store.close)
        with self.store.connection() as con:
            for role in ('scdfe_c', 'scdfe_s', 'scdfe_t'):
                con.executemany('INSERT INTO fes_channel_runtime_status(service_name,channel_id,state,diagnostic_json) VALUES(?,?,?,?)',
                                [(role, n, 'running', '{"rx":12}') for n in range(1, 11)])
            con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)

    def test_chunks_preserve_all_services_order_and_remove_duplicate_ids(self):
        selection = [10, '2', 2, -1, 0, 'bad', 5, 6, 7, 1, 4, 3, 8, 9, 999]
        rows = runtime_status_rows(self.store, channel_ids=iter(selection))
        self.assertEqual([(r['service_name'], r['channel_id']) for r in rows],
                         [(s, n) for s in ('scdfe_c', 'scdfe_s', 'scdfe_t') for n in range(1, 11)])
        self.assertTrue(all(r['diagnostic'] == {'rx':12} for r in rows))
        selected = runtime_status_rows(self.store, service_name='scdfe_s', channel_ids=selection)
        self.assertEqual([r['channel_id'] for r in selected], list(range(1, 11)))
        self.assertEqual(runtime_status_rows(self.store, channel_ids=[]), [])
        self.assertEqual([r['channel_id'] for r in runtime_status_rows(
            self.store, service_name='scdfe_t', channel_id=3, channel_ids=selection)], [3])

    def test_chunks_share_snapshot_but_next_request_observes_external_commit(self):
        other = self.store.open_connection()
        self.addCleanup(other.close)
        reads = []
        def trace(sql):
            if sql.startswith('SELECT * FROM "fes_channel_runtime_status"'):
                reads.append(sql)
                if len(reads) == 2:
                    other.execute("UPDATE fes_channel_runtime_status SET state='error' WHERE channel_id=10")
        with self.store.connection() as con:
            con.set_trace_callback(trace)
        try:
            rows = runtime_status_rows(self.store, service_name='scdfe_c', channel_ids=range(1, 11))
        finally:
            with self.store.connection() as con:
                con.set_trace_callback(None)
                self.assertFalse(con.in_transaction)
        self.assertEqual(len(reads), 4)
        self.assertTrue(all(row['state'] == 'running' for row in rows))
        self.assertEqual(runtime_status_rows(self.store, service_name='scdfe_c', channel_id=10)[0]['state'], 'error')

    def test_upsert_keeps_version_and_rolls_back_failed_write(self):
        with self.store.connection() as con:
            con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            con.execute("CREATE TRIGGER reject_status BEFORE UPDATE ON fes_channel_runtime_status WHEN NEW.state='bad' BEGIN SELECT RAISE(ABORT,'rejected'); END")
        status = RuntimeStatusStore(self.store, 'scdfe_c', write_interval_ms=60000)
        self.assertTrue(status.upsert(1, dict(state='running', rx_count=12), force=True))
        before = status.list(channel_id=1)[0]
        with self.assertRaises(sqlite3.IntegrityError):
            status.upsert(1, dict(state='bad'), force=True)
        self.assertEqual(status.list(channel_id=1)[0], before)
        self.assertTrue(status.upsert(1, dict(state='running', rx_count=13), force=True))
        self.assertEqual(status.list(channel_id=1)[0]['_version'], before['_version'] + 1)


class CommandDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_wide_points_called_once_in_wire_order_and_invalid_tail_calls_none(self):
        calls = []
        server = ModbusTcpTransServer(points=[dict(kind='yt', pnt_no=n+1, address=2*n,
            register_area='holding', data_type='uint32') for n in range(3)],
            command_handler=lambda *args: calls.append(args) or True)
        self.addCleanup(server.close)
        self.assertIsNone(await server._action(16, 0, 0, 6, [], [0, 11, 0, 22, 0, 33]))
        self.assertEqual(calls, [('yt', 1, 11), ('yt', 2, 22), ('yt', 3, 33)])
        calls.clear()
        self.assertEqual(await server._action(16, 0, 0, 7, [], [0]*7), ExcCodes.ILLEGAL_ADDRESS)
        self.assertEqual(calls, [])
        self.assertEqual(await server._action(16, 0, 1, 2, [], [0]*2), ExcCodes.ILLEGAL_VALUE)
        self.assertEqual(calls, [])

    async def test_coil_commands_do_not_cross_read_only_measurement(self):
        calls = []
        server = ModbusTcpTransServer(points=[dict(kind='yk' if n<7 else 'yx', pnt_no=n+1,
            address=n, register_area='coil', data_type='bool') for n in range(8)],
            command_handler=lambda *args: calls.append(args) or True)
        self.addCleanup(server.close)
        self.assertEqual(await server._action(15, 0, 0, 8, [], [True]*8), ExcCodes.ILLEGAL_ADDRESS)
        self.assertEqual(calls, [])
        self.assertIsNone(await server._action(15, 0, 0, 7, [], [True]*7))
        self.assertEqual(calls, [('yk', n, True) for n in range(1, 8)])

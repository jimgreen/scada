from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from client.can import CanFrame, CanMaster
from scdfe_c.web.service import ApiError, ScadaStore
from scdfe_t.trans import TransRuntime


class ObservedConnection:
    def __init__(self, connection, calls):
        self.connection, self.calls = connection, calls

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, params=()):
        if sql.startswith('UPDATE'):
            self.calls.append(('execute', sql))
        return self.connection.execute(sql, params)

    def executemany(self, sql, params):
        self.calls.append(('executemany', sql))
        return self.connection.executemany(sql, params)


class BatchStoreTests(unittest.TestCase):
    def test_single_row_projection_keeps_physical_order_independence_bool_and_bytes(self):
        con = sqlite3.connect(':memory:'); self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT 7 AS _version, X'616263' AS label, NULL AS flag, 1 AS enabled").fetchone()
        columns = [dict(eng_name=name, data_type=typ) for name, typ in [('enabled','bool'),('label','str'),('flag','bool')]]
        self.assertEqual(self.store._public_row(row, columns), dict(enabled=True,label='abc',flag=None,_version=7))

    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.store = ScadaStore(Path(root.name)/'scada.sqlite3', connection_pool_size=2)
        self.addCleanup(self.store.close)
        for rtu in (1, 2):
            self.store.create_row('fes_rtu_define', dict(rtu_id=rtu, channel_id=rtu, channel_type='iec104'))
            for no in (1, 2):
                self.store.create_row('fes_yc_define', dict(rtu_id=rtu, pnt_no=no, value=0))
        self.calls = []
        original = self.store.connection
        @contextmanager
        def observed():
            with original() as con:
                yield ObservedConnection(con, self.calls)
        self.patch = patch.object(self.store, 'connection', observed)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_multi_rtu_batches_keep_order_repeated_keys_versions_and_return_rows(self):
        before = self.store.get_row('fes_yc_define', [1, 1])['_version']
        changes = [('fes_yc_define', [r, n], dict(value=r*10+n)) for r in (1, 2) for n in (1, 2)]
        changes += [('fes_yc_define', [1, 1], dict(value=99)),
                    ('fes_yc_define', [1, 1], dict(status=128)),
                    ('fes_yc_define', [1, 1], dict(value=100))]
        rows = self.store.update_rows(changes, return_rows=True)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[0][0], 'executemany')
        self.assertEqual([row['value'] for row in rows[:4]], [100, 12, 21, 22])
        self.assertEqual(rows[0]['status'], 128)
        self.assertEqual(rows[0]['_version'], before+4)

    def test_late_missing_row_rolls_back_every_station_and_trigger_failure(self):
        before = [self.store.get_row('fes_yc_define', [r, 1]) for r in (1, 2)]
        with self.assertRaises(ApiError) as failure:
            self.store.update_rows([('fes_yc_define', [r, n], dict(value=77)) for r, n in [(1, 1), (2, 1), (2, 99)]])
        self.assertEqual(failure.exception.status, 404)
        self.assertEqual([self.store.get_row('fes_yc_define', [r, 1]) for r in (1, 2)], before)
        with self.store.connection() as con:
            con.execute("CREATE TRIGGER reject_second BEFORE UPDATE ON fes_yc_define WHEN NEW.rtu_id=2 BEGIN SELECT RAISE(ABORT,'fixture'); END")
            con.commit()
        with self.assertRaises(ApiError):
            self.store.update_rows([('fes_yc_define', [r, 1], dict(value=77)) for r in (1, 2)])
        self.assertEqual([self.store.get_row('fes_yc_define', [r, 1]) for r in (1, 2)], before)

    def test_merged_modbus_validation_still_runs_inside_batch(self):
        self.store.create_row('fes_channel_modbus_define_data', dict(id=1, channel_id=1, call_seq=1, cnt=1, trans_type=9, offset_bit=12, all_bit=4))
        before = self.store.get_row('fes_yc_define', [1, 1])
        with self.assertRaises(ApiError):
            self.store.update_rows([('fes_yc_define', [1, 1], dict(value=99)),
                ('fes_channel_modbus_define_data', [1], dict(all_bit=5))])
        self.assertEqual(self.store.get_row('fes_yc_define', [1, 1]), before)


class CanFrameBatchTests(unittest.TestCase):
    def test_shared_word_decode_matches_scalar_for_fd_endianness_signed_float_quality(self):
        master = CanMaster(interface='virtual', channel='round3-unused', rtu_id=7)
        self.addCleanup(master.close)
        rows = []
        for no in range(1, 65):
            typ = ['uint', 'int', 'bool', 'float'][no % 4]
            rows.append(dict(kind='yx' if typ == 'bool' else 'yc', pnt_no=no, frame_id=123,
                is_fd=True, byte_offset=(no%8)*4, bit_offset=0 if typ == 'float' else no%8,
                bit_length=32 if typ == 'float' else 1 if typ == 'bool' else 13,
                byte_order='big' if no%3 else 'little', value_type=typ,
                scale=.25, offset=-2, quality_byte_offset=63, quality_mask=128))
        master.configure(rows)
        frame = CanFrame(123, bytes(range(64)), is_fd=True, timestamp=1)
        expected = [master._decode_point(frame, row, 7) for row in master._points]
        class CountedInt(int):
            calls = 0
            @classmethod
            def from_bytes(cls, *args, **kwargs):
                cls.calls += 1
                return int.from_bytes(*args, **kwargs)
        with patch('client.can.int', CountedInt, create=True):
            self.assertEqual(master._decode_frames([frame]), expected)
        self.assertLessEqual(CountedInt.calls, 2)
        with self.assertRaises(ValueError):
            master._decode_frames([frame._replace(data=b'\x00')])
        master.configure([])
        self.assertEqual(master._decode_frames([frame]), [frame])


class TransRuntimeBatchTests(unittest.TestCase):
    def test_runtime_write_batch_rolls_back_and_can_retry(self):
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.execute('CREATE TABLE fes_scdfe_t_point_define(channel_id INT,pnt_no INT,kind TEXT,value REAL,status INT,_version INT DEFAULT 1)')
        con.executemany("INSERT INTO fes_scdfe_t_point_define(channel_id,pnt_no,kind,value) VALUES(1,?,'yc',0)", [(1,), (2,)])
        con.commit()
        calls = []
        @contextmanager
        def connection():
            try:
                yield ObservedConnection(con, calls)
            finally:
                con.rollback()
        runtime = object.__new__(TransRuntime)
        runtime.store = Mock(connection=connection)
        runtime.channel_id = 1; runtime.logger = Mock(); runtime._runtime_error = Mock()
        runtime._point_runtime_pending = {('yc', n):dict(value=n, status=128) for n in (1, 2)}
        runtime._point_runtime_state = {}
        con.execute("CREATE TRIGGER reject_second BEFORE UPDATE ON fes_scdfe_t_point_define WHEN NEW.pnt_no=2 BEGIN SELECT RAISE(ABORT,'fixture'); END")
        self.assertFalse(runtime._flush_point_runtime())
        self.assertEqual(list(con.execute('SELECT value FROM fes_scdfe_t_point_define')), [(0,), (0,)])
        self.assertEqual(len(runtime._point_runtime_pending), 2)
        con.execute('DROP TRIGGER reject_second'); calls.clear()
        self.assertTrue(runtime._flush_point_runtime())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'executemany')
        self.assertEqual(list(con.execute('SELECT value,status,_version FROM fes_scdfe_t_point_define')), [(1,128,1), (2,128,1)])
        self.assertEqual(runtime._point_runtime_pending, {})

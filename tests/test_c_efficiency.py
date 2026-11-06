"""Boundaries of the C dashboard, log tail and channel configuration revisions."""
from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scdfe_c import ScdfeLogger
from scdfe_c.web.service import ScadaStore, ApiError


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = ScadaStore(self.root / 'db.sqlite3', connection_pool_size=2)
        self.addCleanup(self.store.close)

    def test_summary_and_full_overview_include_every_station(self):
        with self.store.connection() as con:
            con.executemany("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,?)",
                            ((i, i, 'opc' if i > 500 else 'bacnet') for i in range(1, 508)))
            con.commit()
        self.assertEqual(self.store.overview()['channel_types'], {'bacnet': 500, 'opc': 7})
        summary = self.store.overview(summary=True)['counts']
        self.assertEqual(len(summary), 5)
        self.assertEqual(summary['fes_rtu_define'], 507)
        self.assertEqual(self.store.overview(507)['counts']['fes_rtu_define'], 1)
        with self.assertRaises(ApiError):
            self.store.overview(9999)

    def test_move_invalidates_both_owners_once_without_touching_others(self):
        for rid in (1, 2, 3):
            self.store.create_row('fes_rtu_define', dict(rtu_id=rid, channel_id=rid, channel_type='modbus-tcp'))
        table = 'fes_channel_modbus_define_call'
        self.store.create_row(table, dict(id=5, rtu_id=1, call_seq=1))
        def tokens():
            with self.store.connection() as con:
                return [r[0] for r in con.execute('SELECT definition_version FROM _scada_rtu_version ORDER BY rtu_id')]
        before = tokens()
        self.store.update_row(table, [5], {'rtu_id': 2})
        self.assertEqual(tokens(), [before[0] + 1, before[1] + 1, before[2]])
        self.store.update_row(table, [5], {'rtu_id': 2})
        self.assertEqual(tokens(), [before[0] + 1, before[1] + 1, before[2]])
        self.store.update_row(table, [5], {'rtu_id': 1, 'name': 'move with edit'})
        self.assertEqual(tokens(), [before[0] + 2, before[1] + 2, before[2]])

    def test_log_rotation_preserves_clock_and_flushes_each_write(self):
        logger = ScdfeLogger(self.root, process_name='scdfe_c-main')
        with logger.session(), patch('scdfe_c.datetime', wraps=datetime) as clock:
            clock.now.side_effect = [datetime(2026, 11, 6, 23, 59, 59), datetime(2026, 11, 7, 0, 0, 0)]
            logger.normal('before')
            logger.packet('TX', b'\x12', 'Modbus')
            first = self.root / '2026/11/06/23/scdfe_c-main-20261106-23.log'
            last = self.root / '2026/11/07/00/scdfe_c-main-20261107-00.log'
            self.assertEqual(first.read_text(encoding='utf8').splitlines(), ['23:59:59 [日志] before'])
            second = last.read_text(encoding='utf8').splitlines()
            self.assertEqual(len(second), 1)
            self.assertTrue(second[0].startswith('00:00:00 [报文] 报文 TX '))
            self.assertTrue(second[0].endswith('HEX=12'))

    def test_tail_matches_bounded_reference_at_line_and_byte_boundaries(self):
        p = self.root / 'scdfe_c-main-20261106-18.log'
        for data in (b'', b'last line', b'line\n\n', b'\r\n'*10000,
                     ('中文\r\n温度\u2028' * 230000 + 'end').encode(), b'x'*2100000,
                     b'z'*2000000+b'\nline1\r\n\nend'):
            p.write_bytes(data)
            start = max(0, len(data)-2000000)
            usable = data[start:]
            if start:
                usable = usable.partition(b'\n')[2]
            lines = usable.decode('utf8', 'replace').splitlines()
            for n in (1, 200, 5000):
                row = ScdfeLogger.read_file(self.root, p.name, tail=n)
                self.assertEqual((row['content'],row['line_count'],row['truncated']),
                                 ('\n'.join(lines[-n:]),len(lines[-n:]),bool(start or len(lines)>n)))

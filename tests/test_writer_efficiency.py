"""Mixed heartbeat/value batches must preserve row, field and version semantics."""
from copy import deepcopy
from itertools import product
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

import scdfe_c as runtime


def group(columns, rows, unique=True, **extra):
    return dict(table="points", key_names=("pnt_no", "rtu_id"),
                value_names=columns, rows=rows, _unique_rows=unique, **extra)


class WriterEfficiencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="scada-writer-test-")
        self.addCleanup(tmp.cleanup)
        self.connection = sqlite3.connect(Path(tmp.name) / "test.sqlite3")
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE points (rtu_id INTEGER, pnt_no INTEGER, "
                                "value REAL DEFAULT 0, status INTEGER DEFAULT 0, "
                                "refresh_time INTEGER DEFAULT 0, _version INTEGER DEFAULT 1, "
                                "PRIMARY KEY(rtu_id,pnt_no))")
        self.connection.executemany("INSERT INTO points(rtu_id,pnt_no) VALUES(?,?)",
                                    [(rtu, point) for rtu in (7, 8) for point in (1, 2, 3)])
        self.connection.commit()

    def flush(self, messages, success=True):
        original = deepcopy(messages)
        queue = Mock()
        self.assertIs(runtime._flush_batch(None, Mock(), messages, queue, self.connection), success)
        self.assertEqual(queue.task_done.call_count, len(messages))
        self.assertEqual(messages, original)
        self.assertFalse(self.connection.in_transaction)

    def row(self, rtu=7, point=1):
        return self.connection.execute("SELECT value,status,refresh_time,_version FROM points "
                                       "WHERE rtu_id=? AND pnt_no=?", (rtu, point)).fetchone()

    def test_disjoint_shapes_and_station_keys(self):
        self.flush([dict(op="batch", groups=[
            group(("value", "status"), [(12.5, 2, 1, 7), [13.5, 3, 1, 8]]),
            group(("refresh_time",), [(100, 2, 7), (101, 2, 8)]),
            group(("value", "status"), [(14.5, 4, 3, 7)]),
        ])])
        self.assertEqual(self.row(), (12.5, 2, 0, 2))
        self.assertEqual(self.row(8), (13.5, 3, 0, 2))
        self.assertEqual(self.row(7, 2), (0, 0, 100, 2))
        self.assertEqual(self.row(8, 2), (0, 0, 101, 2))
        self.assertEqual(self.row(7, 3), (14.5, 4, 0, 2))
        self.assertEqual(self.row(8, 3), (0, 0, 0, 1))

    def test_overlap_in_third_group_merges_once_for_all_unique_markers(self):
        for flags in product((False, True), repeat=3):
            with self.subTest(flags=flags):
                before = self.row()[3]
                self.flush([dict(op="batch", groups=[
                    group(("value", "status"), [(12.5, 2, 1, 7)], flags[0]),
                    group(("refresh_time",), [(100, 2, 7)], flags[1]),
                    group(("status", "refresh_time"), [(3, 101, 1, 7)], flags[2]),
                ])])
                self.assertEqual(self.row(), (12.5, 3, 101, before + 1))

    def test_duplicates_in_first_marked_group_still_merge_when_second_group_exists(self):
        self.flush([dict(op="batch", groups=[
            group(("value",), [(12.5, 1, 7), (14.5, 1, 7)]),
            group(("refresh_time",), [(100, 2, 7)]),
        ])])
        self.assertEqual(self.row(), (14.5, 0, 0, 2))

    def test_shared_bindings_and_tuple_subclass_are_normalized(self):
        class Row(tuple):
            def __getitem__(self, key):
                raise AssertionError("normalize before slicing")
        self.flush([dict(op="batch", groups=[
            group(("value", "status"), [Row((5, 1, 7))], value_indexes=(0, 0)),
            group(("refresh_time",), [[100, 2, 7]]),
        ])])
        self.assertEqual(self.row(), (5, 5, 0, 2))

    def test_bad_second_group_rejects_entire_transaction(self):
        for bad in (42, (5, 2), (5, [], 7)):
            with self.subTest(bad=bad):
                self.flush([dict(op="batch", groups=[
                    group(("value",), [(12.5, 1, 7)]),
                    group(("refresh_time",), [bad]),
                ])], success=False)
                self.assertEqual(self.row(), (0, 0, 0, 1))

    def test_preserved_messages_increment_per_message_and_keep_last_fields(self):
        messages = [dict(op="batch", _preserve_versions=True, groups=[
            group(("value", "status"), [(cycle + .5, cycle, 1, 7)]),
            group(("refresh_time",), [(cycle, 2, 7)]),
        ]) for cycle in range(1, 4)]
        self.flush(messages)
        self.assertEqual(self.row(), (3.5, 3, 0, 4))
        self.assertEqual(self.row(7, 2), (0, 0, 3, 4))

    def test_unmarked_messages_keep_cross_message_coalescing(self):
        self.flush([dict(op="batch", groups=[group(("value",), [(1, 1, 7)])]),
                    dict(op="batch", groups=[group(("status",), [(2, 1, 7)])])])
        self.assertEqual(self.row(), (1, 2, 0, 2))

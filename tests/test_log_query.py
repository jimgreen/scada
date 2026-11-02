import tempfile
from pathlib import Path
from datetime import datetime, timezone
import unittest
import base64
import json
from unittest.mock import patch

import web.log_query as log_query

from web.log_query import search_logs


class LogQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Keep the files deterministic and exercise date reconstruction from
        # the hourly filename. The production logger writes this compact form.
        for process in ("main", "db-writer"):
            (self.root / f"scdfe_c-{process}-20261101-10.log").write_text(
                "10:00:00 [进程] 旧过程\n"
                "10:01:00 [日志] 关键字正常\n"
                "10:02:00 [警告] 关键字告警\n"
                "10:03:00 [错误] 关键字错误\n",
                encoding="utf-8",
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_combined_keyword_time_level_and_process_filters(self):
        result = search_logs(self.root, {
            "q": "关键字", "level": "ERROR", "process": "scdfe_c-main",
            "start": datetime(2026, 11, 1, 10, 2).astimezone().isoformat(),
            "end": datetime(2026, 11, 1, 10, 3).astimezone().isoformat(),
        })
        self.assertEqual(len(result["items"]), 1)
        self.assertIn("关键字错误", result["items"][0]["text"])
        self.assertFalse(result["has_more"])

    def test_cursor_paginates_earlier_matching_lines(self):
        first = search_logs(self.root, {"q": "关键字", "limit": "1", "process": "scdfe_c-main"})
        self.assertEqual(len(first["items"]), 1)
        self.assertTrue(first["next_cursor"])
        second = search_logs(self.root, {
            "q": "关键字", "limit": "2", "process": "scdfe_c-main", "cursor": first["next_cursor"],
        })
        self.assertEqual(len(second["items"]), 2)
        self.assertNotEqual(first["items"][0]["text"], second["items"][0]["text"])

    def test_all_file_scope_and_safe_validation(self):
        result = search_logs(self.root, {"q": "正常", "level": "INFO"})
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all("filename" in item for item in result["items"]))
        for query in (
            {"level": "BAD"}, {"start": "2026-11-02T00:00:00+08:00", "end": "2026-11-01T00:00:00+08:00"},
            {"filename": "../../secret.log"}, {"limit": "0"}, {"q": "x" * 257},
        ):
            with self.assertRaises(ValueError):
                search_logs(self.root, query)

    def test_chinese_log_headers_are_searchable_with_canonical_levels(self):
        path = self.root / "scdfe_c-main-20261101-11.log"
        path.write_text(
            "11:00:00 [日志] 中文日志\n"
            "11:01:00 [警告] 中文警告\n"
            "11:02:00 [错误] 中文错误\n"
            "11:03:00 [进程] 中文进程\n",
            encoding="utf-8",
        )
        for code, label in (("INFO", "日志"), ("WARNING", "警告"), ("ERROR", "错误"), ("PROCESS", "进程")):
            with self.subTest(level=code):
                result = search_logs(self.root, {"filename": path.name, "level": code})
                self.assertEqual(len(result["items"]), 1)
                self.assertEqual(result["items"][0]["level"], code)
                self.assertIn(f"[{label}]", result["items"][0]["text"])
                self.assertIsInstance(result["items"][0]["time"], int)

    def test_old_headers_are_not_recognized_as_typed_records(self):
        path = self.root / "scdfe_c-main-20261101-11.log"
        path.write_text("11:00:00 [INFO] old type\n2026-11-01T11:00:00+08:00 [日志] old clock\n",
                        encoding="utf-8")
        self.assertEqual(search_logs(self.root, {"filename": path.name, "level": "INFO"})["items"], [])

    def test_truncated_and_rotated_file_cursor_is_rejected(self):
        first = search_logs(self.root, {"q": "关键字", "limit": "1"})
        cursor = json.loads(base64.urlsafe_b64decode(first["next_cursor"] + "=="))
        path = self.root / cursor["file"]
        path.write_text("10:00:00 [错误] 新文件\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            search_logs(self.root, {"q": "关键字", "cursor": first["next_cursor"]})

    def collect(self, query):
        items, cursor = [], None
        for _ in range(200):
            result = search_logs(self.root, {**query, **({"cursor": cursor} if cursor else {})})
            self.assertLessEqual(result["scanned_bytes"], log_query.MAX_SCAN_BYTES)
            items.extend(result["items"])
            if not result["next_cursor"]:
                return items
            self.assertNotEqual(cursor, result["next_cursor"])
            cursor = result["next_cursor"]
        self.fail("pagination did not finish")

    def test_appending_active_file_keeps_cursor_and_does_not_duplicate_results(self):
        query = {"process": "scdfe_c-main", "limit": "1"}
        first = search_logs(self.root, query)
        name = first["items"][0]["filename"]
        with (self.root / name).open("a", encoding="utf-8") as stream:
            stream.write("10:04:00 [日志] newly appended\n")
        second = search_logs(self.root, {**query, "cursor": first["next_cursor"]})
        self.assertIn("关键字告警", second["items"][0]["text"])
        self.assertIn("newly appended", search_logs(self.root, query)["items"][0]["text"])

    def test_scan_budget_utf8_and_oversized_lines_resume_without_missing_older_rows(self):
        path = self.root / "scdfe_c-rtu-7-20261031-10.log"
        expected = [f"10:00:{n:02d} [日志] 中文目标 {n}" for n in range(20)]
        path.write_bytes(("\r\n".join(expected) + "\r\n" + "x" * 3000 + "\r\n").encode("utf-8"))
        with patch.object(log_query, "MAX_SCAN_BYTES", 256), patch.object(log_query, "CHUNK_SIZE", 64), \
                patch.object(log_query, "MAX_LINE_BYTES", 128):
            items = self.collect({"filename": path.name, "q": "中文目标", "limit": "3"})
        self.assertEqual([r["text"] for r in items], list(reversed(expected)))

    def test_cross_hour_and_rtu_filters_and_deleted_cursor_file(self):
        names = [f"scdfe_c-rtu-7-20261101-{hour}.log" for hour in ("08", "09")]
        for name in names:
            (self.root / name).write_text(f"{name[-6:-4]}:00:00 [错误] RTU7\n", encoding="utf-8")
        query = {"rtu_id": "7", "file_query": "20261101", "limit": "1"}
        first = search_logs(self.root, query)
        self.assertEqual(first["items"][0]["filename"], names[1])
        (self.root / names[1]).unlink()
        later = self.collect({**query, "cursor": first["next_cursor"]})
        self.assertEqual([r["filename"] for r in later], [names[0]])

    def test_nested_hourly_log_paths_are_searchable(self):
        path = self.root / "2026" / "11" / "01" / "10" / "scdfe_c-db-writer-20261101-10.log"
        path.parent.mkdir(parents=True)
        path.write_text("10:01:00 [日志] nested-hour-log\n", encoding="utf-8")
        result = search_logs(self.root, {"filename": path.relative_to(self.root).as_posix()})
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["filename"], "2026/11/01/10/scdfe_c-db-writer-20261101-10.log")

    def test_type_uses_header_not_message_text_and_timezone_boundaries_are_exact(self):
        path = self.root / "scdfe_c-rtu-9-20261101-10.log"
        path.write_text(
            "10:00:00 [错误] boundary\n"
            "10:00:00 [错误] boundary\n"
            "10:00:01 [错误] excluded\n"
            "10:00:00 [日志] mentions [错误]\n",
            encoding="utf-8",
        )
        start = datetime(2026, 11, 1, 10).astimezone()
        end = start.replace(microsecond=999000).astimezone(timezone.utc)
        result = search_logs(self.root, {"filename": path.name, "level": "ERROR",
            "start": start.isoformat(), "end": end.isoformat()})
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all("boundary" in r["text"] for r in result["items"]))

    def test_changed_filters_and_invalid_cursor_are_rejected(self):
        first = search_logs(self.root, {"limit": "1"})
        for query in ({"cursor": "not-base64"}, {"cursor": first["next_cursor"], "level": "INFO"}):
            with self.assertRaises(ValueError):
                search_logs(self.root, query)

    def test_match_before_old_tail_and_scan_window_remains_reachable(self):
        path = self.root / "scdfe_c-rtu-11-20261101-10.log"
        with path.open("wb") as stream:
            stream.write("10:00:00 [错误] early-match\n".encode())
            line = "10:00:01 [日志] ".encode() + b"x" * 3000 + b"\n"
            for _ in range(1700):
                stream.write(line)
        query = {"filename": path.name, "q": "early-match", "level": "ERROR"}
        first = search_logs(self.root, query)
        self.assertEqual(first["items"], [])
        self.assertTrue(first["has_more"])
        self.assertLessEqual(first["scanned_bytes"], log_query.MAX_SCAN_BYTES)
        rest = self.collect({**query, "cursor": first["next_cursor"]})
        self.assertEqual(len(rest), 1)
        self.assertIn("early-match", rest[0]["text"])


if __name__ == "__main__":
    unittest.main()

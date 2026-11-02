"""One-time conversion preserves log payloads, timestamps and file retention."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.migrate_log_content import migrate, migrate_file, normalize_line


class LogContentMigrationTests(unittest.TestCase):
    def test_types_clock_and_identity_fields_preserve_payload(self):
        for code, label in (("INFO", "日志"), ("WARNING", "警告"), ("ERROR", "错误"), ("PROCESS", "进程")):
            raw = f"2026-11-02T07:01:02.123+08:00 [{code}] pid=7 rtu=4 channel=3 tid=9 正文 [ERROR]\r\n"
            expected = f"07:01:02 [{label}] 正文 [ERROR]\r\n"
            self.assertEqual(normalize_line(raw), expected)
            self.assertEqual(normalize_line(expected), expected)
        self.assertEqual(normalize_line("07:00:00 [日志] 停止 rtu=4 chanel=3；等待\n"),
                         "07:00:00 [日志] 停止；等待\n")
        self.assertEqual(normalize_line("07:00:00 [日志] 写库进程=运行 写库pid=7\n"),
                         "07:00:00 [日志] 写库进程=运行\n")
        payload = '07:00:00 [错误] URL=/api?rtu=4 payload="pid=8" 值  42\n'
        self.assertEqual(normalize_line(payload), payload)
        self.assertEqual(normalize_line('    raise ValueError("pid=42")\n'),
                         '    raise ValueError("pid=42")\n')

    def test_migration_is_idempotent_preserves_mtime_and_ignores_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / '2026/11/02/07/scdfe_c-main-20261102-07.log'
            path.parent.mkdir(parents=True)
            raw = b'07:00:00 [INFO] pid=1 startup\r\n07:00:01 [ERROR] pid=1 shutdown'
            path.write_bytes(raw)
            os.utime(path, (1000000000, 1000000000))
            stamp = path.stat().st_mtime_ns
            other = path.parent / 'unrelated.log'
            other.write_bytes(raw)
            self.assertEqual(migrate(root, dry_run=True)['changed_lines'], 2)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(migrate(root)['changed_files'], 1)
            self.assertEqual(path.read_bytes(), '07:00:00 [日志] startup\r\n07:00:01 [错误] shutdown'.encode())
            self.assertEqual(path.stat().st_mtime_ns, stamp)
            self.assertEqual(migrate(root)['changed_files'], 0)
            self.assertEqual(other.read_bytes(), raw)
            self.assertEqual(list(path.parent.glob('.*')), [])

    def test_changed_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scdfe_c-main-20261102-07.log'
            raw = b'07:00:00 [INFO] pid=1 startup\n'
            path.write_bytes(raw)
            with patch('tools.migrate_log_content._signature', side_effect=[(1, 2), (1, 3)]):
                with self.assertRaisesRegex(RuntimeError, '仍在写入'):
                    migrate_file(path)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(list(path.parent.glob('.*')), [])


if __name__ == '__main__':
    unittest.main()

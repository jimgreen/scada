"""Restoring/retaining one service's history must not consume another's files."""
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from core.service_log import ServiceFileLog


class ServiceFileLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = SimpleNamespace(db_path=self.root / "scada.sqlite3")

    def file(self, service, hour, message):
        path = self.root / "logs" / hour[:4] / hour[4:6] / hour[6:8] / hour[9:]
        path.mkdir(parents=True, exist_ok=True)
        path /= f"scdfe_s-{service}-{hour}.log"
        path.write_text(message, encoding="utf-8")
        return path

    def test_restored_history_excludes_server_with_same_protocol_prefix(self):
        for protocol in ("modbus-tcp", "modbus-rtu", "can"):
            with self.subTest(protocol=protocol):
                self.file(protocol, "20261104-23", "23:01:00 [日志] old own status\n")
                self.file(protocol, "20261105-00", "00:01:00 [报文] 报文 RX HEX=01\n")
                self.file(protocol + "-server", "20261105-01", "01:01:00 [报文] 报文 TX foreign HEX=02\n")
                rows = ServiceFileLog(self.store, protocol).recent()
                self.assertEqual(len(rows), 2)
                self.assertEqual([row["level"] for row in rows], ["INFO", "PACKET"])
                self.assertTrue(all("foreign" not in row["message"] for row in rows))
                limited = ServiceFileLog(self.store, protocol).recent(1)
                self.assertEqual(limited, rows[-1:])

    def test_cleanup_leaves_other_service_and_unrecognized_filenames_untouched(self):
        own = self.file("modbus-tcp", "20000101-00", "00:00:00 [日志] expired own\n")
        other = self.file("modbus-tcp-server", "20000101-00", "00:00:00 [报文] foreign\n")
        backup = own.with_name("scdfe_s-modbus-tcp-backup-20000101-00.log")
        backup.write_text("backup", encoding="utf-8")
        stale = time.time() - 721 * 3600
        for path in (own, other, backup):
            os.utime(path, (stale, stale))
        ServiceFileLog(self.store, "modbus-tcp").write("PACKET", "报文 RX HEX=03")
        self.assertFalse(own.exists())
        self.assertTrue(other.exists())
        self.assertTrue(backup.exists())
        self.assertEqual(list((self.root/'logs').rglob('scdfe_s-modbus-tcp-2026*.log')), [])

    def test_recent_keeps_long_records_and_skips_only_invalid_clocks(self):
        long_message = '报文 RX ' + 'a' * (160*1024)
        self.file('iec104', '20261104-23', '23:59:59 [日志] previous day\n')
        self.file('iec104', '20261105-00', '00:00:01 [报文] '+long_message+'\n'
                  '29:01:90 [日志] broken clock\n00:00:03 [错误] last\n')
        history = ServiceFileLog(self.store, 'iec104')
        self.assertEqual(history.recent(0), [])
        rows = history.recent(3)
        self.assertEqual([r['message'] for r in rows], ['previous day',long_message,'last'])
        self.assertLess(rows[0]['time'], rows[1]['time'])
        self.assertEqual(history.recent(2), rows[-2:])

    def test_shared_startup_snapshot_excludes_foreign_files_then_sees_live_files(self):
        self.file('modbus-tcp','20261104-22','22:00:00 [日志] client\n')
        self.file('modbus-tcp-server','20261104-22','22:00:00 [日志] server\n')
        catalog = ServiceFileLog.startup_catalog(self.root / 'logs')
        history = ServiceFileLog(self.store,'modbus-tcp',startup_catalog=catalog)
        self.assertEqual([r['message'] for r in history.recent()], ['client'])
        history.write('INFO','ready')
        self.file('modbus-tcp','20261104-23','23:00:00 [报文] fresh\n')
        messages = [r['message'] for r in history.recent()]
        self.assertIn('fresh', messages)
        self.assertNotIn('server', messages)


if __name__ == "__main__":
    unittest.main()

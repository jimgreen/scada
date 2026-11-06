"""Hourly file operations must stay below the configured log root."""
import os
from datetime import datetime
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from core.service_log import ServiceFileLog
from core.web_logs import log_files
from scdfe_c import ScdfeLogger
from scdfe_c.web.service import ApiError
from scdfe_c.web.log_query import search_logs
from scdfe_t.web.service import TransStore
from scdfe_t.trans import TransRuntime


class DirectoryLinkTests(unittest.TestCase):
    def test_linked_year_is_not_searched_listed_written_or_cleaned(self):
        # A Windows directory junction requires no symbolic-link privilege.
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "logs"
            root.mkdir()
            outside = base / "other"
            outside.mkdir()
            link = root / str(datetime.now().year)
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
            else:
                link.symlink_to(outside, target_is_directory=True)
            try:
                family_names = {"c": "scdfe_c-main", "s": "scdfe_s-iec104", "t": "scdfe_t-channel-1"}
                past = time.time() - 1000 * 3600
                for family, process in family_names.items():
                    name = process + "-20010101-01.log"
                    target = outside / name
                    target.write_text("01:02:03 [日志] outside\n", encoding="utf-8")
                    os.utime(target, (past, past))
                    self.assertFalse(search_logs(root, {}, family=family)["items"])
                    with self.assertRaises(ValueError):
                        search_logs(root, {"filename": link.name + "/" + name}, family=family)
                    self.assertFalse(log_files(root, family, {})["items"])
                self.assertFalse(ScdfeLogger.list_files(root))
                self.assertFalse(ScdfeLogger.cleanup(root))
                with self.assertRaisesRegex(OSError, "日志目录"):
                    ScdfeLogger(root, process_name="scdfe_c-main").normal("must stay inside")
                self.assertFalse(any(p.is_dir() for p in outside.iterdir()))
                stub = SimpleNamespace(db_path=base / "scada.sqlite3")
                s = ServiceFileLog(stub, "iec104")
                self.assertEqual(s.recent(), [])
                self.assertEqual(s.startup_catalog(root), {})
                s.write("PACKET", "disabled")
                self.assertFalse(TransStore.logs(stub)["items"])
                for name in (link.name + "/scdfe_t-channel-1-20010101-01.log", "scdfe_t-channel-backup.log"):
                    with self.assertRaises(ApiError) as error:
                        TransStore.read_log(stub, name)
                    self.assertEqual(error.exception.status, 400)
                t = TransRuntime(stub.db_path)
                try:
                    t._cleanup_logs(1)
                finally:
                    t.store.close()
                self.assertEqual({p.name for p in outside.iterdir()},
                                 {p + "-20010101-01.log" for p in family_names.values()})
            finally:
                if os.name == "nt":
                    link.rmdir()
                else:
                    link.unlink()

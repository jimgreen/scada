"""One-time migration of legacy flat hourly log files."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.migrate_log_directories import migrate


class LogDirectoryMigrationTests(unittest.TestCase):
    def test_moves_flat_files_and_merges_legacy_channel_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scdfe_c-main-20261101-10.log").write_text(
                "10:00:00 [PROCESS] main\n", encoding="utf-8"
            )
            first = root / "scdfe_c-rtu-7-ch-70-20261101-10.log"
            second = root / "scdfe_c-rtu-7-ch-71-20261101-10.log"
            first.write_text("10:01:00 [INFO] channel 70", encoding="utf-8")
            second.write_text("10:02:00 [INFO] channel 71\n", encoding="utf-8")
            # The merged order follows the source file write time, rather
            # than channel number, and is deterministic for equal times.
            first.touch()
            second.touch()

            report = migrate(root)

            self.assertEqual(report["moved"], 1)
            self.assertEqual(report["merged"], 2)
            target_dir = root / "2026" / "11" / "01" / "10"
            main = target_dir / "scdfe_c-main-20261101-10.log"
            rtu = target_dir / "scdfe_c-rtu-7-20261101-10.log"
            self.assertTrue(main.is_file())
            self.assertEqual(
                rtu.read_text(encoding="utf-8"),
                "10:01:00 [INFO] channel 70\n10:02:00 [INFO] channel 71\n",
            )
            self.assertEqual(list(root.glob("*.log")), [])

            # A second run is a no-op and does not duplicate merged content.
            self.assertEqual(migrate(root)["moved"], 0)
            self.assertEqual(migrate(root)["merged"], 0)
            self.assertEqual(rtu.read_text(encoding="utf-8").count("channel"), 2)

    def test_leaves_unrecognized_files_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unknown = root / "application.log"
            unknown.write_text("keep\n", encoding="utf-8")

            self.assertEqual(migrate(root), {"directory": str(root.resolve()), "moved": 0, "merged": 0})
            self.assertTrue(unknown.exists())


if __name__ == "__main__":
    unittest.main()

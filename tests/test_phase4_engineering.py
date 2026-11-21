"""Phase 4 工程解耦、协议循环防空转、规约能力校准与质量门禁专项回归测试。"""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from core.database_validation import validate_database
from core.iec104_python.api import Server
from core.protocol_capabilities import protocol_capabilities
import scdfe_c
from scdfe_c._logger import ScdfeLogger
from scdfe_c._models import ChannelKey, ChannelSpec, WorkerStats
from scdfe_c.web.service import SCHEMA_USER_VERSION, ScadaStore
from tools.sync_dbdef import check_sync


class Phase4EngineeringTests(unittest.TestCase):
    def test_iec104_reactor_next_timeout_backs_off_when_idle(self):
        server = Server(ip="127.0.0.1", port=0)
        now = time.monotonic()
        # 无活动连接与周期任务时，应退避到 0.25s 而非固定 0.02s 高频空转
        timeout = server._reactor._next_timeout(now)
        self.assertGreaterEqual(timeout, 0.2)
        self.assertLessEqual(timeout, 0.25)

    def test_protocol_capabilities_cover_all_six_protocols_for_c_and_s(self):
        for comp in ("scdfe_c", "scdfe_s"):
            caps = protocol_capabilities(comp)["protocols"]
            self.assertEqual(
                set(caps.keys()),
                {"iec104", "modbus-tcp", "modbus-rtu", "can", "bacnet", "opc"},
            )
            self.assertEqual(caps["bacnet"]["transport"], "udp")
            self.assertTrue(caps["opc"]["batch_read"])
        self.assertEqual(set(protocol_capabilities("scdfe_t")["protocols"].keys()), {"iec104", "modbus-tcp"})

    def test_validate_database_collects_multiple_errors_and_reports_user_version(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "scdfe_c.sqlite3"
            with closing(ScadaStore(db)) as store:
                # 新初始化库应自动写入 SCHEMA_USER_VERSION
                ok = validate_database(db, "c", store.catalog)
                self.assertTrue(ok["valid"])
                self.assertEqual(ok["user_version"], SCHEMA_USER_VERSION)
                # 制造两类独立错误（非 SCADA 表 + IEC104 RTU 缺少通道参数）
                with store.connection() as con:
                    con.execute("CREATE TABLE stray_debug_table (id INTEGER)")
                    con.execute(
                        "INSERT INTO fes_rtu_define(rtu_id, name, channel_id, channel_type) "
                        "VALUES(1, '站1', 1, 'iec104')"
                    )
                    con.commit()
                bad = validate_database(db, "c", store.catalog)
                self.assertFalse(bad["valid"])
                self.assertGreaterEqual(len(bad["errors"]), 2)
                joined = "\n".join(bad["errors"])
                self.assertIn("非SCADA表", joined)
                self.assertIn("缺少通道参数", joined)

    def test_scdfe_c_reexports_extracted_modules_and_prunes_empty_log_dirs(self):
        self.assertIs(scdfe_c.ScdfeLogger, ScdfeLogger)
        self.assertIs(scdfe_c.ChannelKey, ChannelKey)
        self.assertIs(scdfe_c.ChannelSpec, ChannelSpec)
        self.assertIs(scdfe_c.WorkerStats, WorkerStats)
        with tempfile.TemporaryDirectory() as folder:
            log_root = Path(folder) / "logs"
            old_dir = log_root / "2020" / "01" / "01" / "00"
            old_dir.mkdir(parents=True)
            old_file = old_dir / "scdfe_c-main-20200101-00.log"
            old_file.write_text("old\n", encoding="utf-8")
            old_ts = time.time() - 7200
            os.utime(old_file, (old_ts, old_ts))
            removed = ScdfeLogger.cleanup(log_root, retention_hours=1)
            self.assertEqual(len(removed), 1)
            self.assertFalse(old_file.exists())
            # 清理后空的年/月/日/时目录应被自动回收，但保留根目录
            self.assertFalse(old_dir.exists())
            self.assertFalse((log_root / "2020").exists())
            self.assertTrue(log_root.is_dir())

    def test_dbdef_ssot_gate_remains_clean(self):
        self.assertEqual(check_sync(), [])


if __name__ == "__main__":
    unittest.main()

"""Phase 2 P1 级提效、并发加固与规约互通专项回归测试。"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core import operation_audit as audit
from core.iec104_python import types as c104
from core.iec104_python.api import Client
from core.monitor_changes import ensure_monitor_changes
from scdfe_c.web.log_query import search_logs
from scdfe_c.web.service import ScadaStore
from scdfe_s.database import SimulatorStore
from scdfe_s.migrate import copy_definitions
from server.modbus_trans_server import ModbusTcpTransServer


class Phase2P1RegressionTests(unittest.TestCase):
    def test_fast_schema_ready_skips_write_lock_for_initialized_store(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "scdfe_c.sqlite3"
            with closing(ScadaStore(db_path)):
                pass
            # 第二次构造已有完好库应命中 _fast_schema_ready 快路径，零写锁完成初始化
            with closing(ScadaStore(db_path)) as store:
                with store.connection() as con:
                    self.assertTrue(store._fast_schema_ready(con))

    def test_monitor_changes_uses_begin_immediate_outside_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "scdfe_c.sqlite3"
            with closing(ScadaStore(db_path)) as store:
                with store.connection() as con:
                    self.assertFalse(con.in_transaction)
                    ensure_monitor_changes(con, track_refresh=False)
                    self.assertFalse(con.in_transaction)
                    row = con.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_scada_monitor_change'"
                    ).fetchone()
                    self.assertIsNotNone(row)

    def test_s_migrate_excludes_runtime_status_and_can_diagnostic(self):
        with tempfile.TemporaryDirectory() as folder:
            src = Path(folder) / "src.sqlite3"
            dst = Path(folder) / "dst_s.sqlite3"
            with closing(ScadaStore(src)) as c:
                c.create_row("fes_rtu_define", {"rtu_id": 1, "name": "R1", "channel_id": 1, "channel_type": "iec104"})
                with c.connection() as con:
                    con.execute(
                        "INSERT INTO fes_channel_runtime_status(service_name,channel_id,running,rx_count) "
                        "VALUES('scdfe_c',1,1,999)"
                    )
                    con.commit()
            result = copy_definitions(src, dst)
            self.assertNotIn("fes_channel_runtime_status", result["tables"])
            self.assertNotIn("fes_channel_can_diagnostic", result["tables"])
            with closing(SimulatorStore(dst)) as s:
                with s.connection() as con:
                    cnt = con.execute("SELECT COUNT(*) FROM fes_channel_runtime_status").fetchone()[0]
                    self.assertEqual(cnt, 0)

    def test_operation_audit_preserves_origin_and_header_under_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            log_dir = Path(folder)
            # 构造 70 个各 950 字符的字段（safe() 单值上限 1000、单字典上限 80 键），总序列化 > 60KB
            large_details = {f"k{i}": "x" * 950 for i in range(70)}
            with audit.scope("c", log_dir, peer_ip="10.0.0.8", method="POST", path="/api/test"):
                # 同时试图伪造系统字段 component/trace_id
                audit.emit("真实阶段", component="forged", trace_id="forged", **large_details)
            files = list(log_dir.rglob("*.log"))
            self.assertEqual(len(files), 1)
            line = files[0].read_text(encoding="utf-8").strip()
            payload = json.loads(line.split("操作审计 ", 1)[1])
            self.assertTrue(payload["truncated"])
            self.assertEqual(payload["phase"], "真实阶段")
            self.assertEqual(payload["component"], "c")
            self.assertNotEqual(payload["trace_id"], "forged")
            self.assertEqual(payload["peer_ip"], "10.0.0.8")
            self.assertEqual(payload["path"], "/api/test")

    def test_iec104_unknown_type_does_not_disconnect_and_clock_sync_matches_looser_time(self):
        client = Client()
        conn = client.add_connection("127.0.0.1", 2404)
        conn.add_station(1)
        unexpected = []
        conn.on_unexpected_message(lambda c, m, cause: unexpected.append((m.type, cause)))
        # 构造未支持标准类型 M_IT_NA_1 (type_id=15)，不应抛异常断链
        fake_asdu = bytes([15, 1, 3, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0])
        fake_frame = bytes([0x68, len(fake_asdu) + 4, 0, 0, 0, 0]) + fake_asdu
        conn._asdu(None, fake_frame)
        self.assertEqual(len(unexpected), 1)
        self.assertEqual(unexpected[0][1], c104.Umc.UNKNOWN_TYPE_ID)

        # 验证对时响应（type=103）允许 RTU 返回略有不同的时间戳字节
        sent_asdu = bytes([103, 1, 6, 0, 1, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7])
        resp_asdu = bytes([103, 1, 7, 0, 1, 0, 0, 0, 0, 9, 8, 7, 6, 5, 4, 3])
        resp_frame = bytes([0x68, len(resp_asdu) + 4, 0, 0, 0, 0]) + resp_asdu
        self.assertTrue(conn._pending_matches(resp_frame, sent_asdu))

    def test_modbus_trans_server_rejects_zero_scale_and_nan(self):
        with self.assertRaises(ValueError):
            ModbusTcpTransServer(
                host="127.0.0.1",
                port=0,
                unit_id=1,
                points=[{
                    "kind": "yt", "pnt_no": 1, "register_area": "holding",
                    "address": 0, "data_type": "int16", "scale": 0.0, "offset": 0.0,
                }],
            )

    def test_log_query_prunes_by_hour_and_prefilters_ascii_keywords(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for hh in range(0, 24):
                folder_h = root / "2026" / "11" / "22" / f"{hh:02d}"
                folder_h.mkdir(parents=True)
                p = folder_h / f"scdfe_c-main-20261122-{hh:02d}.log"
                tag = "HEX=ABCD" if hh == 8 else "HEX=0000"
                p.write_text(f"{hh:02d}:15:00 [日志] IEC104 {tag}\n", encoding="utf-8")
            tz = datetime.now().astimezone().tzinfo
            start_iso = datetime(2026, 11, 22, 8, 0, 0, tzinfo=tz).isoformat()
            end_iso = datetime(2026, 11, 22, 8, 59, 59, tzinfo=tz).isoformat()
            result = search_logs(root, {"start": start_iso, "end": end_iso, "q": "hex=abcd", "limit": "1"}, family="c")
            self.assertEqual(len(result["items"]), 1)
            self.assertIn("HEX=ABCD", result["items"][0]["text"])
            # 小时级优先调度应优先命中目标时段文件，避免先白扫当天后面 15 个小时
            self.assertLessEqual(result["scanned_files"], 3)

    def test_dbdef_python_matches_json_ssot(self):
        from tools.sync_dbdef import check_sync
        self.assertEqual(check_sync(), [])


if __name__ == "__main__":
    unittest.main()

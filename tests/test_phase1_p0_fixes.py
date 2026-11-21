"""Phase 1 P0 级高危缺陷专项回归测试。"""
from __future__ import annotations

from contextlib import closing
import math
import os
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from core.iec104_python import types as c104
from core.iec104_python.api import Point
from core.log_retention import cleanup_log_tree
from core.packet_logging import DatabasePacketLogSwitch
from scdfe_c.web.service import ApiError, ScadaStore
from scdfe_c.writer_ipc import WriterRequest
from scdfe_d.service import DeviceStore
from server.modbus_server import build_context
from server.point_store import coerce_value


class Phase1P0RegressionTests(unittest.TestCase):
    def test_db01_create_channel_row_does_not_remap_primary_key_in_uncommitted_tx(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(ScadaStore(Path(folder) / "c.sqlite3")) as store:
                # RTU 1 拥有 channel_id=10；此时新建 channel_id=1（恰巧等于 RTU 1 的 rtu_id）
                store.create_row("fes_rtu_define", {"rtu_id": 1, "name": "R1", "channel_id": 10, "channel_type": "modbus-tcp"})
                created = store.create_row(
                    "fes_channel_modbus_tcp_define",
                    {"channel_id": 1, "remote_host": "127.0.0.1", "remote_port": 502},
                )
                self.assertEqual(created["channel_id"], 1)

    def test_db03_db04_patch_enforces_iec104_and_can_validation_and_numeric_allowed_values(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(ScadaStore(Path(folder) / "c.sqlite3")) as store:
                store.create_row("fes_rtu_define", {"rtu_id": 1, "name": "R1", "channel_id": 1, "channel_type": "iec104"})
                store.create_row("fes_channel_iec104_define", {"id": 1, "channel_id": 1, "rtu_addr": 1, "k": 12, "w": 8})
                # PATCH 单改 w=20 > k=12 必须被拦截
                with self.assertRaises(ApiError):
                    store.update_row("fes_channel_iec104_define", [1], {"w": 20})
                # 合法 PATCH 应成功
                updated = store.update_row("fes_channel_iec104_define", [1], {"w": 6})
                self.assertEqual(updated["w"], 6)

                # Modbus inum 大于 4（如 64）不应被错误 allowed_values:[1,2,3,4] 拦截
                store.create_row("fes_rtu_define", {"rtu_id": 2, "name": "R2", "channel_id": 2, "channel_type": "modbus-tcp"})
                row = store.create_row(
                    "fes_channel_modbus_define_call",
                    {"id": 1, "channel_id": 2, "irtuaddr": 1, "icode": 3, "iplace": 0, "inum": 64},
                )
                self.assertEqual(row["inum"], 64)
                # 而真正的数值枚举 icode=99 应被拒绝
                with self.assertRaises(ApiError):
                    store.create_row(
                        "fes_channel_modbus_define_call",
                        {"id": 2, "channel_id": 2, "irtuaddr": 1, "icode": 99, "iplace": 0, "inum": 10},
                    )

    def test_db02_device_store_enables_wal_and_concurrent_pool(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "scdfe_d.sqlite3"
            with closing(DeviceStore(db_path, workspace_root=root, connection_pool_size=4)) as store:
                with store.connection() as con1:
                    mode = con1.execute("PRAGMA journal_mode").fetchone()[0]
                    self.assertEqual(str(mode).lower(), "wal")
                    # 验证缩小 _mutex 后允许并发获取第 2 条连接，不会死锁
                    entered = threading.Event()
                    def reader():
                        with store.connection() as con2:
                            con2.execute("SELECT 1").fetchone()
                            entered.set()
                    t = threading.Thread(target=reader)
                    t.start()
                    self.assertTrue(entered.wait(2.0), "DeviceStore 连接池被 _mutex 串行锁死")
                    t.join(2.0)

    def test_pr01_iec104_point_info_rejects_type_mismatch(self):
        pt_double = Point(None, 100, c104.Type.C_DC_NA_1)
        with self.assertRaises(ValueError):
            pt_double.info = c104.SingleCmd(on=True)
        pt_double.info = c104.DoubleCmd(state=c104.Double.ON)
        self.assertEqual(pt_double.value, c104.Double.ON)

        pt_single = Point(None, 101, c104.Type.C_SC_NA_1)
        with self.assertRaises(TypeError):
            pt_single.info = c104.DoubleCmd(state=c104.Double.OFF)

    def test_pr05_can_float_scaling_and_int_bounds(self):
        from client.can import CanMaster
        sent = []
        master = CanMaster(interface="virtual", channel="p0-can-test")
        master.send = lambda frame_id, payload, **kw: sent.append((frame_id, bytes(payload)))
        row_float = {
            "kind": "yt", "pnt_no": 1, "frame_id": 0x201, "tx_frame_id": 0x201,
            "byte_offset": 0, "bit_offset": 0, "bit_length": 32, "byte_order": "big",
            "value_type": "float", "scale": 2.0, "offset": 10.0, "dlc": 8,
        }
        master._command_frame(row_float, 30.0)
        import struct
        decoded = struct.unpack(">f", sent[-1][1][:4])[0]
        self.assertAlmostEqual(decoded, 10.0, places=5)

        row_uint = {
            "kind": "yt", "pnt_no": 2, "frame_id": 0x202, "tx_frame_id": 0x202,
            "byte_offset": 0, "bit_offset": 0, "bit_length": 8, "byte_order": "big",
            "value_type": "uint", "scale": 1.0, "offset": 0.0, "dlc": 8,
        }
        with self.assertRaises(ValueError):
            master._command_frame(row_uint, -1)
        with self.assertRaises(ValueError):
            master._command_frame(row_uint, 300)

    def test_pr07_modbus_server_context_zero_based_and_safe_bool(self):
        from server.modbus_server import _context_slot
        ctx = build_context(unit_id=1, size=10)
        values, index, _ = _context_slot(ctx, 1, "yc", 0)
        self.assertEqual(index, 0)
        self.assertEqual(len(values), 10)
        self.assertFalse(coerce_value("yk", "false"))
        self.assertFalse(coerce_value("yx", "0"))
        self.assertTrue(coerce_value("yk", "true"))
        with self.assertRaises(ValueError):
            coerce_value("yc", float("nan"))

    def test_rt03_writer_request_atomic_cancel_prevents_504_execution(self):
        req = WriterRequest({"request_id": "r1"})
        self.assertTrue(req.try_cancel())
        self.assertFalse(req.try_claim())

        req2 = WriterRequest({"request_id": "r2"})
        self.assertTrue(req2.try_claim())
        self.assertFalse(req2.try_cancel())

    def test_lg02_log_retention_cleans_packets_empty_dirs_and_quota(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            log_dir = base / "logs"
            pkt_dir = base / "packets"
            old_pkt = pkt_dir / "2020" / "01" / "01" / "00" / "scdfe_t-channel-1-20200101-00.log"
            old_pkt.parent.mkdir(parents=True)
            old_pkt.write_bytes(b"a" * 512)
            old_time = time.time() - 1000 * 3600
            os.utime(old_pkt, (old_time, old_time))

            fresh_pkt = pkt_dir / "2026" / "11" / "22" / "05" / "scdfe_t-channel-1-20261122-05.log"
            fresh_pkt.parent.mkdir(parents=True)
            fresh_pkt.write_bytes(b"b" * 1024)

            fresh_log = log_dir / "2026" / "11" / "22" / "05" / "scdfe_t-channel-1-20261122-05.log"
            fresh_log.parent.mkdir(parents=True)
            fresh_log.write_bytes(b"c" * 512)

            # 配额 800 字节：应先删过期 old_pkt 与空目录，再因超配额优先淘汰 fresh_pkt，保留 fresh_log
            stats = cleanup_log_tree(log_dir, packet_dir=pkt_dir, retention_hours=720, max_total_bytes=800)
            self.assertFalse(old_pkt.exists())
            self.assertFalse(old_pkt.parent.exists())
            self.assertFalse(fresh_pkt.exists())
            self.assertTrue(fresh_log.exists())
            self.assertEqual(stats["removed_files"], 2)

    def test_lg03_packet_logging_preserves_cache_on_transient_db_error_and_db_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "scdfe_c.sqlite3"
            with closing(ScadaStore(db_path)) as store:
                store.create_row("fes_rtu_define", {"rtu_id": 1, "name": "R1", "channel_id": 1, "channel_type": "iec104"})
            switch = DatabasePacketLogSwitch(db_path, "c")
            # 先写旧 sidecar 为 False，再 DB 设置为 True，验证 snapshot DB 优先且清理 sidecar
            switch._write_legacy_policy({"enabled": False, "channels": {"1": False}})
            switch.set_channel(1, True)
            self.assertTrue(switch.enabled(1, refresh=True))
            self.assertTrue(switch.snapshot()["channels"]["1"])
            # 模拟瞬态锁错误导致 _read_all 返回 None，不得清空已缓存的 True
            with patch.object(switch, "_read_all", return_value=None):
                self.assertTrue(switch.enabled(1, refresh=True))


if __name__ == "__main__":
    unittest.main()

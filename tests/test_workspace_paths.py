"""三参数工作区数据库路径体系与安全迁移工具单元测试。"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.web_database import resolve_web_database_args, selection_file
from core.workspace_paths import (
    ENV_RUNTIME_NAME,
    ENV_SCENE_NAME,
    ENV_WORKSPACE_ROOT,
    LEGACY_REPO_DB_DIR,
    add_workspace_arguments,
    database_directory,
    default_database_path,
    infer_workspace_context,
    list_runtimes,
    list_scenes,
    resolve_cli_database,
    validate_segment,
)
from scdfe_c.web.service import ScadaStore
from scdfe_s.database import SimulatorStore
from tools.migrate_db_workspace import migrate_workspace_databases


class WorkspacePathsTests(unittest.TestCase):
    def test_three_parameter_resolution_and_env_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "ws"
            root.mkdir()
            with patch.dict(
                os.environ,
                {
                    ENV_WORKSPACE_ROOT: str(root),
                    ENV_RUNTIME_NAME: "simulation",
                    ENV_SCENE_NAME: "huashan",
                },
                clear=False,
            ):
                self.assertEqual(
                    database_directory(),
                    (root / "simulation" / "huashan" / "sqldb" / "scada").resolve(),
                )
                self.assertEqual(
                    default_database_path("c"),
                    (root / "simulation" / "huashan" / "sqldb" / "scada" / "scdfe_c.sqlite3").resolve(),
                )
                self.assertEqual(
                    default_database_path("t"),
                    default_database_path("c"),
                )
                self.assertEqual(
                    default_database_path("s"),
                    (root / "simulation" / "huashan" / "sqldb" / "scada" / "scdfe_s.sqlite3").resolve(),
                )
                self.assertEqual(
                    default_database_path("d"),
                    (root / "simulation" / "huashan" / "sqldb" / "scada" / "scdfe_d.sqlite3").resolve(),
                )
                # 显式参数优先于环境变量
                self.assertEqual(
                    default_database_path("c", root, "realtime", "qinling"),
                    (root / "realtime" / "qinling" / "sqldb" / "scada" / "scdfe_c.sqlite3").resolve(),
                )

    def test_validate_segment_rejects_traversal(self):
        for bad in ("", " ", ".", "..", ".hidden", "a/b", r"a\b", "a:b", "bad.", "bad "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_segment(bad, "运行态名称")
        self.assertEqual(validate_segment("realtime", "运行态名称"), "realtime")
        self.assertEqual(validate_segment("qinling_01", "场景名称"), "qinling_01")

    def test_selection_file_confined_to_workspace_and_isolated_by_context(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "ws"
            root.mkdir()
            pref_rt = selection_file("c", "127.0.0.1", 8977, workspace_root=root, runtime_name="realtime", scene_name="qinling")
            pref_sim = selection_file("c", "127.0.0.1", 8977, workspace_root=root, runtime_name="simulation", scene_name="qinling")
            self.assertTrue(pref_rt.is_relative_to(root.resolve()))
            self.assertFalse(pref_rt.is_relative_to(LEGACY_REPO_DB_DIR.resolve()))
            self.assertNotEqual(pref_rt, pref_sim)

    def test_resolve_web_database_args_respects_explicit_runtime_and_scene(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "ws"
            root.mkdir()
            parser = argparse.ArgumentParser()
            parser.add_argument("--host", default="127.0.0.1")
            parser.add_argument("--port", type=int, default=8977)
            parser.add_argument("--db", type=Path, default=None)
            add_workspace_arguments(parser)

            args = parser.parse_args(
                ["--workspace-root", str(root), "--runtime-name", "simulation", "--scene-name", "qinling"]
            )
            ws_root, pref, db_path = resolve_web_database_args("c", args)
            self.assertEqual(ws_root, root.resolve())
            self.assertEqual(
                db_path,
                (root / "simulation" / "qinling" / "sqldb" / "scada" / "scdfe_c.sqlite3").resolve(),
            )
            self.assertTrue(pref.is_relative_to(root.resolve()))

            # CLI 解析同样遵循三参数
            _, cli_db = resolve_cli_database("s", None, workspace_root=root, runtime_name="realtime", scene_name="qinling")
            self.assertEqual(
                cli_db,
                (root / "realtime" / "qinling" / "sqldb" / "scada" / "scdfe_s.sqlite3").resolve(),
            )

    def test_context_discovery_and_inference(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "ws"
            (root / "realtime" / "qinling" / "sqldb" / "scada").mkdir(parents=True)
            (root / "simulation" / "demo" / "sqldb" / "scada").mkdir(parents=True)
            (root / ".scdfe").mkdir(parents=True)
            self.assertEqual(list_runtimes(root), ["realtime", "simulation"])
            self.assertEqual(list_scenes(root, "realtime"), ["qinling"])
            self.assertEqual(list_scenes(root, "simulation"), ["demo"])
            ctx = infer_workspace_context(
                root / "realtime" / "qinling" / "sqldb" / "scada" / "scdfe_c.sqlite3",
                root,
            )
            self.assertEqual(ctx["runtime_name"], "realtime")
            self.assertEqual(ctx["scene_name"], "qinling")
            self.assertEqual(ctx["sqldb_directory"], "realtime/qinling/sqldb/scada")

    def test_simulator_store_blocks_business_filename_in_new_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "realtime" / "qinling" / "sqldb" / "scada" / "scdfe_c.sqlite3"
            with self.assertRaisesRegex(ValueError, "业务库"):
                SimulatorStore(target)

    def test_migration_tool_dry_run_apply_overwrite_and_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source_dir = base / "legacy_db"
            workspace_root = base / "workspace"
            repo_dir = base / "repo"
            source_dir.mkdir()
            workspace_root.mkdir()
            repo_dir.mkdir()

            # 准备源库（模拟旧仓库 db/ 布局与历史垃圾）
            src_c = source_dir / "scada.sqlite3"
            src_s = source_dir / "scdfe_s.sqlite3"
            with closing(ScadaStore(src_c)) as c_store:
                c_store.create_row("fes_rtu_define", {"rtu_id": 1, "name": "秦岭1号", "channel_id": 1, "channel_type": "iec104"})
                c_store.create_row("fes_channel_iec104_define", {"id": 1, "channel_id": 1, "rtu_addr": 1})
                c_store.create_row("fes_scdfe_c_define", {"id": 1, "log_dir": r"D:\old\outside\logs"})
            with closing(SimulatorStore(src_s)) as s_store:
                s_store.create_row("fes_rtu_define", {"rtu_id": 2, "name": "模拟站", "channel_id": 2, "channel_type": "iec104"})
                s_store.create_row("fes_channel_iec104_define", {"id": 2, "channel_id": 2, "rtu_addr": 2})

            (source_dir / "scada.sqlite3.old.bak").write_bytes(b"bak" * 100)
            (source_dir / "scada.sqlite3.scdfe_c-main.lock").write_bytes(b"1")
            (source_dir / "web_database_c_123456.json").write_text("{}", encoding="utf-8")
            (source_dir / "test_protocol_schema.sqlite3").write_bytes(b"old")
            (repo_dir / ".tmp_junk.py").write_text("x = 1\n", encoding="utf-8")

            # 先在目标放一个已存在旧库，测试 --overwrite 备份覆盖
            target_dir = workspace_root / "realtime" / "qinling" / "sqldb" / "scada"
            target_dir.mkdir(parents=True)
            with closing(ScadaStore(target_dir / "scdfe_c.sqlite3")) as old_dst:
                old_dst.create_row("fes_rtu_define", {"rtu_id": 99, "name": "旧目标", "channel_id": 99, "channel_type": "iec104"})

            # 1) dry-run 不写入、不清理
            dry = migrate_workspace_databases(
                source_dir=source_dir,
                workspace_root=workspace_root,
                runtime_name="realtime",
                scene_name="qinling",
                apply=False,
                overwrite=True,
                clean_legacy=True,
                repo_root=repo_dir,
            )
            self.assertEqual(dry["mode"], "dry_run")
            self.assertEqual(len(dry["databases"]), 2)
            self.assertTrue((source_dir / "scada.sqlite3.old.bak").exists())

            # 2) apply 正式迁移 + 覆盖备份 + VACUUM + 清理遗留
            res = migrate_workspace_databases(
                source_dir=source_dir,
                workspace_root=workspace_root,
                runtime_name="realtime",
                scene_name="qinling",
                apply=True,
                overwrite=True,
                vacuum=True,
                clean_legacy=True,
                repo_root=repo_dir,
            )
            self.assertEqual(res["mode"], "apply")
            self.assertEqual(len(res["databases"]), 2)
            c_entry = next(item for item in res["databases"] if item["role"] == "c")
            self.assertTrue(c_entry["validation"]["valid"])
            self.assertIsNotNone(c_entry["backup"])
            self.assertTrue(Path(c_entry["backup"]).is_file())

            with closing(ScadaStore(target_dir / "scdfe_c.sqlite3")) as migrated_c:
                self.assertEqual(migrated_c.get_row("fes_rtu_define", [1])["name"], "秦岭1号")
                with migrated_c.connection() as con:
                    log_dir = con.execute("SELECT log_dir FROM fes_scdfe_c_define WHERE id=1").fetchone()[0]
                    self.assertEqual(log_dir, "logs")

            with closing(SimulatorStore(target_dir / "scdfe_s.sqlite3")) as migrated_s:
                self.assertEqual(migrated_s.get_row("fes_rtu_define", [2])["name"], "模拟站")

            # 旧垃圾均已清理
            self.assertFalse((source_dir / "scada.sqlite3.old.bak").exists())
            self.assertFalse((source_dir / "web_database_c_123456.json").exists())
            self.assertFalse((source_dir / "test_protocol_schema.sqlite3").exists())
            self.assertFalse((repo_dir / ".tmp_junk.py").exists())


if __name__ == "__main__":
    unittest.main()

"""SCADA 侧三参数工作区数据库路径与跨目录 FES 源库发现单元测试。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core.web_database import resolve_web_database_args, selection_file
from core.workspace_paths import (
    ENV_RUNTIME_NAME,
    ENV_SCENE_NAME,
    ENV_WORKSPACE_ROOT,
    add_workspace_arguments,
    database_directory,
    default_database_path,
    fes_database_directory,
    infer_workspace_context,
    list_runtimes,
    list_scenes,
    validate_segment,
)
from scdfe_d.service import DeviceStore


class ScadaWorkspacePathsTests(unittest.TestCase):
    def test_three_parameter_resolution_separates_scada_and_fes(self):
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
                    fes_database_directory(),
                    (root / "simulation" / "huashan" / "sqldb" / "fes").resolve(),
                )
                self.assertEqual(
                    default_database_path("d"),
                    (root / "simulation" / "huashan" / "sqldb" / "scada" / "scdfe_d.db").resolve(),
                )
                self.assertEqual(
                    default_database_path("c"),
                    (root / "simulation" / "huashan" / "sqldb" / "fes" / "scdfe_c.db").resolve(),
                )
                self.assertEqual(
                    default_database_path("s"),
                    (root / "simulation" / "huashan" / "sqldb" / "fes" / "scdfe_s.db").resolve(),
                )

    def test_scdfe_d_discovers_sibling_fes_source_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scada_dir = root / "realtime" / "qinling" / "sqldb" / "scada"
            fes_dir = root / "realtime" / "qinling" / "sqldb" / "fes"
            scada_dir.mkdir(parents=True)
            fes_dir.mkdir(parents=True)
            c_db = fes_dir / "scdfe_c.db"
            sqlite3.connect(c_db).close()
            store = DeviceStore(scada_dir / "scdfe_d.db", workspace_root=root)
            try:
                self.assertEqual(
                    store.default_source_database(),
                    "realtime/qinling/sqldb/fes/scdfe_c.db",
                )
            finally:
                store.close()

    def test_validate_segment_and_context_discovery(self):
        for bad in ("", " ", ".", "..", ".hidden", "a/b", r"a\b", "a:b", "bad.", "bad "):
            with self.assertRaises(ValueError):
                validate_segment(bad, "运行态名称")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "ws"
            (root / "realtime" / "qinling" / "sqldb" / "scada").mkdir(parents=True)
            self.assertEqual(list_runtimes(root), ["realtime"])
            self.assertEqual(list_scenes(root, "realtime"), ["qinling"])
            ctx = infer_workspace_context(
                root / "realtime" / "qinling" / "sqldb" / "scada" / "scdfe_d.db",
                root,
            )
            self.assertEqual(ctx["sqldb_directory"], "realtime/qinling/sqldb/scada")
            parser = argparse.ArgumentParser()
            parser.add_argument("--host", default="127.0.0.1")
            parser.add_argument("--port", type=int, default=8980)
            parser.add_argument("--db", type=Path, default=None)
            add_workspace_arguments(parser)
            args = parser.parse_args(["--workspace-root", str(root), "--runtime-name", "realtime", "--scene-name", "qinling"])
            ws_root, pref, db_path = resolve_web_database_args("d", args)
            self.assertEqual(ws_root, root.resolve())
            self.assertTrue(pref.is_relative_to(root.resolve()))
            self.assertEqual(db_path, (root / "realtime" / "qinling" / "sqldb" / "scada" / "scdfe_d.db").resolve())


if __name__ == "__main__":
    unittest.main()

"""Phase 3 Web 后端统一、静态缓存、写锁消除、审计与前端自明性/选择题改造专项回归测试。"""
from __future__ import annotations

from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from scdfe_c.web.process_control import ProcessController
from scdfe_c.web.service import ApiError, ScadaStore
from scdfe_d.web.server import make_server as make_d_server
from scdfe_t.web.server import make_server as make_t_server
from scdfe_t.web.service import TransStore


ROOT = Path(__file__).resolve().parents[1]


class Phase3WebRegressionTests(unittest.TestCase):
    def test_trans_create_points_batch_allows_same_pnt_no_across_kinds_and_maps_conflict_to_409(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "scdfe_c.sqlite3"
            trans = TransStore(db)
            try:
                trans.create_channel({
                    "channel_id": 1, "name": "转发1", "protocol": "iec104",
                    "bind_host": "127.0.0.1", "bind_port": 2404,
                })
                # 同批次不同 kind 使用相同转发点号 1 应成功
                created = trans.create_points_batch(1, {"items": [
                    {"kind": "yc", "pnt_no": 1, "source_rtu_id": 1, "source_pnt_no": 1},
                    {"kind": "yx", "pnt_no": 1, "source_rtu_id": 1, "source_pnt_no": 1},
                ]})
                self.assertEqual(len(created), 2)
                # 再次批量插入已存在的 (yc, 1) 应返回 409 ApiError 而非未捕获 sqlite3.IntegrityError
                with self.assertRaises(ApiError) as ctx:
                    trans.create_points_batch(1, {"items": [
                        {"kind": "yc", "pnt_no": 1, "source_rtu_id": 1, "source_pnt_no": 2},
                    ]})
                self.assertEqual(ctx.exception.status, 409)
                self.assertEqual(ctx.exception.code, "conflict")
            finally:
                trans.store.close()

    def test_process_controller_snapshot_is_read_only_in_steady_state(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "scdfe_c.sqlite3"
            with closing(ScadaStore(db)) as store:
                ctrl = ProcessController(store)
                # 首次调用会初始化系统行
                first = ctrl.snapshot()
                self.assertGreaterEqual(len(first["items"]), 3)
                with store.connection() as con:
                    ver_before = con.execute("PRAGMA data_version").fetchone()[0]
                    changes_before = con.total_changes
                # 稳态再次调用不应执行任何 INSERT/UPDATE
                second = ctrl.snapshot()
                self.assertEqual(len(second["items"]), len(first["items"]))
                with store.connection() as con:
                    self.assertEqual(con.execute("PRAGMA data_version").fetchone()[0], ver_before)
                    self.assertEqual(con.total_changes, changes_before)

    def test_shared_static_routes_return_etag_and_304_with_security_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "scdfe_c.sqlite3"
            server = make_t_server("127.0.0.1", 0, db_path=db, workspace_root=Path(folder))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    for path in ("/static/soe.js", "/static/diagnostics.js", "/static/command-traces.js", "/soe", "/diagnostics"):
                        conn.request("GET", path)
                        resp = conn.getresponse()
                        resp.read()
                        self.assertEqual(resp.status, 200, path)
                        etag = resp.getheader("ETag")
                        self.assertTrue(etag, f"{path} 缺少 ETag")
                        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY", path)
                        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff", path)
                        conn.request("GET", path, headers={"If-None-Match": etag})
                        resp304 = conn.getresponse()
                        resp304.read()
                        self.assertEqual(resp304.status, 304, path)
                finally:
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

    def test_scdfe_d_write_request_emits_operation_audit_log_and_security_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "scdfe_d.sqlite3"
            server = make_d_server("127.0.0.1", 0, db_path=db, workspace_root=Path(folder))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    body = json.dumps({"name": "机组1", "category": "generator"}).encode("utf-8")
                    conn.request("POST", "/api/devices", body=body, headers={"Content-Type": "application/json"})
                    resp = conn.getresponse()
                    resp.read()
                    self.assertEqual(resp.status, 201)
                    self.assertEqual(resp.getheader("X-Frame-Options"), "SAMEORIGIN")
                    self.assertEqual(resp.getheader("Referrer-Policy"), "same-origin")
                finally:
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
            audit_files = list((Path(folder) / "logs").rglob("scdfe_d-operations-*.log"))
            self.assertEqual(len(audit_files), 1)
            text = audit_files[0].read_text(encoding="utf-8")
            self.assertIn("请求到达", text)
            self.assertIn("业务返回", text)
            self.assertIn("请求结束", text)

    def test_ui_templates_use_select_instead_of_free_text_and_remove_window_prompt(self):
        soe_html = (ROOT / "core" / "web_static" / "soe.html").read_text(encoding="utf-8")
        self.assertIn('<select id="rtu">', soe_html)
        t_logs = (ROOT / "scdfe_t" / "web" / "static" / "logs.html").read_text(encoding="utf-8")
        self.assertIn('<select id="lb-channel"', t_logs)
        self.assertIn('type="datetime-local"', t_logs)
        d_app = (ROOT / "scdfe_d" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("prompt(", d_app)
        t_app = (ROOT / "scdfe_t" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("addr_base", t_app)
        self.assertIn("iec_type", t_app)

    def test_ui_theme_and_i18n_assets_served_with_etag_and_no_cdn(self):
        theme_css = (ROOT / "core" / "web_static" / "ui-theme.css").read_text(encoding="utf-8")
        i18n_js = (ROOT / "core" / "web_static" / "ui-i18n.js").read_text(encoding="utf-8")
        for theme_id in (
            "minimalist-light",
            "precision-dark",
            "sci-fi-cyan",
            "sci-fi-purple",
            "sci-fi-matrix",
            "bento-grid",
            "glassmorphism",
            "editorial",
        ):
            self.assertIn(f'[data-theme="{theme_id}"]', theme_css)
            self.assertIn(f'"{theme_id}"', i18n_js)
        self.assertNotIn('[data-theme="neo-brutalism"]', theme_css)
        self.assertIn("ui-theme-select", i18n_js)
        self.assertIn("PATTERN_RULES", i18n_js)
        self.assertIn("translateElementAttrs", i18n_js)
        self.assertIn("translateText", i18n_js)
        for sample_phrase in ("一键启动全部", "运行参数与日志保留", "搜索 RTU、名称或规约", "进程已退出，未收到正常退出状态"):
            self.assertIn(sample_phrase, i18n_js)
        self.assertIn("tabular-nums", theme_css)
        self.assertNotIn("https://", theme_css)
        self.assertIn('"zh-CN"', i18n_js)
        self.assertIn('"en-US"', i18n_js)
        self.assertIn('"ru-RU"', i18n_js)
        self.assertNotIn("https://", i18n_js)

        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "scdfe_d.sqlite3"
            server = make_d_server("127.0.0.1", 0, db_path=db, workspace_root=Path(folder))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                try:
                    conn.request("GET", "/")
                    resp = conn.getresponse()
                    html = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200)
                    self.assertIn('data-theme="minimalist-light"', html)
                    self.assertIn("/static/ui-theme.css", html)
                    self.assertIn("/static/ui-i18n.js", html)
                    self.assertIn('class="ui-nav-icon"', html)
                    for asset in ("/static/ui-theme.css", "/static/ui-i18n.js"):
                        conn.request("GET", asset)
                        r1 = conn.getresponse()
                        r1.read()
                        self.assertEqual(r1.status, 200, asset)
                        etag = r1.getheader("ETag")
                        self.assertTrue(etag)
                        conn.request("GET", asset, headers={"If-None-Match": etag})
                        r2 = conn.getresponse()
                        r2.read()
                        self.assertEqual(r2.status, 304, asset)
                finally:
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)


if __name__ == "__main__":
    unittest.main()

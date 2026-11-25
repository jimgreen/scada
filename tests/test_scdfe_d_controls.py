"""scdfe_d 设备遥控（YK）/ 遥调（YT）数据库扩展、实时监视下发与运行日志专项测试。"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scdfe_d.device_panel import read_device
from scdfe_d.measurement_layout import (
    CTRL_STATUS_DISABLED,
    CTRL_STATUS_DISP_MENU,
    CTRL_STATUS_DISP_TYPE,
    CTRL_STATUS_FAILED,
    CTRL_STATUS_IDLE,
    CTRL_STATUS_PENDING,
    CTRL_STATUS_SUCCESS,
    CTRL_SUFFIXES,
)
from scdfe_d.service import (
    DeviceStore,
    parse_device_table_schema,
    validate_database,
)
from scdfe_d.web.server import make_server


def _create_source_scdfe_c(path: Path) -> None:
    """构造带完整四遥（YC/YX/YK/YT）的模拟 scdfe_c.sqlite3。"""
    with closing(sqlite3.connect(path)) as con:
        con.executescript(
            """
            CREATE TABLE fes_yc_define(
                rtu_id INTEGER NOT NULL, pnt_no INTEGER NOT NULL, name TEXT,
                value REAL, status INTEGER, refresh_time INTEGER,
                PRIMARY KEY(rtu_id, pnt_no)
            );
            CREATE TABLE fes_yx_define(
                rtu_id INTEGER NOT NULL, pnt_no INTEGER NOT NULL, name TEXT,
                value INTEGER, status INTEGER, refresh_time INTEGER,
                PRIMARY KEY(rtu_id, pnt_no)
            );
            CREATE TABLE fes_yk_define(
                rtu_id INTEGER NOT NULL, pnt_no INTEGER NOT NULL, name TEXT,
                value INTEGER, status INTEGER DEFAULT 0,
                cmd_time INTEGER DEFAULT 0, cmd_handle_time INTEGER DEFAULT 0,
                man_disable INTEGER DEFAULT 0, auto_disable INTEGER DEFAULT 0,
                _version INTEGER DEFAULT 1,
                PRIMARY KEY(rtu_id, pnt_no)
            );
            CREATE TABLE fes_yt_define(
                rtu_id INTEGER NOT NULL, pnt_no INTEGER NOT NULL, name TEXT,
                value REAL, status INTEGER DEFAULT 0,
                cmd_time INTEGER DEFAULT 0, cmd_handle_time INTEGER DEFAULT 0,
                eng_max REAL, eng_min REAL,
                man_disable INTEGER DEFAULT 0, auto_disable INTEGER DEFAULT 0,
                _version INTEGER DEFAULT 1,
                PRIMARY KEY(rtu_id, pnt_no)
            );
            INSERT INTO fes_yc_define VALUES (3, 1, 'ACGenerator.柴发-1.p', 120.5, 0, 1700000000000);
            INSERT INTO fes_yx_define VALUES (3, 1, 'ACGenerator.柴发-1.run', 1, 0, 1700000000000);
            INSERT INTO fes_yk_define(rtu_id, pnt_no, name, value) VALUES
                (3, 1, 'ACGenerator.柴发-1.run_stat', 0),
                (3, 2, 'ACGenerator.柴发-2.run_stat', 0);
            INSERT INTO fes_yt_define(rtu_id, pnt_no, name, value, eng_max, eng_min) VALUES
                (3, 1, 'ACGenerator.柴发-1.p_set', 0.0, 200.0, 0.0),
                (3, 2, 'ACGenerator.柴发-2.p_set', 0.0, 100.0, 0.0);
            """
        )
        con.commit()


def _http_json(base_url: str, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw) if raw else {}


class TestScdfeDControls(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src_db = self.root / "scdfe_c.sqlite3"
        self.dev_db = self.root / "scdfe_d.sqlite3"
        _create_source_scdfe_c(self.src_db)
        self.store = DeviceStore(
            self.dev_db,
            workspace_root=self.root,
            source_database="scdfe_c.sqlite3",
        )
        self.dev = self.store.create_device({"name": "柴发-1", "category": "generator"})
        self.dev_id = self.dev["device_id"]
        self.table = self.dev["model_table"]
        self.idx = self.dev["model_idx"]

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_mixed_schema_and_pruning_preservation(self):
        """验证量测 10 列与控调 9 列同表混合共存、元数据注入、device_panel 兼容与剪枝互不破坏。"""
        m = self.store.save_measurement(
            {
                "device_id": self.dev_id,
                "metric": "p",
                "kind": "yc",
                "rtu_id": 3,
                "pnt_no": 1,
            }
        )
        yk = self.store.save_control(
            {
                "device_id": self.dev_id,
                "control": "run_stat",
                "kind": "yk",
                "rtu_id": 3,
                "pnt_no": 1,
            }
        )
        yt = self.store.save_control(
            {
                "device_id": self.dev_id,
                "control": "p_set",
                "kind": "yt",
                "rtu_id": 3,
                "pnt_no": 1,
                "min_value": 0.0,
                "max_value": 150.0,
            }
        )
        self.assertTrue(validate_database(self.dev_db)["valid"])

        with self.store.connection() as con:
            info = con.execute(f'PRAGMA table_info("{self.table}")').fetchall()
            metrics, controls = parse_device_table_schema(info)
            self.assertEqual(metrics, [("p", "yc")])
            self.assertEqual(controls, [("run_stat", "yk"), ("p_set", "yt")])
            # 确认 RTDB 嵌入元数据已为 *_cmd_status 注入 6 态菜单
            meta = con.execute(
                "SELECT disp_type, disp_menu FROM rtdb_schema_columns "
                "WHERE table_name=? AND eng_name=?",
                (self.table, "run_stat_cmd_status"),
            ).fetchone()
            self.assertIsNotNone(meta)
            self.assertEqual(meta[0], CTRL_STATUS_DISP_TYPE)
            self.assertEqual(meta[1], CTRL_STATUS_DISP_MENU)

        # device_panel 在含控调列的宽表上读取量测不得报 409
        panel = read_device(self.store, self.table, self.idx, live=True)
        self.assertEqual([i["metric"] for i in panel["items"]], ["p"])

        # 删除最后一个量测：量测列剪枝，控调列完好保留
        self.store.delete_measurement(m["measurement_id"], m["version"])
        with self.store.connection() as con:
            info = con.execute(f'PRAGMA table_info("{self.table}")').fetchall()
            metrics, controls = parse_device_table_schema(info)
            self.assertEqual(metrics, [])
            self.assertEqual(controls, [("run_stat", "yk"), ("p_set", "yt")])

        # 删除一个控调列：剩余控调列保留
        self.store.delete_control(yt["control_id"], yt["version"])
        with self.store.connection() as con:
            info = con.execute(f'PRAGMA table_info("{self.table}")').fetchall()
            _, controls = parse_device_table_schema(info)
            self.assertEqual(controls, [("run_stat", "yk")])
            self.assertEqual(len(info), 2 + len(CTRL_SUFFIXES))

        # 清理最后控调列
        self.store.delete_control(yk["control_id"], yk["version"])
        with self.store.connection() as con:
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{self.table}")')]
            self.assertEqual(cols, ["idx", "name"])

    def test_watch_db_changes_dispatch_and_ack_cycle(self):
        """验证外部直接改宽表值/时标自动监视转发至 scdfe_c，以及回执同步和同值重发。"""
        self.store.save_control(
            {
                "device_id": self.dev_id,
                "control": "run_stat",
                "kind": "yk",
                "rtu_id": 3,
                "pnt_no": 1,
            }
        )
        self.store.save_control(
            {
                "device_id": self.dev_id,
                "control": "p_set",
                "kind": "yt",
                "rtu_id": 3,
                "pnt_no": 1,
                "min_value": 0.0,
                "max_value": 150.0,
            }
        )

        # 预先写入历史值再首次轮询：冷启动建基线不得误发
        with self.store.connection() as con:
            con.execute(
                f'UPDATE "{self.table}" SET run_stat=0, run_stat_cmd_time=1000 WHERE idx=?',
                (self.idx,),
            )
        r0 = self.store.poll_controls_once()
        self.assertEqual(r0["dispatched"], 0)

        # 外部程序（如 AGC）直接改写宽表 YK 值和 YT 值+时间
        t1 = int(time.time() * 1000)
        with self.store.connection() as con:
            con.execute(
                f'UPDATE "{self.table}" SET run_stat=1, run_stat_cmd_time=?, '
                'p_set=88.5, p_set_cmd_time=? WHERE idx=?',
                (t1, t1, self.idx),
            )
        r1 = self.store.poll_controls_once()
        self.assertEqual(r1["dispatched"], 2)
        self.assertEqual(r1["pending"], 2)

        # 检查 scdfe_c.sqlite3 已收到严格单调递增指令并递增 _version
        with closing(sqlite3.connect(self.src_db)) as ccon:
            yk_row = ccon.execute(
                "SELECT value, cmd_time, status, _version FROM fes_yk_define WHERE rtu_id=3 AND pnt_no=1"
            ).fetchone()
            yt_row = ccon.execute(
                "SELECT value, cmd_time, status, _version FROM fes_yt_define WHERE rtu_id=3 AND pnt_no=1"
            ).fetchone()
        self.assertEqual(yk_row[0], 1)
        self.assertGreater(yk_row[1], 0)
        self.assertEqual(yk_row[2], 0)
        self.assertEqual(yk_row[3], 2)
        self.assertAlmostEqual(yt_row[0], 88.5)
        self.assertEqual(yt_row[3], 2)

        # 模拟 scdfe_c 回执：YK 执行成功(status=1)，YT 执行失败(status=3)
        with closing(sqlite3.connect(self.src_db)) as ccon:
            ccon.execute(
                "UPDATE fes_yk_define SET status=1, cmd_handle_time=? WHERE rtu_id=3 AND pnt_no=1",
                (yk_row[1] + 50,),
            )
            ccon.execute(
                "UPDATE fes_yt_define SET status=3, cmd_handle_time=? WHERE rtu_id=3 AND pnt_no=1",
                (yt_row[1] + 60,),
            )
            ccon.commit()

        r2 = self.store.poll_controls_once()
        self.assertEqual(r2["acked"], 2)
        self.assertEqual(r2["pending"], 0)
        with self.store.connection() as con:
            row = con.execute(
                f'SELECT run_stat_cmd_status, run_stat_cmd_ack_time, p_set_cmd_status, p_set_cmd_ack_time '
                f'FROM "{self.table}" WHERE idx=?',
                (self.idx,),
            ).fetchone()
        self.assertEqual(row[0], CTRL_STATUS_SUCCESS)
        self.assertEqual(row[1], yk_row[1] + 50)
        self.assertEqual(row[2], CTRL_STATUS_FAILED)
        self.assertEqual(row[3], yt_row[1] + 60)

        # 同值重发：只更新 p_set_cmd_time 而不改 p_set 值，依然能捕获并转发
        t2 = t1 + 5000
        with self.store.connection() as con:
            con.execute(
                f'UPDATE "{self.table}" SET p_set_cmd_time=? WHERE idx=?',
                (t2, self.idx),
            )
        r3 = self.store.poll_controls_once()
        self.assertEqual(r3["dispatched"], 1)

        # 确认运行日志已落盘并可被 read_logs 检索
        files = self.store.list_log_files()
        self.assertTrue(any(f["process"] == "scdfe_d-control" for f in files))
        logs = self.store.read_logs({"process": "scdfe_d-control", "q": "监视转发指令"})
        self.assertGreaterEqual(len(logs["items"]), 2)

    def test_interlock_and_limit_guard(self):
        """验证禁用闭锁与越限拦截不会污染 scdfe_c。"""
        self.store.save_control(
            {
                "device_id": self.dev_id,
                "control": "run_stat",
                "kind": "yk",
                "rtu_id": 3,
                "pnt_no": 1,
                "notuse": 1,
            }
        )
        self.store.save_control(
            {
                "device_id": self.dev_id,
                "control": "p_set",
                "kind": "yt",
                "rtu_id": 3,
                "pnt_no": 1,
                "min_value": 10.0,
                "max_value": 120.0,
            }
        )
        self.store.poll_controls_once()  # 建基线

        # 外部尝试触发被禁用的 YK 和越上限的 YT
        ts = int(time.time() * 1000)
        with self.store.connection() as con:
            con.execute(
                f'UPDATE "{self.table}" SET run_stat=1, run_stat_cmd_time=?, '
                'p_set=180.0, p_set_cmd_time=? WHERE idx=?',
                (ts, ts, self.idx),
            )
        res = self.store.poll_controls_once()
        self.assertEqual(res["dispatched"], 0)
        self.assertEqual(res["rejected"], 2)

        with self.store.connection() as con:
            row = con.execute(
                f'SELECT run_stat_cmd_status, p_set_cmd_status FROM "{self.table}" WHERE idx=?',
                (self.idx,),
            ).fetchone()
        self.assertEqual(row[0], CTRL_STATUS_DISABLED)
        self.assertEqual(row[1], CTRL_STATUS_FAILED)

        # 源库 scdfe_c 保持未触发状态
        with closing(sqlite3.connect(self.src_db)) as ccon:
            self.assertEqual(
                ccon.execute("SELECT cmd_time FROM fes_yk_define WHERE rtu_id=3 AND pnt_no=1").fetchone()[0],
                0,
            )
            self.assertEqual(
                ccon.execute("SELECT cmd_time FROM fes_yt_define WHERE rtu_id=3 AND pnt_no=1").fetchone()[0],
                0,
            )

    def test_web_api_and_pages(self):
        """端到端测试 scdfe_d Web 的控调接口、日志接口与页面路由。"""
        server = make_server(
            "127.0.0.1",
            0,
            db_path=self.dev_db,
            workspace_root=self.root,
        )
        server.store.set_source_database("scdfe_c.sqlite3")
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # 页面路由返回 200
            for route in ("/controls", "/control-mappings", "/logs"):
                with urlopen(base + route, timeout=5) as resp:
                    body = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200)
                    self.assertIn("控调下发", body)
                    self.assertIn("运行日志", body)

            # 源控调点检索
            st, pts = _http_json(base, "/api/source-points?kind=yt&q=p_set")
            self.assertEqual(st, 200)
            self.assertEqual(pts["total"], 2)

            # 创建并下发 YT
            st, created = _http_json(
                base,
                "/api/controls",
                "POST",
                {
                    "device_id": self.dev_id,
                    "control": "p_set",
                    "kind": "yt",
                    "rtu_id": 3,
                    "pnt_no": 1,
                    "min_value": 0,
                    "max_value": 150,
                },
            )
            self.assertEqual(st, 201)
            cid = created["item"]["control_id"]

            # 越限下发被拒绝
            st_bad, _ = _http_json(base, f"/api/controls/{cid}/dispatch", "POST", {"value": 199})
            self.assertEqual(st_bad, 400)

            # 正常下发成功
            st_ok, disp = _http_json(base, f"/api/controls/{cid}/dispatch", "POST", {"value": 66.5})
            self.assertEqual(st_ok, 200)
            self.assertEqual(disp["cmd_status"], CTRL_STATUS_PENDING)

            # 查询控调列表与历史流水
            st_list, lst = _http_json(base, "/api/controls?kind=yt")
            self.assertEqual(st_list, 200)
            self.assertEqual(lst["total"], 1)
            self.assertAlmostEqual(lst["items"][0]["value"], 66.5)

            st_hist, hist = _http_json(base, "/api/control-history")
            self.assertEqual(st_hist, 200)
            self.assertGreaterEqual(len(hist["items"]), 1)

            # 查询运行日志文件与内容
            st_lf, lf = _http_json(base, "/api/logs/files")
            self.assertEqual(st_lf, 200)
            self.assertTrue(any(f["process"] == "scdfe_d-control" for f in lf["items"]))

            st_lr, lr = _http_json(base, "/api/logs/read?process=scdfe_d-control&q=66.5")
            self.assertEqual(st_lr, 200)
            self.assertGreaterEqual(len(lr["items"]), 1)

            # 一键同步绑定：先预览再执行（用 ACGenerator 表和匹配名称的设备验证）
            with server.store.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                server.store._ensure_device_table(con, "ACGenerator")
                con.execute('INSERT INTO "ACGenerator"(idx, name) VALUES(1, ?)', ("柴发-1",))
            st_pre, pre = _http_json(base, "/api/sync-bindings", "POST", {"scope": "controls", "dry_run": True})
            self.assertEqual(st_pre, 200)
            self.assertTrue(pre["dry_run"])
            self.assertEqual(pre["to_add"], 2)

            st_run, applied = _http_json(base, "/api/sync-bindings", "POST", {"scope": "controls", "dry_run": False})
            self.assertEqual(st_run, 200)
            self.assertEqual(applied["to_add"], 2)

            st_re, re_pre = _http_json(base, "/api/sync-bindings", "POST", {"scope": "controls", "dry_run": True})
            self.assertEqual(st_re, 200)
            self.assertEqual(re_pre["to_add"], 0)
            self.assertEqual(re_pre["unchanged"], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

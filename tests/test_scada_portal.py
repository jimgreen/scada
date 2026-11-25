"""SCADA 综合网站（scada_portal）端到端后端 API、采样日曲线与静态资源回归测试。"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.parse import quote

from scada_portal.server import make_server
from scdfe_d.measurement_layout import SUFFIXES


def http_get(port: int, path: str) -> tuple[int, str, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    ctype = resp.getheader("Content-Type", "")
    body = resp.read()
    conn.close()
    return resp.status, ctype, body


class ScadaPortalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c_db = self.root / "scdfe_c.sqlite3"
        self.d_db = self.root / "scdfe_d.sqlite3"
        self.model_db = self.root / "devices.sqlite3"
        self.svg_path = self.root / "qinling.svg"
        self.log_dir = self.root / "logs"
        self.hist_db = self.root / "history.sqlite3"

        self._seed_c_db()
        self._seed_d_db()
        self._seed_model_db()
        self._seed_svg()
        self._seed_logs()

        self.httpd = make_server(
            "127.0.0.1",
            0,
            c_db=self.c_db,
            d_db=self.d_db,
            model_db=self.model_db,
            svg_path=self.svg_path,
            log_dir=self.log_dir,
            history_db=self.hist_db,
            enable_sampler=False,
        )
        self.httpd.service.sample_once()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def _seed_c_db(self):
        conn = sqlite3.connect(self.c_db)
        try:
            conn.executescript(
                """
                CREATE TABLE fes_rtu_define (
                    rtu_id INTEGER PRIMARY KEY, channel_id INTEGER, channel_type TEXT,
                    name TEXT, process_enable INTEGER DEFAULT 0, status INTEGER DEFAULT 0,
                    refresh_time INTEGER DEFAULT 0
                );
                CREATE TABLE fes_channel_iec104_define (
                    id INTEGER PRIMARY KEY, channel_id INTEGER, rtu_addr INTEGER,
                    remote_host TEXT, remote_port INTEGER
                );
                CREATE TABLE fes_yc_define (
                    rtu_id INTEGER, pnt_no INTEGER, name TEXT,
                    raw_value REAL, value REAL, status INTEGER, refresh_time INTEGER
                );
                CREATE TABLE fes_yx_define (
                    rtu_id INTEGER, pnt_no INTEGER, name TEXT,
                    raw_status INTEGER, value INTEGER, status INTEGER, refresh_time INTEGER
                );
                CREATE TABLE fes_yk_define (
                    rtu_id INTEGER, pnt_no INTEGER, name TEXT,
                    value INTEGER, status INTEGER, refresh_time INTEGER, cmd_handle_time INTEGER
                );
                CREATE TABLE fes_yt_define (
                    rtu_id INTEGER, pnt_no INTEGER, name TEXT,
                    value REAL, status INTEGER, refresh_time INTEGER, cmd_handle_time INTEGER
                );
                CREATE TABLE fes_channel_runtime_status (
                    service_name TEXT, channel_id INTEGER, protocol TEXT, channel_name TEXT,
                    state TEXT, running INTEGER, connection_state TEXT, process_pid INTEGER DEFAULT 0,
                    rx_count INTEGER, tx_count INTEGER, error_count INTEGER,
                    last_error TEXT, process_error TEXT, process_heartbeat_time INTEGER,
                    diagnostic_json TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO fes_rtu_define (rtu_id, channel_id, channel_type, name, process_enable) "
                "VALUES (3, 3, 'iec104', '秦岭站', 1)"
            )
            conn.execute(
                "INSERT INTO fes_channel_iec104_define (id, channel_id, rtu_addr, remote_host, remote_port) "
                "VALUES (3, 3, 1, '127.0.0.1', 2405)"
            )
            conn.execute(
                "INSERT INTO fes_yc_define (rtu_id, pnt_no, name, raw_value, value, status, refresh_time) "
                "VALUES (3, 1, 'ACGenerator.交流风电-1.P_GEN', 45.2, 45.2, 0, 1795517000000)"
            )
            conn.execute(
                "INSERT INTO fes_yx_define (rtu_id, pnt_no, name, value, status, refresh_time) "
                "VALUES (3, 1, 'ACBreak.交流断路器-1.status', 1, 0, 1795517000000)"
            )
            conn.execute(
                "INSERT INTO fes_channel_runtime_status "
                "(service_name, channel_id, protocol, channel_name, state, running, connection_state, rx_count, tx_count, error_count, process_heartbeat_time) "
                "VALUES ('scdfe_c', 3, 'iec104', '秦岭站', 'running', 1, 'connected', 1200, 10, 0, 1795517000000)"
            )
            conn.commit()
        finally:
            conn.close()

    def _create_device_table(self, conn: sqlite3.Connection, table: str, metrics: list[tuple[str, str]]):
        cols = ["idx INTEGER PRIMARY KEY", "name TEXT"]
        for m, kind in metrics:
            vtype = "INTEGER" if kind == "yx" else "REAL"
            cols.append(f'"{m}" {vtype}')
            cols.append(f'"{m}_qual" INTEGER')
            cols.append(f'"{m}_pre" {vtype}')
            cols.append(f'"{m}_time" TEXT')
            cols.append(f'"{m}_notuse" INTEGER DEFAULT 0')
            cols.append(f'"{m}_weight" REAL DEFAULT 1.0')
            cols.append(f'"{m}_rtu" INTEGER')
            cols.append(f'"{m}_pnt" INTEGER')
            cols.append(f'"{m}_max" REAL')
            cols.append(f'"{m}_min" REAL')
        conn.execute(f'CREATE TABLE "{table}" ({", ".join(cols)})')

    def _seed_d_db(self):
        conn = sqlite3.connect(self.d_db)
        try:
            self._create_device_table(conn, "ACGenerator", [("p", "yc"), ("q", "yc"), ("uab", "yc"), ("ia", "yc"), ("run", "yx")])
            self._create_device_table(conn, "DCGenerator", [("p", "yc"), ("u", "yc"), ("i", "yc"), ("run", "yx")])
            self._create_device_table(conn, "ACStorageGen", [("soc", "yc")])
            self._create_device_table(conn, "ACLoad", [("p", "yc"), ("run", "yx")])
            self._create_device_table(conn, "ACBreak", [("ia", "yc"), ("position", "yx")])
            self._create_device_table(conn, "ACBranch", [("i_p", "yc"), ("j_p", "yc"), ("run", "yx")])

            conn.execute(
                'INSERT INTO ACBranch (idx, name, i_p, i_p_qual, i_p_rtu, i_p_pnt, j_p, j_p_qual, j_p_rtu, j_p_pnt, run, run_qual, run_rtu, run_pnt) '
                'VALUES (20, "交流线路-20", 45.2, 0, 3, 10, -45.0, 0, 3, 11, 1, 0, 3, 10)'
            )

            conn.execute(
                'INSERT INTO ACGenerator (idx, name, p, p_qual, p_rtu, p_pnt, uab, uab_qual, uab_rtu, uab_pnt, run, run_qual, run_rtu, run_pnt) '
                'VALUES (1, "交流风电-1", 45.2, 0, 3, 1, 380.1, 0, 3, 2, 1, 0, 3, 1)'
            )
            conn.execute(
                'INSERT INTO ACGenerator (idx, name, p, p_qual, p_rtu, p_pnt, run, run_qual, run_rtu, run_pnt) '
                'VALUES (23, "交流电化学储能-23", 20.0, 0, 3, 3, 1, 0, 3, 2)'
            )
            conn.execute(
                'INSERT INTO ACGenerator (idx, name, p, p_qual, p_rtu, p_pnt, run, run_qual, run_rtu, run_pnt) '
                'VALUES (30, "交流柴油发电机-30", 50.0, 0, 3, 4, 1, 0, 3, 3)'
            )
            conn.execute(
                'INSERT INTO DCGenerator (idx, name, p, p_qual, p_rtu, p_pnt, run, run_qual, run_rtu, run_pnt) '
                'VALUES (1, "直流光伏-1", 34.8, 0, 3, 5, 1, 0, 3, 4)'
            )
            conn.execute(
                'INSERT INTO ACStorageGen (idx, name, soc, soc_qual, soc_rtu, soc_pnt) '
                'VALUES (1, "交流电化学储能-23", 0.75, 0, 3, 6)'
            )
            conn.execute(
                'INSERT INTO ACLoad (idx, name, p, p_qual, p_rtu, p_pnt, run, run_qual, run_rtu, run_pnt) '
                'VALUES (1, "交流负荷-1", 120.0, 0, 3, 7, 1, 0, 3, 5)'
            )
            conn.execute(
                'INSERT INTO ACLoad (idx, name, p, p_qual, p_rtu, p_pnt, run, run_qual, run_rtu, run_pnt) '
                'VALUES (2, "交流电制氢-1_交流设备端交流电负荷", 30.0, 0, 3, 8, 1, 0, 3, 6)'
            )
            conn.execute(
                'INSERT INTO ACBreak (idx, name, ia, ia_qual, ia_rtu, ia_pnt, position, position_qual, position_rtu, position_pnt) '
                'VALUES (1, "交流断路器-1", 15.5, 0, 3, 9, 1, 0, 3, 1)'
            )
            conn.commit()
        finally:
            conn.close()

    def _seed_model_db(self):
        conn = sqlite3.connect(self.model_db)
        try:
            conn.executescript(
                """
                CREATE TABLE rtdb_schema_tables (table_name TEXT, ordinal INTEGER);
                CREATE TABLE rtdb_schema_columns (table_name TEXT, ordinal INTEGER, eng_name TEXT, chn_name TEXT);
                INSERT INTO rtdb_schema_tables VALUES ('ACGenerator', 0), ('ACWindGen', 1);
                INSERT INTO rtdb_schema_columns VALUES
                    ('ACGenerator', 0, 'idx', 'idx'),
                    ('ACGenerator', 1, 'name', 'name'),
                    ('ACGenerator', 2, 'dev_type', 'dev_type'),
                    ('ACGenerator', 3, 'rated_capacity', 'rated_capacity'),
                    ('ACGenerator', 4, 'rated_voltage', 'rated_voltage'),
                    ('ACWindGen', 0, 'idx', 'idx'),
                    ('ACWindGen', 1, 'idx_acgenerator', 'idx_acgenerator'),
                    ('ACWindGen', 2, 'wind_turbine_model', 'wind_turbine_model'),
                    ('ACWindGen', 3, 'cut_out_wind_speed', 'cut_out_wind_speed');
                CREATE TABLE rtdb_data_ACGenerator (row_id INTEGER PRIMARY KEY, c0 INTEGER, c1 TEXT, c2 TEXT, c3 REAL, c4 REAL);
                INSERT INTO rtdb_data_ACGenerator VALUES (1, 1, '交流风电-1', 'ac-wind-source', 10.1, 380);
                CREATE TABLE rtdb_data_ACWindGen (row_id INTEGER PRIMARY KEY, c0 INTEGER, c1 INTEGER, c2 TEXT, c3 REAL);
                INSERT INTO rtdb_data_ACWindGen VALUES (1, 1, 1, 'WT-5MW', 35);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _seed_svg(self):
        self.svg_path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0,0,1000,800">'
            '<g id="root_g"><g id="Background_Layer"><rect width="100%" height="100%" fill="#f1f5f9"/></g>'
            '<g id="Segment_Layer"><path id="edge-1" class="kv380" source-dev-id="ACGenerator-1" target-dev-id="ACBreak-1" d="M 100 100 L 240 100"/></g>'
            '<g id="ACGenerator_Layer"><use id="ACGenerator-1" dev-id="ACGenerator-1" voltage-type="ac" node="1" x="100" y="100" width="80" height="60"/></g>'
            '<g id="ACBreak_Layer"><use id="ACBreak-1" dev-id="ACBreak-1" voltage-type="ac" node-1="1" node-2="2" x="240" y="100" width="80" height="60"/></g>'
            '<g id="ACBranch_Layer"><use id="ACBranch-20" dev-id="ACBranch-20" voltage-type="ac" node-1="1" node-2="11" x="180" y="100" width="120" height="36"/></g>'
            '<g id="Measurement_Layer"><g class="mg" dev="ACGenerator-1">'
            '<text><tspan>P</tspan><tspan class="mv" mt="activePower">--</tspan><tspan>MW</tspan></text>'
            '</g></g></g></svg>',
            encoding="utf-8",
        )

    def _seed_logs(self):
        day_dir = self.log_dir / "2026" / "11" / "25"
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "scdfe_c-main-20261125-01.log").write_text(
            "01:30:01 [日志] 秦岭站 RTU 3 启动成功\n"
            "01:30:05 [警告] 通道 1 重连等待\n",
            encoding="utf-8",
        )

    def test_static_pages_and_assets_served(self):
        for path in (
            "/", "/powerflow", "/devices", "/comm", "/logs",
            "/static/portal.css", "/static/portal.js",
            "/static/ui-theme.css", "/static/ui-i18n.js",
            "/static/assets/qinling-hero.jpg",
        ):
            status, _, body = http_get(self.httpd.server_port, path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body, path)

        _, _, html = http_get(self.httpd.server_port, "/")
        text = html.decode("utf-8")
        for view_id in ("view-home", "view-powerflow", "view-devices", "view-comm", "view-logs"):
            self.assertIn(view_id, text)

        _, _, js = http_get(self.httpd.server_port, "/static/portal.js")
        self.assertNotIn("window.prompt", js.decode("utf-8"))

    def test_overview_energy_aggregation(self):
        status, _, body = http_get(self.httpd.server_port, "/api/overview")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        rt = data["realtime"]
        self.assertAlmostEqual(rt["wind_kw"], 45.2, places=1)
        self.assertAlmostEqual(rt["pv_kw"], 34.8, places=1)
        self.assertAlmostEqual(rt["diesel_kw"], 50.0, places=1)
        self.assertAlmostEqual(rt["storage_kw"], 20.0, places=1)
        self.assertAlmostEqual(rt["storage_soc_pct"], 75.0, places=1)
        self.assertAlmostEqual(rt["load_kw"], 120.0, places=1)
        self.assertAlmostEqual(rt["hydrogen_prod_kw"], 30.0, places=1)
        self.assertEqual(data["health"]["rtu_online"], 1)

    def test_svg_and_powerflow_snapshot(self):
        status, ctype, svg = http_get(self.httpd.server_port, "/api/svg")
        self.assertEqual(status, 200)
        self.assertIn("image/svg+xml", ctype)
        self.assertIn(b"Measurement_Layer", svg)

        status, _, body = http_get(self.httpd.server_port, "/api/powerflow")
        self.assertEqual(status, 200)
        pf = json.loads(body.decode("utf-8"))
        self.assertAlmostEqual(pf["svg_slots"]["ACGenerator-1"]["activePower"]["value"], 45.2)
        self.assertEqual(pf["svg_slots"]["ACGenerator-1"]["activePower"]["unit"], "kW")
        self.assertEqual(pf["svg_slots"]["ACGenerator-1"]["activePower"]["rtu_id"], 3)
        self.assertEqual(pf["svg_slots"]["ACGenerator-1"]["activePower"]["weight"], 1.0)
        self.assertEqual(pf["svg_slots"]["ACGenerator-1"]["activePower"]["quality_label"], "正常")
        # 储能 SOC 应自动关联到对应 ACGenerator-23，并透传其真实扩展表与量测元数据
        self.assertAlmostEqual(pf["svg_slots"]["ACGenerator-23"]["soc"]["value"], 75.0)
        self.assertEqual(pf["svg_slots"]["ACGenerator-23"]["soc"]["table_name"], "ACStorageGen")
        self.assertEqual(pf["svg_slots"]["ACGenerator-23"]["soc"]["metric"], "soc")
        # 设备静态模型参数（含主表 ACGenerator 与扩展表 ACWindGen 合并）
        params = {p["key"]: p for p in pf["devices"]["ACGenerator-1"]["model_params"]}
        self.assertEqual(params["rated_capacity"]["value"], "10.1 kW")
        self.assertEqual(params["rated_voltage"]["value"], "380 V")
        self.assertEqual(params["wind_turbine_model"]["value"], "WT-5MW")
        self.assertEqual(params["cut_out_wind_speed"]["value"], "35 m/s")
        self.assertTrue(pf["breakers"]["ACBreak-1"]["closed"])
        # 潮流箭头矢量（支路与连接边）
        self.assertAlmostEqual(pf["flows"]["branches"]["ACBranch-20"]["p_kw"], 45.2)
        self.assertEqual(pf["flows"]["branches"]["ACBranch-20"]["voltage_type"], "ac")
        self.assertAlmostEqual(pf["flows"]["edges"]["edge-1"]["p_kw"], 45.2)

    def test_devices_categories_and_daily_trend(self):
        status, _, body = http_get(self.httpd.server_port, "/api/devices")
        self.assertEqual(status, 200)
        devs = json.loads(body.decode("utf-8"))
        cat_keys = {c["key"] for c in devs["categories"]}
        self.assertTrue({"wind", "pv", "diesel", "storage", "load", "switch"} <= cat_keys)

        status, _, tbody = http_get(self.httpd.server_port, "/api/devices/ACGenerator/1/trend?hours=24")
        self.assertEqual(status, 200)
        trend = json.loads(tbody.decode("utf-8"))
        metrics = {s["metric"]: s for s in trend["series"]}
        self.assertIn("p", metrics)
        self.assertEqual(metrics["p"]["latest"], 45.2)

        # 按精确 start_ms / end_ms 查询自然时间窗
        now_ms = trend["now_ms"]
        status, _, wbody = http_get(
            self.httpd.server_port,
            f"/api/devices/ACGenerator/1/trend?start_ms={now_ms - 60000}&end_ms={now_ms + 60000}",
        )
        self.assertEqual(status, 200)
        wtrend = json.loads(wbody.decode("utf-8"))
        self.assertEqual(wtrend["since_ms"], now_ms - 60000)
        self.assertEqual(wtrend["end_ms"], now_ms + 60000)
        self.assertIn("p", {s["metric"] for s in wtrend["series"]})

    def test_comm_endpoints_and_logs(self):
        status, _, r_body = http_get(self.httpd.server_port, "/api/comm/rtus")
        self.assertEqual(status, 200)
        rtus = json.loads(r_body.decode("utf-8"))
        self.assertEqual(rtus["total"], 1)
        self.assertEqual(rtus["items"][0]["connection_state"], "connected")

        status, _, ch_body = http_get(self.httpd.server_port, "/api/comm/channels")
        self.assertEqual(status, 200)
        chs = json.loads(ch_body.decode("utf-8"))
        self.assertEqual(chs["items"][0]["endpoint"], "127.0.0.1:2405")

        status, _, pt_body = http_get(self.httpd.server_port, "/api/comm/points?kind=yc&rtu_id=3")
        self.assertEqual(status, 200)
        pts = json.loads(pt_body.decode("utf-8"))
        self.assertEqual(pts["total"], 1)

        status, _, lf_body = http_get(self.httpd.server_port, "/api/logs/files?record=runtime")
        self.assertEqual(status, 200)
        lfiles = json.loads(lf_body.decode("utf-8"))
        self.assertEqual(lfiles["total"], 1)

        status, _, lr_body = http_get(
            self.httpd.server_port,
            "/api/logs/read?record=runtime&keyword=" + quote("秦岭站"),
        )
        self.assertEqual(status, 200)
        lread = json.loads(lr_body.decode("utf-8"))
        self.assertEqual(len(lread["items"]), 1)
        self.assertIn("秦岭站", lread["items"][0]["text"])

    def test_update_device_limits_and_reject_invalid_range(self):
        def http_put(path: str, payload: dict) -> tuple[int, dict]:
            conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
            body = json.dumps(payload).encode("utf-8")
            conn.request("PUT", path, body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            conn.close()
            return resp.status, data

        # 1. 上限 <= 下限应返回 400 拒绝修改
        for bad_max, bad_min in ((20.0, 20.0), (10.0, 30.0)):
            status, data = http_put(
                "/api/devices/ACGenerator/1/limits",
                {"metric": "p", "max_value": bad_max, "min_value": bad_min},
            )
            self.assertEqual(status, 400)
            self.assertIn("上限必须大于下限", data["error"])

        # 2. 合法上下限（当前值 45.2 > 40.0）即时更新为状态 3（越上限）
        status, data = http_put(
            "/api/devices/ACGenerator/1/limits",
            {"metric": "p", "max_value": 40.0, "min_value": 0.0},
        )
        self.assertEqual(status, 200)
        self.assertEqual((data["max_value"], data["min_value"], data["quality"], data["quality_label"]), (40.0, 0.0, 3, "越上限"))

        # 3. 调整为合理范围 [0, 100] 后恢复状态 0（正常）
        status, data = http_put(
            "/api/devices/ACGenerator/1/limits",
            {"metric": "p", "max_value": 100.0, "min_value": 0.0},
        )
        self.assertEqual(status, 200)
        self.assertEqual((data["quality"], data["quality_label"]), (0, "正常"))


if __name__ == "__main__":
    unittest.main()

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
from scada_portal.service import PortalService
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
        self.c_db = self.root / "scdfe_c.db"
        self.d_db = self.root / "scdfe_d.db"
        self.model_db = self.root / "devices.db"
        self.svg_path = self.root / "qinling.svg"
        self.log_dir = self.root / "logs"
        self.hist_db = self.root / "history.db"

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
            "/static/assets/qinling-station-holo.png",
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
        # 设备静态模型参数（含主表 ACGenerator 与扩展表 ACWindGen 合并，并按扩展表自动分页）
        params = {p["key"]: p for p in pf["devices"]["ACGenerator-1"]["model_params"]}
        self.assertEqual(params["rated_capacity"]["value"], "10.1 kW")
        self.assertEqual(params["rated_voltage"]["value"], "380 V")
        self.assertEqual(params["wind_turbine_model"]["value"], "WT-5MW")
        self.assertEqual(params["cut_out_wind_speed"]["value"], "35 m/s")
        tab_labels = [t["label"] for t in pf["devices"]["ACGenerator-1"]["model_param_tabs"]]
        self.assertEqual(tab_labels, ["电气参数", "风机参数"])
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

    def test_energy_flow_single_metric_trend_and_http_guards(self):
        # 1. 全景流向实时聚合与 SPA 路由
        status, _, page = http_get(self.httpd.server_port, "/energy-flow")
        self.assertEqual(status, 200)
        self.assertIn("view-energyflow", page.decode("utf-8"))

        status, _, ef_raw = http_get(self.httpd.server_port, "/api/energy-flow")
        self.assertEqual(status, 200)
        ef = json.loads(ef_raw.decode("utf-8"))
        self.assertIn("summary", ef)
        self.assertIn("nodes", ef)
        self.assertIn("sources", ef)
        self.assertGreaterEqual(ef["summary"]["total_load_kw"], 0.0)

        # 1.2 极地管控三栏大屏 SPA 路由与聚合接口
        status, _, ck_page = http_get(self.httpd.server_port, "/cockpit")
        self.assertEqual(status, 200)
        self.assertIn("view-cockpit", ck_page.decode("utf-8"))

        status, _, ck_raw = http_get(self.httpd.server_port, "/api/polar-cockpit?hours=6")
        self.assertEqual(status, 200)
        ck = json.loads(ck_raw.decode("utf-8"))
        self.assertEqual(set(ck["hub"]["nodes"].keys()), {"wind", "pv", "storage", "load", "hydrogen", "diesel"})
        self.assertGreater(ck["cumulative"]["total_supply_wkwh"], 0.0)
        self.assertGreater(ck["cumulative"]["co2_reduced_tons"], 0.0)
        self.assertGreaterEqual(ck["resilience"]["autonomy_hours"], 0.0)
        self.assertEqual(len(ck["source_mix"]), 4)
        self.assertEqual(len(ck["power_curves"]), 5)
        self.assertGreaterEqual(len(ck["storage_schedule"]), 1)

        # 2. 单指标趋势查询与单边时间窗/超大整数防御
        status, _, tr_raw = http_get(self.httpd.server_port, "/api/devices/ACGenerator/1/trend?hours=1&metric=p")
        self.assertEqual(status, 200)
        tr = json.loads(tr_raw.decode("utf-8"))
        self.assertTrue(all(s["metric"] == "p" for s in tr["series"]))

        status, _, _ = http_get(self.httpd.server_port, "/api/devices/ACGenerator/1/trend?start_ms=1000")
        self.assertEqual(status, 400)

        status, _, _ = http_get(self.httpd.server_port, "/api/comm/points?rtu_id=9999999999999999999999")
        self.assertEqual(status, 400)

        # 3. 不支持的 HTTP 方法返回 JSON 405
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        conn.request("POST", "/api/overview")
        resp = conn.getresponse()
        body_405 = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 405)
        self.assertIn("error", body_405)

        # 4. 静态资源支持 ETag / 304 协商缓存
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        conn.request("GET", "/static/portal.css")
        resp200 = conn.getresponse()
        resp200.read()
        etag = resp200.getheader("ETag")
        conn.close()
        self.assertEqual(resp200.status, 200)
        self.assertTrue(etag)
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        conn.request("GET", "/static/portal.css", headers={"If-None-Match": etag})
        resp304 = conn.getresponse()
        resp304.read()
        conn.close()
        self.assertEqual(resp304.status, 304)

    def test_alarms_local_control_compare_and_d_logs(self):
        def http_json(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
            conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            conn.close()
            return resp.status, data

        # 1. /alarms 页面与活动告警产生/确认闭环
        status, _, page = http_get(self.httpd.server_port, "/alarms")
        self.assertEqual(status, 200)
        self.assertIn("view-alarms", page.decode("utf-8"))

        st, _ = http_json("PUT", "/api/devices/ACGenerator/1/limits", {"metric": "p", "max_value": 40.0, "min_value": 0.0})
        self.assertEqual(st, 200)
        st, alarms = http_json("GET", "/api/alarms?type=over_max")
        self.assertEqual(st, 200)
        self.assertGreaterEqual(alarms["summary"]["over_max"], 1)
        self.assertGreaterEqual(alarms["summary"]["unacked"], 1)

        target_key = alarms["items"][0]["key"]
        st, ack_res = http_json("POST", "/api/alarms/ack", {"keys": [target_key]})
        self.assertEqual(st, 200)
        self.assertEqual(ack_res["acked_count"], 1)
        st, after_ack = http_json("GET", "/api/alarms?type=over_max&acked=0")
        self.assertEqual(st, 200)
        self.assertEqual(len(after_ack["items"]), 0)

        # 验证服务重启后仍保留持久化确认状态
        restarted_svc = PortalService(
            c_db=self.c_db,
            d_db=self.d_db,
            model_db=self.model_db,
            svg_path=self.svg_path,
            log_dir=self.log_dir,
            history_db=self.hist_db,
            enable_sampler=False,
        )
        try:
            restarted_unacked = restarted_svc.get_alarms(alarm_type="over_max", acked="0")
            self.assertEqual(len(restarted_unacked["items"]), 0)
            restarted_all = restarted_svc.get_alarms(alarm_type="over_max")
            self.assertEqual(len(restarted_all["items"]), 1)
            self.assertTrue(restarted_all["items"][0]["acked"])
        finally:
            restarted_svc.close()

        # 恢复合理上限，验证历史告警自动标记为 recovered 且保留 acked=True
        http_json("PUT", "/api/devices/ACGenerator/1/limits", {"metric": "p", "max_value": 100.0, "min_value": 0.0})
        st_hist, hist = http_json("GET", "/api/alarms/history?type=over_max&state=recovered")
        self.assertEqual(st_hist, 200)
        matched = [item for item in hist["items"] if item["key"] == target_key]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["state"], "recovered")
        self.assertTrue(matched[0]["acked"])
        self.assertIsNotNone(matched[0]["recovered_ms"])

        # 健康检查应透出转发线程在线状态（单测未启动采集锁故为 False）
        st_h, health = http_json("GET", "/api/health")
        self.assertEqual(st_h, 200)
        self.assertIn("collector_running", health)
        self.assertFalse(health["collector_running"])

        # 2. 为 ACGenerator 增加控调列并测试门户就地控调写库
        con = sqlite3.connect(self.d_db)
        try:
            con.executescript(
                """
                ALTER TABLE ACGenerator ADD COLUMN p_set REAL;
                ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_time INTEGER;
                ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_status INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_ack_time INTEGER;
                ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_notuse INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_rtu INTEGER;
                ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_pnt INTEGER;
                ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_max REAL;
                ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_min REAL;
                UPDATE ACGenerator SET p_set_cmd_rtu=3, p_set_cmd_pnt=1, p_set_cmd_max=100.0, p_set_cmd_min=0.0 WHERE idx=1;
                """
            )
            con.commit()
        finally:
            con.close()
        # 越限应拒绝（400）
        st_bad, _ = http_json("POST", "/api/devices/ACGenerator/1/control", {"control": "p_set", "value": 150.0})
        self.assertEqual(st_bad, 400)
        # 合法值应写入待回执状态并提示后台转发未启动
        st_ok, disp = http_json("POST", "/api/devices/ACGenerator/1/control", {"control": "p_set", "value": 55.5})
        self.assertEqual(st_ok, 200)
        self.assertEqual(disp["cmd_status"], 1)
        self.assertAlmostEqual(disp["value"], 55.5)
        self.assertFalse(disp["collector_running"])
        self.assertTrue(disp["warning"])

        # 3. 跨设备曲线对比接口
        st_cmp, cmp_data = http_json("GET", "/api/trends/compare?targets=ACGenerator:1:p,ACGenerator:2:p&hours=24")
        self.assertEqual(st_cmp, 200)
        self.assertEqual(len(cmp_data["series"]), 2)

        # 3.5 校验日曲线与告警历史库自动具备完整 RTDB 原生内置结构定义
        hcon = sqlite3.connect(self.httpd.service.history_db)
        try:
            htables = {r[0] for r in hcon.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue(
                {"device_daily_samples", "alarm_events", "rtdb_schema_database", "rtdb_schema_tables", "rtdb_schema_columns"}
                <= htables
            )
            fmt = hcon.execute("SELECT storage_format FROM rtdb_schema_database WHERE version=1").fetchone()
            self.assertEqual(fmt[0], "native")
            tmap = dict(hcon.execute("SELECT table_name, chn_name FROM rtdb_schema_tables").fetchall())
            self.assertEqual(tmap.get("device_daily_samples"), "设备日曲线采样")
            self.assertEqual(tmap.get("alarm_events"), "告警事件记录")
        finally:
            hcon.close()

        # 4. 门户指令审计日志源（scdfe_d/logs 自动落盘）
        st_lf, lf = http_json("GET", "/api/logs/files?record=control")
        self.assertEqual(st_lf, 200)
        self.assertGreaterEqual(lf["total"], 1)
        st_lr, lr = http_json("GET", "/api/logs/read?record=control&keyword=55.5")
        self.assertEqual(st_lr, 200)
        self.assertTrue(any("门户下发指令" in item["text"] for item in lr["items"]))
        # 确认告警与修改限值也应写出审计日志到 scdfe_d/logs
        d_web_logs = list((self.d_db.parent / "logs").rglob("scdfe_d-web-*.log"))
        self.assertTrue(d_web_logs)
        web_log_text = "\n".join(p.read_text(encoding="utf-8") for p in d_web_logs)
        self.assertIn("门户修改量测上下限", web_log_text)
        self.assertIn("门户确认告警", web_log_text)

    def test_upgrade_split_powerflow_maintenance_topology_report_and_rollup(self):
        # 1. 潮流图动静分离 + 强 ETag 304 缓存
        conn0 = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        conn0.request("GET", "/api/powerflow/meta")
        resp0 = conn0.getresponse()
        st_m = resp0.status
        etag = resp0.getheader("ETag")
        body_m = resp0.read()
        conn0.close()
        self.assertEqual(st_m, 200)
        self.assertTrue(etag and etag.startswith('"pf-meta-'))
        meta = json.loads(body_m.decode("utf-8"))
        self.assertIn("ACGenerator-1", meta["devices"])
        self.assertTrue(meta["devices"]["ACGenerator-1"]["model_param_tabs"])

        # 带 If-None-Match 命中 304
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        conn.request("GET", "/api/powerflow/meta", headers={"If-None-Match": etag})
        resp304 = conn.getresponse()
        resp304.read()
        conn.close()
        self.assertEqual(resp304.status, 304)

        # 轻量动态接口不含静态 model_params，体积明显小于全量 /api/powerflow
        st_l, _, body_l = http_get(self.httpd.server_port, "/api/powerflow/live")
        st_f, _, body_f = http_get(self.httpd.server_port, "/api/powerflow")
        self.assertEqual((st_l, st_f), (200, 200))
        live = json.loads(body_l.decode("utf-8"))
        self.assertIn("devices_live", live)
        self.assertNotIn("model_params", live["devices_live"]["ACGenerator-1"])
        self.assertLess(len(body_l), len(body_f))

        # 2. 检修挂牌增删 + 告警隐藏过滤 + 确认操作人/备注留痕
        def req_json(method: str, path: str, payload: dict | None = None):
            c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
            b = json.dumps(payload).encode("utf-8") if payload is not None else None
            h = {"Content-Type": "application/json"} if b is not None else {}
            c.request(method, path, body=b, headers=h)
            r = c.getresponse()
            raw = r.read().decode("utf-8")
            c.close()
            return r.status, (json.loads(raw) if raw else {})

        st_set, maint = req_json(
            "POST",
            "/api/maintenance",
            {"table_name": "ACGenerator", "idx": 1, "device_name": "交流风电-1", "reason": "定期巡检", "operator": "王工", "hours": 4},
        )
        self.assertEqual(st_set, 200)
        self.assertEqual(maint["dev_id"], "ACGenerator-1")
        st_list, mlist = req_json("GET", "/api/maintenance")
        self.assertEqual(st_list, 200)
        self.assertEqual(mlist["total"], 1)
        st_del, mdel = req_json("DELETE", "/api/maintenance/ACGenerator-1")
        self.assertEqual(st_del, 200)
        self.assertEqual(mdel["removed"], 1)

        # 3. 通信链路拓扑、站级运行日报（JSON/CSV/HTML）、健康检查与 1 分钟降采样表
        st_topo, topo = req_json("GET", "/api/comm/topology")
        self.assertEqual(st_topo, 200)
        self.assertEqual(len(topo["links"]), 1)
        self.assertGreaterEqual(topo["links"][0]["device_count"], 1)

        st_rj, rj = req_json("GET", "/api/report/daily?format=json")
        self.assertEqual(st_rj, 200)
        self.assertGreaterEqual(len(rj["kpis"]), 8)
        st_rc, _, rc_body = http_get(self.httpd.server_port, "/api/report/daily?format=csv")
        self.assertEqual(st_rc, 200)
        self.assertIn("站级运行日报", rc_body.decode("utf-8"))
        st_rh, _, rh_body = http_get(self.httpd.server_port, "/api/report/daily?format=html")
        self.assertEqual(st_rh, 200)
        self.assertIn("window.print()", rh_body.decode("utf-8"))

        st_h, health = req_json("GET", "/api/health")
        self.assertEqual(st_h, 200)
        self.assertIn("history", health)
        self.assertGreaterEqual(health["history"]["minute_rollup_rows"], 1)

        # 4. 昨日同比曲线 + 极地衍生指标 + 数据质量传导
        st_tr, tr_data = req_json("GET", "/api/devices/ACGenerator/1/trend?hours=24&compare=prev_day")
        self.assertEqual(st_tr, 200)
        self.assertEqual(tr_data["compare"], "prev_day")
        st_ck, ck = req_json("GET", "/api/polar-cockpit?hours=6")
        self.assertEqual(st_ck, 200)
        self.assertIn("derived", ck)
        self.assertIn("diesel_endurance_hours", ck["derived"])
        self.assertIn("data_quality", ck)


if __name__ == "__main__":
    unittest.main()

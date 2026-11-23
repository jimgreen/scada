"""scdfe_s 导出 E 文件与 scdfe_c 导入 E 文件及端到端 IEC104 通信测试。"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from core.channel_efile import (
    export_channel_efile,
    import_channel_efile,
    parse_efile,
    serialize_efile,
)
import scdfe_c
from scdfe_c.web.server import make_server as make_c_server
from scdfe_c.web.service import ApiError, ScadaStore
from scdfe_s.web.iec104_service import IEC104Service
from scdfe_s.web.server import make_server as make_s_server


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def http_call(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"} if raw is not None else {}
    conn.request(method, path, raw, headers)
    response = conn.getresponse()
    ctype = response.getheader("Content-Type", "")
    data = response.read()
    conn.close()
    if "application/json" in ctype:
        return response.status, json.loads(data.decode("utf-8"))
    return response.status, data


class EfileCodecTests(unittest.TestCase):
    """验证标准电力 E 文件序列化与解析的语法及无损往返能力。"""

    def test_serialize_and_parse_roundtrip_special_values(self):
        header = {
            "System": "SCADA",
            "Version": "1.0",
            "ChannelName": "秦岭 '1号' 变电站",
        }
        modules = [
            {
                "name": "fes_rtu_define",
                "utility": "scada",
                "props": {"rtu_id": 1, "channel_type": "iec104"},
                "columns": [
                    {"name": "rtu_id", "desc": "RTU编号"},
                    {"name": "name", "desc": "名称"},
                    {"name": "note", "desc": "备注"},
                    {"name": "empty_str", "desc": "空串"},
                ],
                "rows": [
                    [1, "秦岭 '1号' 站", None, ""],
                    [2, "Line1 Line2", "null", "普通文本"],
                ],
            }
        ]
        text = serialize_efile(header, modules)
        self.assertTrue(text.startswith("<! "))
        self.assertIn("<fes_rtu_define::scada ", text)
        self.assertIn("</fes_rtu_define>", text)

        # 带 UTF-8 BOM 也可正常解析
        parsed = parse_efile("\ufeff" + text)
        self.assertEqual(parsed["header"]["System"], "SCADA")
        self.assertEqual(parsed["header"]["ChannelName"], "秦岭 '1号' 变电站")
        self.assertEqual(len(parsed["modules"]), 1)
        mod = parsed["modules"][0]
        self.assertEqual(mod["name"], "fes_rtu_define")
        self.assertEqual(mod["rows"][0], ["1", "秦岭 '1号' 站", None, ""])
        self.assertEqual(mod["rows"][1], ["2", "Line1 Line2", "null", "普通文本"])

    def test_parse_rejects_invalid_syntax(self):
        with self.assertRaises(ApiError):
            parse_efile("")
        with self.assertRaises(ApiError):
            parse_efile("not an efile")
        with self.assertRaises(ApiError):
            parse_efile("<! System=SCADA !>\n<fes_rtu_define::scada>\n@rtu_id\n#1")
        with self.assertRaises(ApiError):
            parse_efile(
                "<! System=SCADA !>\n<fes_rtu_define::scada>\n@rtu_id name\n#1 a extra\n</fes_rtu_define>"
            )


class ChannelEfileHttpAndCommTests(unittest.TestCase):
    """验证 scdfe_s HTTP 导出、scdfe_c HTTP 导入、冲突策略及真实 IEC104 通信。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.s_db = self.root / "scdfe_s.sqlite3"
        self.c_db = self.root / "scdfe_c.sqlite3"

        self.s_httpd = make_s_server("127.0.0.1", 0, db_path=self.s_db)
        self.s_thread = threading.Thread(target=self.s_httpd.serve_forever, daemon=True)
        self.s_thread.start()
        self.addCleanup(self._stop_server, self.s_httpd, self.s_thread)

        self.c_httpd = make_c_server("127.0.0.1", 0, db_path=self.c_db)
        self.c_thread = threading.Thread(target=self.c_httpd.serve_forever, daemon=True)
        self.c_thread.start()
        self.addCleanup(self._stop_server, self.c_httpd, self.c_thread)

    @staticmethod
    def _stop_server(httpd, thread):
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)

    def _seed_s_iec104_channel(self, rtu_id: int, channel_id: int, port: int, host: str = "0.0.0.0"):
        store = self.s_httpd.store
        store.create_row(
            "fes_rtu_define",
            {
                "rtu_id": rtu_id,
                "channel_id": channel_id,
                "channel_type": "iec104",
                "name": f"秦岭测试站_{rtu_id}",
                "process_enable": 1,
            },
        )
        store.create_row(
            "fes_channel_iec104_define",
            {
                "id": channel_id,
                "channel_id": channel_id,
                "rtu_id": rtu_id,
                "rtu_addr": 1,
                "remote_host": host,
                "remote_port": port,
                "ycbegin_addr": 0x4001,
                "yxbegin_addr": 1,
                "ykbegin_addr": 0x6001,
                "ytbegin_addr": 0x6201,
            },
        )
        for kind, table, value, type_id, io_address in (
            ("yc", "fes_yc_define", 225.5, 1, 0x4001),
            ("yx", "fes_yx_define", 1, 1, 1),
            ("yk", "fes_yk_define", 0, 1, 0x6001),
            ("yt", "fes_yt_define", 12.5, 3, 0x6201),
        ):
            store.create_row(
                table,
                {
                    "rtu_id": rtu_id,
                    "pnt_no": 1,
                    "name": f"{kind}_测试点_{rtu_id}",
                    "value": value,
                },
            )
            store.create_row(
                "fes_channel_iec104_point_define",
                {
                    "channel_id": channel_id,
                    "rtu_id": rtu_id,
                    "kind": kind,
                    "pnt_no": 1,
                    "io_address": io_address,
                    "type_id": type_id,
                },
            )

    def test_http_export_import_conflicts_and_host_override(self):
        port = free_port()
        self._seed_s_iec104_channel(1, 1, port, host="0.0.0.0")

        # 1. scdfe_s HTTP 导出 E 文件
        status, exported = http_call(
            self.s_httpd.server_port, "GET", "/api/channels/1/export-efile"
        )
        self.assertEqual(status, 200)
        self.assertTrue(exported["filename"].endswith(".e"))
        self.assertEqual(exported["summary"]["total_points"], 4)
        content = exported["content"]
        self.assertIn("<fes_rtu_define::scada ", content)
        self.assertIn("<fes_channel_iec104_define::scada ", content)
        self.assertIn("<fes_channel_iec104_point_define::scada ", content)
        self.assertIn("<fes_yc_define::scada ", content)
        # 0.0.0.0 已被转换为可连接的 127.0.0.1
        self.assertNotIn("0.0.0.0", content)
        self.assertIn("127.0.0.1", content)

        # 2. 首次导入到空 scdfe_c（应保留原编号 RTU 1 / 通道 1）
        status, res1 = http_call(
            self.c_httpd.server_port,
            "POST",
            "/api/channels/import-efile",
            {"content": content, "conflict_mode": "auto_remap"},
        )
        self.assertEqual(status, 201)
        self.assertTrue(res1["imported"])
        self.assertEqual((res1["rtu_id"], res1["channel_id"]), (1, 1))
        self.assertFalse(res1["remapped"])
        self.assertEqual(res1["total_points"], 4)

        # 校验通信参数已正确入库
        c_store = self.c_httpd.store
        c_param = c_store.get_row("fes_channel_iec104_define", [1])
        self.assertEqual(c_param["remote_host"], "127.0.0.1")
        self.assertEqual(c_param["remote_port"], port)

        # 3. 再次导入同一文件，默认 auto_remap 应自动分配新编号且保留原 RTU 1
        status, res2 = http_call(
            self.c_httpd.server_port,
            "POST",
            "/api/channels/import-efile",
            {
                "content": content,
                "conflict_mode": "auto_remap",
                "host_override": "192.168.10.20",
            },
        )
        self.assertEqual(status, 201)
        self.assertTrue(res2["remapped"])
        self.assertEqual(res2["rtu_id"], 2)
        self.assertEqual(res2["channel_id"], 2)
        # 覆写后的主机地址生效，旧通道地址保持不变
        c_param_2 = c_store.get_row("fes_channel_iec104_define", [2])
        self.assertEqual(c_param_2["remote_host"], "192.168.10.20")
        self.assertEqual(c_store.get_row("fes_channel_iec104_define", [1])["remote_host"], "127.0.0.1")
        # 关联规约点表和四遥点表已同步重映射到 RTU 2 / 通道 2
        self.assertEqual(c_store.get_row("fes_yc_define", [2, 1])["name"], "yc_测试点_1")
        with c_store.connection() as conn:
            pt_rows = conn.execute(
                "SELECT channel_id, kind, pnt_no FROM fes_channel_iec104_point_define WHERE channel_id=2"
            ).fetchall()
        self.assertEqual(len(pt_rows), 4)
        self.assertTrue(all(r["channel_id"] == 2 for r in pt_rows))

        # 4. overwrite 模式覆盖 RTU 1（同时给 RTU 1 塞一条额外旧点，验证覆盖会清理旧点）
        c_store.create_row("fes_yc_define", {"rtu_id": 1, "pnt_no": 99, "name": "待清理旧点"})
        status, res3 = http_call(
            self.c_httpd.server_port,
            "POST",
            "/api/channels/import-efile",
            {
                "content": content,
                "conflict_mode": "overwrite",
                "host_override": "10.0.0.8",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual((res3["rtu_id"], res3["channel_id"]), (1, 1))
        self.assertEqual(c_store.get_row("fes_channel_iec104_define", [1])["remote_host"], "10.0.0.8")
        with self.assertRaises(ApiError):
            c_store.get_row("fes_yc_define", [1, 99])

    def test_modbus_tcp_export_and_import_roundtrip(self):
        s_store = self.s_httpd.store
        s_store.create_row(
            "fes_rtu_define",
            {
                "rtu_id": 5,
                "channel_id": 15,
                "channel_type": "modbus-tcp",
                "name": "逆变器_Modbus",
            },
        )
        s_store.create_row(
            "fes_channel_modbus_tcp_define",
            {
                "channel_id": 15,
                "rtu_id": 5,
                "remote_host": "127.0.0.1",
                "remote_port": 15020,
                "unit_id": 3,
                "response_timeout": 1.5,
            },
        )
        s_store.create_row(
            "fes_yc_define",
            {"rtu_id": 5, "pnt_no": 1, "name": "有功功率", "value": 88.0},
        )
        s_store.create_row(
            "fes_yx_define",
            {"rtu_id": 5, "pnt_no": 1, "name": "并网状态", "value": 1},
        )
        exported = export_channel_efile(s_store, 5)
        self.assertIn("<fes_channel_modbus_tcp_define::scada ", exported["content"])
        self.assertIn("<fes_yc_define::scada ", exported["content"])
        self.assertIn("<fes_yx_define::scada ", exported["content"])

        imported = import_channel_efile(self.c_httpd.store, exported["content"])
        self.assertEqual((imported["rtu_id"], imported["channel_id"]), (5, 15))
        self.assertEqual(imported["channel_type"], "modbus-tcp")
        self.assertEqual(imported["total_points"], 2)
        with self.c_httpd.store.connection() as conn:
            m_row = conn.execute(
                "SELECT remote_host, remote_port, unit_id FROM fes_channel_modbus_tcp_define WHERE channel_id=15"
            ).fetchone()
        self.assertEqual((m_row["remote_host"], m_row["remote_port"], m_row["unit_id"]), ("127.0.0.1", 15020, 3))
        self.assertEqual(self.c_httpd.store.get_row("fes_yc_define", [5, 1])["name"], "有功功率")
        self.assertEqual(self.c_httpd.store.get_row("fes_yx_define", [5, 1])["name"], "并网状态")

    def test_import_validation_errors_and_method_not_allowed(self):
        # GET /api/channels/import-efile 应返回 405
        status, _ = http_call(self.c_httpd.server_port, "GET", "/api/channels/import-efile")
        self.assertEqual(status, 405)

        # 未知表名应返回 400 且不污染数据库
        bad_efile = (
            "<! System=SCADA Version=1.0 !>\n"
            "<fes_rtu_define::scada>\n"
            "@rtu_id channel_id channel_type name\n"
            "#1 1 iec104 测试\n"
            "</fes_rtu_define>\n"
            "<fes_scdfe_t_define::scada>\n"
            "@channel_id name\n"
            "#1 越权表\n"
            "</fes_scdfe_t_define>\n"
        )
        status, payload = http_call(
            self.c_httpd.server_port,
            "POST",
            "/api/channels/import-efile",
            {"content": bad_efile},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        self.assertEqual(self.c_httpd.store.list_rows("fes_rtu_define")["total"], 0)

    def test_ui_assets_expose_export_and_import_controls_without_prompt(self):
        status, s_page = http_call(self.s_httpd.server_port, "GET", "/channels")
        self.assertEqual(status, 200)
        s_html = s_page.decode("utf-8")
        self.assertIn("efile-export-dialog", s_html)
        self.assertIn("efile-export-name", s_html)

        status, s_js = http_call(self.s_httpd.server_port, "GET", "/static/channels.js")
        self.assertEqual(status, 200)
        s_script = s_js.decode("utf-8")
        self.assertIn("export-efile", s_script)
        self.assertIn("showSaveFilePicker", s_script)
        self.assertNotIn("window.prompt", s_script)

        status, c_page = http_call(self.c_httpd.server_port, "GET", "/")
        self.assertEqual(status, 200)
        c_html = c_page.decode("utf-8")
        self.assertIn('id="import-efile"', c_html)
        self.assertIn('id="efile-import-dialog"', c_html)
        self.assertIn('id="efile-import-mode"', c_html)
        self.assertIn('id="efile-import-host"', c_html)

        status, c_js = http_call(self.c_httpd.server_port, "GET", "/static/app.js")
        self.assertEqual(status, 200)
        c_script = c_js.decode("utf-8")
        self.assertIn("/api/channels/import-efile", c_script)
        self.assertIn("scadaConfirm", c_script)
        self.assertNotIn("window.prompt", c_script)

    def test_end_to_end_export_import_and_live_iec104_communication(self):
        """实测 scdfe_s 导出 E 文件 -> scdfe_c 导入 -> 真实 IEC104 双向通信采数与下发指令。"""
        port = free_port()
        self._seed_s_iec104_channel(1, 1, port, host="0.0.0.0")

        # 启动真实 scdfe_s IEC104 服务端
        service = IEC104Service(self.s_httpd.store, config_path=self.root / "iec104_svc.json")
        service.configure({"rtu_id": 1, "bind_ip": "127.0.0.1", "port": port})
        service.start()
        self.addCleanup(service.close)

        # 1. 通过 scdfe_s Web API 导出 E 文件
        status, exported = http_call(
            self.s_httpd.server_port, "GET", "/api/channels/1/export-efile"
        )
        self.assertEqual(status, 200)

        # 2. 通过 scdfe_c Web API 导入到全新 scdfe_c 数据库
        status, imported = http_call(
            self.c_httpd.server_port,
            "POST",
            "/api/channels/import-efile",
            {"content": exported["content"], "conflict_mode": "auto_remap"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(imported["rtu_id"], 1)

        # 3. 使用 scdfe_c 真实通道引擎发现并构建 IEC104 主站连接 scdfe_s
        specs = scdfe_c.discover_channels(self.c_httpd.store)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].channel_type, "iec104")
        master = scdfe_c.build_master(self.c_httpd.store, specs[0])
        self.addCleanup(master.close)

        master.connect(timeout=5)
        deadline = time.monotonic() + 3.0
        while service.status()["active_connections"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(service.status()["active_connections"], 1)

        # 总召唤并读取 YC / YX 实时值
        self.assertTrue(master.general_interrogation())
        deadline = time.monotonic() + 3.0
        while (
            master.value("yc", 1).value != 225.5
            or master.value("yx", 1).value is not True
        ) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(master.value("yc", 1).value, 225.5)
        self.assertIs(master.value("yx", 1).value, True)

        # scdfe_s 侧更新遥测值，验证 scdfe_c 实时收到突发上送
        service.incremental_update("yc", 1, 231.25)
        deadline = time.monotonic() + 3.0
        while master.value("yc", 1).value != 231.25 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(master.value("yc", 1).value, 231.25)

        # scdfe_c 下发遥控指令，验证 scdfe_s 真实收到
        master.send_yk_point(1, True)
        deadline = time.monotonic() + 2.0
        while not service.commands() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(service.commands())
        self.assertEqual(service.commands()[0]["kind"], "yk")


if __name__ == "__main__":
    unittest.main()

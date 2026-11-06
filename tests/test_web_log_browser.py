"""Durable file trees and packet searches shared by the s/t Web pages."""
import http.client
import json
import socket
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from scdfe_c.web.log_query import search_logs
from scdfe_s.web.server import make_server as s_server
from scdfe_t.web.server import make_server as t_server


class WebLogBrowserTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.logs = self.root / "logs"
        self.names = {}
        for module, process in (("s", "scdfe_s-iec104"), ("t", "scdfe_t-channel-1")):
            name = f"2026/11/04/23/{process}-20261104-23.log"
            self.names[module] = name
            path = self.logs / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("23:01:00 [日志] 报文 RX IEC104 HEX=68 04\n"
                            "23:02:00 [日志] 报文 TX IEC104 HEX=68 05\n"
                            "23:03:00 [错误] 检查报文 RX 失败\n", encoding="utf-8")
        self.servers = {}
        for module, factory in (("s", s_server), ("t", t_server)):
            server = factory("127.0.0.1", 0, db_path=self.root / ("scdfe_s.sqlite3" if module == "s" else "scada.sqlite3"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda server=server, thread=thread:
                            (server.shutdown(), server.server_close(), thread.join(3)))
            self.servers[module] = server

    def request(self, module, path, query=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.servers[module].server_port)
        connection.request("GET", path + ("?" + urlencode(query) if query else ""))
        response = connection.getresponse()
        value = json.loads(response.read())
        connection.close()
        return response.status, value

    def test_both_file_apis_expose_hour_and_only_own_module(self):
        for module in ("s", "t"):
            status, result = self.request(module, "/api/log-files")
            self.assertEqual(status, 200)
            old = next(row for row in result["items"] if row["name"] == self.names[module])
            self.assertEqual(old["hour"], "20261104-23")
            self.assertTrue(all(f"scdfe_{module}-" in row["name"] for row in result["items"]))

    def test_monitor_and_protocol_routes_write_only_one_http_response(self):
        cases = [
            ("s", "GET", "/api/monitor/processes", None, 200),
            ("s", "GET", "/api/monitor/logs?level=PACKET", None, 200),
            ("s", "GET", "/api/iec104/status", None, 200),
            ("s", "GET", "/api/iec104/logs", None, 200),
            ("s", "GET", "/api/modbus-tcp/status", None, 200),
            ("s", "GET", "/api/modbus-tcp-server/logs", None, 200),
            ("s", "PATCH", "/api/modbus-tcp/config", {"response_timeout": 2}, 200),
            ("s", "HEAD", "/api/monitor/logs", None, 200),
            ("s", "GET", "/api/monitor/logs?service=unknown", None, 400),
            ("s", "GET", "/api/health", None, 200),
            ("t", "GET", "/api/trans/processes", None, 200),
            ("t", "GET", "/api/log-files/search?level=PACKET", None, 200),
        ]
        for module, method, path, data, status in cases:
            with self.subTest(module=module, method=method, path=path):
                server = self.servers[module]
                body = json.dumps(data).encode() if data is not None else b""
                request = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                           f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n"
                           "Connection: close\r\n\r\n").encode() + body
                with socket.create_connection(server.server_address, timeout=5) as connection:
                    connection.sendall(request)
                    chunks = []
                    while chunk := connection.recv(65536):
                        chunks.append(chunk)
                header, content = b"".join(chunks).split(b"\r\n\r\n", 1)
                status_line, *fields = header.split(b"\r\n")
                self.assertEqual(int(status_line.split()[1]), status)
                headers = dict(field.split(b": ", 1) for field in fields)
                self.assertEqual(len(content), 0 if method == "HEAD" else int(headers[b"Content-Length"]))
                if method != "HEAD":
                    self.assertIsInstance(json.loads(content), dict)

    def test_rx_filter_checks_record_header_and_combines_keyword(self):
        for module in ("s", "t"):
            status, result = self.request(module, "/api/log-files/search", {
                "filename": self.names[module], "record": "packet", "direction": "RX", "q": "68 04"})
            self.assertEqual(status, 200)
            self.assertEqual(len(result["items"]), 1)
            self.assertIn("23:01:00", result["items"][0]["text"])
            _, runtime = self.request(module, "/api/log-files/search", {
                "filename": self.names[module], "record": "runtime"})
            self.assertEqual(len(runtime["items"]), 1)
            self.assertIn("检查报文", runtime["items"][0]["text"])

    def test_file_boundary_and_query_validation(self):
        for module, other in (("s", "t"), ("t", "s")):
            for query in ({"filename": self.names[other]}, {"filename": "../secret.log"},
                          {"direction": "WRONG"}, {"record": "WRONG"}):
                self.assertEqual(self.request(module, "/api/log-files/search", query)[0], 400)

    def test_packet_type_filters_new_headers_and_preserves_old_packet_search(self):
        for module in ("s", "t"):
            with (self.logs / self.names[module]).open("a", encoding="utf-8") as stream:
                stream.write("23:04:00 [报文] 报文 RX IEC104 HEX=68 06\n"
                             "23:05:00 [报文] 报文 TX IEC104 HEX=68 07\n")
            query = {"filename": self.names[module], "level": "PACKET"}
            status, result = self.request(module, "/api/log-files/search", query)
            self.assertEqual(status, 200)
            self.assertEqual(len(result["items"]), 2)
            self.assertTrue(all(row["level"] == "PACKET" and "[报文]" in row["text"]
                                and isinstance(row["time"], int) for row in result["items"]))
            _, rx = self.request(module, "/api/log-files/search", {**query, "direction": "RX", "q": "68 06"})
            self.assertEqual(len(rx["items"]), 1)
            _, packets = self.request(module, "/api/log-files/search", {
                "filename": self.names[module], "record": "packet"})
            self.assertEqual(len(packets["items"]), 4)
            _, runtime = self.request(module, "/api/log-files/search", {
                "filename": self.names[module], "record": "runtime"})
            self.assertEqual(len(runtime["items"]), 1)
            self.assertIn("检查报文", runtime["items"][0]["text"])

    def test_packet_pagination_reaches_records_before_old_tail(self):
        name = self.names["t"]
        with (self.logs / name).open("a", encoding="utf-8") as stream:
            stream.write("23:04:00 [日志] ordinary status\n" * 7000)
        first = search_logs(self.logs, {"filename": name, "record": "packet", "limit": "1"}, family="t")
        self.assertIn("报文 TX", first["items"][0]["text"])
        second = search_logs(self.logs, {"filename": name, "record": "packet", "limit": "1",
                                         "cursor": first["next_cursor"]}, family="t")
        self.assertIn("报文 RX", second["items"][0]["text"])


if __name__ == "__main__":
    unittest.main()

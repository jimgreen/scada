"""The three Web services keep browser polling on one HTTP connection."""
import http.client
from pathlib import Path
import tempfile
import threading
import unittest

from scdfe_c.web.server import make_server as make_c_server
from scdfe_s.web.server import make_server as make_s_server
from scdfe_t.web.server import make_server as make_t_server


class WebKeepAliveTests(unittest.TestCase):
    def test_health_requests_reuse_http11_connection_for_all_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            servers = [
                make_c_server("127.0.0.1", 0, db_path=root / "c.sqlite3"),
                make_s_server("127.0.0.1", 0, db_path=root / "s.sqlite3"),
                make_t_server("127.0.0.1", 0, db_path=root / "c.sqlite3"),
            ]
            threads = []
            try:
                for server in servers:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    threads.append(thread)
                for server in servers:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_port, timeout=5
                    )
                    try:
                        connection.request("GET", "/api/health")
                        first = connection.getresponse()
                        self.assertEqual((first.version, first.status), (11, 200))
                        first.read()
                        socket_identity = id(connection.sock)

                        connection.request("GET", "/api/health")
                        second = connection.getresponse()
                        self.assertEqual((second.version, second.status), (11, 200))
                        second.read()
                        self.assertEqual(id(connection.sock), socket_identity)
                    finally:
                        connection.close()
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(3)


if __name__ == "__main__":
    unittest.main()

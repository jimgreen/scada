import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from scdfe_c.command_server import (
    COMMAND_TABLE,
    CommandClient,
    CommandServer,
    endpoint_path,
    run_process,
)
from scdfe_c.web.service import ScadaStore
from core.scdfe_control import CONTROL_TABLE, ensure_control_row


class FakeWriter:
    def __init__(self):
        self.calls = []

    def submit_command(self, rtu_id, kind, point_no, value, **kwargs):
        self.calls.append((rtu_id, kind, point_no, value, kwargs))
        return {"rtu_id": rtu_id, "kind": kind, "pnt_no": point_no, "value": value}


class SlowWriter(FakeWriter):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()

    def submit_command(self, *args, **kwargs):
        self.started.set()
        time.sleep(0.15)
        return super().submit_command(*args, **kwargs)


def request(address, token, payload):
    with socket.create_connection(address, timeout=3) as sock:
        data = dict(payload)
        data["token"] = token
        sock.sendall(json.dumps(data, ensure_ascii=False).encode() + b"\n")
        raw = sock.makefile("rb").readline()
    return json.loads(raw.decode())


class CommandServerTests(unittest.TestCase):
    def test_managed_process_registers_heartbeats_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "scada.sqlite3"
            store = ScadaStore(db)
            store.create_row(COMMAND_TABLE, {
                "id": 1, "bind_host": "127.0.0.1", "bind_port": 0,
                "auth_token": "managed-token", "process_enable": 1,
                "process_status": 0, "process_pid": 0,
                "process_start_time": 0, "process_stop_time": 0,
                "process_heartbeat_time": 0, "process_exit_code": 0,
                "process_error": "", "log_retention_hours": 720,
            })
            stop_event = threading.Event()
            result = []

            def target():
                result.append(run_process(db, store.catalog.directory, port=0,
                                          stop_event=stop_event))

            thread = threading.Thread(target=target)
            thread.start()
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    row = store.get_row(COMMAND_TABLE, [1])
                    if row["process_status"] == 2 and row["process_pid"]:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("managed command server did not enter RUNNING state")
                row = store.get_row(COMMAND_TABLE, [1])
                self.assertTrue(endpoint_path(db).is_file())
                self.assertGreater(row["process_heartbeat_time"], 0)
            finally:
                # A readiness assertion must not delete the database while
                # the managed thread is still acquiring its lease.
                stop_event.set()
                thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [0])
            stopped = store.get_row(COMMAND_TABLE, [1])
            self.assertEqual(stopped["process_status"], 0)
            self.assertEqual(stopped["process_pid"], 0)
            self.assertFalse(endpoint_path(db).exists())
            log_files = list((db.parent / "logs").rglob("scdfe_c-command-server-*.log"))
            self.assertTrue(log_files)
            content = "\n".join(path.read_text(encoding="utf-8") for path in log_files)
            self.assertIn("TCP 指令服务进程启动", content)
            self.assertIn("TCP 指令服务启动，监听", content)
            self.assertIn("TCP 指令服务停止", content)
            self.assertIn("TCP 指令服务进程停止", content)

    def test_managed_process_uses_shared_web_log_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "scada.sqlite3"
            store = ScadaStore(db)
            control = ensure_control_row(store)
            custom_logs = root / "configured-logs"
            store.update_row(CONTROL_TABLE, [1], {
                "log_dir": str(custom_logs),
            })
            store.create_row(COMMAND_TABLE, {
                "id": 1, "bind_host": "127.0.0.1", "bind_port": 0,
                "auth_token": "managed-token", "process_enable": 1,
                "process_status": 0, "process_pid": 0,
                "process_start_time": 0, "process_stop_time": 0,
                "process_heartbeat_time": 0, "process_exit_code": 0,
                "process_error": "", "log_retention_hours": 720,
            })
            stop_event = threading.Event()
            thread = threading.Thread(target=lambda: run_process(
                db, store.catalog.directory, port=0, stop_event=stop_event,
            ))
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if store.get_row(COMMAND_TABLE, [1])["process_status"] == 2:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("managed command server did not start")
                stop_event.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertTrue(list(custom_logs.rglob("scdfe_c-command-server-*.log")))
            finally:
                stop_event.set()
                thread.join(5)

    def test_yk_and_yt_are_forwarded_after_writer_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = FakeWriter()
            server = CommandServer(Path(tmp) / "scada.sqlite3", port=0, token="test-token", writer=writer)
            server.start()
            self.addCleanup(server.close)
            address = server.address
            self.assertIsNotNone(address)
            yk = request(address, server.token, {"request_id": "r1", "rtu_id": 7, "kind": "yk", "point_no": 3, "value": True})
            yt = request(address, server.token, {"request_id": "r2", "rtu_id": 7, "kind": "yt", "point_no": 4, "value": 12.5})
            self.assertTrue(yk["ok"])
            self.assertTrue(yt["ok"])
            self.assertEqual([(x[0], x[1], x[2], x[3]) for x in writer.calls],
                             [(7, "yk", 3, 1), (7, "yt", 4, 12.5)])
            log_files = list((Path(tmp) / "logs").rglob("scdfe_c-command-server-*.log"))
            self.assertTrue(log_files)
            content = "\n".join(path.read_text(encoding="utf-8") for path in log_files)
            self.assertIn("TCP 指令已受理 request_id=r1", content)
            self.assertIn("TCP 指令已受理 request_id=r2", content)

    def test_request_id_is_idempotent_and_token_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = FakeWriter()
            server = CommandServer(Path(tmp) / "scada.sqlite3", port=0, token="test-token", writer=writer)
            server.start()
            self.addCleanup(server.close)
            address = server.address
            payload = {"request_id": "same", "rtu_id": 1, "kind": "yk", "point_no": 1, "value": 0}
            first = request(address, server.token, payload)
            second = request(address, server.token, payload)
            self.assertEqual(first, second)
            self.assertEqual(len(writer.calls), 1)
            conflict = request(address, server.token, {
                **payload, "value": 1,
            })
            self.assertFalse(conflict["ok"])
            self.assertEqual(conflict["code"], "request_id_conflict")
            self.assertEqual(conflict["status"], 409)
            self.assertEqual(len(writer.calls), 1)
            unauthorized = request(address, "wrong", payload)
            self.assertFalse(unauthorized["ok"])
            self.assertEqual(unauthorized["status"], 401)

    def test_invalid_command_is_rejected_without_writer_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = FakeWriter()
            server = CommandServer(Path(tmp) / "scada.sqlite3", port=0, token="test-token", writer=writer)
            server.start()
            self.addCleanup(server.close)
            address = server.address
            for payload in (
                {"request_id": "a", "rtu_id": 0, "kind": "yk", "point_no": 1, "value": 1},
                {"request_id": "a0", "rtu_id": 1, "kind": "yk", "point_no": 0, "value": 1},
                {"request_id": "b", "rtu_id": 1, "kind": "yc", "point_no": 1, "value": 1},
                {"request_id": "c", "rtu_id": 1, "kind": "yk", "point_no": 1, "value": 2},
                {"request_id": "d", "rtu_id": 1.5, "kind": "yk", "point_no": 1, "value": 1},
                {"request_id": "e", "rtu_id": 1, "kind": "yk", "point_no": True, "value": 1},
            ):
                response = request(address, server.token, payload)
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)
            self.assertEqual(writer.calls, [])

    def test_command_client_discovers_endpoint_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "scada.sqlite3"
            writer = FakeWriter()
            server = CommandServer(db, port=0, token="test-token", writer=writer)
            server.start()
            self.addCleanup(server.close)
            response = CommandClient(db).submit_command(2, "yt", 5, 3.25, request_id="client-1")
            self.assertTrue(response["ok"])
            self.assertEqual(writer.calls[0][:4], (2, "yt", 5, 3.25))

    def test_endpoint_metadata_is_written_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "scada.sqlite3"
            server = CommandServer(db, port=0, token="test-token", writer=FakeWriter())
            server.start()
            metadata = endpoint_path(db)
            self.assertTrue(metadata.is_file())
            self.assertEqual(json.loads(metadata.read_text())["port"], server.address[1])
            server.close()
            self.assertFalse(metadata.exists())

    def test_start_failure_is_written_to_runtime_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "scada.sqlite3"
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            try:
                server = CommandServer(db, port=listener.getsockname()[1], token="test-token",
                                       writer=FakeWriter())
                with self.assertRaises(OSError):
                    server.start()
            finally:
                listener.close()
            log_files = list((Path(tmp) / "logs").rglob("scdfe_c-command-server-*.log"))
            self.assertTrue(log_files)
            content = "\n".join(path.read_text(encoding="utf-8") for path in log_files)
            self.assertIn("TCP 指令服务启动失败", content)

    def test_concurrent_duplicate_request_id_is_submitted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = SlowWriter()
            server = CommandServer(Path(tmp) / "scada.sqlite3", port=0, token="test-token",
                                    writer=writer, request_timeout=2)
            server.start()
            self.addCleanup(server.close)
            address = server.address
            payload = {"request_id": "concurrent", "rtu_id": 1, "kind": "yk", "point_no": 1, "value": 1}
            results = []

            def send():
                results.append(request(address, server.token, payload))

            first = threading.Thread(target=send)
            first.start()
            self.assertTrue(writer.started.wait(1))
            second = threading.Thread(target=send)
            second.start()
            first.join(3)
            second.join(3)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            self.assertEqual(len(writer.calls), 1)

    def test_idle_client_is_closed_after_request_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = CommandServer(Path(tmp) / "scada.sqlite3", port=0, token="test-token",
                                    writer=FakeWriter(), request_timeout=0.1)
            server.start()
            self.addCleanup(server.close)
            with socket.create_connection(server.address, timeout=2) as sock:
                sock.settimeout(2)
                raw = sock.makefile("rb").readline()
            response = json.loads(raw.decode())
            self.assertFalse(response["ok"])
            self.assertEqual(response["code"], "request_timeout")


if __name__ == "__main__":
    unittest.main()

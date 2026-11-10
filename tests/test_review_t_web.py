"""Regression coverage for transmission Web editing, pagination and ownership."""
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from core.scdfe_control import InstanceLock
from scdfe_c.web.service import ApiError
from scdfe_t.web.server import make_server
from scdfe_t.web.service import CHANNEL_TABLE, TransStore


class TransWebReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # HTTP servers release their idle SQLite handles before directory cleanup.
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "review.sqlite3"
        self.service = TransStore(self.db)
        self.channel = self.service.create_channel({
            "channel_id": 1, "name": "REVIEW", "protocol": "modbus-tcp",
            "bind_host": "127.0.0.1", "bind_port": self.free_port(),
            "scan_interval_ms": 50,
        })

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def http_server(self):
        server = make_server("127.0.0.1", 0, db_path=self.db)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        return server

    @staticmethod
    def request(server, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=request_headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        connection.close()
        return response.status, value

    def test_point_edit_keeps_composite_identity_and_saves_other_fields(self):
        self.service.create_point(1, {"pnt_no": 1, "kind": "yc", "source_rtu_id": 7,
                                      "source_pnt_no": 2, "name": "Before"})
        server = self.http_server()
        status, value = self.request(server, "/api/trans/channels/1/points/yc/1", "PATCH",
                                     {"kind": "yc", "pnt_no": 1, "name": "After"})
        self.assertEqual(status, 200, value)
        self.assertEqual(value["item"]["name"], "After")
        status, _ = self.request(server, "/api/trans/channels/1/points/yc/1", "PATCH", {"kind": "yk"})
        self.assertEqual(status, 400)

    def test_point_pagination_and_kind_filter_include_rows_after_500(self):
        with self.service.store.connection() as connection:
            connection.executemany(
                "INSERT INTO fes_scdfe_t_point_define "
                "(channel_id,pnt_no,kind,source_rtu_id,source_pnt_no) VALUES(1,?,?,?,?)",
                [(number, "yk" if number == 501 else "yc", 7, number)
                 for number in range(1, 502)],
            )
            connection.commit()
        server = self.http_server()
        status, first = self.request(server, "/api/trans/channels/1/points?limit=500")
        self.assertEqual(status, 200)
        self.assertEqual(first["total"], 501)
        self.assertEqual(len(first["items"]), 500)
        _, second = self.request(server, "/api/trans/channels/1/points?limit=500&offset=500")
        self.assertEqual([point["pnt_no"] for point in second["items"]], [501])
        _, filtered = self.request(server, "/api/trans/channels/1/points?kind=yk&limit=20")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["pnt_no"], 501)
        status, _ = self.request(server, "/api/trans/channels/1/points?offset=-1")
        self.assertEqual(status, 400)

    def test_source_catalog_lists_existing_rtu_and_points(self):
        self.service.store.create_row("fes_rtu_define", {
            "rtu_id": 7, "channel_id": 70, "channel_type": "iec104", "name": "源站",
        })
        self.service.store.create_row("fes_yc_define", {
            "rtu_id": 7, "pnt_no": 3, "name": "母线电压",
        })
        server = self.http_server()
        status, catalog = self.request(server, "/api/trans/source-catalog?kind=yc&rtu_id=7")
        self.assertEqual(status, 200)
        self.assertEqual(catalog["rtus"][0]["rtu_id"], 7)
        self.assertEqual(catalog["items"][0]["pnt_no"], 3)
        self.assertEqual(catalog["items"][0]["name"], "母线电压")

    def test_runtime_status_projection_contains_diagnostic_snapshot(self):
        server = self.http_server()
        status, payload = self.request(server, "/api/trans/status")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["total"], 1)
        item = payload["items"][0]
        self.assertEqual(item["channel_id"], 1)
        self.assertIn("runtime_status", item)
        runtime = item["runtime_status"]
        self.assertEqual(runtime["service_name"], "scdfe_t")
        self.assertIn("diagnostic", runtime)
        self.assertIn("rx_count", runtime)

    def test_batch_point_mapping_allocates_forward_numbers(self):
        self.service.store.create_row("fes_rtu_define", {
            "rtu_id": 7, "channel_id": 70, "channel_type": "iec104", "name": "源站",
        })
        with self.service.store.connection() as con:
            con.executemany(
                "INSERT INTO fes_yc_define(rtu_id,pnt_no,name) VALUES(?,?,?)",
                [(7, 3, "YC3"), (7, 4, "YC4")],
            )
            con.commit()
        server = self.http_server()
        status, result = self.request(server, "/api/trans/channels/1/points/batch", "POST", {
            "items": [
                {"kind": "yc", "source_rtu_id": 7, "source_pnt_no": 3},
                {"kind": "yc", "source_rtu_id": 7, "source_pnt_no": 4},
            ],
        })
        self.assertEqual(status, 201, result)
        self.assertEqual([item["pnt_no"] for item in result["items"]], [1, 2])
        self.assertEqual(self.service.point_page(1)["total"], 2)

    def test_current_etag_deletes_channel_and_mappings(self):
        self.service.create_point(1, {"pnt_no": 1, "kind": "yc", "source_rtu_id": 7, "source_pnt_no": 2})
        row = self.service.get_channel(1)
        self.service.delete_channel(1, row["_version"])
        with self.assertRaises(ApiError) as caught:
            self.service.get_channel(1)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.service.store.list_rows("fes_scdfe_t_point_define")["total"], 0)

    def test_stale_etag_never_stops_or_modifies_channel(self):
        row = self.service.store.update_row(CHANNEL_TABLE, [1], {"process_enable": 1})
        with patch.object(self.service, "stop", wraps=self.service.stop) as stop:
            with self.assertRaises(ApiError) as caught:
                self.service.delete_channel(1, row["_version"] - 1)
            self.assertEqual(caught.exception.code, "version_conflict")
            stop.assert_not_called()
        self.assertEqual(self.service.get_channel(1), row)

    def test_unrelated_live_pid_is_neither_killed_nor_reported_as_channel(self):
        self.service.store.update_row(CHANNEL_TABLE, [1], {"process_pid": os.getpid(), "process_status": 2})
        with patch("os.kill") as kill:
            self.assertFalse(self.service.processes()[0]["alive"])
            stopped = self.service.stop(1)
        kill.assert_not_called()
        self.assertEqual(stopped["process_status"], 0)
        self.assertEqual(stopped["process_pid"], 0)

    def test_running_channel_delete_waits_for_cooperative_exit(self):
        lock = InstanceLock(self.db, "trans-1")
        self.assertTrue(lock.acquire())
        row = self.service.store.update_row(CHANNEL_TABLE, [1], {
            "process_enable": 1, "process_status": 2, "process_pid": os.getpid(),
        })
        errors = []
        def runtime():
            try:
                until = time.monotonic() + 5
                while self.service.get_channel(1)["process_enable"] and time.monotonic() < until:
                    time.sleep(.01)
            except Exception as exc:
                errors.append(exc)
            finally:
                lock.release()
        thread = threading.Thread(target=runtime)
        thread.start()
        try:
            self.service.delete_channel(1, row["_version"])
        finally:
            thread.join(6)
            lock.release()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_real_web_start_stop_and_restart_preserve_listener_ownership(self):
        self.service.create_point(1, {"pnt_no": 1, "kind": "yc", "source_rtu_id": 7,
                                      "source_pnt_no": 2, "register_area": "input"})
        # A reused unrelated PID must not prevent starting a real channel.
        self.service.store.update_row(CHANNEL_TABLE, [1], {"process_pid": os.getpid(), "process_status": 2})
        try:
            started = self.service.start(1)
            self.assertEqual(started["process_status"], 2)
            self.assertTrue(InstanceLock.held(self.db, "trans-1"))
            self.assertTrue(self.service.processes()[0]["alive"])
            # A new Web instance has no Popen handle. It must stop by channel
            # control state and lease, never by signaling an unverified PID.
            another = TransStore(self.db)
            with patch("os.kill") as kill:
                stopped = another.stop(1)
            kill.assert_not_called()
            self.assertEqual(stopped["process_status"], 0)
            self.assertFalse(InstanceLock.held(self.db, "trans-1"))
            self.assertEqual(self.service.start(1)["process_status"], 2)
        finally:
            self.service.stop(1)
        self.assertFalse(InstanceLock.held(self.db, "trans-1"))

    def test_stop_during_child_start_does_not_reenable_or_leave_child(self):
        outcomes = []
        def start():
            try:
                outcomes.append(self.service.start(1))
            except ApiError as exc:
                outcomes.append(exc.code)
        thread = threading.Thread(target=start)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not self.service._children and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(self.service._children)
            stopped = self.service.stop(1)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(stopped["process_enable"], 0)
            self.assertEqual(stopped["process_status"], 0)
            self.assertFalse(InstanceLock.held(self.db, "trans-1"))
            self.assertEqual(self.service._children, {})
            self.assertTrue(outcomes)
        finally:
            thread.join(5)
            self.service.stop(1)

    def test_stop_timeout_does_not_report_success_or_signal_unowned_pid(self):
        lock = InstanceLock(self.db, "trans-1")
        self.assertTrue(lock.acquire())
        self.service.store.update_row(CHANNEL_TABLE, [1], {
            "process_enable": 1, "process_status": 2, "process_pid": os.getpid(),
        })
        try:
            with patch("scdfe_t.web.service.STOP_TIMEOUT", .02), patch("os.kill") as kill:
                with self.assertRaises(ApiError) as caught:
                    self.service.stop(1)
                self.assertEqual(caught.exception.code, "trans_stop_timeout")
                kill.assert_not_called()
            row = self.service.get_channel(1)
            self.assertEqual(row["process_enable"], 0)
            self.assertEqual(row["process_status"], 3)
            self.assertNotEqual(row["process_pid"], 0)
        finally:
            lock.release()
            self.service.stop(1)

    def test_old_failed_start_waiter_does_not_disable_new_launch(self):
        previous = Mock(pid=111, returncode=2)
        previous.poll.return_value = 2
        self.service._children[1] = previous
        current = self.service.store.update_row(CHANNEL_TABLE, [1], {
            "process_enable": 1, "process_status": 1, "process_pid": 222,
            "process_start_time": 200,
        })
        with patch("scdfe_t.web.service.START_TIMEOUT", .01):
            with self.assertRaises(ApiError) as caught:
                self.service._wait_started(1, launch_time=100)
        self.assertEqual(caught.exception.code, "trans_start_timeout")
        self.assertEqual(self.service.get_channel(1), current)
        self.service._children.clear()

    def test_duplicate_start_waits_for_listener_ready(self):
        lock = InstanceLock(self.db, "trans-1")
        self.assertTrue(lock.acquire())
        self.service.store.update_row(CHANNEL_TABLE, [1], {
            "process_enable": 1, "process_status": 1, "process_pid": os.getpid(),
        })
        ready = threading.Event()
        def publish_ready():
            time.sleep(.1)
            self.service.store.update_row(CHANNEL_TABLE, [1], {"process_status": 2})
            ready.set()
        thread = threading.Thread(target=publish_ready)
        thread.start()
        try:
            started = self.service.start(1)
            self.assertTrue(ready.is_set())
            self.assertEqual(started["process_status"], 2)
        finally:
            thread.join(3)
            lock.release()


if __name__ == "__main__":
    unittest.main()

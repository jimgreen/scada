"""Regression coverage for the reviewed c Web process and PATCH flows."""
import http.client
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from scdfe_c.web.server import make_server
from scdfe_c.web.process_control import COMMAND_TABLE
from scdfe_c.web.service import ApiError
from core.scdfe_control import CONTROL_TABLE, ensure_control_row


class ReviewedCWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server("127.0.0.1", 0, db_path=Path(self.temp.name) / "scada.sqlite3")
        self.store = self.server.store
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.store.create_row("fes_rtu_define", {
            "rtu_id": 101, "channel_id": 101, "channel_type": "modbus-tcp",
        })

    def tearDown(self):
        controller = self.server.process_controller
        if controller._thread is not None:
            controller._thread.join(3)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def request(self, method, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, json.dumps(payload), {"Content-Type": "application/json"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def synchronise_missing_row(self, table):
        """Make both initial readers observe an absent singleton."""
        barrier = threading.Barrier(2)
        original = self.store.get_row

        def read(name, key):
            try:
                return original(name, key)
            except ApiError as exc:
                if name == table and exc.code == "not_found":
                    barrier.wait(3)
                raise

        return patch.object(self.store, "get_row", side_effect=read)

    def test_concurrent_first_process_page_requests_share_one_command_config(self):
        ensure_control_row(self.store)
        with self.synchronise_missing_row(COMMAND_TABLE), ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(
                lambda path: self.request("GET", path, None),
                ("/api/processes", "/api/scdfe_c/command-server"),
            ))
        self.assertEqual([status for status, _ in responses], [200, 200], responses)
        token = responses[1][1]["item"]["auth_token"]
        self.assertTrue(token)
        self.assertEqual(token, self.store.get_row(COMMAND_TABLE, [1])["auth_token"])
        self.assertEqual(self.store.list_rows(COMMAND_TABLE)["total"], 1)

    def test_concurrent_blank_command_tokens_are_initialised_once(self):
        controller = self.server.process_controller
        self.store.create_row(COMMAND_TABLE, {"id": 1, "auth_token": ""})
        barrier = threading.Barrier(2)
        original = self.store.get_row

        def read(table, key):
            row = original(table, key)
            if table == COMMAND_TABLE and not row["auth_token"]:
                barrier.wait(3)
            return row

        with patch.object(self.store, "get_row", side_effect=read), ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: controller.command_config(), range(2)))
        self.assertTrue(rows[0]["auth_token"])
        self.assertEqual(rows[0]["auth_token"], rows[1]["auth_token"])
        self.assertEqual(rows[0]["auth_token"], self.store.get_row(COMMAND_TABLE, [1])["auth_token"])

    def test_concurrent_control_initialisation_returns_the_same_singleton(self):
        with self.synchronise_missing_row(CONTROL_TABLE), ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: ensure_control_row(self.store), range(2)))
        self.assertEqual(rows[0], rows[1])
        self.assertEqual(self.store.list_rows(CONTROL_TABLE)["total"], 1)

    def test_command_service_start_does_not_claim_the_running_main_lock(self):
        controller = self.server.process_controller
        # The existing main lock is expected during normal acquisition. Mock
        # only process launch, so the real HTTP/operation/lock flow still runs.
        with patch("scdfe_c.web.process_control.ServerInstanceLock.held", return_value=True) as held, \
                patch.object(controller, "_start") as start:
            status, body = self.request("POST", "/api/processes/control", {
                "target": "command-server", "action": "start",
            })
            self.assertEqual(status, 202, body)
            controller._thread.join(3)
            self.assertFalse(controller._thread.is_alive())
            self.assertEqual(controller._operation["status"], "completed")
            start.assert_called_once_with("command-server")
            held.assert_not_called()

        # Communication instances must retain their original exclusion.
        with patch("scdfe_c.web.process_control.ServerInstanceLock.held", return_value=True):
            status, body = self.request("POST", "/api/processes/control", {
                "target": "main", "action": "start",
            })
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "process_already_running")

    def test_bitfield_edit_validates_against_unchanged_stored_fields(self):
        table = "fes_channel_modbus_define_data"
        self.store.create_row(table, {
            "id": 1, "rtu_id": 101, "call_seq": 0, "cnt": 1,
            "trans_type": 8, "reg_addr": 0, "offset_bit": 0, "all_bit": 2,
        })
        status, body = self.request("PATCH", f"/api/tables/{table}/rows/1", {"trans_type": 9})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["item"]["trans_type"], body["item"]["offset_bit"],
                          body["item"]["all_bit"]), (9, 0, 2))
        version = body["item"]["_version"]
        status, body = self.request("PATCH", f"/api/tables/{table}/rows/1", {"all_bit": None})
        self.assertEqual(status, 400, body)
        unchanged = self.store.get_row(table, [1])
        self.assertEqual(unchanged["all_bit"], 2)
        self.assertEqual(unchanged["_version"], version)

    def test_partial_poll_function_edit_cannot_bypass_register_count_limit(self):
        table = "fes_channel_modbus_define_call"
        self.store.create_row(table, {
            "id": 1, "rtu_id": 101, "call_seq": 0,
            "irtuaddr": 1, "iplace": 0, "inum": 200, "icode": 2,
        })
        status, body = self.request("PATCH", f"/api/tables/{table}/rows/1", {"icode": 3})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.store.get_row(table, [1])["icode"], 2)
        status, body = self.request("PATCH", f"/api/tables/{table}/rows/1", {"icode": 3, "inum": 100})
        self.assertEqual(status, 200, body)

    def test_partial_trans_mapping_edit_keeps_source_and_exposed_kinds_equal(self):
        table = "fes_scdfe_t_point_define"
        self.store.create_row("fes_scdfe_t_define", {
            "channel_id": 101, "code": "T101", "name": "Review", "protocol": "modbus-tcp",
        })
        self.store.create_row(table, {
            "channel_id": 101, "pnt_no": 1, "kind": "yc",
            "source_rtu_id": 101, "source_pnt_no": 1,
        })
        status, body = self.request("PATCH", f"/api/tables/{table}/rows/101,1,yc", {"source_kind": "yk"})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.store.get_row(table, [101, 1, "yc"])["kind"], "yc")


if __name__ == "__main__":
    unittest.main()

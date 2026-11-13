"""Atomic S monitor publication, fresh HTTP diagnostics and lifecycle order."""
from pathlib import Path
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from scdfe_s.web.server import make_server, ScadaHandler, _rtu_point_counts
from core.runtime_status import runtime_status_rows
from scdfe_c.web.service import ApiError


class MonitorBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.httpd = make_server('127.0.0.1', 0, db_path=Path(temporary.name)/'s.sqlite3')
        self.addCleanup(self.httpd.server_close)
        self.store = self.httpd.store
        for rid, protocol in [(1, 'iec104'), (2, 'modbus-tcp'), (3, 'can')]:
            self.store.create_row('fes_rtu_define', dict(rtu_id=rid, channel_id=rid+100, channel_type=protocol, name=f'RTU{rid}'))
        self.services = self.httpd.channel_services()
        self.handler = object.__new__(ScadaHandler)
        self.handler.server = self.httpd

    def test_batch_has_one_commit_and_current_diagnostics(self):
        sql = []
        with self.store.connection() as con: con.set_trace_callback(sql.append)
        self.handler._monitor_statuses(self.services, _rtu_point_counts(self.store))
        self.assertEqual(sql.count('BEGIN IMMEDIATE'), 1)
        self.assertEqual(sql.count('COMMIT'), 1)
        self.assertEqual(len(runtime_status_rows(self.store, service_name='scdfe_s')), 3)
        # Legacy callers continue to publish without the monitor collector.
        self.services['can-rtu-3']._rx_count = 7
        self.services['can-rtu-3'].status()
        self.assertEqual(runtime_status_rows(self.store, service_name='scdfe_s', channel_id=103)[0]['rx_count'], 7)

    def test_failed_last_write_rolls_back_every_snapshot_and_allows_retry(self):
        with self.store.connection() as con:
            con.execute("CREATE TRIGGER reject_snapshot BEFORE INSERT ON fes_channel_runtime_status WHEN NEW.channel_id=103 BEGIN SELECT RAISE(ABORT,'fixture');END")
        with self.assertRaisesRegex(ApiError, '保存通道诊断快照失败') as failure:
            self.handler._monitor_statuses(self.services, {})
        self.assertEqual(failure.exception.status, 503)
        self.assertEqual(failure.exception.code, 'runtime_snapshot_failed')
        self.assertEqual(runtime_status_rows(self.store, service_name='scdfe_s'), [])
        self.assertTrue(all(not service._runtime_status._last_write for service in self.services.values()))
        with self.store.connection() as con: con.execute('DROP TRIGGER reject_snapshot')
        self.handler._monitor_statuses(self.services, {})
        self.assertEqual(len(runtime_status_rows(self.store, service_name='scdfe_s')), 3)

    def test_stop_cannot_be_overwritten_by_an_older_captured_snapshot(self):
        captured, release, stopped, attempted = (threading.Event() for _ in range(4))
        first = self.services['iec104-rtu-1']
        last = self.services['can-rtu-3']
        original = last.status
        errors = []
        def pause(**kwargs):
            value = original(**kwargs); captured.set()
            if not release.wait(3): raise TimeoutError('fixture did not release collection')
            return value
        def monitor():
            try: self.handler._monitor_statuses(self.services, {})
            except Exception as error: errors.append(error)
        def control():
            attempted.set()
            first.stop(); stopped.set()
        with patch.object(last, 'status', side_effect=pause):
            reader = threading.Thread(target=monitor); reader.start()
            controller = threading.Thread(target=control)
            try:
                self.assertTrue(captured.wait(2)); controller.start(); self.assertTrue(attempted.wait(2))
                self.assertFalse(stopped.wait(.05))
            finally:
                release.set(); reader.join(3)
                if controller.ident: controller.join(3)
        self.assertFalse(errors)
        self.assertTrue(stopped.is_set())
        row = runtime_status_rows(self.store, service_name='scdfe_s', channel_id=101)[0]
        self.assertEqual(row['process_stop_time'], first._stopped_at)
        self.assertGreater(row['process_stop_time'], 0)

    def test_http_uses_this_refresh_snapshot_and_rejects_query_writes(self):
        thread = threading.Thread(target=self.httpd.serve_forever); thread.start()
        self.addCleanup(thread.join, 3); self.addCleanup(self.httpd.shutdown)
        def request(method, path):
            con = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=5)
            try:
                con.request(method, path); response = con.getresponse()
                return response.status, json.loads(response.read())
            finally: con.close()
        service = self.services['iec104-rtu-1']
        for count in (19, 27):
            service._rx_count = count
            code, body = request('GET', '/api/monitor/communications?rtu_id=1')
            self.assertEqual(code, 200)
            item = body['items'][0]
            self.assertEqual(item['rx_count'], count)
            self.assertEqual(item['runtime_status']['rx_count'], count)
            self.assertEqual(item['runtime_status']['channel_id'], 101)
        for endpoint in ('processes', 'communications'):
            self.assertEqual(request('POST', '/api/monitor/'+endpoint)[0], 405)
        # A failed publication is visible to the caller, not a 200 with stale
        # diagnostics. An unrelated scoped request remains available.
        with self.store.connection() as con:
            con.execute("CREATE TRIGGER reject_snapshot BEFORE INSERT ON fes_channel_runtime_status WHEN NEW.channel_id=103 BEGIN SELECT RAISE(ABORT,'fixture');END")
        code, body = request('GET', '/api/monitor/communications')
        self.assertEqual(code, 503, body)
        self.assertEqual(request('GET', '/api/monitor/communications?rtu_id=1')[0], 200)

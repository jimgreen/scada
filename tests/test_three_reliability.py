"""Regression coverage for the shared HTTP boundary and packet policy."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from core.packet_logging import PacketLogSwitch
from scdfe_c.web.server import make_server as c_server
from scdfe_s.web.server import make_server as s_server
from scdfe_t.web.server import make_server as t_server


class RequestBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'scada.sqlite3'
        self.servers = {}
        for family, factory in (('c', c_server), ('s', s_server), ('t', t_server)):
            web = factory('127.0.0.1', 0, db_path=self.db)
            thread = threading.Thread(target=web.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda w=web,t=thread: (w.shutdown(),w.server_close(),t.join(3)))
            self.servers[family] = web

    def request(self, web, method, path, fields, body, *, separate=False, half_close=False):
        with socket.create_connection(web.server_address, timeout=3) as sock:
            prefix = (f'{method} {path} HTTP/1.1\r\nHost: localhost:{web.server_port}\r\n'
                      'Content-Type: application/json\r\nConnection: close\r\n')
            sock.sendall((prefix + fields + '\r\n').encode())
            if separate:
                sock.settimeout(.15)
                with self.assertRaises(socket.timeout, msg='server responded before consuming the body'):
                    sock.recv(1)
                sock.settimeout(3)
            if body:
                sock.sendall(body)
            if half_close:
                sock.shutdown(socket.SHUT_WR)
            with closing(http.client.HTTPResponse(sock, method=method)) as response:
                response.begin()
                data = response.read()
                return response.status, data

    def test_delayed_body_of_rejected_write_returns_complete_json_for_every_service(self):
        for family, web in self.servers.items():
            with self.subTest(family=family):
                status, data = self.request(web, 'POST', '/api/packet-recording', 'Content-Length: 2\r\n', b'{}', separate=True)
                self.assertEqual(status,405)
                self.assertIn('error',json.loads(data))

    def test_cross_origin_or_ambiguous_frame_cannot_change_packet_policy(self):
        payload=b'{"enabled":true}'
        for family, web in self.servers.items():
            for suffix, expected in [('Origin: http://unrelated.invalid\r\n',403),
                                     ('Sec-Fetch-Site: cross-site\r\n',403),
                                     (f'Content-Length: {len(payload)}\r\n',400)]:
                with self.subTest(family=family,header=suffix):
                    status, _ = self.request(web,'PUT','/api/packet-recording',
                                             f'Content-Length: {len(payload)}\r\n'+suffix,payload)
                    self.assertEqual(status,expected)
                    self.assertFalse(PacketLogSwitch(self.db,family).enabled())
            with self.subTest(family=family,case='short'):
                status,_=self.request(web,'PUT','/api/packet-recording',f'Content-Length: {len(payload)+10}\r\n',payload,half_close=True)
                self.assertEqual(status,400)
                self.assertFalse(PacketLogSwitch(self.db,family).enabled())

    def test_health_head_matches_get_without_response_body(self):
        for family, web in self.servers.items():
            with self.subTest(family=family):
                self.assertEqual(self.request(web,'HEAD','/api/health','',b''),(200,b''))


class PacketPolicyRecoveryTests(unittest.TestCase):
    def test_corrupt_settings_fail_closed_and_an_edit_cannot_erase_other_channels(self):
        with tempfile.TemporaryDirectory() as temp:
            switch=PacketLogSwitch(Path(temp)/'db','s')
            switch.set_channel(9,True)
            for contents in ('{"channels":', '{"enabled":false,"channels":{"9":true,"8":"broken"}}'):
                switch.path.write_text(contents,encoding='utf-8')
                with self.subTest(contents=contents):
                    self.assertFalse(switch.enabled(9,refresh=True))
                    with self.assertRaises((ValueError,OSError)):
                        switch.set_channel(10,True)
                    self.assertEqual(switch.path.read_text(encoding='utf-8'), contents)

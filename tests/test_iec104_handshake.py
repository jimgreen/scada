"""Use a raw TCP peer so that STARTDT confirmation timing is deterministic."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import socket
import unittest

import c104
from client.iec104 import IEC104Master


class IEC104HandshakeTests(unittest.TestCase):
    def setUp(self):
        self.listener = socket.socket()
        self.addCleanup(self.listener.close)
        self.listener.bind(('127.0.0.1',0))
        self.listener.listen()
        self.listener.settimeout(4)
        self.port = self.listener.getsockname()[1]

    def test_socket_is_not_ready_for_first_gi_before_startdt_confirmation(self):
        master = IEC104Master('127.0.0.1', self.port, init='NONE')
        self.addCleanup(master.close)
        with ThreadPoolExecutor(1) as pool:
            ready = pool.submit(master.connect, 3)
            peer, _ = self.listener.accept()
            with peer:
                peer.settimeout(2)
                request = bytearray()
                while len(request) < 6:
                    part = peer.recv(6-len(request))
                    self.assertTrue(part)
                    request.extend(part)
                self.assertEqual(request, bytes.fromhex('68 04 07 00 00 00'))
                with self.assertRaises(TimeoutError):
                    ready.result(.12)
                peer.sendall(bytes.fromhex('68 04 0b 00 00 00'))
                self.assertIs(ready.result(2), master)
                self.assertEqual(master.connection.state, c104.ConnectionState.OPEN)

    def test_deliberately_muted_connection_remains_quiet(self):
        master = IEC104Master('127.0.0.1', self.port, init='MUTED')
        self.addCleanup(master.close)
        with ThreadPoolExecutor(1) as pool:
            ready = pool.submit(master.connect, 2)
            peer, _ = self.listener.accept()
            with peer:
                self.assertIs(ready.result(2), master)
                self.assertEqual(master.connection.state, c104.ConnectionState.OPEN_MUTED)
                peer.settimeout(.35)
                with self.assertRaises(socket.timeout):
                    peer.recv(1)

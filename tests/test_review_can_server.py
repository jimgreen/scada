"""CAN server failure state and resource ownership regressions."""
import queue
import threading
import time
import unittest
import uuid
from unittest.mock import patch

import can

from server.can_server import CanServer, CanServerError


class FakeBus:
    def __init__(self, *, send_error=None, recv_error=None):
        self.send_error = send_error
        self.recv_error = recv_error
        self.messages = queue.Queue()
        self.closed = threading.Event()
        self.shutdown_count = 0

    def recv(self, timeout):
        if self.recv_error:
            raise self.recv_error
        if self.closed.is_set():
            return None
        try:
            return self.messages.get(timeout=min(timeout, .02))
        except queue.Empty:
            return None

    def send(self, message):
        if self.send_error:
            raise self.send_error

    def shutdown(self):
        self.shutdown_count += 1
        self.closed.set()


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("CAN failure state was not published")


class CanServerFailureTests(unittest.TestCase):
    def test_auto_reply_failure_marks_stopped_and_can_restart(self):
        first = FakeBus(send_error=OSError("send failed"))
        second = FakeBus()
        logs = []
        server = CanServer(request_id=0x100, response_id=0x101,
                           response_data="01 02", receive_timeout=.02,
                           on_log=lambda level, message: logs.append((level, message)))
        self.addCleanup(server.close)
        with patch("server.can_server.can.Bus", side_effect=[first, second]):
            server.start()
            first.messages.put(can.Message(arbitration_id=0x100, data=b"\x01"))
            wait_for(lambda: server.error is not None)
            wait_for(lambda: first.closed.is_set())
            self.assertFalse(server.running)
            self.assertIn("send failed", str(server.error))
            self.assertTrue(any(level == "ERROR" for level, _ in logs))
            server.start()
            self.assertTrue(server.running)
            self.assertIsNone(server.error)
            self.assertEqual(first.shutdown_count, 1)
            server.close()
            self.assertFalse(server.running)
            self.assertEqual(second.shutdown_count, 1)

    def test_receive_failure_releases_bus(self):
        bus = FakeBus(recv_error=OSError("receive failed"))
        server = CanServer(receive_timeout=.02)
        self.addCleanup(server.close)
        with patch("server.can_server.can.Bus", return_value=bus):
            server.start()
            wait_for(lambda: server.error is not None)
            wait_for(lambda: bus.closed.is_set())
        self.assertFalse(server.connected)
        self.assertEqual(bus.shutdown_count, 1)

    def test_direct_send_failure_marks_transport_failed(self):
        bus = FakeBus(send_error=OSError("send failed"))
        server = CanServer(receive_timeout=.02)
        self.addCleanup(server.close)
        with patch("server.can_server.can.Bus", return_value=bus):
            server.start()
            with self.assertRaises(CanServerError):
                server.send_frame(0x123, "01")
            self.assertFalse(server.running)
            self.assertIsNotNone(server.error)
            self.assertTrue(bus.closed.is_set())

    def test_invalid_auto_reply_is_rejected_before_opening_bus(self):
        with patch("server.can_server.can.Bus") as factory:
            with self.assertRaises(CanServerError):
                CanServer(request_id=0x100, response_id=0x101, response_data="NOT HEX")
            factory.assert_not_called()

    def test_diagnostic_callback_cannot_kill_receiver(self):
        bus = FakeBus()

        def broken_logger(*_):
            raise RuntimeError("log sink unavailable")

        server = CanServer(receive_timeout=.02, on_log=broken_logger)
        self.addCleanup(server.close)
        with patch("server.can_server.can.Bus", return_value=bus):
            server.start()
            bus.messages.put(can.Message(arbitration_id=0x123, data=b"\x01"))
            wait_for(lambda: bool(server.frames()))
            self.assertTrue(server.running)
            self.assertIsNone(server.error)

    def test_virtual_bus_auto_reply_and_shutdown(self):
        channel = "review-can-" + uuid.uuid4().hex
        server = CanServer(channel=channel, request_id=0x100, response_id=0x101,
                           response_data="12 34", receive_timeout=.02)
        self.addCleanup(server.close)
        client = can.Bus(interface="virtual", channel=channel)
        self.addCleanup(client.shutdown)
        server.start()
        client.send(can.Message(arbitration_id=0x100, data=b"\x00", is_extended_id=False))
        response = client.recv(timeout=2)
        self.assertIsNotNone(response)
        self.assertEqual(response.arbitration_id, 0x101)
        self.assertEqual(bytes(response.data), b"\x12\x34")
        self.assertTrue(server.connected)
        server.close()
        self.assertFalse(server.connected)
        self.assertIsNone(server._thread)


if __name__ == "__main__":
    unittest.main()

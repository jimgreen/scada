"""A received CAN frame can finish logging asynchronously, even after close."""
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from client.can import CanMaster
from scdfe_c import ScdfeLogger


class CanPeriodicLoggingTests(unittest.TestCase):
    def test_inflight_callback_finishes_logging_after_close(self):
        with tempfile.TemporaryDirectory(prefix='can-async-log-') as directory:
            channel = 'can-log-' + uuid4().hex
            sender = CanMaster(interface='virtual', channel=channel, auto_start=True)
            receiver = CanMaster(interface='virtual', channel=channel, auto_start=True)
            entered, release, logged = threading.Event(), threading.Event(), threading.Event()
            closed = threading.Event()
            logger = ScdfeLogger(Path(directory), process_name='scdfe_c-rtu-10')
            calls, errors = [], []

            def content():
                return '\n'.join(p.read_text(encoding='utf8') for p in Path(directory).rglob('*.log'))

            def record(direction, data, detail):
                calls.append(direction)
                if len(calls) == 2:
                    entered.set()
                    if not release.wait(3):
                        errors.append('callback gate timed out')
                logger.packet(direction, data, detail)
                if len(calls) == 2:
                    logged.set()

            def close():
                try:
                    task.stop()
                    sender.close()
                except Exception as exc:
                    errors.append(repr(exc))
                finally:
                    closed.set()

            sender.set_packet_handler(record)
            task = sender.send_periodic(0x123, b'\x01\x02', period=.02)
            closer = threading.Thread(target=close, daemon=True)
            try:
                for _ in range(2):
                    self.assertEqual(receiver.recv(1).arbitration_id, 0x123)
                self.assertTrue(entered.wait(1))
                self.assertEqual(content().count('报文 TX'), 1)
                closer.start()
                # Existing asynchronous stop does not destroy the handler or
                # logger. Releasing the delayed callback must still write TX.
                self.assertTrue(closed.wait(1))
                self.assertFalse(logged.is_set())
                release.set()
                self.assertTrue(logged.wait(1), 'in-flight TX never finished logging')
                self.assertEqual(content().count('报文 TX'), 2)
                self.assertEqual(errors, [])
            finally:
                release.set()
                if closer.ident is not None:
                    closer.join(3)
                task.stop()
                sender.close()
                receiver.close()
                task.thread.join(1)

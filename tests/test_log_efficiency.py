"""Faster packet writes retain visibility, rotation and deterministic cleanup."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import scdfe_c as runtime


class LogEfficiencyTests(unittest.TestCase):
    def test_packet_metadata_is_sanitized_without_changing_hex_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            logger = runtime.ScdfeLogger(root, process_name='scdfe_c-rtu-1')
            payload = bytes(range(256))*8
            logger.packet('RX', payload, 'IEC104 pid=12 RTU=3 写库pid=45  HEX=metadata\t\tvalue')
            lines = next(Path(root).rglob('*.log')).read_text(encoding='utf-8').splitlines()
            for line in lines:
                self.assertNotIn('pid=12', line)
                self.assertNotIn('RTU=3', line)
                self.assertNotIn('pid=45', line)
                self.assertNotIn('  ', line)
                self.assertIn('HEX=metadata value', line)
            self.assertEqual(b''.join(bytes.fromhex(line.rsplit(' HEX=',1)[1]) for line in lines), payload)

    def test_session_flushes_each_record_and_closes_on_exit(self):
        with tempfile.TemporaryDirectory() as root:
            logger = runtime.ScdfeLogger(root, process_name='scdfe_c-rtu-1')
            with logger.session():
                logger.packet('RX', b'\x68\x04')
                path = next(Path(root).rglob('*.log'))
                self.assertIn('[报文] 报文 RX', path.read_text(encoding='utf-8'))
                logger.normal('visible immediately')
                self.assertIn('visible immediately', path.read_text(encoding='utf-8'))
            # This requires the file handle to be closed on Windows too.
            path.unlink()
            logger.normal('after session')
            self.assertIn('after session', path.read_text(encoding='utf-8'))

    def test_large_packet_is_complete_and_written_through_one_open(self):
        with tempfile.TemporaryDirectory() as root:
            logger = runtime.ScdfeLogger(root, process_name='scdfe_c-rtu-1')
            payload = bytes(range(256)) * 256
            opened = []
            real_open = Path.open
            def observe(path, *args, **kwargs):
                opened.append(path)
                return real_open(path, *args, **kwargs)
            with patch.object(Path, 'open', observe):
                logger.packet('RX', payload)
            self.assertEqual(len(opened), 1)
            lines = opened[0].read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 64)
            self.assertTrue(all('[报文]' in line for line in lines))
            self.assertEqual(b''.join(bytes.fromhex(line.split('HEX=', 1)[1]) for line in lines), payload)

    def test_session_rolls_hour_and_releases_old_file(self):
        with tempfile.TemporaryDirectory() as root:
            logger = runtime.ScdfeLogger(root, process_name='scdfe_c-rtu-1')
            now = datetime(2026, 11, 5, 23, 59, 59).astimezone()
            with patch.object(runtime, 'datetime') as clock, logger.session():
                clock.now.return_value = now
                logger.normal('before midnight')
                old = next(Path(root).rglob('*.log'))
                clock.now.return_value = now + timedelta(seconds=2)
                logger.normal('after midnight')
                new = next(path for path in Path(root).rglob('*.log') if path != old)
                self.assertIn('00:00:01 [日志] after midnight', new.read_text(encoding='utf-8'))
                old.unlink()
            self.assertIn('2026/11/06/00/', new.as_posix())

    def test_cached_directory_is_recreated_after_external_removal(self):
        with tempfile.TemporaryDirectory() as root:
            logger = runtime.ScdfeLogger(root, process_name='scdfe_c-rtu-1')
            logger.normal('first')
            path = next(Path(root).rglob('*.log'))
            path.unlink()
            path.parent.rmdir()
            logger.normal('second')
            self.assertIn('second', path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()

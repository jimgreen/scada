"""Optimized full-detail logs keep their safety, visibility and failure contract."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock, patch
import random
import re
import tempfile
import threading
import unittest

from core.communication_log import CommunicationLog
from core.scdfe_control import log_message_text
from scdfe_c import ScdfeLogger


class BusinessLogEfficiencyTests(unittest.TestCase):
    def test_single_line_text_is_preserved_without_regex_normalization(self):
        rng = random.Random(20261114)
        tokens = ['业务事件','写库','pid','RTU','tid','CHANNEL','chanel','=', '123', '１２３',
                  '编号',' ', '  ', '\t',
                  '异常码=2','原始值=[0, 1]','通道编号=8','owner_pid=42']
        for _ in range(2000):
            text = ''.join(rng.choices(tokens,k=20))
            self.assertEqual(log_message_text(text),text)

    def test_line_breaks_are_visible_escapes_and_identity_and_spacing_survive(self):
        text = '  pid=12 RTU=3\t\tchannel=8\r\n12:00:00 [进程] forged\u2028end  '
        expected = r'  pid=12 RTU=3'+'\t\t'+r'channel=8\r\n12:00:00 [进程] forged\u2028end  '
        with patch('re.sub',side_effect=AssertionError('no normalization')):
            self.assertEqual(log_message_text(text),expected)
            self.assertEqual(log_message_text(expected),expected)
            self.assertEqual(log_message_text('trailing\n'),r'trailing\n')
            self.assertEqual(log_message_text(''), '')

    def test_field_text_preserves_original_values_before_record_formatting(self):
        class DeviceNumber(int):
            def __str__(self): return '1\nforged'
        class DeviceText:
            def __str__(self): return 'first\u2028second'
        values = [0,1.25,True,None,float('nan'),float('inf'),10**1600,
                  DeviceNumber(1),DeviceText(),'value\r\nnext']
        for value in values:
            self.assertEqual(CommunicationLog._text(value),str(value)[:1500])

    def test_empty_rows_have_no_clock_or_io_but_invalid_rows_report_error(self):
        sink = Mock()
        log = CommunicationLog(sink)
        with patch('core.communication_log.datetime') as clock:
            log.rows('无变化',iter(()))
            clock.now.assert_not_called()
        sink.assert_not_called()
        with self.assertLogs('core.communication_log',level='ERROR'):
            log.rows('无效数据',[None])
        sink.assert_not_called()

    def test_bounded_writes_flush_all_rows_before_returning(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = ScdfeLogger(directory,process_name='scdfe_c-rtu-1')
            with logger.session():
                logger.normal('warmup')
                stream = Mock(wraps=logger._streams['runtime'])
                logger._streams['runtime'] = stream
                logger.business_lines(f'业务事件 变化 点号={i}\n下一行 pid=9' for i in range(1000))
                self.assertEqual(stream.write.call_count,8)
                self.assertEqual(stream.flush.call_count,1)
                lines = next(Path(directory).rglob('*.log')).read_text(encoding='utf8').splitlines()[1:]
                self.assertEqual(len(lines),1000)
                for i,line in enumerate(lines):
                    self.assertTrue(line.endswith(f'业务事件 变化 点号={i}'+r'\n下一行 pid=9'))

    def test_service_preview_matches_stored_text_and_query_result(self):
        from scdfe_s.web.iec104_service import IEC104Service
        from scdfe_s.web.protocol_service import ProtocolService
        from scdfe_c.web.log_query import search_logs
        from core.packet_trace import service_packet_recorder
        for service_type in (IEC104Service,ProtocolService):
            with self.subTest(service=service_type.__name__), tempfile.TemporaryDirectory() as directory:
                logger = ScdfeLogger(directory,process_name='scdfe_s-channel-8')
                file_log = SimpleNamespace(_logger_for=lambda channel:logger,
                    write=lambda level,message,**kwargs:logger.emit('normal',message,level=level))
                service = service_type.__new__(service_type)
                service._logs, service._alerts, service._log_lock = deque(),deque(),threading.RLock()
                service._file_log = file_log
                text = '  pid=7 rtu=9 channel=8  值=3.14\t\t质量=0\n详细原因  '
                service._record('INFO',text)
                sink = service_packet_recorder(service._record,9,8)
                sink.business.event('变化',详情='pid=17  值=8\r\n下一段')
                def concurrent_event(index):
                    if index % 2:
                        service._record('INFO',f'低频诊断 pid=17 第{index}条')
                    else:
                        sink.business.event('并发变化',点号=index,值=index)
                with logger.session(), ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(concurrent_event,range(60)))
                stored = next(Path(directory).rglob('*.log')).read_text(encoding='utf8').splitlines()
                previews = [row['message'] for row in service._logs]
                self.assertEqual([line.split('[日志] ',1)[1] for line in stored],previews)
                result = search_logs(directory,{'record':'runtime'},family='s')
                self.assertEqual([row['text'] for row in reversed(result['items'])],stored)
                self.assertEqual(previews[0],log_message_text(text))
                self.assertIn('日志上下文 @',previews[1])
                self.assertIn(r'详情=pid=17  值=8\r\n下一段',previews[2])

    def test_failed_context_write_is_not_cached_or_shown_as_stored(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = ScdfeLogger(directory,process_name='scdfe_c-rtu-1')
            log = CommunicationLog(logger.business_lines,RTU编号=1,通道编号=8)
            with logger.session():
                logger.normal('warmup')
                stream = Mock(wraps=logger._streams['runtime'])
                stream.write.side_effect = OSError('full')
                logger._streams['runtime'] = stream
                with self.assertLogs('core.communication_log',level='ERROR'):
                    log.event('写入失败',点号=1)
                self.assertEqual(logger._business_contexts,{})
                log.event('恢复写入',点号=2)
            lines = next(Path(directory).rglob('*.log')).read_text(encoding='utf8').splitlines()
            self.assertEqual(len(lines),3)
            self.assertIn('日志上下文 @',lines[1])
            self.assertIn('恢复写入',lines[2])
            self.assertNotIn('写入失败','\n'.join(lines))

    def test_failing_iterator_keeps_already_formatted_records_and_reopens(self):
        def records():
            for i in range(150): yield f'点号={i}'
            raise ValueError('source failed')
        with tempfile.TemporaryDirectory() as directory:
            logger = ScdfeLogger(directory,process_name='scdfe_s-channel-1')
            with logger.session():
                with self.assertRaisesRegex(ValueError,'source failed'):
                    logger.business_lines(records())
                self.assertIsNone(logger._streams['runtime'])
                logger.normal('recovered')
            lines = next(Path(directory).rglob('*.log')).read_text(encoding='utf8').splitlines()
            self.assertEqual(len(lines),151)
            self.assertTrue(lines[-2].endswith('点号=149'))
            self.assertTrue(lines[-1].endswith('recovered'))

    def test_concurrent_batches_retain_every_point_and_batch_order(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = ScdfeLogger(directory,process_name='scdfe_t-channel-1')
            def write(owner):
                log = CommunicationLog(logger.business_lines,RTU编号=owner)
                for batch in range(10):
                    log.rows('变化',({'点号':batch*17+i,'值':i} for i in range(17)))
            with logger.session(), ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write,range(1,5)))
            lines = next(Path(directory).rglob('*.log')).read_text(encoding='utf8').splitlines()
            self.assertEqual(len([line for line in lines if '业务事件' in line]),680)
            for owner in range(1,5):
                contexts = {re.search(r'日志上下文 @(\S+)',line)[1] for line in lines
                            if f'RTU编号={owner}' in line and '日志上下文 @' in line}
                selected = [line for line in lines if '业务事件' in line and
                            any(f'@{context} ' in line for context in contexts)]
                self.assertEqual([int(re.search(r'点号=(\d+)',line)[1]) for line in selected],list(range(170)))
                self.assertTrue(all('RTU编号' not in line and '线程=' not in line for line in selected))

    def test_context_is_defined_per_hour_and_restart_without_losing_identity(self):
        from datetime import datetime, timedelta
        import scdfe_c as runtime
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime,'datetime') as clock:
            clock.now.return_value = datetime(2026,11,14,23,59,59)
            logger = ScdfeLogger(directory,process_name='scdfe_c-rtu-1')
            with logger.session():
                log = CommunicationLog(logger.business_lines,组件='C',RTU编号=1,通道编号=8)
                log.rows('变化',({'点号':i,'值':i} for i in range(1000)))
                old = next(Path(directory).rglob('*.log'))
                lines = old.read_text(encoding='utf8').splitlines()
                self.assertEqual(sum('日志上下文 @' in line for line in lines),1)
                self.assertEqual(sum('RTU编号=1' in line for line in lines),1)
                self.assertEqual(sum('通道编号=8' in line for line in lines),1)
                self.assertEqual(len(lines),1001)
                clock.now.return_value += timedelta(seconds=2)
                log.event('变化',点号=1000,值=1000)
            new = next(path for path in Path(directory).rglob('*.log') if path != old)
            restarted = ScdfeLogger(directory,process_name='scdfe_c-rtu-1')
            CommunicationLog(restarted.business_lines,组件='C',RTU编号=1,通道编号=9).event('重连',点号=1)
            lines = new.read_text(encoding='utf8').splitlines()
            self.assertEqual(len(lines),4)
            ids = [re.search(r'日志上下文 @(\S+)',line)[1] for line in lines if '日志上下文 @' in line]
            self.assertEqual(len(set(ids)),2)
            self.assertIn('通道编号=8',lines[0])
            self.assertIn('通道编号=9',lines[2])
            self.assertIn('@'+ids[0]+' ',lines[1])
            self.assertIn('@'+ids[1]+' ',lines[3])


if __name__ == '__main__': unittest.main()

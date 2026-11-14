"""Runtime text classification and per-request reuse preserve the query contract."""
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from scdfe_c.web import log_query


class LogQueryEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, lines):
        path = self.root/name
        path.write_text('\n'.join(lines)+'\n',encoding='utf8')
        return path

    def test_runtime_message_cannot_become_packet_by_quoting_rx_or_tx(self):
        for family in ('c','s','t'):
            with self.subTest(family=family):
                process = 'scdfe_c-rtu-1' if family=='c' else f'scdfe_{family}-channel-1'
                filename = f'{process}-20261114-10.log'
                lines = ['10:00:00 [日志] @example.1 业务事件 诊断 详情=报文 RX 通道=9 数据异常',
                         '10:00:01 [日志] 业务事件 指令失败 原因=报文 TX 通道=9 未确认',
                         '10:00:02 [日志] 报文 RX 通道=9 frame=1 HEX=68 04',
                         '10:00:03 [报文] 报文 TX 通道=9 frame=2 HEX=68 04',
                         '10:00:04 [日志]   报文 RX 通道=9 frame=3 HEX=68 04',
                         '10:00:05 [警告] 报文 TX 通道=9 超时']
                self.write(filename,lines)
                def read(**query):
                    return log_query.search_logs(self.root,dict(filename=filename,**query),family=family)['items']
                self.assertEqual([r['text'] for r in read(record='runtime')], [lines[5],lines[1],lines[0]])
                self.assertEqual([r['text'] for r in read(record='packet')], [lines[4],lines[3],lines[2]])
                self.assertEqual([r['text'] for r in read(direction='RX')], [lines[4],lines[2]])
                self.assertEqual([r['text'] for r in read(direction='TX')], [lines[3]])

    def test_same_day_clock_is_reused_across_files_but_not_across_queries(self):
        names = [f'scdfe_c-rtu-{i}-20261114-10.log' for i in range(1,7)]
        for name in names:
            self.write(name,['10:00:01 [日志] 业务事件 值=1']*20)
        self.write('scdfe_c-rtu-7-20261115-10.log',['10:00:01 [日志] 业务事件 值=2'])
        native = log_query._log_timestamp
        with patch.object(log_query,'_log_timestamp',wraps=native) as convert:
            first = log_query.search_logs(self.root,{'q':'业务事件'})
            self.assertEqual(len(first['items']),121)
            self.assertEqual(convert.call_count,2)
        with patch.object(log_query,'_log_timestamp',side_effect=lambda clock,date:native(clock,date)+3600):
            second = log_query.search_logs(self.root,{'q':'业务事件'})
        self.assertEqual([r['time']+3600000 for r in first['items']], [r['time'] for r in second['items']])

    def test_header_cache_handles_whitespace_invalid_headers_and_unicode_digits(self):
        filename='scdfe_c-rtu-1-20261114-10.log'
        lines=['10:00:01 [日志] repeat']*5+[
            '10:00:01 [日志]', '10:00:01 [日志]Xinvalid', '10:00:01 [日志]Yinvalid',
            '10:00:01 [日志]\tvalue', '10:00:01 [日志]\u2003value',
            '１０:００:０１ [日志] Unicode digits', '99:99:99 [日志] invalid clock',
            '10:00:01 [INFO] old header', 'just text', '10:00:01 [日志] repeat']
        self.write(filename,lines)
        rows=log_query.search_logs(self.root,{'filename':filename})['items']
        date=datetime(2026,11,14,10)
        for raw,row in zip(reversed(lines),rows):
            header=log_query.HEADER.match(raw)
            self.assertEqual(row['text'],raw)
            self.assertEqual(row['level'], log_query.LOG_LEVEL_CODES[header[2]] if header else '')
            try: stamp=int(log_query._log_timestamp(header[1],date)*1000) if header else None
            except ValueError: stamp=None
            self.assertEqual(row['time'],stamp)

    def test_keyword_prefilter_retains_unicode_casefold_matches(self):
        filename='scdfe_c-rtu-1-20261114-10.log'
        values=['Straße','STRASSE','straße','İstanbul','i\u0307stanbul','σςΣ','Κ K k',
                'ﬀ ﬃ','FF FFI','业务事件 质量=0','<literal.*[x]>','ПРИВЕТ привет']
        lines=[f'10:00:01 [日志] {value}' for value in values]
        self.write(filename,lines)
        for keyword in values+['strasse','STANBUL','σ','k','ffi','业务','质量=0','[x]','Привет','\u0307','1']:
            with self.subTest(keyword=keyword):
                result=log_query.search_logs(self.root,{'filename':filename,'q':keyword})
                expected=[line for line in reversed(lines) if keyword.casefold() in line.casefold()]
                self.assertEqual([r['text'] for r in result['items']],expected)

    def test_timestamp_cache_eviction_preserves_cross_day_and_hour_results(self):
        for day in (14, 15):
            for hour in (10, 11):
                self.write(f'scdfe_c-rtu-1-202611{day}-{hour}.log', [
                    f'10:00:{second:02d} [日志] 业务事件 {second}' for second in range(8)
                ] + ['99:99:99 [日志] invalid clock'])
        queries = [{}, {'start': datetime(2026,11,14,10,0,3).astimezone().isoformat(),
                        'end': datetime(2026,11,15,10,0,6).astimezone().isoformat()}]
        for query in queries:
            expected = log_query.search_logs(self.root,query)
            with patch.object(log_query,'MAX_TIMESTAMP_CACHE',2):
                actual = log_query.search_logs(self.root,query)
            self.assertEqual(actual,expected)

    def test_literal_prefilter_keeps_invalid_utf8_and_cursor_boundaries(self):
        filename='scdfe_c-rtu-1-20261114-10.log'
        raw = [b'10:00:01 [\xe6\x97\xa5\xe5\xbf\x97] '+tail for tail in (
            b'\xff'+ '中文目标 123'.encode(), b'\xffunrelated',
            '中文'.encode()+b'\xff'+'目标 12'.encode(), '中文目标 123'.encode())]
        (self.root/filename).write_bytes(b'\r\n'.join(raw)+b'\r\n')
        for keyword in ('中文目标','123','中文','12'):
            rows, cursor = [], None
            with patch.object(log_query,'MAX_SCAN_BYTES',64), patch.object(log_query,'CHUNK_SIZE',32):
                for _ in range(20):
                    query = dict(filename=filename,q=keyword,limit='1')
                    if cursor:
                        query['cursor'] = cursor
                    page = log_query.search_logs(self.root,query)
                    self.assertLessEqual(page['scanned_bytes'],64)
                    rows.extend(row['text'] for row in page['items'])
                    if not page['next_cursor']:
                        break
                    self.assertNotEqual(cursor,page['next_cursor'])
                    cursor = page['next_cursor']
                else:
                    self.fail('pagination did not finish')
            decoded = [line.decode('utf8','replace') for line in reversed(raw)]
            self.assertEqual(rows,[line for line in decoded if keyword in line])


if __name__ == '__main__': unittest.main()

"""Business receipts must describe observed phases without changing I/O."""
from contextlib import ExitStack
from pathlib import Path
from queue import Full
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from core.communication_log import CommunicationLog, decode_asdu
from core.packet_trace import PacketTrace
from tests.test_protocol_interface_contracts import free_port, eventually


def capture(adapter, **context):
    lines = []
    business = CommunicationLog(lines.extend, **context)
    adapter.set_packet_handler(business.wrap_packet(lambda *args: None))
    return lines, business


class BusinessDecoderTests(unittest.TestCase):
    def test_iec104_types_sequence_quality_and_clocks(self):
        stamp = bytes.fromhex('d2 04 85 86 2e 0b 1a')
        header, rows = decode_asdu(bytes([31, 0x82, 3, 2, 9, 0]) +
                                  (101).to_bytes(3, 'little') + b'\xb3' + stamp + b'\x02' + stamp)
        self.assertEqual((header['CA'], header['OA'], header['原因']), (9, 2, '突发/变位'))
        self.assertEqual([row['IOA'] for row in rows], [101, 102])
        self.assertEqual((rows[0]['值'], rows[0]['质量']), (3, '0xB0'))
        self.assertEqual(rows[0]['时标原始'], stamp.hex())
        self.assertEqual((rows[0]['时标无效'], rows[0]['夏令时']), (1, 1))
        for typ, payload, value, quality in [
            (1, b'\x91', 1, '0x90'), (5, b'\xff\x81', -1, '0x81'),
            (9, struct.pack('<hB', 16384, 0x20), .5, '0x20'),
            (11, struct.pack('<hB', -300, 1), -300, '0x01'),
            (13, struct.pack('<fB', 12.5, 0x80), 12.5, '0x80'),
            (21, struct.pack('<h', -16384), -.5, '0x00')]:
            with self.subTest(typ=typ):
                _, result = decode_asdu(bytes([typ, 1, 20, 0, 1, 0, 1, 0, 0]) + payload)
                self.assertEqual((result[0]['值'], result[0]['质量']), (value, quality))
        _, result = decode_asdu(bytes.fromhex('02 01 03 00 01 00 01 00 00 01 d2 04 05'))
        self.assertIn('不含日期和小时', result[0]['源时标'])

    def test_command_gi_rejection_and_malformed_are_distinct(self):
        _, rows = decode_asdu(bytes([45, 1, 6, 0, 1, 0, 3, 96, 0, 0x81]))
        self.assertEqual((rows[0]['选择'], rows[0]['值']), (1, 1))
        header, rows = decode_asdu(bytes([45, 1, 0x47, 0, 1, 0, 3, 96, 0, 1]))
        self.assertEqual((header['否定确认'], header['原因'], rows[0]['选择']), (1, '激活确认', 0))
        lines = []
        log = CommunicationLog(lines.extend)
        for cot in (6, 7, 10):
            asdu = bytes([100, 1, cot, 0, 1, 0, 0, 0, 0, 20])
            log.packet('RX', bytes([0x68, len(asdu)+4, 0, 0, 0, 0])+asdu, 'IEC104 APDU')
        self.assertTrue(all('总召' in line and 'QOI=20' in line for line in lines))
        log.packet('RX', b'\x68', 'IEC104 APDU')
        self.assertIn('解析失败', lines[-1])
        with self.assertRaises(ValueError): decode_asdu(bytes.fromhex('01 02 03 00 01 00 01 00 00 01'))

    def test_all_points_batched_custom_ioa_and_failed_sink(self):
        batches = []
        log = CommunicationLog(lambda rows: batches.append(list(rows)), 通道编号=8)
        log.rows('变化', ({'点号': i, '值': i} for i in range(1000)))
        self.assertEqual([len(b) for b in batches], [128]*7+[104])
        self.assertIn('点号=999', batches[-1][-1])
        log.bind_points(SimpleNamespace(_points={('yx', 7): SimpleNamespace(io_address=123)}))
        log.packet('RX', bytes.fromhex('68 0e 00 00 00 00 01 01 03 00 01 00 7b 00 00 01'), 'IEC104')
        self.assertIn('点号=7', batches[-1][-1])
        log.write_many = Mock(side_effect=OSError('full'))
        with self.assertLogs('core.communication_log', level='ERROR'):
            log.event('不影响业务', 值=1)

    def test_modbus_pdu_hook_is_identity_and_reports_exception(self):
        from pymodbus.pdu import ExceptionResponse
        trace = PacketTrace()
        lines, log = capture(trace)
        response = ExceptionResponse(3, 2)
        response.dev_id, response.transaction_id = 9, 654
        self.assertIs(trace._trace_modbus_pdu(True, response), response)
        self.assertIn('站号=9', lines[0])
        self.assertIn('事务标识=654', lines[0])
        self.assertIn('异常码=2', lines[0])

    def test_can_schedule_reports_definition_and_failure_without_retry(self):
        from core.can_schedule import CanFrameSchedule
        for fails in (False, True):
            events=[]
            send=Mock(side_effect=OSError('bus unavailable') if fails else None)
            schedule=CanFrameSchedule(send,[dict(id=8,arbitration_id=0x188,data_hex='01',
                schedule_mode='periodic',period_ms=10,repeat_count=1)],
                observe=lambda action,**fields:events.append((action,fields)))
            try:
                schedule.start()
                self.assertTrue(eventually(lambda:len(events)==1))
                self.assertEqual(send.call_count,1)
                self.assertEqual(events[0][1]['定义编号'],8)
                self.assertEqual(events[0][1]['周期毫秒'],10)
                self.assertGreaterEqual(events[0][1]['调度延迟毫秒'],0)
                self.assertIn('停止本任务' if fails else '已提交驱动',events[0][0])
            finally:
                schedule.stop()


class BusinessRuntimeTests(unittest.TestCase):
    def test_rtu_wire_exception_and_corrupt_crc_remain_observable_failures(self):
        from tests.test_modbus_rtu_master import RtuSimulator
        from client import ModbusRtuMaster
        from client.modbus_rtu import ModbusError
        with ExitStack() as stack:
            sim = RtuSimulator(); stack.callback(sim.close)
            peer = ModbusRtuMaster(f'socket://127.0.0.1:{sim.port}', unit_id=9, parity='N', timeout=.15)
            lines, _ = capture(peer)
            peer.connect(); stack.callback(peer.close)
            self.assertEqual(peer.read_holding_registers(0,2),[9,9])
            self.assertTrue(any('规约=Modbus-RTU' in x and '原始值=[9, 9]' in x for x in lines))
            with self.assertRaises(ModbusError): peer.read_holding_registers(200)
            self.assertIn('异常码=2',lines[-1])
            sim.corrupt = True
            before=len(lines)
            with self.assertRaises(ModbusError): peer.read_holding_registers(0)
            self.assertIn('Modbus请求失败',lines[-1])
            self.assertFalse(any('读取完成' in x for x in lines[before:]))

    def test_s_modbus_db_quality_mapping_and_command_receipt(self):
        from client import ModbusTcpMaster
        from scdfe_c.web.service import ScadaStore
        from server.modbus_mapped_server import ModbusMappedServer
        with ExitStack() as stack:
            root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
            store=ScadaStore(root/'db.sqlite3');stack.callback(store.close)
            store.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='modbus-tcp'))
            for kind in ('yc','yt'):store.create_row(f'fes_{kind}_define',dict(rtu_id=1,pnt_no=1,value=10))
            server=ModbusMappedServer(store=store,rtu_id=1,port=free_port(),points=[
                dict(kind='yc',pnt_no=1,address=0,register_area='input',data_type='int16'),
                dict(kind='yt',pnt_no=1,address=2,register_area='holding',data_type='int16')])
            lines,_=capture(server);server.start();stack.callback(server.close)
            peer=ModbusTcpMaster('127.0.0.1',server.port);peer.connect();stack.callback(peer.close)
            store.update_row('fes_yc_define',[1,1],dict(value=42,status=128))
            self.assertTrue(eventually(lambda:peer.read_input_registers(0)==[42]))
            self.assertTrue(any('等待对端读取' in x and '新值=42' in x and '新质量=128' in x for x in lines))
            # Quality-only edits are logged but do not fabricate a wire quality field.
            store.update_row('fes_yc_define',[1,1],dict(status=32))
            self.assertTrue(eventually(lambda:any('新质量=32' in x for x in lines)))
            peer.write_single_register(2,63)
            self.assertEqual(store.get_row('fes_yt_define',[1,1])['value'],63)
            self.assertTrue(any('已提交模拟数据库' in x and '四遥=YT' in x and '值=63' in x for x in lines))

    def test_soe_submit_disconnect_ack_and_logging_failure(self):
        from core.iec104_soe import SoeForwarder
        from scdfe_s.database import SimulatorStore
        from tools.verify_iec104_soe import fill_one
        with ExitStack() as stack:
            root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
            store=SimulatorStore(root/'s.sqlite3');stack.callback(store.close);fill_one(store,2404)
            store.update_row('fes_yx_define',[1,1],dict(value=1))
            events=[]
            forwarder=SoeForwarder(store,'business-test','S',lambda row:[row],rtu_id=1,
                                  on_event=lambda action,rows:events.append((action,list(rows))))
            stack.callback(forwarder.close)
            sender=SimpleNamespace(submit_soe=lambda events:True,poll_soe=lambda:0)
            forwarder.tick(sender)
            self.assertIn('等待APCI确认',events[-1][0]);self.assertEqual(forwarder.last_event_id,0)
            sender.poll_soe=lambda:-1;forwarder.tick(sender)
            self.assertIn('保留游标重试',events[-1][0]);self.assertEqual(forwarder.last_event_id,0)
            forwarder.tick(sender)
            sender.poll_soe=lambda:1;forwarder.tick(sender)
            self.assertIn('APCI确认并推进游标',events[-1][0]);self.assertEqual(forwarder.last_event_id,1)
            self.assertEqual(events[-1][1][0]['pnt_no'],1)
            store.update_row('fes_yx_define',[1,1],dict(value=0))
            forwarder.on_event=Mock(side_effect=OSError('full'))
            with self.assertLogs('core.iec104_soe',level='ERROR'):
                forwarder.tick(sender);forwarder.tick(sender)
            self.assertEqual(forwarder.last_event_id,2)

    def test_c_queue_commit_failure_and_stable_values(self):
        from tests.test_scdfe_c import CaptureWriterQueue, Value
        from scdfe_c import ChannelSpec, ChannelWorker, ScdfeLogger, _flush_batch
        from scdfe_c.web.service import ScadaStore
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            store = ScadaStore(root/'db.sqlite3'); stack.callback(store.close)
            store.create_row('fes_rtu_define', dict(rtu_id=1, channel_id=1, channel_type='modbus-tcp'))
            store.create_row('fes_yc_define', dict(rtu_id=1,pnt_no=1,value=0))
            queue = CaptureWriterQueue()
            worker = ChannelWorker(store, ChannelSpec(1,1,'modbus-tcp'), log_dir=root/'logs', writer_queue=queue)
            lines = []; worker.business.write_many = lines.extend
            values = [Value('yc',1,23.5,0x80)]
            queue.fail_next = True
            with self.assertRaises(Full): worker._sync_measurements(values)
            self.assertEqual(lines, [])
            worker._sync_measurements(values)
            self.assertIn('已入写库队列', lines[-1])
            self.assertEqual(store.get_row('fes_yc_define',[1,1])['value'], 0)
            count = len(lines)
            worker._sync_measurements(values)
            self.assertEqual(len(lines), count)
            logger = ScdfeLogger(root/'logs', process_name='writer')
            logger.business_lines = lines.extend
            self.assertTrue(_flush_batch(store,logger,queue.messages,queue))
            self.assertIn('写库事务已提交', lines[-1])
            self.assertIn(queue.messages[0]['business']['写库批次'], lines[-1])
            self.assertEqual(store.get_row('fes_yc_define',[1,1])['value'], 23.5)
            with store.connection() as con:
                con.execute("CREATE TRIGGER reject_yc BEFORE UPDATE ON fes_yc_define BEGIN SELECT RAISE(ABORT,'fixture'); END")
            before = len(lines)
            self.assertFalse(_flush_batch(store,logger,queue.messages,queue))
            self.assertFalse(any('事务已提交' in x for x in lines[before:]))

    def test_modbus_real_tcp_operations_exception_and_failed_log_disk(self):
        from client.modbus_tcp import ModbusTcpMaster, ModbusError
        from server.modbus_server import ModbusTcpServer
        with ExitStack() as stack:
            port = free_port()
            server = ModbusTcpServer(port=port)
            server_lines, _ = capture(server)
            server.start(); stack.callback(server.close)
            peer = ModbusTcpMaster('127.0.0.1',port)
            lines, log = capture(peer, 组件='C', RTU编号=9)
            peer.connect(); stack.callback(peer.close)
            peer.write_single_coil(2,True)
            peer.write_single_register(3,123)
            peer.write_multiple_coils(4,[True,False])
            peer.write_multiple_registers(6,[12,34])
            self.assertEqual(peer.read_coils(2),[True])
            peer.read_discrete_inputs(0)
            self.assertEqual(peer.read_holding_registers(6,2),[12,34])
            peer.read_input_registers(0)
            for code in (1,2,3,4,5,6,15,16):
                self.assertTrue(any(f'功能码={code} ' in x for x in lines))
            self.assertTrue(any('事务标识=' in x and '原始值=[12, 34]' in x for x in lines))
            with self.assertRaises(ModbusError): peer.read_holding_registers(60000)
            self.assertTrue(any('Modbus请求失败' in x and '异常码=2' in x for x in lines))
            self.assertTrue(any('Modbus响应' in x and '异常码=2' in x for x in server_lines))
            log.write_many = Mock(side_effect=OSError('full'))
            with self.assertLogs('core.communication_log', level='ERROR'):
                self.assertEqual(peer.read_holding_registers(3),[123])
            log.write_many=lines.extend
            peer.close()
            before=len(lines)
            with self.assertRaises(ModbusError):peer.write_multiple_registers(6,[88,99])
            self.assertTrue(any('待写原始值=[88, 99]' in x for x in lines[before:]))
            self.assertFalse(any('收到正常响应' in x for x in lines[before:]))

    def test_can_virtual_frame_signal_state_and_timeout(self):
        import can
        from client.can import CanMaster, CanTimeout
        with ExitStack() as stack:
            channel = uuid4().hex
            receiver = CanMaster(interface='virtual',channel=channel,fd=True)
            lines, _ = capture(receiver, 组件='C')
            receiver.configure([dict(kind='yc',pnt_no=1,frame_id=0x12345,is_extended_id=True,
                byte_offset=0,bit_length=16,value_type='uint',byte_order='big',scale=.5,offset=2)])
            receiver.connect(); stack.callback(receiver.close)
            sender = CanMaster(interface='virtual',channel=channel,auto_start=True,fd=True)
            stack.callback(sender.close)
            for number in (10,10,12):
                sender.send(0x12345, struct.pack('>H',number),is_extended_id=True,is_fd=True,bitrate_switch=True)
            values = receiver.collect_once(timeout=.1,max_frames=3)
            self.assertEqual([v.value for v in values],[7,7,8])
            frames = [x for x in lines if '业务事件 CAN帧 ' in x]
            self.assertEqual(len(frames),3)
            self.assertIn('帧格式=扩展帧 FD=1',frames[0])
            self.assertIn('BRS=1',frames[0])
            signals = [x for x in lines if 'CAN信号解析变化' in x]
            self.assertEqual(len(signals),2)
            self.assertIn('缩放=0.5',signals[0])
            self.assertIn('原始数据=00 0c',signals[1])
            # An error frame is observed, not fabricated from a normal frame.
            receiver._trace_can('RX',can.Message(arbitration_id=1,is_error_frame=True,data=b'\x01'))
            self.assertTrue(any('CAN错误帧' in x for x in lines))
            with self.assertRaises(CanTimeout): receiver.request(0x100,response_id=0x101,timeout=.01)
            self.assertIn('CAN等待应答超时',lines[-1])
            self.assertTrue(any('CAN驱动总线状态' in x for x in lines))

    def test_native_iec104_business_logs_with_packet_recording_disabled(self):
        from client import IEC104Master
        from scdfe_c.web.service import ScadaStore
        from scdfe_s.web.iec104_service import IEC104Service
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            store = ScadaStore(root/'scdfe_s.sqlite3'); stack.callback(store.close)
            store.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='iec104',packet_recording=0))
            store.create_row('fes_channel_iec104_define',dict(id=1,channel_id=1,rtu_addr=1))
            for kind,value in [('yc',12.5),('yx',0),('yk',0),('yt',0)]:
                store.create_row(f'fes_{kind}_define',dict(rtu_id=1,pnt_no=1,value=value))
            service = IEC104Service(store,config_path=root/'s.json')
            port=free_port();service.configure(dict(rtu_id=1,port=port));service.start();stack.callback(service.close)
            peer=IEC104Master('127.0.0.1',port,common_address=1,init='NONE')
            for kind in ('yc','yx','yk','yt'):peer.add_point(kind,1)
            lines, business = capture(peer,组件='C');business.bind_points(peer)
            peer.connect(timeout=3);stack.callback(peer.close)
            self.assertTrue(peer.general_interrogation())
            self.assertTrue(eventually(lambda:any('站总召返回' in x and '值=12.5' in x for x in lines)))
            store.update_row('fes_yc_define',[1,1],dict(value=25.5,status=0x80))
            self.assertTrue(eventually(lambda:any('突发/变位' in x and '值=25.5' in x for x in lines)))
            peer.send_yk_point(1,True)
            self.assertTrue(eventually(lambda:any('指令报文' in x and '原因=激活确认' in x for x in lines)))
            content='\n'.join(path.read_text(encoding='utf8') for path in (root/'logs').rglob('*.log'))
            self.assertIn('检测到数据库实时变化',content)
            self.assertIn('新值=25.5',content)
            self.assertIn('新质量=128',content)
            self.assertIn('总召',content)
            self.assertTrue(any('业务事件' in row['message'] for row in service.logs(record='runtime')))
            self.assertFalse(list((root/'packets').rglob('*.log')))


from tests import test_scdfe_t as trans_fixtures


class ForwardBusinessTests(trans_fixtures.ScdfeTTestBase):
    def test_t_source_target_mapping_and_command_waits_for_c(self):
        from scdfe_t import TransRuntime
        self.add_channel('modbus-tcp',free_port())
        runtime=TransRuntime(self.db,channel_id=1)
        lines=[];runtime.business.write_many=lines.extend
        spec,points,_=runtime.load();server=runtime.build_server(spec,points)
        self.addCleanup(server.close);server.start();runtime.refresh()
        self.store.update_row('fes_yc_define',[1,1],dict(value=54.5,status=128))
        runtime.refresh()
        self.assertTrue(any('转发源值变化' in x and '源RTU=1' in x and '源点号=1' in x and '新值=54.5' in x for x in lines))
        self.assertTrue(any('等待对端读取' in x and '54.5' in x for x in lines))
        self.assertTrue(runtime.commands([(runtime.points[('yk',3)],1)]), '\n'.join(lines[-5:]))
        self.assertTrue(any('转发指令已入源库，等待C处理' in x and '转发点号=3' in x and '源点号=1' in x for x in lines))


if __name__ == '__main__': unittest.main()

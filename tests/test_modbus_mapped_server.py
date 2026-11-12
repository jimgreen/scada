from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest

from client.modbus_tcp import ModbusTcpMaster, ModbusError
from scdfe_c.web.service import ScadaStore
from scdfe_s.web.protocol_service import ModbusTcpServerService
from server.modbus_mapped_server import ModbusMappedServer
from tests.test_protocol_interface_contracts import free_port, eventually


class MappedModbusContracts(unittest.TestCase):
    def test_database_mappings_float_word_orders_and_commands(self):
        for order in range(4):
            with self.subTest(order=order), ExitStack() as stack:
                root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
                store = ScadaStore(root/'scdfe_s.sqlite3'); stack.callback(store.close)
                store.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='modbus-tcp'))
                store.create_row('fes_channel_modbus_tcp_define',dict(channel_id=1,remote_host='127.0.0.1',remote_port=502,unit_id=1))
                for kind,value in [('yc',12.5),('yx',1),('yk',0),('yt',0)]:
                    store.create_row(f'fes_{kind}_define',dict(rtu_id=1,pnt_no=1,value=value))
                store.create_row('fes_channel_modbus_define_call',dict(id=1,channel_id=1,call_seq=1,irtuaddr=1,icode=4,iplace=30001,address_base=30001,inum=2))
                store.create_row('fes_channel_modbus_define_data',dict(id=1,channel_id=1,call_seq=1,cnt=1,if_yx=0,reg_addr=30001,trans_type=5,fourbytedata_byteseq=order))
                store.create_row('fes_channel_modbus_define_call',dict(id=2,channel_id=1,call_seq=2,irtuaddr=1,icode=2,iplace=0,inum=1))
                store.create_row('fes_channel_modbus_define_data',dict(id=2,channel_id=1,call_seq=2,cnt=1,if_yx=1,reg_addr=0,trans_type=8))
                for ident,kind,fc,typ,address in [(1,'yk',5,6,0),(2,'yt',16,5,40001)]:
                    store.create_row('fes_channel_modbus_define_ytyk',dict(id=ident,channel_id=1,cnt=1,ifyk=int(kind=='yk'),irtuaddr=1,icode=fc,ytyk_type=typ,reg_addr=address,address_base=40001 if address else 0,fourbytedata_byteseq=order,multi_para=2))
                service = ModbusTcpServerService(store,config_path=root/'service.json'); stack.callback(service.close)
                port = free_port(); service.configure(dict(rtu_id=1,listen_port=port)); service.start()
                peer = ModbusTcpMaster.from_store(store,1,transport_overrides=dict(remote_port=port))
                stack.callback(peer.close); peer.connect()
                self.assertEqual(peer.collect_once()[0].value,12.5)
                self.assertEqual(peer.read_discrete_inputs(0),[True])
                # Independent database edits, without calling a Web/engine update.
                store.update_row('fes_yc_define',[1,1],dict(value=-23.75))
                self.assertTrue(eventually(lambda: peer.collect_once()[0].value == -23.75))
                result = service.incremental_update('yc',1,8.25)
                self.assertEqual(result['value'],8.25)
                self.assertEqual(store.get_row('fes_yc_define',[1,1])['value'],8.25)
                peer.send_yt_point(1,123.25)
                self.assertEqual(store.get_row('fes_yt_define',[1,1])['value'],123.25)
                self.assertGreater(store.get_row('fes_yt_define',[1,1])['cmd_handle_time'],0)
                peer.send_yk_point(1,True)
                self.assertEqual(store.get_row('fes_yk_define',[1,1])['value'],1)
                with self.assertRaises(ModbusError): peer.read_input_registers(200,1)

    def test_signed_and_wide_values_and_serial_codec(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            store = ScadaStore(root/'scdfe_s.sqlite3'); stack.callback(store.close)
            store.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='modbus-rtu'))
            points = []
            for no,dtype,value in [(1,'int16',-123),(2,'int32',-123456),(3,'uint32',123456)]:
                store.create_row('fes_yc_define',dict(rtu_id=1,pnt_no=no,value=value))
                points.append(dict(kind='yc',pnt_no=no,address=no*4,register_area='input',data_type=dtype))
            engine = ModbusMappedServer(store=store,rtu_id=1,points=points,tcp=False,serial='unused')
            stack.callback(engine.close)
            self.assertFalse(engine.tcp)
            batch = engine.prepare_updates([('yc',1,-321),('yc',2,-987654),('yc',3,987654)])
            engine.publish_updates(batch)
            self.assertEqual([v['value'] for v in engine.snapshot()],[-321,-987654,987654])
            with self.assertRaises((ValueError, OverflowError)):
                engine.prepare_updates([('yc',1,65536)])
            with self.assertRaises(ValueError):
                engine.prepare_updates([('yc',3,-1)])
            # A newly defined point can have no initial value; the simulator
            # must retain the raw-bank startup convention of zero.
            store.create_row('fes_yc_define',dict(rtu_id=1,pnt_no=4))
            blank = ModbusMappedServer(store=store,rtu_id=1,points=[dict(kind='yc',pnt_no=4,address=0,data_type='float32')])
            stack.callback(blank.close)
            self.assertEqual(blank.snapshot()[0]['value'],0)

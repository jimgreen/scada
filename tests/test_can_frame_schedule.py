from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
import unittest
import uuid
import can

from client.can import CanMaster
from core.can_schedule import CanFrameSchedule
from scdfe_c.web.service import ScadaStore
from scdfe_s.web.protocol_service import CanServerService
from tests.test_protocol_interface_contracts import eventually


class CanScheduleContracts(unittest.TestCase):
    def fixture(self, stack):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        store = ScadaStore(root/'scdfe_s.sqlite3'); stack.callback(store.close)
        channel = 'schedule-'+uuid.uuid4().hex
        store.create_row('fes_rtu_define',dict(rtu_id=1,channel_id=1,channel_type='can'))
        store.create_row('fes_channel_can_define',dict(channel_id=1,interface='virtual',channel=channel))
        for ident,mode,period,repeat in [(1,'periodic',20,3),(2,'manual',0,1),(3,'replay',10,1),(4,'replay',10,2)]:
            store.create_row('fes_channel_can_frame_define',dict(id=ident,channel_id=1,
                arbitration_id=0x100+ident, data_hex='01 02', dlc=2, schedule_mode=mode,
                period_ms=period,repeat_count=repeat,sequence_no=ident,replay_group='test',enabled=1))
        peer = can.Bus(interface='virtual',channel=channel); stack.callback(peer.shutdown)
        return root,store,channel,peer

    def test_master_consumes_database_periodic_manual_and_replay_definitions(self):
        with ExitStack() as stack:
            root,store,channel,peer = self.fixture(stack)
            master = CanMaster.from_store(store,1); stack.callback(master.close); master.connect()
            self.assertEqual([peer.recv(1).arbitration_id for _ in range(3)], [0x101]*3)
            self.assertIsNone(peer.recv(.05))
            self.assertEqual(master.schedule_frames(frame_id=2)['queued'],1)
            self.assertEqual(peer.recv(1).arbitration_id,0x102)
            self.assertEqual(master.schedule_frames(replay_group='test')['queued'],3)
            self.assertEqual([peer.recv(1).arbitration_id for _ in range(3)],[0x103,0x104,0x104])
            self.assertTrue(eventually(lambda: master.diagnostics['frame_schedule']['sent']==7))
            master.close()
            self.assertFalse(master.diagnostics['frame_schedule']['running'])
            self.assertIsNone(peer.recv(.05))

    def test_s_server_consumes_same_definitions_and_rejects_invalid_frames(self):
        with ExitStack() as stack:
            root,store,channel,peer = self.fixture(stack)
            service = CanServerService(store,config_path=root/'can.json'); stack.callback(service.close)
            service.configure(dict(rtu_id=1,channel=channel,simulation=1))
            service.start()
            self.assertEqual([peer.recv(1).arbitration_id for _ in range(3)], [0x101]*3)
            service.schedule_frames(replay_group='test')
            self.assertEqual([peer.recv(1).arbitration_id for _ in range(3)],[0x103,0x104,0x104])
            with self.assertRaises(ValueError):
                service._engine.send_frame(0x900,'01',is_extended_id=False)

    def test_invalid_injection_and_fd_fail_before_sending(self):
        sent=[]
        for row in [dict(id=1,arbitration_id=1,schedule_mode='inject'),
                    dict(id=1,arbitration_id=1,schedule_mode='periodic',period_ms=0),
                    dict(id=1,arbitration_id=1,is_fd=1)]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                CanFrameSchedule(lambda *args,**kw:sent.append(args),[row])
        self.assertEqual(sent,[])

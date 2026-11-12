"""Regression contracts for the audited IEC104 interfaces (private fixtures)."""
from contextlib import ExitStack
from pathlib import Path
import socket
import tempfile
import time
import unittest
from datetime import datetime, timezone
import c104
import threading
import signal
from unittest.mock import patch

from client.iec104 import IEC104Master
from server.iec104_server import IEC104Server
from scdfe_c.web.service import ScadaStore
from scdfe_s.web.iec104_service import IEC104Service
from scdfe_t.trans import TransRuntime, TransPoint, TransSpec


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return bool(predicate())


class RecordingWriter:
    def __init__(self): self.calls = []
    def submit_command(self, *args, **kwargs): self.calls.append(args); return True
    def close(self): pass


class IEC104InterfaceContracts(unittest.TestCase):
    def fixture(self, stack, store_class=ScadaStore):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='scada-interface-')))
        store = store_class(root / 'scdfe_s.sqlite3')
        stack.callback(store.close)
        store.create_row('fes_rtu_define', dict(rtu_id=1, channel_id=1, channel_type='iec104', process_enable=0))
        store.create_row('fes_channel_iec104_define', dict(id=1, channel_id=1, rtu_addr=1,
                         remote_host='127.0.0.1', remote_port=free_port()))
        for kind, typ, ioa, value in [('yc', 13, 0x4001, 10), ('yx', 1, 1, 0),
                                      ('yk', 45, 0x6001, 0), ('yt', 50, 0x6201, 0)]:
            store.create_row(f'fes_{kind}_define', dict(rtu_id=1, pnt_no=1, value=value, status=0))
            store.create_row('fes_channel_iec104_point_define', dict(channel_id=1, kind=kind,
                             pnt_no=1, io_address=ioa, type_id=typ))
        return root, store

    def test_s_quality_only_changes_and_recovery_reach_client(self):
        with ExitStack() as stack:
            root, store = self.fixture(stack)
            listen = free_port()
            service = IEC104Service(store, config_path=root/'service.json')
            stack.callback(service.close)
            service.configure(dict(rtu_id=1, port=listen)); service.start()
            peer = IEC104Master('127.0.0.1', listen, init='NONE')
            stack.callback(peer.close)
            peer.add_point('yc', 1); peer.add_point('yx', 1)
            peer.connect(timeout=3); peer.general_interrogation()
            self.assertTrue(eventually(lambda: peer.value('yc', 1).value == 10))
            for status in (128, 16, 0):
                for kind in ('yc', 'yx'):
                    store.update_row(f'fes_{kind}_define', [1, 1], dict(status=status))
                self.assertTrue(eventually(lambda: all(peer.value(k, 1).quality == status for k in ('yc', 'yx'))))
                self.assertEqual(peer.value('yc', 1).value, 10)
                self.assertFalse(peer.value('yx', 1).value)

    def test_t_deactivation_does_not_submit_another_execution(self):
        with ExitStack() as stack:
            # T requires the business database name, independent of S fixtures.
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='scada-t-interface-')))
            runtime = TransRuntime(root/'scada.sqlite3')
            stack.callback(runtime.store.close)
            writer = RecordingWriter(); runtime.writer = writer
            point = TransPoint.from_row(dict(channel_id=1, pnt_no=1, kind='yk', source_rtu_id=1,
                         source_pnt_no=1, type_id=45, command_mode='SELECT_AND_EXECUTE'))
            listen = free_port()
            server = runtime.build_server(TransSpec(1,'iec104','127.0.0.1',listen,1,1,.05), [point])
            server.start(); stack.callback(server.stop)
            peer = IEC104Master('127.0.0.1',listen,init='NONE'); stack.callback(peer.close)
            peer.configure([dict(kind='yk',pnt_no=1,type_id=45,command_mode='SELECT_AND_EXECUTE')])
            peer.connect(timeout=3); peer.send_yk_point(1,True)
            self.assertEqual(len(writer.calls),1)
            peer.cancel_yk_point(1,True)
            self.assertEqual(len(writer.calls),1)

    def test_cp56_command_type_conversion(self):
        for cls in (IEC104Master, IEC104Server):
            for kind, typ, expected in [('yk',45,58),('yk',46,59),('yt',48,61),('yt',49,62),('yt',50,63)]:
                with self.subTest(adapter=cls.__name__,kind=kind,type_id=typ):
                    self.assertEqual(cls._canonical_type_id(kind,typ,'CP56Time2a'), expected)
                    with self.assertRaisesRegex(ValueError, 'CP24'):
                        cls._canonical_type_id(kind,typ,'CP24Time2a')

    def test_cp56_commands_and_setpoint_deactivation_on_wire(self):
        with ExitStack() as stack:
            port = free_port()
            server = IEC104Server(ip='127.0.0.1', port=port)
            events, frames = [], []
            server.set_raw_handlers(on_receive=lambda data: frames.append(bytes(data)))
            definitions = []
            for no, (kind, typ, value) in enumerate([
                    ('yk',45,True), ('yk',46,False), ('yt',48,.5),
                    ('yt',49,123), ('yt',50,12.5)], 1):
                server.add_point(kind, no, data_type=typ, time_tag='CP56Time2a',
                                 command_handler=lambda event: events.append(event) or True)
                definitions.append(dict(kind=kind, pnt_no=no, type_id=typ, time_tag='CP56Time2a', qualifier=1))
            server.start(); stack.callback(server.stop)
            peer = IEC104Master('127.0.0.1', port, init='NONE'); stack.callback(peer.close)
            peer.configure(definitions); peer.connect(timeout=3)
            now = datetime.now(timezone.utc)
            for definition, value in zip(definitions, (True, False, .5, 123, 12.5)):
                kind, no = definition['kind'], definition['pnt_no']
                getattr(peer, f'send_{kind}_point')(no, value, recorded_at=now)
                self.assertEqual(events[-1].cause, c104.Cot.ACTIVATION)
                getattr(peer, f'cancel_{kind}_point')(no, value, recorded_at=now)
                self.assertEqual(events[-1].cause, c104.Cot.DEACTIVATION)
            self.assertEqual(len(events), 10)
            types = [frame[6] for frame in frames if len(frame)>6 and frame[2]&1 == 0]
            self.assertEqual(types, [58,58,59,59,61,61,62,62,63,63])
            for frame in frames:
                if len(frame)>6 and frame[2]&1 == 0:
                    qualifier = (frame[-8] >> 2) & 31 if frame[6] in (58,59) else frame[-8] & 127
                    self.assertEqual(qualifier,1,frame.hex())

    def test_explicit_testfr_does_not_send_application_command(self):
        peer = IEC104Master('127.0.0.1', free_port(), init='NONE')
        self.addCleanup(peer.close)
        with self.assertRaisesRegex(NotImplementedError, 'test_command'):
            peer.test_fr()

    def test_cli_uses_autonomous_database_sync_and_command_receipts(self):
        from scdfe_s import run_iec104_server as cli
        with ExitStack() as stack:
            root, store = self.fixture(stack, cli.SimulatorStore)
            port = free_port()
            callbacks, errors = {}, []
            ready = threading.Event()
            def run():
                try: cli.main()
                except Exception as exc: errors.append(exc); ready.set()
            with patch.object(cli.sys, 'argv', ['run_iec104_server','--db',str(store.db_path),
                              '--rtu-id','1','--port',str(port)]), \
                 patch.object(cli.signal,'signal',side_effect=lambda sig,fn:callbacks.update({sig:fn})), \
                 patch('builtins.print',side_effect=lambda *a,**kw:ready.set()):
                worker = threading.Thread(target=run); worker.start()
                try:
                    self.assertTrue(ready.wait(5)); self.assertEqual(errors,[])
                    peer = IEC104Master('127.0.0.1',port,init='NONE'); stack.callback(peer.close)
                    peer.add_point('yc',1); peer.add_point('yk',1); peer.connect(timeout=3)
                    store.update_row('fes_yc_define',[1,1],dict(value=54.25,status=16))
                    self.assertTrue(eventually(lambda: peer.value('yc',1).value==54.25 and peer.value('yc',1).quality==16))
                    peer.send_yk_point(1,True)
                    self.assertEqual(store.get_row('fes_yk_define',[1,1])['value'],1)
                    self.assertGreater(store.get_row('fes_yk_define',[1,1])['cmd_handle_time'],0)
                    peer.close()
                finally:
                    if signal.SIGINT in callbacks: callbacks[signal.SIGINT]()
                    worker.join(5)
                self.assertFalse(worker.is_alive()); self.assertEqual(errors,[])

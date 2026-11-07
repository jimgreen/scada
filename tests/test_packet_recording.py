"""Three independent default-off switches, live application and persisted API."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from core.packet_logging import PacketLogSwitch, packet_log_switch
from core.service_log import ServiceFileLog
from scdfe_c import ChannelWorker, ChannelSpec
from scdfe_c.web.service import ScadaStore
from scdfe_c.web.server import make_server as c_web
from scdfe_s.web.server import make_server as s_web
from scdfe_s.database import SimulatorStore
from scdfe_t.web.server import make_server as t_web
from scdfe_t import TransRuntime


class PacketRecordingTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.store=ScadaStore(self.root/'db.sqlite3')
        self.sim = SimulatorStore(self.root/'scdfe_s.sqlite3')
        self.addCleanup(self.sim.close)

    def test_default_off_persistence_external_change_and_invalid_configuration(self):
        switch=PacketLogSwitch(self.store.db_path,'c')
        self.assertFalse(switch.enabled())
        switch.set_enabled(True)
        other=PacketLogSwitch(self.store.db_path,'c')
        self.assertTrue(other.enabled())
        other.set_enabled(False)
        self.assertFalse(switch.enabled(refresh=True))
        switch.path.write_text('{invalid',encoding='utf-8')
        self.assertFalse(switch.enabled(refresh=True))
        with self.assertRaises(ValueError): switch.set_enabled(1)

    def test_channel_overrides_fallback_persistence_and_independent_writers(self):
        switch = PacketLogSwitch(self.store.db_path, 'c')
        other = PacketLogSwitch(self.store.db_path, 'c')
        first, second = switch.for_channel(107), switch.for_channel(108)
        self.assertFalse(first.enabled())
        switch.set_channel(107, True)
        self.assertTrue(first.enabled())
        self.assertFalse(second.enabled())
        other.set_channel(108, False)
        other.set_enabled(True)
        self.assertTrue(first.enabled(refresh=True))
        self.assertFalse(second.enabled())
        switch.set_channel(107, None)
        self.assertTrue(first.enabled())
        switch.set_enabled(False)
        self.assertFalse(first.enabled())
        self.assertEqual(PacketLogSwitch(self.store.db_path, 'c').snapshot()['channels'], {'108': False})
        with self.assertRaises(ValueError): switch.set_channel(107, 'false')
        with self.assertRaises(ValueError): switch.for_channel(True)

    def test_three_channel_apis_and_bound_loggers(self):
        for cid in (107, 108):
            self.store.create_row('fes_rtu_define', {'rtu_id': cid-100, 'channel_id': cid,
                                  'channel_type': 'modbus-tcp', 'name': f'Test {cid}'})
            self.sim.create_row('fes_rtu_define', {'rtu_id': cid-100, 'channel_id': cid, 'channel_type': 'modbus-tcp'})
            self.store.create_row('fes_scdfe_t_define', {'channel_id': cid, 'protocol': 'modbus-tcp'})
        for family, factory in [('c', c_web), ('s', s_web), ('t', t_web)]:
            with self.subTest(family=family):
                web = factory('127.0.0.1', 0, db_path=self.sim.db_path if family == 's' else self.store.db_path)
                thread = threading.Thread(target=web.serve_forever, daemon=True)
                thread.start()
                def api(path='', method='GET', body=None, status=200):
                    req = Request(f'http://127.0.0.1:{web.server_port}/api/packet-recording'+path,
                                  method=method, data=None if body is None else json.dumps(body).encode(),
                                  headers={'Content-Type': 'application/json'})
                    try: response = urlopen(req, timeout=5)
                    except HTTPError as error: response = error
                    with response:
                        self.assertEqual(response.status, status)
                        return json.load(response)
                try:
                    self.assertEqual([row['channel_id'] for row in api()['channels']], [107, 108])
                    if family == 'c':
                        owners = [ChannelWorker(self.store, ChannelSpec(cid-100, cid, 'modbus-tcp'),
                                                log_dir=self.root/'logs') for cid in (107,108)]
                        def emit(i, marker): owners[i].logger.packet('RX', b'\x01', marker)
                    elif family == 't':
                        owners = [TransRuntime(self.store.db_path, channel_id=cid) for cid in (107,108)]
                        def emit(i, marker): owners[i].logger.packet('TX', b'\x01', marker)
                    else:
                        owner = ServiceFileLog(self.sim, 'channel-test')
                        def emit(i, marker): owner.write('PACKET', marker, channel_id=107+i)
                    def contents():
                        files = list((self.root/'logs').rglob(f'scdfe_{family}-*.log'))
                        files.extend((self.root/'packets').rglob(f'scdfe_{family}-*.log'))
                        return ''.join(p.read_text(encoding='utf-8') for p in files)
                    api('/channels/107', 'PUT', {'enabled': True})
                    emit(0, 'first-saved'); emit(1, 'second-hidden')
                    self.assertIn('first-saved', contents())
                    self.assertNotIn('second-hidden', contents())
                    self.assertFalse(api()['enabled'])
                    api('/channels/108', 'PUT', {'enabled': False})
                    api('', 'PUT', {'enabled': True})
                    emit(1, 'still-hidden')
                    self.assertNotIn('still-hidden', contents())
                    result = api('/channels/108', 'PUT', {'enabled': None})
                    self.assertIsNone(result['override'])
                    self.assertTrue(result['enabled'])
                    emit(1, 'second-saved')
                    self.assertIn('second-saved', contents())
                    api('/channels/107', 'PUT', {'enabled': False})
                    saved = contents(); emit(0, 'first-hidden')
                    self.assertEqual(contents(), saved)
                    api('/channels/999', 'PUT', {'enabled': True}, 404)
                    api('/channels/107', 'PUT', {'enabled': 'false'}, 400)
                    api('/channels/107', 'PUT', {'enabled': True, 'extra': 1}, 400)
                    api('', 'PUT', {'enabled': None}, 400)
                finally:
                    web.shutdown(); thread.join(5); web.server_close()

    def test_concurrent_independent_settings_do_not_lose_other_channels(self):
        from concurrent.futures import ThreadPoolExecutor
        barrier = threading.Barrier(8)
        def save(cid):
            switch = PacketLogSwitch(self.store.db_path, 's')
            barrier.wait(timeout=5)
            switch.set_channel(cid, bool(cid % 2))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(save, range(8)))
        self.assertEqual(PacketLogSwitch(self.store.db_path, 's').snapshot()['channels'],
                         {str(cid): bool(cid % 2) for cid in range(8)})

    @unittest.skipUnless(os.name == 'nt', 'Windows byte-range locking')
    def test_first_settings_writer_does_not_write_into_an_owned_empty_lock_file(self):
        import msvcrt
        from core.scdfe_control import InstanceLock
        lock = InstanceLock(self.store.db_path, 'packet-log-s')
        with lock.path.open('a+b') as owner:
            msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                self.assertFalse(lock.acquire())
                self.assertEqual(lock.path.stat().st_size, 0)
            finally:
                msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
        self.assertTrue(lock.acquire())
        lock.release()
        self.assertEqual(lock.path.read_bytes(), bytes(1))

    def test_s_iec104_uses_channel_identity_instead_of_rtu_identity(self):
        from scdfe_s.web.iec104_service import IEC104Service
        self.store.create_row('fes_rtu_define', {'rtu_id': 7, 'channel_id': 107, 'channel_type': 'iec104'})
        self.store.create_row('fes_channel_iec104_define', {'id': 1, 'rtu_id': 7, 'rtu_addr': 1,
                              'remote_host': '127.0.0.1', 'remote_port': 25141})
        service = IEC104Service(self.store)
        self.addCleanup(service.close)
        service.configure({'rtu_id': 7})
        server = service._build_server()
        self.addCleanup(server.stop)
        switch = packet_log_switch(self.store.db_path, 's')
        switch.set_channel(7, True)
        service._on_receive_raw(b'\x68\x04\x07\x00\x00\x00')
        def content():
            files = list((self.root/'logs').rglob('*.log'))
            files.extend((self.root/'packets').rglob('*.log'))
            return ''.join(p.read_text(encoding='utf-8') for p in files)
        self.assertNotIn('[报文]', content())
        switch.set_channel(107, True)
        service._on_receive_raw(b'\x68\x04\x07\x00\x00\x00')
        self.assertIn('[报文]', content())
        switch.set_channel(107, False)
        saved = content()
        service._on_send_raw(b'\x68\x04\x0b\x00\x00\x00')
        self.assertEqual(content(), saved)

    def test_three_apis_switch_existing_loggers_without_removing_history(self):
        worker=ChannelWorker(self.store,ChannelSpec(7,7,'modbus-tcp','test'),log_dir=self.root/'logs')
        trans=TransRuntime(self.store.db_path,channel_id=7)
        service=ServiceFileLog(self.sim,'modbus-tcp-server')
        writers={
            'c':lambda marker:worker.logger.packet('RX',marker.encode(),'test'),
            's':lambda marker:service.write('PACKET','报文 RX '+marker),
            't':lambda marker:trans.logger.packet('RX',marker.encode(),'test'),
        }
        normals={'c':worker.logger.normal,'s':lambda text:service.write('INFO',text),'t':trans.logger.normal}
        for family,factory in [('c',c_web),('s',s_web),('t',t_web)]:
            with self.subTest(family=family):
                web=factory('127.0.0.1',0,db_path=self.sim.db_path if family == 's' else self.store.db_path)
                thread=threading.Thread(target=web.serve_forever,daemon=True);thread.start()
                def api(method='GET',body=None,status=200):
                    req=Request(f'http://127.0.0.1:{web.server_port}/api/packet-recording',method=method,
                                data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
                    try: response=urlopen(req,timeout=5)
                    except HTTPError as error:response=error
                    with response:
                        self.assertEqual(response.status,status)
                        return json.load(response)
                try:
                    self.assertFalse(api()['enabled'])
                    writers[family]('disabled-before')
                    normals[family]('ordinary-before')
                    def all_paths():
                        files = list((self.root/'logs').rglob(f'scdfe_{family}-*.log'))
                        files.extend((self.root/'packets').rglob(f'scdfe_{family}-*.log'))
                        return files
                    paths=all_paths()
                    self.assertTrue(paths)
                    self.assertNotIn('[报文]', ''.join(p.read_text(encoding='utf-8') for p in paths))
                    self.assertTrue(api('PUT',{'enabled':True})['enabled'])
                    writers[family]('enabled')
                    saved=''.join(p.read_text(encoding='utf-8') for p in all_paths())
                    self.assertIn('[报文]',saved)
                    self.assertTrue(PacketLogSwitch(self.sim.db_path if family=='s' else self.store.db_path,family).enabled())
                    api('PUT',{'enabled':'false'},400)
                    self.assertTrue(api()['enabled'])
                    self.assertFalse(api('PUT',{'enabled':False})['enabled'])
                    writers[family]('disabled-after')
                    self.assertEqual(''.join(p.read_text(encoding='utf-8') for p in all_paths()),saved)
                    normals[family]('ordinary-after')
                    self.assertIn('ordinary-after',''.join(p.read_text(encoding='utf-8') for p in all_paths()))
                    for other in {'c','s','t'}-{family}:
                        self.assertFalse(packet_log_switch(self.sim.db_path if other=='s' else self.store.db_path,other).enabled(refresh=True))
                finally:
                    web.shutdown();thread.join(5);web.server_close()


if __name__=='__main__':unittest.main()

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from scdfe_c.web.server import make_server as make_c
from scdfe_s.web.server import make_server as make_s
from scdfe_t.web.server import make_server as make_t


class CapabilityApiContracts(unittest.TestCase):
    def test_all_components_report_actual_scope(self):
        for component, maker in [('scdfe_c',make_c),('scdfe_s',make_s),('scdfe_t',make_t)]:
            with self.subTest(component=component), tempfile.TemporaryDirectory() as directory:
                server = maker('127.0.0.1',0,db_path=Path(directory)/'scada.sqlite3')
                thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
                client = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                try:
                    client.request('GET','/api/protocol-capabilities')
                    response = client.getresponse(); body = json.loads(response.read())
                    self.assertEqual(response.status,200)
                    self.assertEqual(body['component'],component)
                    iec = body['protocols']['iec104']
                    self.assertFalse(iec['cp24']); self.assertFalse(iec['explicit_u_testfr'])
                    self.assertTrue(iec['soe_event_log'])
                    self.assertFalse(iec['soe_lossless_forwarding'])
                    self.assertEqual(iec['monitoring_semantics'],'latest_point_snapshot_and_event_journal')
                    self.assertEqual(iec['soe_forward_types'],[30,31])
                    self.assertEqual(iec['soe_delivery'],'at_least_once_apci_ack')
                    self.assertEqual(iec['max_asdu_length_scope'],'diagnostic_threshold')
                    if component == 'scdfe_t':
                        self.assertEqual(set(body['protocols']),{'iec104','modbus-tcp'})
                        self.assertEqual(iec['confirmation_scope'],'database_commit')
                        self.assertEqual(body['protocols']['modbus-tcp']['confirmation_scope'],'database_commit')
                        self.assertTrue(body['protocols']['modbus-tcp']['atomic_request_database_write'])
                        self.assertFalse(body['protocols']['modbus-tcp']['requires_c_process'])
                    else:
                        self.assertFalse(body['protocols']['can']['uds_stack'])
                    if component == 'scdfe_s':
                        for protocol in ('modbus-tcp', 'modbus-rtu'):
                            capability = body['protocols'][protocol]
                            self.assertTrue(capability['register_slices'])
                            self.assertTrue(capability['atomic_request_database_write'])
                            self.assertEqual(capability['confirmation_scope'], 'simulator_database')
                finally:
                    client.close(); server.shutdown(); thread.join(3); server.server_close()

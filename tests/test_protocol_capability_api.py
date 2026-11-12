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
                    self.assertEqual(iec['max_asdu_length_scope'],'diagnostic_threshold')
                    if component == 'scdfe_t':
                        self.assertEqual(set(body['protocols']),{'iec104','modbus-tcp'})
                        self.assertEqual(iec['confirmation_scope'],'writer_commit')
                    else:
                        self.assertFalse(body['protocols']['can']['uds_stack'])
                finally:
                    client.close(); server.shutdown(); thread.join(3); server.server_close()

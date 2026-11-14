import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from scdfe_c.web.server import make_server as make_c
from scdfe_s.web.server import make_server as make_s
from scdfe_t.web.server import make_server as make_t


class SoeApiTests(unittest.TestCase):
    def test_all_components_support_bounded_keyset_queries(self):
        for component,maker in [('c',make_c),('s',make_s),('t',make_t)]:
            with self.subTest(component=component), tempfile.TemporaryDirectory() as directory:
                server=maker('127.0.0.1',0,db_path=Path(directory)/'db.sqlite3')
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                try:
                    store=server.trans_store.store if component=='t' else server.store
                    with store.connection() as con:
                        for i in range(4):
                            con.execute('''INSERT INTO _scada_iec104_soe(origin,channel_id,rtu_id,pnt_no,
                                common_address,io_address,type_id,raw_state,quality,cot,received_time_ms)
                                VALUES(?,1,?,1,1,1,30,1,0,3,1)''',('S' if component=='s' else 'C',1+i%2))
                    def request(path,method='GET'):
                        client.request(method,path);response=client.getresponse()
                        return response.status,response.read()
                    status,body=request('/api/soe?after_id=1&limit=2')
                    self.assertEqual(status,200);result=json.loads(body)
                    self.assertEqual([r['event_id'] for r in result['items']],[2,3])
                    self.assertTrue(result['has_more']);self.assertEqual(result['next_after_id'],3)
                    _,body=request('/api/soe?tail=1&limit=2&rtu_id=1')
                    self.assertEqual([r['event_id'] for r in json.loads(body)['items']],[1,3])
                    _,body=request('/api/soe?after_id=4')
                    self.assertEqual(json.loads(body)['items'],[])
                    for limit in (10,100,1000):
                        status,body=request(f'/api/soe?limit={limit}')
                        self.assertEqual(status,200)
                        self.assertEqual(json.loads(body)['limit'],limit)
                    self.assertEqual(request('/api/soe?rtu_id=123&after_id=123456')[0],200)
                    for suffix in ('limit=0','limit=1001','tail=1&after_id=1','after_id=-1','rtu_id=0',
                                   'unknown=1','limit=1&limit=2','after_id=99999999999999999999'):
                        self.assertEqual(request('/api/soe?'+suffix)[0],400,suffix)
                    self.assertEqual(request('/api/soe','POST')[0],405)
                    status,body=request('/soe')
                    self.assertEqual(status,200);self.assertIn('SOE 事件顺序记录',body.decode())
                    self.assertNotIn('<script>',body.decode())
                    self.assertEqual(request('/static/soe.js')[0],200)
                    self.assertEqual(request('/static/soe.css')[0],200)
                finally:
                    client.close();server.shutdown();thread.join(3);server.server_close()


if __name__=='__main__':unittest.main()

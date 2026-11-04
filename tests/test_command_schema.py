from pathlib import Path
import sqlite3
import tempfile
import unittest
from scdfe_c.web.service import ScadaStore

class CommandServerSchemaTests(unittest.TestCase):
    def test_command_server_schema_and_singleton_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'scada.sqlite3'
            store=ScadaStore(db)
            schema=store.catalog.public('fes_scdfe_command_define')
            self.assertEqual(schema['primary_keys'], ['id'])
            self.assertEqual({c['eng_name'] for c in schema['columns']} & {'bind_host','bind_port','auth_token','process_enable','process_pid'},
                             {'bind_host','bind_port','auth_token','process_enable','process_pid'})
            row=store.create_row('fes_scdfe_command_define', {
                'id':1,'bind_host':'127.0.0.1','bind_port':8890,'auth_token':'',
                'process_enable':0,'process_status':0,'process_pid':0,
                'process_start_time':0,'process_stop_time':0,'process_heartbeat_time':0,
                'process_exit_code':0,'process_error':'','log_retention_hours':720,
            })
            self.assertEqual(row['bind_port'], 8890)
            con=sqlite3.connect(db)
            self.assertEqual(con.execute("select count(*) from fes_scdfe_command_define").fetchone()[0], 1)
            con.close()

if __name__ == '__main__': unittest.main()

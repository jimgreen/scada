"""Multi-RTU mapping writes and partial IEC104 batch publication contracts."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sqlite3
import tempfile
import unittest

from scdfe_c.web.service import ApiError
from scdfe_t.web.service import TransStore
from scdfe_t.trans import TransRuntime
from server.iec104_server import IEC104Server


class MappingBatchTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.service=TransStore(Path(tmp.name)/'scada.sqlite3',connection_pool_size=1)
        self.addCleanup(self.service.store.close)
        self.service.create_channel(dict(channel_id=1,protocol='iec104',bind_host='127.0.0.1',bind_port=25441))
        self.sql=[]
        with self.service.store.connection() as con:
            con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,99)
            con.set_trace_callback(self.sql.append)

    def test_batch_reads_are_bounded_and_preserve_mixed_kinds_order(self):
        items=[dict(source_rtu_id=i,source_pnt_no=1,kind='yc' if i%2 else 'yx',name=f'点{i}') for i in range(1,81)]
        rows=self.service.create_points_batch(1,dict(items=items))
        self.assertEqual([r['source_rtu_id'] for r in rows],list(range(1,81)))
        self.assertEqual([r['pnt_no'] for r in rows],list(range(1,81)))
        self.assertEqual([r['kind'] for r in rows],[r['kind'] for r in items])
        self.assertLessEqual(sum(q.startswith(('SELECT','WITH')) for q in self.sql),6)
        self.assertTrue(all(r['_version']==1 for r in rows))

    def test_last_insert_failure_rolls_back_all_and_number_allocation(self):
        with self.service.store.connection() as con:
            con.execute("CREATE TRIGGER reject_mapping BEFORE INSERT ON fes_scdfe_t_point_define WHEN NEW.source_rtu_id=3 BEGIN SELECT RAISE(ABORT,'fixture'); END")
            con.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.create_points_batch(1,dict(items=[dict(kind='yc',source_rtu_id=r,source_pnt_no=1) for r in (1,2,3)]))
        self.assertEqual(self.service.points(1),[])
        row=self.service.create_points_batch(1,dict(items=[dict(kind='yc',source_rtu_id=1,source_pnt_no=1)]))[0]
        self.assertEqual(row['pnt_no'],1)

    def test_source_detail_can_skip_global_counts_without_changing_legacy_response(self):
        with self.service.store.connection() as con:
            for n in (1,2):
                con.execute("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,'modbus-tcp')",(n,n))
                con.execute('INSERT INTO fes_yc_define(rtu_id,pnt_no,value) VALUES(?,1,42)',(n,))
            con.commit()
        full=self.service.source_catalog('yc',1)
        self.assertEqual(len(full['rtus']),2)
        self.sql.clear()
        detail=self.service.source_catalog('yc',1,include_rtus=False)
        self.assertEqual(detail['items'],full['items'])
        self.assertEqual(detail['rtus'],[])
        self.assertEqual(sum(q.startswith('SELECT') for q in self.sql),1)


class ForwardBatchTests(unittest.TestCase):
    def test_native_batch_isolates_bad_points_and_failed_type_groups(self):
        server=IEC104Server(ip='127.0.0.1',port=25442)
        native=server._server
        self.addCleanup(native.stop)
        server.add_point('yc',1,value=0);server.add_point('yc',2,value=0)
        server.add_point('yx',1,value=False)
        server._server=Mock(transmit_batch=Mock(side_effect=[False,True]),active_connection_count=1)
        errors=server.update_monitoring([('yc',1,8,128),('yc',2,'bad',0),('yx',1,True,64)],isolate_errors=True)
        self.assertEqual(set(errors),{('yc',1),('yc',2)})
        self.assertEqual(server.monitoring_snapshot()[('yx',1)],(True,64))
        self.assertEqual(server._server.transmit_batch.call_count,2)

    def test_runtime_only_advances_successful_samples_and_retries_unchanged_memory(self):
        server=IEC104Server(ip='127.0.0.1',port=25443)
        native=server._server; self.addCleanup(native.stop)
        for no in (1,2):server.add_point('yc',no,value=0)
        transport=Mock(transmit_batch=Mock(side_effect=[False,True]),active_connection_count=1)
        server._server=transport
        runtime=object.__new__(TransRuntime)
        runtime.server=server;runtime.points={('yc',n):SimpleNamespace(kind='yc',pnt_no=n) for n in (1,2)}
        runtime.values={('yc',n):0 for n in (1,2)};runtime.qualities={('yc',n):0 for n in (1,2)}
        runtime._rx_count=runtime._tx_count=0;runtime._scan_plan=()
        runtime._record_sample=Mock();runtime._queue_point_runtime=Mock();runtime._flush_point_runtime=Mock()
        runtime._runtime_error=Mock();runtime.logger=Mock()
        runtime._read_sources=Mock(return_value={('yc',1):(7,128,12),('yc',2):(9,0,13)})
        runtime.refresh()
        self.assertEqual(runtime._tx_count,0)
        self.assertEqual(runtime.values[('yc',1)],0)
        runtime._queue_point_runtime.assert_not_called()
        runtime.refresh()
        self.assertEqual(runtime._tx_count,2)
        self.assertEqual(runtime.values[('yc',1)],7)
        runtime.refresh()
        self.assertEqual(transport.transmit_batch.call_count,2)
        self.assertEqual(runtime._queue_point_runtime.call_count,2)

"""Durable SOE contracts, separate from the current-value point tables."""
from pathlib import Path
import tempfile
import unittest

from scdfe_c.web.service import ScadaStore
from scdfe_s.database import SimulatorStore
from tools.verify_iec104_soe import fill_one, cp56, EPOCH


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def store(self, cls=SimulatorStore):
        store = cls(Path(self.tmp.name) / ('s.sqlite3' if cls is SimulatorStore else 'c.sqlite3'))
        self.addCleanup(store.close)
        fill_one(store, 2404)
        return store

    def test_s_records_every_committed_value_or_quality_change_only(self):
        store = self.store()
        with store.connection() as con:
            for value in (1, 0, 1):
                con.execute('UPDATE fes_yx_define SET value=?', (value,))
            con.execute('UPDATE fes_yx_define SET status=128')
            con.execute('UPDATE fes_yx_define SET value=1, status=128')
            con.execute('BEGIN')
            con.execute('UPDATE fes_yx_define SET value=0')
            con.rollback()
            rows = con.execute('SELECT * FROM _scada_iec104_soe ORDER BY event_id').fetchall()
        self.assertEqual([(r['raw_state'], r['quality']) for r in rows], [(1,0),(0,0),(1,0),(1,128)])
        self.assertTrue(all(r['event_time_ms'] > 0 for r in rows))
        self.assertEqual([r['type_id'] for r in rows], [30]*4)
        store.close()
        reopened = SimulatorStore(store.db_path)
        self.addCleanup(reopened.close)
        with reopened.connection() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM _scada_iec104_soe').fetchone()[0], 4)

    def test_c_latest_value_updates_do_not_create_simulator_events(self):
        store = self.store(ScadaStore)
        store.update_row('fes_yx_define', [1,1], {'value':1})
        with store.connection() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM _scada_iec104_soe').fetchone()[0], 0)

    def test_receiver_does_not_journal_periodic_or_interrogation_snapshots(self):
        from types import SimpleNamespace
        from core.iec104_soe import SoeReceiver
        store=self.store(ScadaStore)
        receiver=SoeReceiver(store,1,1,{(1,30):1})
        self.addCleanup(receiver.close)
        for cot in (1,20,0x43,3):
            asdu=bytes((30,1,cot,0,1,0,1,0,0,1))+cp56(EPOCH)
            receiver.receive(SimpleNamespace(raw=b'\x68'+bytes((len(asdu)+4,))+b'\0'*4+asdu))
        with store.connection() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM _scada_iec104_soe').fetchone()[0],1)

    def test_raw_event_roundtrip_preserves_unknown_dp_and_time_flags(self):
        from core.iec104_soe import decode_events, encode_events
        raw_time = cp56(EPOCH, invalid=True, summer=True, substituted=True)
        asdu = bytes((31,2,0x83,7,1,0)) + b'\x01\0\0\xb0' + raw_time + b'\x01\0\0\xb3' + raw_time
        rows = decode_events(asdu)
        self.assertEqual([r['raw_state'] for r in rows], [0,3])
        self.assertEqual(rows[0]['quality'], 0xb0)
        self.assertTrue(rows[0]['time_invalid'] and rows[0]['summer'] and rows[0]['time_substituted'])
        self.assertEqual(encode_events(rows), [asdu])
        self.assertRaises(ValueError, decode_events, asdu[:-1])

    def test_forward_cursor_advances_after_ack_only_and_survives_retry(self):
        from core.iec104_soe import SoeForwarder
        store = self.store()
        for value in (1,0,1):
            store.update_row('fes_yx_define', [1,1], {'value':value})
        class Sender:
            state = 0
            sent = []
            def submit_soe(self, events):
                self.sent.append(events)
                return True
            def poll_soe(self): return self.state
        sender = Sender()
        route = lambda row: [row]
        forwarder = SoeForwarder(store, 'test', 'S', route, rtu_id=1)
        self.addCleanup(forwarder.close)
        forwarder.tick(sender)
        self.assertEqual(forwarder.last_event_id, 0)
        sender.state = -1
        forwarder.tick(sender)
        self.assertEqual(forwarder.last_event_id, 0)
        sender.state = 0
        forwarder.tick(sender)
        sender.state = 1
        forwarder.tick(sender)
        self.assertEqual(forwarder.last_event_id, 3)
        forwarder.close()
        again = SoeForwarder(store, 'test', 'S', route, rtu_id=1)
        self.addCleanup(again.close)
        again.tick(sender)
        self.assertEqual(again.last_event_id, 3)
        self.assertEqual(len(sender.sent), 2)

    def test_acknowledged_cursor_commit_retries_without_resending_batch(self):
        from core.iec104_soe import SoeForwarder
        store=self.store()
        store.update_row('fes_yx_define',[1,1],{'value':1})
        class Sender:
            sent=0
            polled=0
            def submit_soe(self,events): self.sent+=1;return True
            def poll_soe(self): self.polled+=1;return 1 if self.polled==1 else -1
        sender=Sender();forwarder=SoeForwarder(store,'test','S',lambda row:[row],rtu_id=1)
        self.addCleanup(forwarder.close);forwarder.tick(sender)
        with store.connection() as con:
            con.execute("CREATE TRIGGER fail_cursor BEFORE INSERT ON _scada_iec104_soe_cursor BEGIN SELECT RAISE(ABORT,'test full'); END")
        with self.assertRaisesRegex(Exception,'test full'):forwarder.tick(sender)
        self.assertEqual(forwarder.last_event_id,0)
        with store.connection() as con:con.execute('DROP TRIGGER fail_cursor')
        forwarder.tick(sender)
        self.assertEqual((forwarder.last_event_id,sender.sent,sender.polled),(1,1,1))


if __name__ == '__main__': unittest.main()

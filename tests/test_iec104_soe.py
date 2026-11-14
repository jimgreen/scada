"""SOE frame decoding and database type registration (not full SOE acceptance)."""
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from client.iec104 import IEC104Master
from scdfe_c.web.service import ScadaStore
from tools.verify_iec104_soe import EPOCH, cp56, decode, fill_one


class SOEContracts(unittest.TestCase):
    def test_independent_decoder_preserves_flags_quality_ca_ioa_and_milliseconds(self):
        stamp=EPOCH+timedelta(milliseconds=101)
        data=bytes((31,1,3,5,0x34,0x12))+bytes((3,2,1,0xb3))+cp56(stamp,invalid=True,summer=True,substituted=True)
        frame=bytes((104,len(data)+4))+b'\0'*4+data
        row=decode(frame)[0]
        self.assertEqual((row['ca'],row['ioa'],row['state'],row['quality']),(0x1234,0x010203,3,0xb0))
        self.assertEqual(row['source_time'],stamp.isoformat())
        self.assertTrue(row['time_invalid'] and row['summer'] and row['time_substituted'])
        self.assertEqual(row['cot'],3)

    def test_sequential_objects_and_gi_are_distinct_from_spontaneous(self):
        data=bytes((30,130,20,0,1,0))+bytes((9,0,0,1))+cp56(EPOCH)+bytes((0,))+cp56(EPOCH)
        rows=decode(bytes((104,len(data)+4))+b'\0'*4+data)
        self.assertEqual([(r['ioa'],r['state'],r['cot']) for r in rows],[(9,1,20),(10,0,20)])

    def test_database_point_type_takes_precedence_over_channel_default(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ScadaStore(Path(directory)/'scada.sqlite3')
            try:
                fill_one(store,2404)
                for typ in (30,31,32,1,3,5):
                    with self.subTest(type=typ):
                        with store.connection() as con:
                            con.execute('UPDATE fes_channel_iec104_point_define SET type_id=?',(typ,));con.commit()
                        peer=IEC104Master.from_store(store,1,init='NONE')
                        try:self.assertEqual(peer._points['yx',1].type.value,typ)
                        finally:peer.close()
            finally:store.close()

    def test_explicit_overrides_and_unmapped_default_still_work(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ScadaStore(Path(directory)/'scada.sqlite3')
            try:
                fill_one(store,2404)
                for override,expected in (({'data_type':3},3),({'type_id':31},31)):
                    with self.subTest(override=override):
                        peer=IEC104Master.from_store(store,1,point_map={('yx',1):override})
                        try:self.assertEqual(peer._points['yx',1].type.value,expected)
                        finally:peer.close()
                with store.connection() as con:
                    con.execute('DELETE FROM fes_channel_iec104_point_define');con.commit()
                peer=IEC104Master.from_store(store,1)
                try:self.assertEqual(peer._points['yx',1].type.value,1)
                finally:peer.close()
            finally:store.close()


if __name__=='__main__':unittest.main()

"""The simulator must never edit or acquire ownership of a business database."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scdfe_c.web.service import DEFAULT_DB as SYSTEM_DB, ScadaStore
from scdfe_s.database import DEFAULT_DB, SimulatorStore
from scdfe_s.migrate import copy_definitions
from scdfe_s.web.server import make_server


class SimulatorDatabaseTests(unittest.TestCase):
    def test_wrong_database_is_rejected_in_both_directions(self):
        with tempfile.TemporaryDirectory() as root:
            business = Path(root) / "custom-business.sqlite3"
            simulator = Path(root) / "custom-simulator.sqlite3"
            self.assertNotEqual(DEFAULT_DB, SYSTEM_DB)
            with self.assertRaisesRegex(ValueError, "业务库"):
                make_server(port=0, db_path=SYSTEM_DB)
            with closing(ScadaStore(business)) as c:
                c.create_row("fes_rtu_define", {"rtu_id": 7, "channel_id": 10, "channel_type": "modbus-tcp"})
            with self.assertRaisesRegex(ValueError, "用途不匹配"):
                make_server(port=0, db_path=business)
            with closing(SimulatorStore(simulator)):
                pass
            with self.assertRaisesRegex(ValueError, "用途不匹配"):
                ScadaStore(simulator)
            with closing(ScadaStore(business)) as c:
                self.assertEqual(c.get_row("fes_rtu_define", [7])["channel_id"], 10)

    def test_copy_is_one_time_and_does_not_copy_live_process_or_command_state(self):
        with tempfile.TemporaryDirectory() as root:
            source, dest = (Path(root) / n for n in ("original", "simulator"))
            with closing(ScadaStore(source)) as c:
                c.create_row("fes_rtu_define", dict(rtu_id=7, channel_id=10, channel_type="modbus-tcp"))
                c.create_row("fes_yt_define", dict(rtu_id=7,pnt_no=1,name="definition",value=42))
                c.create_row("fes_channel_modbus_tcp_define", dict(rtu_id=7,remote_port=1502))
                with c.connection() as db:
                    db.execute("UPDATE fes_rtu_define SET process_pid=123 WHERE rtu_id=7")
                    db.execute("UPDATE fes_yt_define SET cmd_time=789 WHERE rtu_id=7")
                self.assertEqual(copy_definitions(source, dest)["tables"]["fes_yt_define"], 1)
                with self.assertRaises(FileExistsError):
                    copy_definitions(source, dest)
                with closing(SimulatorStore(dest)) as s:
                    self.assertFalse(s.get_row("fes_rtu_define",[7])["process_pid"])
                    point = s.get_row("fes_yt_define",[7,1])
                    self.assertEqual((point["name"],point["value"]),("definition",42))
                    self.assertFalse(point["cmd_time"])
                    self.assertEqual(s.list_rows("fes_channel_modbus_tcp_define")["items"][0]["remote_port"],1502)
                    s.delete_rtu_cascade(7)
                self.assertEqual(c.get_row("fes_yt_define",[7,1])["cmd_time"],789)
            damaged = Path(root) / "incomplete-source"
            with closing(sqlite3.connect(damaged)) as db:
                db.execute("CREATE TABLE unrelated(id INTEGER)")
            unpublished = Path(root) / "must-not-exist"
            with self.assertRaises(ValueError):
                copy_definitions(damaged, unpublished)
            self.assertFalse(unpublished.exists())

    def test_unbound_station_is_reported_and_orphan_points_are_not_discarded(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            template, source = root/"template", root/"source"
            with closing(ScadaStore(template)) as business:
                tables = ["fes_rtu_define", "fes_yc_define", "fes_yx_define", "fes_yt_define", "fes_yk_define",
                          *(n for n in business.catalog.names() if n.startswith("fes_channel_"))]
            with closing(sqlite3.connect(source)) as source_db:
                source_db.execute("ATTACH DATABASE ? AS seed",(str(template),))
                for name in tables:
                    source_db.execute(f'CREATE TABLE {name} AS SELECT * FROM seed.{name}')
                source_db.execute("INSERT INTO fes_rtu_define(rtu_id) VALUES(9)")
                source_db.commit()
                self.assertEqual(copy_definitions(source,root/"first")["skipped_unbound_rtus"],[9])
                source_db.execute("INSERT INTO fes_yc_define(rtu_id,pnt_no) VALUES(9,1)")
                source_db.commit()
                with self.assertRaisesRegex(ValueError,"关联数据"):
                    copy_definitions(source,root/"second")
                self.assertFalse((root/"second").exists())

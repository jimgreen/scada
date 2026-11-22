from contextlib import closing
import sqlite3
import time
import unittest

from tests import test_scdfe_d as fixtures


class CollectorWriteTests(unittest.TestCase):
    setUp = fixtures.DeviceTests.setUp
    row = fixtures.DeviceTests.row

    def test_unchanged_sample_advances_batch_without_rewriting_device_row(self):
        first = self.store.collect_once()
        with self.store.connection() as connection:
            connection.execute("CREATE TABLE write_log (id INTEGER PRIMARY KEY)")
            connection.execute("""CREATE TRIGGER observe_device_writes AFTER UPDATE ON Manual_generator
                BEGIN INSERT INTO write_log VALUES(NULL); END""")

        second = self.store.collect_once()
        self.assertGreater(second["refresh_id"], first["refresh_id"])
        self.assertEqual(second["updated_rows"], 0)
        with self.store.connection() as connection:
            writes = connection.execute("SELECT count(*) FROM write_log").fetchone()[0]
            self.assertEqual(writes, 0)
            tables = {
                r[0]
                for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("measurement_value", tables)
            self.assertNotIn("measurement_refresh", tables)
            self.assertNotIn("scada_point_mapping", tables)

        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute(
                "UPDATE fes_yc_define SET value=12,refresh_time=refresh_time+1"
            )
        self.store.collect_once()
        with self.store.connection() as connection:
            written = connection.execute(
                "SELECT p,p_pre,p_qual,p_rtu,p_pnt FROM Manual_generator"
            ).fetchone()
            self.assertEqual(tuple(written), (12, 10, 0, 1, 1))
            self.assertEqual(
                connection.execute("SELECT count(*) FROM write_log").fetchone()[0], 1
            )

    def test_missing_rebound_point_clears_old_binding_runtime_fields(self):
        self.store.collect_once()
        with self.store.connection() as connection:
            connection.execute("UPDATE Manual_generator SET p_pnt=99 WHERE idx=1")
        self.store.collect_once()
        with self.store.connection() as connection:
            data = connection.execute(
                "SELECT p,p_qual,p_pre,p_time,p_pnt FROM Manual_generator"
            ).fetchone()
            self.assertEqual(tuple(data), (None, 128, None, None, 99))

    def test_cached_source_and_plan_cache_detect_source_and_config_changes(self):
        # 将源库改造为带复合主键 + WAL 模式，贴近生产 scdfe_c 库结构
        with closing(sqlite3.connect(self.source)) as con, con:
            con.execute("PRAGMA journal_mode=WAL")
            now_ms = int(time.time() * 1000)
            con.execute("DROP TABLE fes_yc_define")
            con.execute(
                """CREATE TABLE fes_yc_define (
                    rtu_id INTEGER NOT NULL, pnt_no INTEGER NOT NULL,
                    name TEXT, value REAL, status INTEGER, refresh_time INTEGER,
                    PRIMARY KEY(rtu_id, pnt_no)
                )"""
            )
            con.executemany(
                "INSERT INTO fes_yc_define VALUES(?,?,?,?,?,?)",
                [
                    (1, 1, "有功功率1", 10.0, 0, now_ms),
                    (1, 2, "有功功率2", 42.0, 0, now_ms),
                    (2, 1, "无关RTU测点", 99.0, 0, now_ms),
                ],
            )

        orig_source_values = self.store._source_values
        source_reads = []

        def tracked_source_values(con, kind, keys, *, pk_cache=None):
            source_reads.append((kind, len(keys)))
            return orig_source_values(con, kind, keys, pk_cache=pk_cache)

        self.store._source_values = tracked_source_values
        writer = self.store._open_connection()
        source_cache = {}
        plan_cache = {}
        try:
            self.store.collect_once(
                _writer=writer, _source_cache=source_cache, _plan_cache=plan_cache
            )
            self.assertEqual(len(source_reads), 1)
            self.assertEqual(self.row()["value"], 10.0)

            # 源库无写入时，第二轮采集直接命中 PRAGMA data_version 快照缓存
            self.store.collect_once(
                _writer=writer, _source_cache=source_cache, _plan_cache=plan_cache
            )
            self.assertEqual(len(source_reads), 1)
            self.assertEqual(self.row()["value"], 10.0)

            # 源库在 WAL 模式下更新数值后，缓存立即失效并读到新值
            with closing(sqlite3.connect(self.source)) as con, con:
                con.execute(
                    "UPDATE fes_yc_define SET value=25.5, refresh_time=refresh_time+10 WHERE rtu_id=1 AND pnt_no=1"
                )
            self.store.collect_once(
                _writer=writer, _source_cache=source_cache, _plan_cache=plan_cache
            )
            self.assertEqual(len(source_reads), 2)
            self.assertEqual(self.row()["value"], 25.5)
            self.assertEqual(self.row()["previous_value"], 10.0)

            # 外部连接直接修改设备宽表触发器改绑点号后，_plan_cache 自动感知变更
            with self.store.connection() as con:
                con.execute("UPDATE Manual_generator SET p_pnt=2 WHERE idx=1")
            self.store.collect_once(
                _writer=writer, _source_cache=source_cache, _plan_cache=plan_cache
            )
            self.assertEqual(self.row()["value"], 42.0)
            self.assertEqual(self.row()["pnt_no"], 2)
        finally:
            for entry in source_cache.values():
                entry[0].close()
            writer.close()

    def test_partial_change_advances_unchanged_rows_and_updates_changed_rows(self):
        with closing(sqlite3.connect(self.source)) as con, con:
            con.execute(
                "INSERT INTO fes_yc_define VALUES(1,2,?,20,0,?)",
                ("无功功率", int(time.time() * 1000)),
            )
        second_pt = self.store.save_measurement(
            dict(
                device_id=self.device["device_id"],
                metric="q",
                kind="yc",
                name="无功功率",
                unit="kVar",
                source_database="c.sqlite3",
                rtu_id=1,
                pnt_no=2,
            )
        )
        first_batch = self.store.collect_once()
        # 仅修改点 1，点 2 保持不变
        with closing(sqlite3.connect(self.source)) as con, con:
            con.execute(
                "UPDATE fes_yc_define SET value=15, refresh_time=refresh_time+5 WHERE rtu_id=1 AND pnt_no=1"
            )
        second_batch = self.store.collect_once()
        self.assertGreater(second_batch["refresh_id"], first_batch["refresh_id"])
        with self.store.connection() as con:
            row = con.execute(
                "SELECT p, p_pre, p_qual, q, q_pre, q_qual FROM Manual_generator WHERE idx=1"
            ).fetchone()
        self.assertEqual(tuple(row), (15.0, 10.0, 0, 20.0, None, 0))

    def test_incremental_table_sync_and_fast_reopen_do_not_rewrite_unrelated_rows(self):
        gen2 = self.store.create_device(dict(name="发电机 2", category="generator"))
        load = self.store.create_device(dict(name="负荷 1", category="load"))
        self.store.save_measurement(
            dict(
                device_id=load["device_id"],
                metric="p",
                kind="yc",
                name="负荷有功",
                unit="kW",
                source_database="c.sqlite3",
                rtu_id=1,
                pnt_no=1,
            )
        )
        with self.store.connection() as con:
            con.execute("CREATE TABLE unrelated_writes (tag TEXT)")
            con.execute(
                """CREATE TRIGGER watch_load AFTER UPDATE ON Manual_load
                BEGIN INSERT INTO unrelated_writes VALUES('load'); END"""
            )
            con.execute(
                """CREATE TRIGGER watch_gen2 AFTER UPDATE ON Manual_generator
                WHEN NEW.idx=2 BEGIN INSERT INTO unrelated_writes VALUES('gen2'); END"""
            )

        # 修改发电机 1 已有列的参数，不应重写负荷表或发电机 2
        self.store.save_measurement(
            dict(self.point, version=1, weight=2.5),
            self.point["measurement_id"],
        )
        with self.store.connection() as con:
            writes = [r[0] for r in con.execute("SELECT tag FROM unrelated_writes")]
            con.execute("DROP TRIGGER watch_load")
            con.execute("DROP TRIGGER watch_gen2")
            con.execute("DROP TABLE unrelated_writes")
        self.assertEqual(writes, [])
        # 重新打开已有库走 _schema_ready 快路径
        reopened = fixtures.DeviceStore(self.store.db_path, workspace_root=self.root)
        self.assertEqual(len(reopened.devices({})), 3)
        reopened.close()
        self.assertEqual(gen2["model_idx"], 2)

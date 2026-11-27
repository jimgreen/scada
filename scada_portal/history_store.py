"""SCADA 综合门户 SQLite 连接复用池、分级降采样与辅助业务持久化存储。"""
from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

LOG = logging.getLogger("scada.portal.store")


class ReadOnlyConnPool:
    """线程局部只读 SQLite 连接复用池，避免高频轮询反复 open/close 带来的 WAL 开销。"""

    def __init__(self) -> None:
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all_conns: set[sqlite3.Connection] = set()
        self._closed = False

    def _file_identity(self, db_path: Path) -> tuple[int, int]:
        st = db_path.stat()
        return (getattr(st, "st_ino", 0), getattr(st, "st_dev", 0))

    @contextmanager
    def acquire(self, db_path: Path, *, timeout: float = 10.0):
        """获取当前线程复用的只读连接，文件被替换或损坏时自动重建；池关闭后退化为一次性短连接。"""
        if not db_path.is_file():
            raise FileNotFoundError(f"数据库不存在：{db_path}")
        if self._closed:
            tmp = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=timeout)
            try:
                tmp.row_factory = sqlite3.Row
                tmp.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
                yield tmp
            finally:
                tmp.close()
            return
        key = str(db_path.resolve())
        ident = self._file_identity(db_path)
        cache: dict[str, tuple[tuple[int, int], sqlite3.Connection]] = getattr(self._local, "conns", None)
        if cache is None:
            cache = {}
            self._local.conns = cache

        entry = cache.get(key)
        conn: sqlite3.Connection | None = None
        if entry is not None:
            cached_ident, cached_conn = entry
            if cached_ident == ident:
                with self._lock:
                    still_open = cached_conn in self._all_conns
                if still_open:
                    conn = cached_conn
                else:
                    cache.pop(key, None)
            else:
                self._discard_conn(cache, key, cached_conn)

        if conn is None:
            # 按线程局部隔离复用，绝不跨线程并发使用；开启 check_same_thread=False 仅用于停机时主线程 close_all() 彻底释放句柄
            conn = sqlite3.connect(
                f"{db_path.as_uri()}?mode=ro",
                uri=True,
                timeout=timeout,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            conn.execute("PRAGMA temp_store=MEMORY")
            cache[key] = (ident, conn)
            with self._lock:
                if self._closed:
                    cache.pop(key, None)
                else:
                    self._all_conns.add(conn)

        try:
            yield conn
        except sqlite3.Error:
            self._discard_conn(cache, key, conn)
            raise
        finally:
            if self._closed:
                self._discard_conn(cache, key, conn)
            else:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                except sqlite3.Error:
                    self._discard_conn(cache, key, conn)

    def _discard_conn(
        self,
        cache: dict[str, tuple[tuple[int, int], sqlite3.Connection]],
        key: str,
        conn: sqlite3.Connection,
    ) -> None:
        cache.pop(key, None)
        with self._lock:
            self._all_conns.discard(conn)
        try:
            conn.close()
        except Exception:
            pass

    def close_all(self) -> None:
        """关闭所有线程已缓存的只读连接（用于服务停机或单元测试清理）。"""
        with self._lock:
            self._closed = True
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()
            except Exception:
                pass
        cache = getattr(self._local, "conns", None)
        if isinstance(cache, dict):
            cache.clear()


def ensure_extended_history_schema(conn: sqlite3.Connection) -> None:
    """平滑初始化分级降采样表、检修挂牌表、控调审计表及告警操作人字段。"""
    # 1. 1 分钟级自动降采样汇总表（加速跨日/长时段查询并支撑长期保留）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS device_minute_rollup (
            bucket_ms INTEGER NOT NULL,
            table_name TEXT NOT NULL,
            idx INTEGER NOT NULL,
            metric TEXT NOT NULL,
            avg_val REAL NOT NULL,
            min_val REAL NOT NULL,
            max_val REAL NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (table_name, idx, metric, bucket_ms)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_minute_rollup_bucket ON device_minute_rollup(bucket_ms)"
    )

    # 2. 设备检修挂牌 / 告警屏蔽表
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS device_maintenance (
            dev_id TEXT PRIMARY KEY,
            table_name TEXT NOT NULL,
            idx INTEGER NOT NULL,
            device_name TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            operator TEXT NOT NULL DEFAULT '',
            created_ms INTEGER NOT NULL,
            expires_ms INTEGER
        )
        """
    )

    # 3. 控调指令审计回执表
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS control_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL,
            table_name TEXT NOT NULL,
            idx INTEGER NOT NULL,
            device_name TEXT NOT NULL,
            control TEXT NOT NULL,
            control_label TEXT NOT NULL,
            kind TEXT NOT NULL,
            target_value TEXT NOT NULL,
            operator TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'submitted',
            detail TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_control_audit_ts ON control_audit(ts_ms DESC)")

    # 4. 为 alarm_events 平滑补充 ack_operator / ack_note 列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alarm_events)").fetchall()}
    if cols:
        if "ack_operator" not in cols:
            conn.execute("ALTER TABLE alarm_events ADD COLUMN ack_operator TEXT NOT NULL DEFAULT ''")
        if "ack_note" not in cols:
            conn.execute("ALTER TABLE alarm_events ADD COLUMN ack_note TEXT NOT NULL DEFAULT ''")


def upsert_minute_rollup(
    conn: sqlite3.Connection,
    sample_rows: list[tuple[int, str, int, str, float]],
) -> None:
    """在写入原始秒级样本的同时增量聚合 1 分钟级汇总表。"""
    if not sample_rows:
        return
    rollup_rows = [
        ((ts_ms // 60000) * 60000, tbl, idx, metric, val, val, val)
        for ts_ms, tbl, idx, metric, val in sample_rows
    ]
    conn.executemany(
        """
        INSERT INTO device_minute_rollup(
            bucket_ms, table_name, idx, metric, avg_val, min_val, max_val, sample_count
        ) VALUES(?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(table_name, idx, metric, bucket_ms) DO UPDATE SET
            avg_val = (device_minute_rollup.avg_val * device_minute_rollup.sample_count + excluded.avg_val)
                      / (device_minute_rollup.sample_count + 1),
            min_val = MIN(device_minute_rollup.min_val, excluded.min_val),
            max_val = MAX(device_minute_rollup.max_val, excluded.max_val),
            sample_count = device_minute_rollup.sample_count + 1
        """,
        rollup_rows,
    )


def prune_history_samples(
    conn: sqlite3.Connection,
    *,
    raw_retention_days: int = 14,
    rollup_retention_days: int = 365,
    batch_limit: int = 20000,
) -> dict[str, int]:
    """按保留期分批滚动清理过期历史样本，避免长事务锁库。"""
    now_ms = int(time.time() * 1000)
    raw_cutoff = now_ms - max(1, int(raw_retention_days)) * 86400 * 1000
    rollup_cutoff = now_ms - max(7, int(rollup_retention_days)) * 86400 * 1000

    # 分批删除过期原始点，限制单次写入时长
    cur_raw = conn.execute(
        """
        DELETE FROM device_daily_samples
        WHERE (table_name, idx, metric, ts_ms) IN (
            SELECT table_name, idx, metric, ts_ms
            FROM device_daily_samples
            WHERE ts_ms < ?
            LIMIT ?
        )
        """,
        (raw_cutoff, int(batch_limit)),
    )
    cur_roll = conn.execute(
        "DELETE FROM device_minute_rollup WHERE bucket_ms < ?",
        (rollup_cutoff,),
    )
    return {
        "raw_deleted": max(0, int(cur_raw.rowcount or 0)),
        "rollup_deleted": max(0, int(cur_roll.rowcount or 0)),
    }


def get_history_stats(conn: sqlite3.Connection, db_path: Path) -> dict[str, Any]:
    """返回历史库体量与分级降采样状态摘要。"""
    try:
        size_mb = round(db_path.stat().st_size / (1024 * 1024), 2)
    except OSError:
        size_mb = 0.0
    rollup_cnt = 0
    maint_cnt = 0
    audit_cnt = 0
    try:
        rollup_cnt = int(conn.execute("SELECT COUNT(*) FROM device_minute_rollup").fetchone()[0])
    except sqlite3.Error:
        pass
    try:
        now_ms = int(time.time() * 1000)
        maint_cnt = int(
            conn.execute(
                "SELECT COUNT(*) FROM device_maintenance WHERE expires_ms IS NULL OR expires_ms > ?",
                (now_ms,),
            ).fetchone()[0]
        )
    except sqlite3.Error:
        pass
    try:
        audit_cnt = int(conn.execute("SELECT COUNT(*) FROM control_audit").fetchone()[0])
    except sqlite3.Error:
        pass
    return {
        "db_size_mb": size_mb,
        "minute_rollup_rows": rollup_cnt,
        "maintenance_active": maint_cnt,
        "control_audit_rows": audit_cnt,
    }

"""FULL SQLite commits for command transitions; restore pooled handle settings."""
from contextlib import contextmanager


@contextmanager
def full_sync(connection, timings=None):
    from core.command_metrics import phase
    if connection.in_transaction:
        raise RuntimeError('指令持久化必须在独立事务开始前设置')
    with phase(timings,'setup_ms'):
        previous=connection.execute('PRAGMA synchronous').fetchone()[0]
        connection.execute('PRAGMA synchronous=FULL')
    try:
        yield connection
    finally:
        with phase(timings, 'cleanup_ms'):
            try:
                if connection.in_transaction:
                    connection.rollback()
                connection.execute(f'PRAGMA synchronous={int(previous)}')
            except Exception:
                # 恢复 PRAGMA 失败时关闭损坏连接防止污染连接池，但不覆盖业务原始异常。
                try:
                    connection.close()
                except Exception:
                    pass


@contextmanager
def durable_connection(store):
    with store.connection() as connection,full_sync(connection):
        yield connection

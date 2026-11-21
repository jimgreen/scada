"""Database-only command exchange between independent protocol processes."""
from __future__ import annotations

import math
import time
from collections import defaultdict
from contextlib import ExitStack
import logging
from core.command_metrics import CommandWriteStats, phase


def submit_commands(connection, commands, *, timings=None):
    from core.command_durability import full_sync
    with full_sync(connection,timings):
        return _submit_commands(connection,commands,timings=timings)


def _submit_commands(connection, commands, *, timings=None):
    """Validate and publish a whole command batch in one SQLite transaction.

    Acknowledgement means committed for acquisition, not executed by equipment.
    Unhandled slots cannot be overwritten. Any invalid point rolls back all.
    """
    commands=list(commands)
    if not commands:
        return []
    seen=set()
    for rtu_id,kind,point_no,value in commands:
        if type(rtu_id) is not int or type(point_no) is not int or rtu_id<=0 or point_no<=0:
            raise ValueError('rtu_id/point_no 必须是正整数')
        if kind not in {'yk','yt'}:
            raise ValueError('只支持遥控和遥调命令')
        key=(rtu_id,kind,point_no)
        if key in seen:
            raise ValueError('一批命令不能重复引用同一源测点')
        seen.add(key)
    with phase(timings,'lock_wait_ms'):
        connection.execute('BEGIN IMMEDIATE')
    try:
        with phase(timings,'validation_ms'):
            groups=defaultdict(list)
            for rid,kind,no,_ in commands:groups[(rid,kind)].append(no)
            rtus={};ids=list(dict.fromkeys(r for r,_,_,_ in commands))
            for chunk in _chunks(ids):
                rtus.update((row[0],row[1]) for row in connection.execute(
                    f"SELECT rtu_id,channel_type FROM fes_rtu_define WHERE rtu_id IN ({','.join('?' for _ in chunk)})",chunk))
            rows=_point_rows(connection,groups)
            prepared=[]
            for rtu_id,kind,point_no,value in commands:
                row=rows.get((rtu_id,kind,point_no))
                if row is None:raise KeyError('遥控/遥调测点不存在')
                if rtu_id not in rtus:raise KeyError('RTU 不存在')
                if rtus[rtu_id] not in {'iec104','modbus-tcp','modbus-rtu','can','bacnet','opc'}:
                    raise ValueError('该规约尚未配置按点遥控/遥调下发接口')
                if row['man_disable'] or row['auto_disable']:raise ValueError('测点已闭锁，禁止下发')
                if int(row['cmd_time'] or 0)>int(row['cmd_handle_time'] or 0):raise ValueError('该测点还有待处理指令')
                if kind=='yk':
                    if not isinstance(value,(bool,int,float)) or value not in (0,1):raise ValueError('遥控值只能为 0 或 1')
                    value=int(value)
                else:
                    try:value=float(value)
                    except (TypeError,ValueError,OverflowError) as exc:raise ValueError('遥调值必须为有限数字') from exc
                    if not math.isfinite(value):raise ValueError('遥调值必须为有限数字')
                    low,high=row['eng_min'],row['eng_max']
                    if low is not None and high is not None and low>high:raise ValueError('遥调工程上下限配置错误')
                    if low is not None and value<low or high is not None and value>high:raise ValueError('遥调值超出工程上下限')
                stamp=max(int(time.time()*1000),int(row['cmd_time'] or 0)+1,int(row['cmd_handle_time'] or 0)+1)
                prepared.append((kind,rtu_id,point_no,value,stamp))
        with phase(timings,'apply_ms'):
            for kind in ('yk','yt'):
                connection.executemany(f'UPDATE fes_{kind}_define SET value=?,cmd_time=?,status=0,_version=_version+1 WHERE rtu_id=? AND pnt_no=?',
                    [(value,stamp,rid,no) for k,rid,no,value,stamp in prepared if k==kind])
            # Re-read inside the same transaction so trigger-updated fields and
            # the historical full-row response contract remain exact.
            updated=_point_rows(connection,groups)
            receipts=[dict(updated[(r,k,n)]) for r,k,n,_ in commands]
        with phase(timings,'commit_ms'):connection.commit()
        if timings is not None:timings['committed']=True
        return receipts
    except Exception:
        if connection.in_transaction: connection.rollback()
        raise


def _chunks(values,size=400):
    for offset in range(0,len(values),size):yield values[offset:offset+size]


def _point_rows(connection,groups):
    rows={}
    for (rid,kind),numbers in groups.items():
        for chunk in _chunks(numbers):
            for row in connection.execute(f"SELECT * FROM fes_{kind}_define WHERE rtu_id=? AND pnt_no IN ({','.join('?' for _ in chunk)})",[rid,*chunk]):
                rows[(rid,kind,row['pnt_no'])]=row
    return rows


class DatabaseCommandWriter:
    """Use the caller's local DB connection; no sockets or C process required."""
    def __init__(self,store):
        self.store=store
        self.metrics=CommandWriteStats()

    def submit_commands(self,commands,*,timeout=5):
        if not math.isfinite(timeout) or timeout<=0:
            raise ValueError('timeout 必须是正有限数')
        commands=list(commands)
        if not commands:return []
        timings={'committed':False};receipts=[];error=None;start=time.perf_counter()
        try:
            with ExitStack() as resources:
                with phase(timings,'connection_ms'):connection=resources.enter_context(self.store.connection())
                with phase(timings,'setup_ms'):
                    previous=connection.execute('PRAGMA busy_timeout').fetchone()[0]
                    connection.execute(f'PRAGMA busy_timeout={max(1,int(timeout*1000))}')
                try:
                    receipts=submit_commands(connection,commands,timings=timings)
                    return receipts
                finally:
                    with phase(timings, 'cleanup_ms'):
                        try:
                            connection.execute(f'PRAGMA busy_timeout={previous}')
                        except Exception:
                            try:
                                connection.close()
                            except Exception:
                                pass
        except BaseException as exc:
            error=exc
            raise
        finally:
            timings['total_ms']=(time.perf_counter()-start)*1000
            try:self.metrics.record(timings,commands,receipts,error)
            except Exception:
                logging.getLogger(__name__).exception('记录指令写库诊断失败，保留原提交结果')

    def submit_command(self,rtu_id,kind,point_no,value,*,timeout=5):
        return self.submit_commands([(rtu_id,kind,point_no,value)],timeout=timeout)[0]

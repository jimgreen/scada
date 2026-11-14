"""Database-only command exchange between independent protocol processes."""
from __future__ import annotations

import math
import time


def submit_commands(connection, commands):
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
    connection.execute('BEGIN IMMEDIATE')
    try:
        prepared=[]
        for rtu_id,kind,point_no,value in commands:
            table=f'fes_{kind}_define'
            row=connection.execute(f'SELECT * FROM {table} WHERE rtu_id=? AND pnt_no=?',(rtu_id,point_no)).fetchone()
            if row is None:
                raise KeyError('遥控/遥调测点不存在')
            rtu=connection.execute('SELECT channel_type FROM fes_rtu_define WHERE rtu_id=?',(rtu_id,)).fetchone()
            if rtu is None:
                raise KeyError('RTU 不存在')
            if rtu['channel_type'] not in {'iec104','modbus-tcp','modbus-rtu','can','bacnet','opc'}:
                raise ValueError('该规约尚未配置按点遥控/遥调下发接口')
            if row['man_disable'] or row['auto_disable']:
                raise ValueError('测点已闭锁，禁止下发')
            if int(row['cmd_time'] or 0)>int(row['cmd_handle_time'] or 0):
                raise ValueError('该测点还有待处理指令')
            if kind=='yk':
                if not isinstance(value,(bool,int,float)) or value not in (0,1):
                    raise ValueError('遥控值只能为 0 或 1')
                value=int(value)
            else:
                try: value=float(value)
                except (TypeError,ValueError,OverflowError) as exc:
                    raise ValueError('遥调值必须为有限数字') from exc
                if not math.isfinite(value):
                    raise ValueError('遥调值必须为有限数字')
                low,high=row['eng_min'],row['eng_max']
                if low is not None and high is not None and low>high:
                    raise ValueError('遥调工程上下限配置错误')
                if low is not None and value<low or high is not None and value>high:
                    raise ValueError('遥调值超出工程上下限')
            stamp=max(int(time.time()*1000),int(row['cmd_time'] or 0)+1,int(row['cmd_handle_time'] or 0)+1)
            prepared.append((table,rtu_id,point_no,value,stamp))
        receipts=[]
        for table,rtu_id,point_no,value,stamp in prepared:
            connection.execute(f'UPDATE {table} SET value=?,cmd_time=?,status=0,_version=_version+1 WHERE rtu_id=? AND pnt_no=?',
                               (value,stamp,rtu_id,point_no))
            receipts.append(dict(connection.execute(f'SELECT * FROM {table} WHERE rtu_id=? AND pnt_no=?',(rtu_id,point_no)).fetchone()))
        connection.commit()
        return receipts
    except Exception:
        if connection.in_transaction: connection.rollback()
        raise


class DatabaseCommandWriter:
    """Use the caller's local DB connection; no sockets or C process required."""
    def __init__(self,store): self.store=store

    def submit_commands(self,commands,*,timeout=5):
        if not math.isfinite(timeout) or timeout<=0:
            raise ValueError('timeout 必须是正有限数')
        with self.store.connection() as connection:
            previous=connection.execute('PRAGMA busy_timeout').fetchone()[0]
            connection.execute(f'PRAGMA busy_timeout={max(1,int(timeout*1000))}')
            try: return submit_commands(connection,commands)
            finally: connection.execute(f'PRAGMA busy_timeout={previous}')

    def submit_command(self,rtu_id,kind,point_no,value,*,timeout=5):
        return self.submit_commands([(rtu_id,kind,point_no,value)],timeout=timeout)[0]

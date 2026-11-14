"""One monotonic scheduler per bus for persisted CAN frames and replay groups."""
from __future__ import annotations
import heapq
import threading
import time
import can


def load_frame_definitions(store, rtu_id):
    if rtu_id is None:
        return []
    with store.connection() as con:
        return [dict(r) for r in con.execute(
            'SELECT f.* FROM fes_channel_can_frame_define f JOIN fes_rtu_define r '
            'ON f.channel_id=r.channel_id WHERE r.rtu_id=? AND r.channel_type=\'can\' '
            'AND COALESCE(f.enabled,1)=1 ORDER BY f.sequence_no,f.id', (rtu_id,))]


class CanFrameSchedule:
    def __init__(self, send, rows=(), *, fd=False, observe=None):
        self.send = send
        self.observe = observe
        self._condition = threading.Condition()
        self._thread = None
        self._stopped = True
        self._queue = []
        self._serial = 0
        self._sent = 0
        self._errors = 0
        self._last_error = None
        self.rows = {}
        for source in rows:
            row = dict(source)
            if row.get('enabled') == 0 or row.get('direction', 'tx') == 'rx':
                continue
            mode = row.get('schedule_mode') or 'manual'
            if mode not in {'manual','periodic','replay'} or row.get('inject_error_type') not in (None,'','none'):
                raise ValueError('CAN 异常注入需要专用硬件接口；通用发送接口不能模拟 ACK/CRC/Bus-off 错误')
            if row.get('replay_source'):
                raise ValueError('CAN 回放当前使用数据库报文序列，尚不支持 replay_source 文件导入')
            period = int(row.get('period_ms') or 0)
            repeat = int(row.get('repeat_count') or 0)
            if period < 0 or repeat < 0 or (mode == 'periodic' and period == 0):
                raise ValueError('周期发送的 period_ms 必须大于 0，重复次数不能为负数')
            options = {k: bool(row.get(k) or False) for k in
                       ('is_extended_id','is_fd','bitrate_switch','is_remote_frame')}
            if options['is_fd'] and not fd:
                raise ValueError('CAN FD 报文需要启用通道 fd 参数')
            data = bytes.fromhex(row.get('data_hex') or '')
            if row.get('dlc') is not None:
                options['dlc'] = int(row['dlc'])
            can.Message(arbitration_id=int(row['arbitration_id']), data=data, check=True, **options)
            ident = int(row['id'])
            if ident in self.rows:
                raise ValueError(f'重复 CAN 报文定义 {ident}')
            self.rows[ident] = dict(row, schedule_mode=mode, period=period/1000,
                                   repeat=repeat, data=data, options=options)

    def _enqueue(self, row, due, repeat):
        self._serial += 1
        heapq.heappush(self._queue, (due,self._serial,row,repeat))

    def start(self):
        with self._condition:
            if not self._stopped:
                return
            self._stopped = False
            for row in self.rows.values():
                if row['schedule_mode'] == 'periodic':
                    self._enqueue(row, time.monotonic(), row['repeat'])
            if self.rows:
                self._thread = threading.Thread(target=self._run, name='can-frame-schedule', daemon=True)
                self._thread.start()

    def trigger(self, *, frame_id=None, replay_group=None):
        if (frame_id is None) == (replay_group is None):
            raise ValueError('指定 frame_id 或 replay_group 之一')
        with self._condition:
            if self._stopped:
                raise ValueError('CAN 报文调度未启动')
            if frame_id is not None:
                rows = [self.rows[int(frame_id)]]
                if rows[0]['schedule_mode'] != 'manual':
                    raise ValueError('frame_id 仅用于 manual 报文')
            else:
                rows = sorted((r for r in self.rows.values() if r.get('replay_group') == replay_group
                               and r['schedule_mode'] == 'replay'),
                              key=lambda r:(int(r.get('sequence_no') or 0),int(r['id'])))
            if not rows:
                raise ValueError('没有匹配的 CAN 发送定义')
            count = sum(max(1,r['repeat']) for r in rows)
            if len(self._queue) + count > 10000:
                raise ValueError('CAN 调度队列超过 10000 帧')
            due = time.monotonic()
            for row in rows:
                for _ in range(max(1,row['repeat'])):
                    self._enqueue(row,due,1)
                    due += row['period']
            self._condition.notify_all()
            return {'queued': count}

    def _run(self):
        while True:
            with self._condition:
                if self._stopped:
                    return
                if not self._queue:
                    self._condition.wait()
                    continue
                wait = self._queue[0][0] - time.monotonic()
                if wait > 0:
                    self._condition.wait(wait)
                    continue
                due,_,row,repeat = heapq.heappop(self._queue)
            started = time.monotonic()
            try:
                self.send(int(row['arbitration_id']),row['data'],**row['options'])
            except Exception as exc:
                self._event('CAN调度发送失败，停止本任务', row, due, started, 错误=str(exc))
                # Stop this task on failure; never retry an ambiguous send.
                with self._condition:
                    self._errors += 1
                    self._last_error = str(exc)
                continue
            self._event('CAN调度帧已提交驱动', row, due, started)
            with self._condition:
                self._sent += 1
                if not self._stopped and (repeat == 0 or repeat > 1):
                    self._enqueue(row,max(due+row['period'],time.monotonic()+row['period']),
                                  repeat-1 if repeat else 0)

    def _event(self, action, row, due, started, **extra):
        if self.observe is not None:
            try:
                self.observe(action, 定义编号=row['id'], 调度模式=row['schedule_mode'],
                    回放组=row.get('replay_group'), 帧ID=f"0x{int(row['arbitration_id']):X}",
                    周期毫秒=row['period']*1000, 调度延迟毫秒=round(max(0,started-due)*1000,3),
                    驱动调用毫秒=round((time.monotonic()-started)*1000,3), **extra)
            except Exception:
                # A logging observer must never stop a healthy periodic task.
                pass

    def stop(self):
        with self._condition:
            self._stopped = True
            self._queue.clear()
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(3)
            if thread.is_alive():
                raise TimeoutError('CAN 调度发送尚未结束')
        self._thread = None

    def status(self):
        with self._condition:
            return dict(running=not self._stopped, queued=len(self._queue), sent=self._sent,
                        errors=self._errors,last_error=self._last_error,definitions=len(self.rows))

"""Bounded per-writer command admission timings, never a command outcome."""
from collections import deque
from contextlib import contextmanager
import threading
import time
from core.pipeline_metrics import LatencyStats


@contextmanager
def phase(timings,name):
    if timings is None:
        yield
        return
    start=time.perf_counter()
    try:yield
    except BaseException:
        timings.setdefault('failed_stage',name)
        raise
    finally:timings[name]=timings.get(name,0)+(time.perf_counter()-start)*1000


class CommandWriteStats:
    def __init__(self,limit=16,threshold_ms=100):
        self._lock=threading.Lock();self._recent=deque(maxlen=limit)
        self.latency=LatencyStats();self.count=0;self.failed=0;self.threshold_ms=threshold_ms

    def record(self,timings,commands,receipts,error):
        row=dict(timings,error=str(error)[:500] if error else '',sampled_at=time.time_ns()//1000000)
        row['points']=[dict(rtu_id=r,kind=k,pnt_no=n,cmd_time=receipts[i]['cmd_time'] if i<len(receipts) else None)
                       for i,(r,k,n,v) in enumerate(commands[:8])]
        row['point_count']=len(commands)
        with self._lock:
            self.count+=1;self.failed+=int(not row.get('committed',False))
            row['sequence']=self.count
            for key,value in timings.items():
                if key.endswith('_ms'):self.latency.observe(key,value)
            if error or row['total_ms']>=self.threshold_ms:
                self._recent.append({k:round(v,3) if isinstance(v,float) else v for k,v in row.items()})

    def snapshot(self):
        with self._lock:
            return dict(count=self.count,failed=self.failed,threshold_ms=self.threshold_ms,
                        latency=self.latency.snapshot(),recent=list(self._recent))

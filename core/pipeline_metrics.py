"""Bounded in-process timing samples; persisted through runtime diagnostics."""
from collections import deque
import math
import threading
import time


class LatencyStats:
    def __init__(self, window=256):
        self.window = max(1, int(window))
        self._stages = {}
        self._lock = threading.Lock()

    def observe(self, stage, milliseconds):
        value = float(milliseconds)
        with self._lock:
            entry = self._stages.setdefault(stage, dict(values=deque(maxlen=self.window),count=0,invalid=0))
            if not math.isfinite(value) or value < 0:
                entry['invalid'] += 1
                return
            entry['values'].append(value)
            entry['count'] += 1

    def snapshot(self):
        with self._lock:
            copied = [(name,list(e['values']),e['count'],e['invalid']) for name,e in self._stages.items()]
        result = {}
        for name, values, count, invalid in copied:
            ordered = sorted(values)
            percentile = lambda p: round(ordered[math.ceil(len(ordered)*p)-1],3) if ordered else None
            result[name] = dict(count=count,window_count=len(values),invalid_count=invalid,
                last_ms=round(values[-1],3) if values else None,p50_ms=percentile(.5),
                p95_ms=percentile(.95),p99_ms=percentile(.99),max_ms=max(ordered) if ordered else None)
        return result


class SlowBatchStats:
    """Small correlated samples, without point values or per-batch log writes."""
    def __init__(self,threshold_ms=100,limit=16):
        self.threshold_ms=threshold_ms;self.rows=deque(maxlen=limit);self.count=0

    def observe(self,**row):
        if row['queue_commit_ms']<self.threshold_ms:return
        self.count+=1
        self.rows.append(dict(sequence=self.count,sampled_at=time.time_ns()//1000000,
                             **{key:round(value,3) if isinstance(value,float) else value for key,value in row.items()}))

    def snapshot(self):
        return dict(threshold_ms=self.threshold_ms,count=self.count,items=list(self.rows))

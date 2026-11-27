"""SCADA 综合门户轻量级 SSE（Server-Sent Events）实时推送事件总线。"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any


class PortalEventBus:
    """线程安全广播总线，负责向在线浏览器客户端推送实时脉冲、告警与指令回执。"""

    def __init__(self, *, max_subscribers: int = 64, queue_size: int = 32) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[tuple[str, dict[str, Any]]]] = set()
        self._max_subscribers = max_subscribers
        self._queue_size = queue_size
        self._version = 1

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def subscribe(self) -> queue.Queue[tuple[str, dict[str, Any]]] | None:
        """注册一个新的 SSE 客户端队列；超出并发上限时返回 None。"""
        q: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            if len(self._subscribers) >= self._max_subscribers:
                return None
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[tuple[str, dict[str, Any]]]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event_name: str, payload: dict[str, Any] | None = None) -> int:
        """广播事件到所有活跃订阅者，慢客户端自动丢弃最旧消息防止阻塞服务端。"""
        with self._lock:
            self._version += 1
            ver = self._version
            subs = list(self._subscribers)
        msg = dict(payload or {})
        msg.setdefault("version", ver)
        msg.setdefault("ts_ms", int(time.time() * 1000))
        for q in subs:
            try:
                q.put_nowait((event_name, msg))
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait((event_name, msg))
                except queue.Full:
                    pass
        return ver

    @staticmethod
    def format_sse(event_name: str, payload: dict[str, Any]) -> bytes:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event_name}\ndata: {data}\n\n".encode("utf-8")

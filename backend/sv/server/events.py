"""WS 广播总线：runner/下载进度 → 所有连接的 UI。"""
from __future__ import annotations

import asyncio
import json
from typing import Set


class EventBus:
    def __init__(self):
        self._queues: Set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def publish(self, event: dict) -> None:
        dead = []
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)  # 消费太慢，丢弃该订阅者积压
        for q in dead:
            self.unsubscribe(q)

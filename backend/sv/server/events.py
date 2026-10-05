"""WS 广播总线：runner/下载进度 → 所有连接的 UI。

publish 只能在事件循环线程调用（asyncio.Queue 非线程安全）；
工作线程（下载回调/TRT 安装线程）必须走 publish_threadsafe。
"""
from __future__ import annotations

import asyncio
from typing import Set


class EventBus:
    def __init__(self):
        self._queues: Set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def publish(self, event: dict) -> None:
        try:  # 首次从循环线程调用时记下 loop，供 publish_threadsafe 回跳
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass  # 无循环上下文（关机期）：尽力投递
        dead = []
        for q in list(self._queues):  # copy：断连侧 unsubscribe 不会打断迭代
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 明确通知 WS 关闭，客户端才能重连并补快照；不能留下活连接死订阅。
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "_bus_overflow"})
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)

    def publish_threadsafe(self, event: dict) -> None:
        """线程池/后台线程发布：回到事件循环线程操作队列（竞态会损坏内部状态/丢事件）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.publish, event)
        except RuntimeError:
            pass  # loop 在发布前关闭（退出竞态），事件丢弃

"""进程级共享单例：事件总线 / 队列调度器 / 性能采样器 / 下载锁 / 硬件缓存。

此前全部内联在 app.py；routes/* 拆分后多个模块共同依赖，独立成模块
避免 routes 反向 import app 造成循环（trt_component 的延迟 import hack
随之消灭）。
"""
from __future__ import annotations

import asyncio
import time

from .events import EventBus
from .hardware import hardware_info
from .perf import PerfSampler
from .runner import Runner

bus = EventBus()
runner = Runner(bus)
perf = PerfSampler(bus, runner)
_download_lock = asyncio.Lock()


# 硬件探测含 nvidia-smi/NVENC 试编码，按 60s 缓存（任务创建/trim/模型页共用）
_hw_cache: tuple[float, dict] = (0.0, {})


def cached_hardware() -> dict:
    global _hw_cache
    ts, data = _hw_cache
    if not data or time.time() - ts > 60:
        data = hardware_info()
        _hw_cache = (time.time(), data)
    return data

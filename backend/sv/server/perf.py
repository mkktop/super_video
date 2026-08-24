"""性能采样器：daemon 线程周期采 CPU/内存/GPU 占用与当前任务进程树,
环形缓冲保留最近 1 小时(重启清零),每拍经 EventBus 推送所有 WS 客户端。

- CPU/内存走 psutil;GPU 走 nvidia-smi 子进程(非 N 卡为空列表,UI 显示不可采集)。
- 任务归因:串行队列保证同时只有一个 worker,runner.proc.pid 的进程树
  (worker + 2×ffmpeg)即超分负载;trim 在 sidecar 进程内跑,轻量不计入。
"""
from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from collections import deque

import psutil

from ..utils.process import WINDOWS_CREATE_FLAGS
from .events import EventBus
from .runner import Runner
from .settings import load as load_settings

INTERVAL_S = 2.0
MAXLEN = 1800  # 2s × 1800 = 最近 1 小时


class PerfSampler:
    def __init__(self, bus: EventBus, runner: Runner, maxlen: int = MAXLEN,
                 interval_s: float = INTERVAL_S):
        self.bus = bus
        self.runner = runner
        self.samples: deque[dict] = deque(maxlen=maxlen)
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._nvidia_dead = False  # 找不到 nvidia-smi 时置位,永不再试
        self._task_procs: dict[int, psutil.Process] = {}  # pid 缓存:cpu_percent 跨拍才有基准

    # ---- 生命周期 ----

    def start(self) -> None:
        # 同进程内可能多次启停(测试、热重启):必须清除上次的停止信号,
        # 否则 stop() 置位的 _stop 会让本线程立即退出
        self._stop.clear()
        psutil.cpu_percent(interval=None)  # 首次调用返回无意义值,先打底丢弃
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="perf-sampler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def snapshot(self) -> list[dict]:
        return list(self.samples)

    # ---- 主循环 ----

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not self._enabled():
                    continue  # 开关关闭:线程保活等待重新开启
                self.tick()
            except Exception:  # noqa: BLE001 — 单拍失败不能杀死采样器
                pass

    def _enabled(self) -> bool:
        return bool(load_settings().get("perf_sampling", True))

    def tick(self) -> dict:
        """采一拍:入环形缓冲并推送。测试可直接调用。"""
        sample = self._collect()
        self.samples.append(sample)
        if self._loop is not None:
            # asyncio.Queue 非线程安全,publish 必须回到事件循环线程执行
            self._loop.call_soon_threadsafe(
                self.bus.publish, {"type": "perf", **sample})
        return sample

    # ---- 采集 ----

    def _collect(self) -> dict:
        vm = psutil.virtual_memory()
        return {
            "t": round(time.time(), 1),
            "cpu": round(psutil.cpu_percent(interval=None), 1),
            "mem_pct": round(vm.percent, 1),
            "mem_used_gb": round(vm.used / 1e9, 1),
            "gpus": self._gpus(),
            "task": self._task_usage(),
        }

    def _gpus(self) -> list[dict]:
        if self._nvidia_dead:
            return []
        try:
            out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=index,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, timeout=5, creationflags=WINDOWS_CREATE_FLAGS,
            )
        except FileNotFoundError:
            self._nvidia_dead = True  # 机器上无 nvidia-smi(非 N 卡/未装驱动)
            return []
        except (OSError, subprocess.TimeoutExpired):
            return []
        gpus: list[dict] = []
        if out.returncode == 0:
            for line in out.stdout.decode("utf-8", "replace").strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 4:
                    continue
                try:
                    gpus.append({
                        "util": int(parts[1]),
                        "mem_used_mb": float(parts[2]),
                        "mem_total_mb": float(parts[3]),
                    })
                except ValueError:
                    continue
        return gpus

    def _task_usage(self) -> dict | None:
        proc = getattr(self.runner, "proc", None)
        task_id = getattr(self.runner, "current_id", None)
        if proc is None or task_id is None:
            self._task_procs.clear()
            return None
        try:
            root = psutil.Process(proc.pid)
            procs = [root, *root.children(recursive=True)]
        except psutil.Error:  # worker 已退出(收尾/被杀)
            self._task_procs.clear()
            return None
        # 复用旧 Process 对象:cpu_percent(interval=None) 度量的是"距上次调用"的均值
        alive = {p.pid: self._task_procs.get(p.pid, p) for p in procs}
        self._task_procs = alive
        cpu = 0.0
        rss = 0
        for p in alive.values():
            try:
                cpu += p.cpu_percent(interval=None)
                rss += p.memory_info().rss
            except psutil.Error:
                pass
        cores = psutil.cpu_count() or 1
        return {
            "task_id": task_id,
            "cpu_pct": round(cpu / cores, 1),  # 单核%求和归一为整机百分比
            "mem_gb": round(rss / 1e9, 2),
            "n_proc": len(alive),
        }

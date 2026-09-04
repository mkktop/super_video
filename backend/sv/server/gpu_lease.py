"""GPU 租约：队列任务与 sidecar 内辅助 GPU 作业（模型对比/剪切）的显存互斥。

DML 会话对显存压力敏感——OOM 即会话损坏级联（v0.4.7 前后的多起事故，
见 worker._oom 与 BENCH），而模型对比在 sidecar 进程内直接建 ONNX 会话、
剪切走 NVENC 编码，此前都能与队列 worker 的推理同时抢显存。租约让三者
确定性错开：

- 队列任务运行全程持有租约（runner._run_one 获取/释放；子进程 worker 无感）；
- compare/trim 作业运行前须取得租约，拿不到就阻塞各自 worker 线程排队；
- **辅助作业插队**：任务间隙（release 时）有辅助作业等待则先于下一个任务
  获得租约——对比是用户在页面等结果的短作业（≤2min），不应排在可能数小时
  的队列后面饿死；反过来队列任务最多等一个辅助作业。

不纳入仲裁的 GPU 触点：probe 的 NVENC 试编码、导入模型的 64×64 验证帧
（秒级、低频，争抢窗口可忽略）；task_stills/share-card 走 ffmpeg 软解。
"""
from __future__ import annotations

import threading

_cond = threading.Condition()
_holder: str | None = None
_aux_waiting = 0


def try_acquire(who: str) -> bool:
    """非阻塞获取（任务方专用语义）：有辅助作业在等待即让位失败。

    只有 runner 用本入口（who 仅为持有者标识，如 "task"）；辅助作业走
    acquire()。两者语义不同，不做字符串判别。
    """
    global _holder
    with _cond:
        if _holder is not None:
            return False
        if _aux_waiting == 0:
            _holder = who
            _cond.notify_all()
            return True
        return False


def acquire(who: str) -> None:
    """阻塞获取。辅助作业（compare/trim worker 线程）使用，拿到为止。"""
    global _aux_waiting, _holder
    with _cond:
        _aux_waiting += 1
        try:
            while _holder is not None:
                _cond.wait()
            _holder = who
            _cond.notify_all()
        finally:
            _aux_waiting -= 1


def release(who: str) -> None:
    """释放租约；who 与持有者不符抛 RuntimeError（配对错误尽早暴露）。"""
    global _holder
    with _cond:
        if _holder != who:
            raise RuntimeError(f"gpu_lease: {who} 释放了不持有的租约（当前={_holder}）")
        _holder = None
        _cond.notify_all()


def holder() -> str | None:
    """当前持有者（perf/排障可观测；None=空闲）。"""
    with _cond:
        return _holder


def waiting_aux() -> int:
    """正在排队等待租约的辅助作业数（测试断言用）。"""
    with _cond:
        return _aux_waiting

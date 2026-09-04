"""GPU 租约：任务/辅助作业显存互斥与辅助插队语义。"""
import threading
import time

import pytest

from sv.server import gpu_lease


@pytest.fixture(autouse=True)
def _clean_lease():
    """每个用例前确保租约空闲（全局单例状态隔离）。"""
    with gpu_lease._cond:
        gpu_lease._holder = None
        gpu_lease._aux_waiting = 0
    yield


def test_mutual_exclusion_basic():
    assert gpu_lease.try_acquire("task")
    assert gpu_lease.holder() == "task"
    assert not gpu_lease.try_acquire("task-next")
    gpu_lease.release("task")
    assert gpu_lease.holder() is None
    gpu_lease.acquire("compare")  # 辅助方走阻塞入口（空闲时立即返回）
    assert gpu_lease.holder() == "compare"
    gpu_lease.release("compare")


def test_release_wrong_owner_raises():
    gpu_lease.try_acquire("task")
    with pytest.raises(RuntimeError):
        gpu_lease.release("compare")
    gpu_lease.release("task")


def test_aux_yields_queue_gap():
    """任务释放瞬间有辅助作业排队时，新任务获取被让位拒绝（防长队列饿死对比）。"""
    gpu_lease.try_acquire("task")
    got = threading.Event()

    def aux():
        gpu_lease.acquire("compare")
        got.set()

    t = threading.Thread(target=aux, daemon=True)
    t.start()
    deadline = time.time() + 5
    while gpu_lease.waiting_aux() == 0 and time.time() < deadline:
        time.sleep(0.01)
    gpu_lease.release("task")
    # 释放后新任务立刻请求：aux 等待计数未清零前被让位拒绝；即便 aux 已抢先
    # 拿到租约，holder 非空同样拒绝——两个分支语义一致
    assert not gpu_lease.try_acquire("task2")
    assert got.wait(5)
    assert gpu_lease.holder() == "compare"
    gpu_lease.release("compare")
    # 辅助作业清空后任务恢复获取
    assert gpu_lease.try_acquire("task")
    gpu_lease.release("task")


def test_aux_blocks_until_release():
    """辅助作业在任务持有时阻塞等待，释放后获得。"""
    gpu_lease.try_acquire("task")
    got = threading.Event()

    def aux():
        gpu_lease.acquire("trim")
        got.set()

    t = threading.Thread(target=aux, daemon=True)
    t.start()
    assert not got.wait(0.3)  # 仍被任务阻塞
    gpu_lease.release("task")
    assert got.wait(5)
    assert gpu_lease.holder() == "trim"
    gpu_lease.release("trim")

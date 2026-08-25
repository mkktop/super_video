"""性能采样器测试:字段/环形缓冲/开关/任务归因/历史端点。"""
import os
import time
import types
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR
from sv.server.events import EventBus
from sv.server.perf import INTERVAL_S, PerfSampler

os.environ["SV_DB"] = str(TEMP_DIR / "test_perf.db")
_DB = Path(str(TEMP_DIR / "test_perf.db"))  # 固定路径:全量运行时 env 会被后续模块改写


class IdleRunner:
    """无运行任务的 runner 替身。"""

    proc = None
    current_id = None


def make_sampler(maxlen=10) -> PerfSampler:
    s = PerfSampler(EventBus(), IdleRunner(), maxlen=maxlen)
    s._gpus = lambda: []  # 测试不依赖 nvidia-smi
    return s


def test_sample_fields():
    """一拍采样字段齐全、数值合法;无任务时 task 为 None。"""
    psutil.cpu_percent(interval=None)  # cpu_percent 首调返回 0,先打底
    s = make_sampler()
    sample = s.tick()
    assert {"t", "cpu", "mem_pct", "mem_used_gb", "gpus", "task"} <= set(sample)
    assert sample["cpu"] >= 0
    assert sample["mem_pct"] > 0 and sample["mem_used_gb"] > 0
    assert sample["task"] is None
    assert len(s.samples) == 1


def test_ring_buffer_cap():
    """环形缓冲截断:超出 maxlen 只留最新。"""
    s = make_sampler(maxlen=5)
    for _ in range(10):
        s.tick()
    assert len(s.samples) == 5
    assert len(s.snapshot()) == 5
    ts = [x["t"] for x in s.snapshot()]
    assert ts == sorted(ts)  # 时间单调不减


def test_disabled_by_setting(monkeypatch):
    """perf_sampling=False 时主循环整拍跳过(不采样不推送)。"""
    monkeypatch.setattr("sv.server.perf.load_settings", lambda: {"perf_sampling": False})
    s = make_sampler()
    assert s._enabled() is False
    monkeypatch.setattr("sv.server.perf.load_settings", lambda: {"perf_sampling": True})
    assert s._enabled() is True


def test_settings_toggle_validation(tmp_path, monkeypatch):
    """bool 开关持久化:合法落盘,非 bool 拒绝(perf_sampling / auto_update_check)。"""
    monkeypatch.setattr("sv.server.settings.SETTINGS_PATH", tmp_path / "s.json")
    from sv.server.settings import DEFAULTS, load, save

    assert DEFAULTS["perf_sampling"] is True
    assert DEFAULTS["auto_update_check"] is True
    assert save({"perf_sampling": False})["perf_sampling"] is False
    assert save({"auto_update_check": False})["auto_update_check"] is False
    assert load()["perf_sampling"] is False
    assert load()["auto_update_check"] is False
    with pytest.raises(ValueError):
        save({"perf_sampling": "yes"})
    with pytest.raises(ValueError):
        save({"auto_update_check": 1})


def test_publishes_perf_event():
    """tick 推送 {"type":"perf",...} 到总线(loop 直连执行,便于断言)。"""
    published = []

    class DirectLoop:
        def call_soon_threadsafe(self, fn, *args):
            fn(*args)

    s = make_sampler()
    s._loop = DirectLoop()
    s.bus.publish = published.append
    s.tick()
    assert len(published) == 1
    assert published[0]["type"] == "perf"
    assert published[0]["cpu"] >= 0


def test_task_usage_own_tree():
    """任务归因:以自身进程为 worker root,树内至少 1 进程且字段合法。"""
    s = PerfSampler(EventBus(), types.SimpleNamespace(
        proc=types.SimpleNamespace(pid=os.getpid()), current_id="t-test"))
    usage = s._task_usage()
    assert usage["task_id"] == "t-test"
    assert usage["n_proc"] >= 1
    assert usage["cpu_pct"] >= 0
    assert usage["mem_gb"] > 0
    # 无任务时清空并返回 None
    s.runner = IdleRunner()
    assert s._task_usage() is None
    assert not s._task_procs


@pytest.fixture(scope="module")
def client():
    if _DB.exists():
        _DB.unlink()
    from sv.server.app import app

    with TestClient(app) as c:  # 触发 lifespan: runner + perf 采样线程
        yield c


def test_perf_history_endpoint(client, monkeypatch):
    """/api/perf/history 返回间隔与样本列表(强制开采样,不受本机设置影响)。"""
    monkeypatch.setattr("sv.server.perf.load_settings", lambda: {"perf_sampling": True})
    body = None
    deadline = time.time() + 15  # 采样线程 2s 一拍,留足启动余量
    while time.time() < deadline:
        body = client.get("/api/perf/history").json()
        if body["samples"]:
            break
        time.sleep(0.5)
    assert body and body["samples"], "采样线程应在数秒内产出样本"
    assert body["interval_s"] == INTERVAL_S
    s0 = body["samples"][0]
    assert {"t", "cpu", "mem_pct", "mem_used_gb", "gpus", "task"} <= set(s0)

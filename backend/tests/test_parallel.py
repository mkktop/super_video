"""双路并行：分片规则 / checkpoint 并发合并 / 设置校验 / worker 入口解析。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sv.pipeline.segmented import SegmentedPipeline, shard_starts
from sv.server import settings as settings_mod


# ---- 分片规则 ----

def test_shard_starts_interleaved():
    starts = [0, 60, 120, 180, 240]
    assert shard_starts(starts, None, 1) == starts  # 单路不过滤
    a = shard_starts(starts, 0, 2)
    b = shard_starts(starts, 1, 2)
    assert a == [0, 120, 240] and b == [60, 180]
    assert sorted(a + b) == starts  # 两路并集 = 全部段（不重不漏）


def test_pipeline_carries_shard_params():
    from sv.pipeline.probe import MediaInfo

    info = MediaInfo(path=Path("x.mp4"), container="mp4", duration_s=1,
                     width=64, height=64, fps=24, fps_str="24/1", vfr=False,
                     video_codec="h264", pix_fmt="yuv420p", bit_depth=8,
                     color_transfer="", audio=[], total_frames=24)
    p = SegmentedPipeline(info, "out.mp4", None, shard=1, nshards=2)
    assert p.shard == 1 and p.nshards == 2
    # shard=0 也是合法分片号（契约）：不过滤回退只认 None
    p0 = SegmentedPipeline(info, "out.mp4", None, shard=0, nshards=2)
    assert p0.shard == 0


# ---- checkpoint 并发合并 ----

def test_ckpt_merge_under_shards(tmp_path):
    """两路各写各的段：后写的必须并入先写的（丢段=续跑重跑别人做过的活）。"""
    info = None  # _save_ckpt 不用 info
    from sv.pipeline.segmented import SegmentedPipeline as SP

    sp = SP.__new__(SP)  # 只测 _save_ckpt，不跑 __init__（避免 MediaInfo 构造）
    sp.nshards = 2
    work, ckpt = tmp_path, tmp_path / "checkpoint.json"
    sp._save_ckpt(work, ckpt, {0, 120})          # 路 A 写完两段
    sp._save_ckpt(work, ckpt, {60, 180, 240})    # 路 B 写完三段
    assert set(json.loads(ckpt.read_text())["done"]) == {0, 60, 120, 180, 240}

    # 损坏的既有 checkpoint：按空集处理不炸
    ckpt.write_text("{broken", encoding="utf-8")
    sp._save_ckpt(work, ckpt, {0})
    assert json.loads(ckpt.read_text())["done"] == [0]


# ---- 设置 ----

def test_parallel_streams_setting_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    data = settings_mod.save({"parallel_streams": True})
    assert data["parallel_streams"] is True
    with pytest.raises(ValueError):
        settings_mod.save({"parallel_streams": "yes"})
    # 旧配置文件缺键：load 补默认 False
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    assert settings_mod.load()["parallel_streams"] is False


# ---- worker 入口解析（真子进程：argv → main(shard, nshards)） ----

def test_worker_entry_parses_shard(tmp_path, monkeypatch):
    """python -m sv.server.worker <id> --shard 1 2 → 走到 main 并按分片参数运行。

    用不存在的任务 id：入口解析正确就会输出标准 failed 事件（rc=2）；
    解析失败（旧版 len(argv)!=2 检查）会打印 usage 且 rc=2 但无 JSON 行。
    注入 PYTHONIOENCODING=cp1252 模拟英文 Windows CI：入口必须自带 UTF-8
    reconfigure 兜底，否则中文错误行 UnicodeEncodeError、stdout 一行皆无
    （v0.2.0 CI 实际翻车场景）。
    """
    import os
    import subprocess
    import sys

    from sv.server import db as sv_db

    # 隔离库自建 schema：worker 读任务表必须存在（此前靠跨模块共享库才碰巧有表）
    monkeypatch.setenv("SV_DB", str(tmp_path / "worker_entry.db"))
    sv_db.init_db()

    r = subprocess.run(
        [sys.executable, "-m", "sv.server.worker", "no-such-task", "--shard", "1", "2"],
        capture_output=True, timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),  # backend/：-m 从仓库根跑会找不到 sv 包
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )
    lines = [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
    assert lines and lines[-1].startswith('{"type": "failed"'), lines[-3:]
    assert "no-such-task" in lines[-1]


# ---- 双路并行最小 E2E（真实子进程 + 真实 ffmpeg + 真实 checkpoint/concat） ----
# 协调者(worker.main)在进程内跑、engine 打桩为轻量假引擎控制节奏与取消；
# 分片子进程是真实 `python -m sv.server.worker --shard 1 2`（真引擎、真段文件），
# 覆盖：parallel 分叉判定、子进程事件泵、双路 checkpoint 并集、收尾 concat、
# 取消时 finally 对子进程的 kill_tree 连坐。

import numpy as np
import psutil

from sv.paths import TEMP_DIR
from sv.pipeline.stream import TaskCanceled
from sv.server import db as sv_db
from sv.server import worker as worker_mod
from sv.server import worker_engine
from sv.utils.process import WINDOWS_CREATE_FLAGS
from sv.paths import ffmpeg_bin

_PAR_CLIP = TEMP_DIR / "par_e2e_in.mp4"


class _Fast2x:
    """协调者侧假引擎：最近邻 x2（真模型在子进程跑，两边 scale 一致）。"""
    scale = 2
    batch = 1
    provider_used = ["test-fake"]

    def __init__(self, weight, scale, io=None, tile=0, batch=1,
                 device="auto", validate_hw=None):
        pass

    def load(self):
        pass

    def process(self, frame):
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


class _CancelAfterSeg1(_Fast2x):
    """第 1 次调用是 worker 预热；第 0 段(60 帧)完成后第 4 帧起抛 TaskCanceled。

    取消点选在段边界之后：第 0 段的 checkpoint 是同步写在两段之间的，
    断言"取消保留 checkpoint"由此变成确定性的（不依赖子进程的完成时序）。
    """
    calls = 0

    def process(self, frame):
        cls = type(self)
        cls.calls += 1
        if cls.calls > 64:  # 1 预热 + 60 帧(段0) + 3 帧(段1) 后取消
            raise TaskCanceled()
        return super().process(frame)


def _make_par_clip() -> None:
    if _PAR_CLIP.exists():
        return
    import subprocess

    TEMP_DIR.mkdir(exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", "20", "-c:v", "libx264", "-crf", "22", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(_PAR_CLIP)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


@pytest.fixture()
def par_task(monkeypatch):
    _make_par_clip()
    sv_db.init_db()
    # 每个用例=全新 worker 进程的语义：清掉进程内引擎缓存，
    # 否则上个用例留下的同签名条目会绕过本用例的引擎打桩
    import sv.server.worker_engine as _we

    _we._ENGINE_CACHE.clear()
    out = TEMP_DIR / "par_e2e_out.mp4"
    task = sv_db.new_task(str(_PAR_CLIP), str(out), "realesr-animevideov3",
                          params={"scale": 2})
    # 对齐 runner 的子进程环境：PYTHONPATH=backend（否则从仓库根跑 pytest 时
    # 子进程 `python -m sv.server.worker` 找不到 sv 包，rc=1）
    monkeypatch.setenv("PYTHONPATH", str(Path(worker_mod.__file__).resolve().parents[2]))
    # 协调者进程内的设置/引擎打桩（子进程不受影响，走真实设置默认值）
    monkeypatch.setattr(settings_mod, "load", lambda: {
        "engine": "auto", "precision": "fp32", "parallel_streams": True,
    })
    child_pids: list[int] = []
    real_spawn = worker_mod._spawn_shard_child

    def wrapped_spawn(tid):
        p = real_spawn(tid)
        child_pids.append(p.pid)
        return p

    monkeypatch.setattr(worker_mod, "_spawn_shard_child", wrapped_spawn)
    yield task["id"], out, child_pids, monkeypatch
    # 清理：任务行 + 工作目录 + 产物
    sv_db.delete_task(task["id"])
    import shutil

    shutil.rmtree(TEMP_DIR / "segmented" / task["id"], ignore_errors=True)
    out.unlink(missing_ok=True)


def test_dual_stream_e2e_success(par_task):
    """双路全流程：协调者+子进程都完成 → concat 出完整 480 帧产物，工作目录清理。"""
    task_id, out, child_pids, monkeypatch = par_task
    from sv.pipeline.probe import probe

    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _Fast2x)
    assert worker_mod.main(task_id) == 0
    assert child_pids, "parallel 判定未生效：没有分片子进程被拉起"
    assert out.exists(), "双路收尾 concat 未产出最终文件"
    o = probe(out)
    assert abs(o.duration_s - 20) < 0.8, f"时长 {o.duration_s} ≠ 20s（音画漂移）"
    assert 478 <= o.total_frames <= 482, f"总帧数 {o.total_frames} ≠ 480（段丢失/重复）"
    assert o.has_audio
    assert not (TEMP_DIR / "segmented" / task_id).exists(), "成功后必须清理工作目录"


def test_dual_stream_cancel_kills_child(par_task):
    """协调者取消 → finally 连坐 kill_tree 子进程；checkpoint 保留供续跑。"""
    task_id, out, child_pids, monkeypatch = par_task
    _CancelAfterSeg1.calls = 0  # 类计数器跨用例复位
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _CancelAfterSeg1)
    assert worker_mod.main(task_id) == 3  # worker 约定：3 = 已取消
    assert child_pids
    assert not psutil.pid_exists(child_pids[0]), "取消后分片子进程必须被连坐杀死"
    ckpt = TEMP_DIR / "segmented" / task_id / "checkpoint.json"
    assert ckpt.exists(), "取消不得清掉 checkpoint（续跑依据）"
    assert not out.exists()

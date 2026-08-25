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

def test_worker_entry_parses_shard():
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

    r = subprocess.run(
        [sys.executable, "-m", "sv.server.worker", "no-such-task", "--shard", "1", "2"],
        capture_output=True, timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )
    lines = [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
    assert lines and lines[-1].startswith('{"type": "failed"'), lines[-3:]
    assert "no-such-task" in lines[-1]

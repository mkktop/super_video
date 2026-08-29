"""真实片尾（解码 EOF）× probe 总帧数高估：段校验不再误报"解码异常"。

背景：BDRemux MKV 容器 duration 元数据偏大，probe 估 139813 帧 / 实际 139566
（+247），尾段 139200 产出 366/600 被 v0.2.4 校验判"解码异常"硬失败（正式版
两次复现）。修复：StreamPipeline 把"请求帧数没拿满就干净 EOF"标为 decode_eof
（产满 max_frames 后解码器正常退出不算），SegmentedPipeline 视为真实片尾——
接受短段、落盘 eof.json 真实总长（取更小值）、其后段全部跳过、0 帧空段不入
checkpoint，done 事件带实测总长回填 DB。
"""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
import uuid

import numpy as np
import pytest

from sv.paths import TEMP_DIR
from sv.pipeline.probe import probe
from sv.pipeline.segmented import SegmentedPipeline, read_eof_marker
from sv.pipeline.stream import EncodeOpts, StreamPipeline
from tests.test_stream import make_video


class Nearest2x:
    scale = 2

    def process(self, frame):
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


@pytest.fixture(scope="module")
def clip24():
    """24 帧小片（probe 估算与实际一致；高估由测试注入 total_frames 模拟）。"""
    TEMP_DIR.mkdir(exist_ok=True)
    p = TEMP_DIR / "tail_eof_in.mp4"
    if not p.exists():
        make_video(p, w=160, h=120, duration=1, fps=24, audio=False)
    return p


def _run(info, out: Path, tid: str, **kw):
    return asyncio.run(SegmentedPipeline(
        info, out, Nearest2x(), EncodeOpts(), task_id=tid,
        seg_frames=10, cleanup=False, **kw).run())


def _ckpt(tid: str) -> list[int]:
    return json.loads((TEMP_DIR / "segmented" / tid / "checkpoint.json")
                      .read_text())["done"]


def test_decode_eof_flag_semantics(clip24, tmp_path):
    """没拿满就干净 EOF → decode_eof=1；产满 max_frames 正常退出 → 不标。"""
    info = probe(clip24)
    sp = StreamPipeline(info, tmp_path / "a.mp4", Nearest2x(), EncodeOpts(),
                        max_frames=10_000)
    asyncio.run(sp.run())
    assert sp.stage_stats.get("decode_eof") == 1.0

    sp2 = StreamPipeline(info, tmp_path / "b.mp4", Nearest2x(), EncodeOpts(),
                         max_frames=int(info.total_frames))
    asyncio.run(sp2.run())
    assert "decode_eof" not in sp2.stage_stats
    assert int(sp2.stage_stats["frames"]) == info.total_frames


def test_probe_overestimate_tail_accepted(clip24, tmp_path):
    """估算 +30 帧（约 1.25 段）：原逻辑在含真实 EOF 的段硬失败，现应成功且
    输出帧数=真实值×scale，checkpoint 只含真实段。"""
    info = probe(clip24)
    real = info.total_frames
    tid = f"teof1_{uuid.uuid4().hex[:6]}"
    out = tmp_path / "out.mp4"
    stats = _run(replace(info, total_frames=real + 30), out, tid)

    assert out.exists()
    # scale 只放大分辨率不改帧数；frames 口径=输入帧×factor(补帧)，无补帧=真实帧数
    assert probe(out).total_frames == real
    assert (probe(out).width, probe(out).height) == (info.width * 2, info.height * 2)
    assert stats.frames == real  # RunStats 用修正后总长（原代码=估算 total_in×factor）
    assert _ckpt(tid) == [0, 10, real // 10 * 10]  # 含被接受的短尾段
    assert read_eof_marker(TEMP_DIR / "segmented" / tid) == real


def test_empty_segment_beyond_eof_not_checkpointed(clip24):
    """单跑 shard=1（starts=[10,30,50]）：段 30 起点已在真实片尾外 → 0 帧空段
    不入 checkpoint，只落粗上界标记（首发现者，另一路可用更精确值覆盖）。"""
    info = probe(clip24)
    tid = f"teof2_{uuid.uuid4().hex[:6]}"
    _run(replace(info, total_frames=info.total_frames + 40),
         TEMP_DIR / f"{tid}_out.mp4", tid, shard=1, nshards=2)

    assert _ckpt(tid) == [10]  # 空段 30 未入 checkpoint
    work = TEMP_DIR / "segmented" / tid
    assert read_eof_marker(work) == 30


def test_dual_shard_marker_skips_and_concat_real_frames(clip24, tmp_path):
    """双路顺序模拟（同工作目录）：shard1 先撞尾落标记，shard0 读标记后仍会在
    自己的短段处发现精确片尾（min 覆盖粗上界）；concat 产物帧数=真实×scale。"""
    from sv.pipeline.segmented import concat_segments

    info = probe(clip24)
    real = info.total_frames
    info2 = replace(info, total_frames=real + 30)
    tid = f"teof3_{uuid.uuid4().hex[:6]}"
    final = tmp_path / "final.mp4"
    _run(info2, final, tid, shard=1, nshards=2)  # 先：另一路撞尾
    _run(info2, final, tid, shard=0, nshards=2)  # 后：读标记跳过界外段

    assert _ckpt(tid) == [0, 10, real // 10 * 10]
    assert read_eof_marker(TEMP_DIR / "segmented" / tid) == real  # 精确值覆盖粗上界
    concat_segments(TEMP_DIR / "segmented" / tid, info, EncodeOpts(), final)
    assert probe(final).total_frames == real  # concat 产物帧数=真实帧数（原误判场景下任务直接失败）

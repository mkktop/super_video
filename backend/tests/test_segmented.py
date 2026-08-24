"""分段管线（GAN 断点续跑）：全流程产物正确 / 续跑跳过已完成段 / 补帧帧数不变。"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.probe import probe, validate_m0
from sv.pipeline.segmented import SegmentedPipeline
from sv.pipeline.stream import EncodeOpts
from sv.utils.process import WINDOWS_CREATE_FLAGS


class Nearest2x:
    scale = 2

    def process(self, frame):
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


class CountingNearest2x:
    """计数版 2x：验证续跑时已完成段不再进推理。"""

    scale = 2

    def __init__(self):
        self.n = 0

    def process(self, frame):
        self.n += 1
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


class InterpStub:
    """补帧桩：返回前一帧（只验证帧数翻倍与段边界不丢帧）。"""

    def interpolate(self, a, b):
        return a


def make_video(path: Path, duration=3, fps=24) -> None:
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", str(duration), "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ]
    subprocess.run(cmd, check=True, creationflags=WINDOWS_CREATE_FLAGS)


@pytest.fixture(scope="module")
def sample_video():
    TEMP_DIR.mkdir(exist_ok=True)
    p = TEMP_DIR / "seg_in.mp4"
    make_video(p, duration=3)  # 72 帧 @24fps
    return p


def run(pipeline):
    return asyncio.run(pipeline.run())


def test_segmented_full_run(sample_video):
    """分段全流程：分辨率/音轨/时长与直跑一致，成功后清理工作目录。"""
    info = probe(sample_video)
    validate_m0(info)
    work = TEMP_DIR / "segmented" / "t_seg_full"
    shutil.rmtree(work, ignore_errors=True)
    out = TEMP_DIR / "seg_out_full.mp4"
    stats = run(SegmentedPipeline(
        info, out, Nearest2x(), EncodeOpts(), task_id="t_seg_full", seg_frames=24,
    ))
    assert stats.frames == info.total_frames
    assert not work.exists(), "成功后必须清理工作目录"
    o = probe(out)
    assert (o.width, o.height) == (640, 480)
    assert o.has_audio
    assert abs(o.duration_s - info.duration_s) < 0.3
    assert abs(o.total_frames - info.total_frames) <= 2


def test_segmented_resume_skips_done_segments(sample_video):
    """真正的续跑：只保留第一段+checkpoint，重跑必须跳过该段且产物完整。"""
    info = probe(sample_video)
    work = TEMP_DIR / "segmented" / "t_seg_resume"
    shutil.rmtree(work, ignore_errors=True)
    out = TEMP_DIR / "seg_out_resume.mp4"

    # 第一遍完整跑（cleanup=False 保留工作目录：3 段各 24 帧）
    run(SegmentedPipeline(
        info, out, Nearest2x(), EncodeOpts(), task_id="t_seg_resume",
        seg_frames=24, cleanup=False,
    ))
    assert (work / "checkpoint.json").exists()
    assert len(list(work.glob("seg_*.mp4"))) == 3
    # 模拟"跑到 1/3 被取消"：只剩第一段已完成，checkpoint done=[0]
    (work / "seg_000024.mp4").unlink()
    (work / "seg_000048.mp4").unlink()
    (work / "checkpoint.json").write_text(json.dumps({"done": [0]}))
    out.unlink(missing_ok=True)

    first_reported = {}
    tx = CountingNearest2x()

    def cb(frames, total, fps, eta):
        first_reported.setdefault("f", frames)

    out2 = TEMP_DIR / "seg_out_resume2.mp4"
    run(SegmentedPipeline(
        info, out2, tx, EncodeOpts(), task_id="t_seg_resume",
        seg_frames=24, cleanup=False, progress_cb=cb,
    ))
    assert first_reported.get("f", 0) >= 24, "续跑应从已完成段之后开始报告"
    assert tx.n == 48, f"已完成段不得重跑（推理了 {tx.n} 帧，应为 48）"
    o = probe(out2)
    assert abs(o.duration_s - info.duration_s) < 0.3
    assert abs(o.total_frames - info.total_frames) <= 2
    shutil.rmtree(work, ignore_errors=True)


def test_segmented_interp_doubles_frames(sample_video):
    """补帧 x2 下分段输出仍为 2N 帧，段边界不丢帧。"""
    info = probe(sample_video)
    work = TEMP_DIR / "segmented" / "t_seg_interp"
    shutil.rmtree(work, ignore_errors=True)
    out = TEMP_DIR / "seg_out_interp.mp4"
    stats = run(SegmentedPipeline(
        info, out, Nearest2x(), EncodeOpts(), task_id="t_seg_interp",
        seg_frames=24, interp=InterpStub(),
    ))
    assert stats.frames == info.total_frames * 2
    o = probe(out)
    assert abs(o.duration_s - info.duration_s) < 0.3  # 帧率翻倍，时长不变
    assert abs(o.total_frames - info.total_frames * 2) <= 2

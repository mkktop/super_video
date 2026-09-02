"""自定义输出分辨率（target_size/target_w/target_h）与 CPU 回落路径。

对应 v0.2.0：
- 管线级：Stream/Segmented 支持"原生超分后 lanczos 缩放到精确宽高"；
- 引擎级：DML 初始化失败（无独显机器）应回落 CPU 而非崩溃；
- CPU EP 端到端：无独显机器可用性（慢但能出片）的代码级验证。
"""
import asyncio
import subprocess
from pathlib import Path

import numpy as np
import pytest

from sv.engines.onnx_engine import OnnxSrEngine
from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.probe import probe
from sv.pipeline.segmented import SegmentedPipeline
from sv.pipeline.stream import EncodeOpts, StreamPipeline
from sv.utils.process import WINDOWS_CREATE_FLAGS

# 内置权重（AnimeJaNai V3.1，原生 fp16、RGB、x2）
BUNDLED_V31 = (
    Path(__file__).parent.parent / "sv" / "models" / "bundled"
    / "2x_AnimeJaNai_HD_V3.1_Balanced_SPANF3_b8f64_unshuffle_fp16.onnx"
)
IO_V31 = {"color": "rgb", "range": "0-1", "batch_hint": 1}


class Nearest2x:
    scale = 2

    def process(self, frame):
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


def make_video(path: Path, w=320, h=240, duration=2, fps=24, audio=True) -> None:
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}",
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440"]
    cmd += ["-t", str(duration), "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True, creationflags=WINDOWS_CREATE_FLAGS)


@pytest.fixture(scope="module")
def sample():
    TEMP_DIR.mkdir(exist_ok=True)
    p = TEMP_DIR / "targetres_in.mp4"
    make_video(p)
    return p


def test_stream_target_size(sample):
    """引擎 2x（640x480）+ 目标 400x300：编码器 lanczos 缩放，非整数倍率。"""
    info = probe(sample)
    out = TEMP_DIR / "targetres_stream.mp4"
    asyncio.run(StreamPipeline(
        info, out, Nearest2x(), EncodeOpts(), target_size=(400, 300),
    ).run())
    o = probe(out)
    assert (o.width, o.height) == (400, 300)
    assert o.has_audio
    assert abs(o.total_frames - info.total_frames) <= 2


def test_segmented_target_size(sample):
    """分段管线同样支持精确目标宽高（每段独立缩放，concat 后一致）。"""
    info = probe(sample)
    out = TEMP_DIR / "targetres_seg.mp4"
    asyncio.run(SegmentedPipeline(
        info, out, Nearest2x(), EncodeOpts(),
        task_id="t_targetres", target_size=(400, 300), seg_frames=24,
    ).run())
    o = probe(out)
    assert (o.width, o.height) == (400, 300)
    assert abs(o.duration_s - info.duration_s) < 0.5


def test_engine_dml_init_failure_falls_back_to_cpu(monkeypatch):
    """模拟无独显机器：DML 会话创建抛错 → 引擎回落 CPU 而非崩溃。"""
    import onnxruntime as ort

    real_init = ort.InferenceSession

    def fake_init(path, so=None, providers=None, **kw):
        if providers and "DmlExecutionProvider" in providers:
            raise RuntimeError("Failed to create DmlExecutionProvider (无显卡机器模拟)")
        return real_init(path, so, providers=["CPUExecutionProvider"], **kw)

    monkeypatch.setattr(ort, "InferenceSession", fake_init)
    eng = OnnxSrEngine(BUNDLED_V31, scale=2, io=IO_V31)
    eng.load()
    assert eng.provider_used == ["CPUExecutionProvider"]
    out = eng.process(np.zeros((32, 32, 3), dtype=np.uint8))
    assert out.shape == (64, 64, 3)


def test_engine_device_cpu():
    """device='cpu' 显式走 CPU EP（无独显机器的实际执行路径）。"""
    eng = OnnxSrEngine(BUNDLED_V31, scale=2, io=IO_V31, device="cpu")
    eng.load()
    assert eng.provider_used == ["CPUExecutionProvider"]
    out = eng.process(np.zeros((32, 32, 3), dtype=np.uint8))
    assert out.shape == (64, 64, 3)


def test_cpu_e2e_pipeline():
    """CPU EP 全管线端到端：慢但能出片，音轨保留（无独显机器可用性）。"""
    small = TEMP_DIR / "targetres_cpu_in.mp4"
    make_video(small, w=160, h=90, duration=1)
    eng = OnnxSrEngine(BUNDLED_V31, scale=2, io=IO_V31, device="cpu")
    eng.load()
    info = probe(small)
    out = TEMP_DIR / "targetres_cpu_e2e.mp4"
    stats = asyncio.run(StreamPipeline(info, out, eng, EncodeOpts()).run())
    o = probe(out)
    assert stats.frames == info.total_frames
    assert (o.width, o.height) == (info.width * 2, info.height * 2)
    assert o.has_audio

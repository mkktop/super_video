"""管道无推理往返：ffmpeg→python→ffmpeg 全链路，验证 IO/音轨/分辨率。"""
import asyncio
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.probe import probe, validate_m0
from sv.pipeline.stream import EncodeOpts, StreamPipeline
from sv.utils.process import WINDOWS_CREATE_FLAGS


class PassThrough:
    scale = 1

    def process(self, frame):
        return frame


class Nearest2x:
    scale = 2

    def process(self, frame):
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


def make_video(path: Path, w=320, h=240, duration=3, fps=24, audio=True) -> None:
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}",
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440"]
    cmd += ["-t", str(duration), "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, creationflags=WINDOWS_CREATE_FLAGS)


@pytest.fixture(scope="module")
def sample_video():
    TEMP_DIR.mkdir(exist_ok=True)
    p = TEMP_DIR / "stream_in.mp4"
    make_video(p)
    return p


def run(pipeline):
    return asyncio.run(pipeline.run())


def test_probe_and_validate(sample_video):
    info = probe(sample_video)
    validate_m0(info)
    assert (info.width, info.height) == (320, 240)
    assert info.has_audio
    assert info.total_frames == 72  # 3s * 24fps
    assert not info.vfr


def test_passthrough_same_resolution(sample_video):
    info = probe(sample_video)
    out = TEMP_DIR / "stream_out_1x.mp4"
    stats = run(StreamPipeline(info, out, PassThrough(), EncodeOpts()))
    assert stats.frames == info.total_frames
    o = probe(out)
    assert (o.width, o.height) == (320, 240)
    assert o.has_audio
    assert abs(o.duration_s - info.duration_s) < 0.3
    assert abs(o.total_frames - info.total_frames) <= 2


def test_2x_upscale_resolution(sample_video):
    info = probe(sample_video)
    out = TEMP_DIR / "stream_out_2x.mp4"
    run(StreamPipeline(info, out, Nearest2x(), EncodeOpts()))
    o = probe(out)
    assert (o.width, o.height) == (640, 480)
    assert o.has_audio  # 音轨保留


def test_no_audio_input(sample_video):
    info = probe(sample_video)
    silent = TEMP_DIR / "stream_silent.mp4"
    make_video(silent, audio=False)
    i2 = probe(silent)
    out = TEMP_DIR / "stream_out_silent.mp4"
    run(StreamPipeline(i2, out, Nearest2x(), EncodeOpts()))
    assert not probe(out).has_audio

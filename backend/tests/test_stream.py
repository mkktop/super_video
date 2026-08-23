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


def test_target_scale_downscale(sample_video):
    """引擎 2x + 目标 1x：编码器 lanczos 缩回原尺寸。"""
    info = probe(sample_video)
    out = TEMP_DIR / "stream_out_target1x.mp4"
    run(StreamPipeline(info, out, Nearest2x(), EncodeOpts(), target_scale=1))
    o = probe(out)
    assert (o.width, o.height) == (320, 240)


def test_preview_pair_same_frame(sample_video):
    """对比预览的源图与输出图必须是同一帧（interval=0 -> 最终为末帧配对）。"""
    from PIL import Image

    info = probe(sample_video)
    out = TEMP_DIR / "stream_out_prev.mp4"
    pv, pv_src = TEMP_DIR / "prev_out.jpg", TEMP_DIR / "prev_src.jpg"
    run(StreamPipeline(
        info, out, PassThrough(), EncodeOpts(),
        preview_path=pv, src_preview_path=pv_src, preview_interval_s=0,
    ))
    a = np.asarray(Image.open(pv_src).convert("RGB"), dtype=np.float32)
    b = np.asarray(Image.open(pv).convert("RGB"), dtype=np.float32)
    assert a.shape == b.shape
    diff = np.abs(a - b).mean()
    assert diff < 10, f"源/输出预览不是同一帧 (mean diff {diff:.1f})"


def test_no_audio_input(sample_video):
    info = probe(sample_video)
    silent = TEMP_DIR / "stream_silent.mp4"
    make_video(silent, audio=False)
    i2 = probe(silent)
    out = TEMP_DIR / "stream_out_silent.mp4"
    run(StreamPipeline(i2, out, Nearest2x(), EncodeOpts()))
    assert not probe(out).has_audio


def test_10bit_accepted(sample_video):
    """M3：10bit 输入接受，解码统一转 8bit，管线正常出片。"""
    p10 = TEMP_DIR / "stream_in_10bit.mp4"
    subprocess.run([
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", "2", "-c:v", "libx265", "-pix_fmt", "yuv420p10le",
        "-c:a", "aac", "-shortest",
        str(p10),
    ], check=True, creationflags=WINDOWS_CREATE_FLAGS)
    info = probe(p10)
    assert info.bit_depth == 10
    validate_m0(info)  # 不再抛 UnsupportedMedia
    out = TEMP_DIR / "stream_out_10bit.mp4"
    stats = run(StreamPipeline(info, out, PassThrough(), EncodeOpts()))
    assert stats.frames == info.total_frames
    o = probe(out)
    assert o.bit_depth == 8 and o.has_audio


def test_vfr_accepted_cfr_output(sample_video):
    """M3：VFR 输入接受，按平均帧率 CFR 化，时长与音轨保持。"""
    a = TEMP_DIR / "vfr_a.mp4"
    b = TEMP_DIR / "vfr_b.mp4"
    concat = TEMP_DIR / "vfr_list.txt"
    vfr = TEMP_DIR / "stream_in_vfr.mp4"
    for p, rate in ((a, 30), (b, 12)):
        subprocess.run([
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={rate}",
            "-t", "2", "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(p),
        ], check=True, creationflags=WINDOWS_CREATE_FLAGS)
    concat.write_text(
        f"file '{a.as_posix()}'\nfile '{b.as_posix()}'\n", encoding="utf-8"
    )
    subprocess.run([
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat),
        "-c", "copy", str(vfr),
    ], check=True, creationflags=WINDOWS_CREATE_FLAGS)
    info = probe(vfr)
    assert info.vfr, "拼接不同帧率应识别为 VFR"
    validate_m0(info)
    assert info.total_frames > 0 and 15 < info.fps < 30  # 平均帧率
    out = TEMP_DIR / "stream_out_vfr.mp4"
    stats = run(StreamPipeline(info, out, PassThrough(), EncodeOpts()))
    o = probe(out)
    assert not o.vfr, "输出必须 CFR"
    assert abs(o.duration_s - info.duration_s) < 0.5
    # CFR 化的帧数与 duration×avg 估算有 1~2 帧出入（与 probe 少计同源）
    assert abs(stats.frames - info.total_frames) <= 2

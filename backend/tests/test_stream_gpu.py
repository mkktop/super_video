"""GpuStreamPipeline + SegmentedPipeline 工厂注入测试。

CPU 环境可跑防御性拒绝；全链 e2e 需 PyNvVideoCodec + GPU（跳过门与
test_gpu_codec 相同）。toy 2x 模型（最近邻上采样）当「SR 权重」，
产物应 == 源 YUV 转 RGB 后的 2x 最近邻放大（NV12 量化域内）。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sv.paths import TEMP_DIR, ffmpeg_bin  # noqa: E402
from sv.pipeline.probe import probe  # noqa: E402
from sv.pipeline.segmented import SegmentedPipeline  # noqa: E402
from sv.pipeline.stream import EncodeOpts, PipelineError  # noqa: E402
from sv.pipeline.stream_gpu import GpuStreamPipeline  # noqa: E402
from sv.utils.process import WINDOWS_CREATE_FLAGS  # noqa: E402

from test_gpu_codec import _make_src, nvcodec_available  # noqa: E402


def _toy_weight() -> Path:
    from test_gpu_graph import _toy_model

    return _toy_model()


class _StubInfo:
    pass


def test_rejects_unsupported_features(tmp_path):
    """门控漏判时防御性拒绝（不静默走样）。"""
    from sv.pipeline.probe import MediaInfo

    info = MediaInfo(path=tmp_path / "x.mkv", container="matroska", duration_s=1.0,
                     width=320, height=180, fps=24.0, fps_str="24/1", vfr=False,
                     video_codec="h264", pix_fmt="yuv420p", bit_depth=8,
                     color_transfer="gamma22")

    class _Gi:
        w, h = 320, 180

    for kw in ({"interp": object()}, {"decode_vf": "bwdif=mode=send_frame"},
               {"target_size": (640, 360)}):
        with pytest.raises(PipelineError):
            GpuStreamPipeline(info, tmp_path / "o.mp4", None,
                              EncodeOpts(codec="h264_nvenc"), gpu_infer=_Gi(), **kw)
    enc = EncodeOpts(codec="libx264")
    with pytest.raises(PipelineError):
        GpuStreamPipeline(info, tmp_path / "o.mp4", None, enc, gpu_infer=_Gi())
    with pytest.raises(PipelineError):
        GpuStreamPipeline(info, tmp_path / "o.mp4", None,
                          EncodeOpts(codec="h264_nvenc"))


@pytest.mark.skipif(not nvcodec_available(), reason="需要 PyNvVideoCodec 环境")
def test_e2e_segmented_gpu_chain(tmp_path, monkeypatch):
    """切段→NVDEC→图手术 toy 2x→NVENC→seg mp4→concat 全链（含续跑）。"""
    from sv.pipeline.stream_gpu import GpuInfer

    monkeypatch.setattr("sv.pipeline.segmented.TEMP_DIR", tmp_path)
    src = _make_src(tmp_path, "hevc10", n=48)  # GPU 直连链门控 10bit 源
    info = probe(src)
    gi = GpuInfer(_toy_weight(), info.height, info.width, 10, scale=2)
    out = tmp_path / "out.mp4"

    async def run_once(seg_frames):
        sp = SegmentedPipeline(
            info, out, None, EncodeOpts(codec="h264_nvenc", crf=18),
            task_id="gpu_e2e", seg_frames=seg_frames, cleanup=False,
            pipeline_factory=lambda *a, **k: GpuStreamPipeline(
                *a, gpu_infer=gi, **k))
        return await sp.run()

    stats = asyncio.run(run_once(20))  # 48 帧 → 3 段
    assert stats.frames == 48, stats.frames
    r = subprocess.run(
        [str(ffmpeg_bin()).replace("ffmpeg.exe", "ffprobe.exe"), "-v", "error",
         "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames,width,height", "-of", "csv",
         str(out)], capture_output=True, text=True,
        creationflags=WINDOWS_CREATE_FLAGS)
    assert "1024" in r.stdout and "576" in r.stdout and "48" in r.stdout, r.stdout

    # 内容 sanity：输出首帧 ≈ 源首帧 2x 最近邻（NV12 量化域 PSNR）
    raw_src = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-map", "0:v:0", "-frames:v", "1", "-f", "rawvideo",
         "-pix_fmt", "yuv420p10le", "-"], capture_output=True,
        creationflags=WINDOWS_CREATE_FLAGS).stdout
    s16 = np.frombuffer(raw_src, "<u2").reshape(432, 512)
    from sv.engines.gpu_graph import np_rgb_to_nv12, np_yuv_to_rgb, psnr_u8

    # ffmpeg 裸解 10bit 值 → MSB 对齐槽（与解码表面同口径）+ riffle 排列
    # （注意先 ravel：2-D 切片会变成行级交错，与表面的元素级 riffle 不符）
    y_sh = (s16[:288] << 6).astype(np.uint16)
    flat = (s16[288:].ravel() << 6).astype(np.uint16)
    n = len(flat)
    slots = np.empty_like(flat)
    slots[0::2] = flat[:n // 2]
    slots[1::2] = flat[n // 2:]
    src_rgb = np_yuv_to_rgb(y_sh, slots, 10)
    sr = np.repeat(np.repeat(src_rgb, 2, 0), 2, 1)
    ref_nv12 = np_rgb_to_nv12(sr.astype(np.float32) / 255.0)
    raw_out = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(out),
         "-map", "0:v:0", "-frames:v", "1", "-f", "rawvideo",
         "-pix_fmt", "yuv420p", "-"], capture_output=True,
        creationflags=WINDOWS_CREATE_FLAGS).stdout
    o = np.frombuffer(raw_out, np.uint8).reshape(864, 1024)
    psnr = psnr_u8(o[:576], ref_nv12[:576])
    # testsrc2 硬彩条边 + toy 最近邻 2x 的插值口径差基线 ~39dB（实测 39.2）
    assert psnr >= 35.0, f"首帧 Y PSNR={psnr:.1f}"

    # 续跑：删掉末段产物 + checkpoint 保留前两段 → 重跑只补末段
    work = tmp_path / "segmented" / "gpu_e2e"
    import json

    ck = json.loads((work / "checkpoint.json").read_text())
    ck["done"] = [s_ for s_ in ck["done"] if s_ < 40]
    (work / "checkpoint.json").write_text(json.dumps(ck))
    for p in work.glob("seg_000040*.mp4"):
        p.unlink()
    stats2 = asyncio.run(run_once(20))
    assert stats2.frames == 48
    r2 = subprocess.run(
        [str(ffmpeg_bin()).replace("ffmpeg.exe", "ffprobe.exe"), "-v", "error",
         "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv", str(out)],
        capture_output=True, text=True, creationflags=WINDOWS_CREATE_FLAGS)
    assert "48" in r2.stdout, r2.stdout

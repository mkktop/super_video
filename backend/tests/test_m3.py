"""M3-A 测试：Real-CUGAN / RIFE 补帧 / 7z 包下载。"""
import asyncio
import os
from pathlib import Path

import numpy as np
import pytest

from sv.models import manager
from sv.models.registry import get_model, model_file
from sv.paths import MODELS_DIR, TEMP_DIR

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_m3.db"))

RIFE_ONNX = MODELS_DIR / "rife-v4.26" / "rife_v4.26.onnx"
CUGAN_X2 = MODELS_DIR / "real-cugan" / "up2x-latest-conservative.onnx"


class Nearest2x:
    scale = 2

    def process(self, frame):
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


def _skip_if_no_models():
    if not RIFE_ONNX.exists() or not CUGAN_X2.exists():
        pytest.skip("模型未安装（models_store 缺 real-cugan / rife-v4.26）")


def test_cugan_registry_and_fp16_flag():
    spec = get_model("real-cugan")
    assert spec.scale == [2, 3, 4]
    assert spec.fp16 is False  # fp16 转换在 DML 加载崩溃，必须禁用
    p = model_file(spec, 2, "fp16", "denoise3")
    assert p.name == "up2x-latest-denoise3x.onnx"
    assert not p.stem.endswith("_fp16")  # fp16 请求被 fp16=False 挡住
    assert model_file(spec, 4, "fp32", None).name == "up4x-latest-conservative.onnx"


def test_cugan_engine_dml_runs():
    _skip_if_no_models()
    from sv.engines.onnx_engine import OnnxSrEngine

    spec = get_model("real-cugan")
    eng = OnnxSrEngine(model_file(spec, 2, variant="conservative"), 2, io=spec.io)
    eng.load()
    frame = np.random.default_rng(3).integers(0, 256, (100, 64, 3), dtype=np.uint8)
    out = eng.process(frame)
    assert out.shape == (200, 128, 3) and out.dtype == np.uint8
    assert int(out.std()) > 5


def test_rife_unit_plausibility():
    _skip_if_no_models()
    from sv.engines.rife import Rife2x

    r = Rife2x(RIFE_ONNX)
    r.load()
    rng = np.random.default_rng(5)
    a = np.clip(rng.normal(60, 30, (120, 160, 3)), 0, 255).astype(np.uint8)
    b = np.clip(rng.normal(200, 30, (120, 160, 3)), 0, 255).astype(np.uint8)
    mid = r.interpolate(a, b)
    assert mid.shape == a.shape and mid.dtype == np.uint8

    def psnr(x, y):
        m = ((x.astype(np.float64) - y.astype(np.float64)) ** 2).mean()
        return 10 * np.log10(255**2 / m)

    blend = ((a.astype(np.float64) + b) / 2).round().astype(np.uint8)
    assert psnr(mid, blend) > 12, "中间帧应接近线性混合（光流修正）"
    assert psnr(mid, a) < 45 and psnr(mid, b) < 45, "中间帧不应等于任一端点"


def test_interp_pipeline_doubles_fps():
    _skip_if_no_models()
    from sv.engines.rife import Rife2x
    from sv.pipeline.probe import probe
    from sv.pipeline.stream import EncodeOpts, StreamPipeline

    clip = TEMP_DIR / "stream_in.mp4"
    if not clip.exists():
        # 自足：字母序 test_m3 先于 test_stream，全新环境（CI）没人先生成
        import subprocess as _sp

        from sv.paths import ffmpeg_bin
        from sv.utils.process import WINDOWS_CREATE_FLAGS as _WCF

        TEMP_DIR.mkdir(exist_ok=True)
        _sp.run(
            [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", "3", "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(clip)],
            check=True, creationflags=_WCF,
        )
    info = probe(clip)
    r = Rife2x(RIFE_ONNX)
    r.load()
    out = TEMP_DIR / "stream_out_interp.mp4"
    stats = asyncio.run(StreamPipeline(info, out, Nearest2x(), EncodeOpts(), interp=r).run())
    assert stats.frames == info.total_frames * 2
    o = probe(out)
    assert o.fps == pytest.approx(info.fps * 2, rel=0.02), "输出帧率必须翻倍"
    assert abs(o.duration_s - info.duration_s) < 0.5, "时长保持（音画同步前提）"
    assert o.has_audio


def test_archive_download_extracts_member(tmp_path, monkeypatch):
    """7z 包内单文件下载提取（file:// 本地 7z 模拟远端）。"""
    import hashlib

    import sv.models.registry as reg
    from sv.models.registry import ModelSpec

    src7z = Path(__file__).resolve().parents[2] / ".tmp" / "m3dl" / "rife_v4.26.7z"
    if not src7z.exists():
        pytest.skip("本地 7z 不存在")
    store = tmp_path / "models"
    store.mkdir(parents=True)
    monkeypatch.setattr(reg, "MODELS_DIR", store)
    spec = ModelSpec(
        id="fake-rife", name="f", engine="onnx", scale=[1], content=[],
        speed="fast", vram_gb=1,
        files=[{
            "name": "rife.onnx", "url": src7z.as_uri(),
            "archive": "rife_v2/rife_v4.26.onnx",
            "sha256": hashlib.sha256(RIFE_ONNX.read_bytes()).hexdigest(),
            "size": RIFE_ONNX.stat().st_size,
        }],
    )
    manager.download(spec)
    got = store / "fake-rife" / "rife.onnx"
    assert got.exists() and got.stat().st_size == RIFE_ONNX.stat().st_size

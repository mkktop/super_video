"""真模型端到端：合成 240p 片段 x4 超分，校验输出规格与音轨。"""
import subprocess
from pathlib import Path

import pytest

from sv.engines.onnx_engine import OnnxSrEngine
from sv.models.registry import get_model, model_file
from sv.models import manager
from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.probe import probe, validate_m0
from sv.pipeline.stream import EncodeOpts, StreamPipeline
from sv.utils.process import WINDOWS_CREATE_FLAGS


def make_clip(path: Path):
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", "3", "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


@pytest.fixture(scope="module")
def clip():
    TEMP_DIR.mkdir(exist_ok=True)
    p = TEMP_DIR / "e2e_in.mp4"
    make_clip(p)
    return p


def maybe_skip(model_id):
    spec = get_model(model_id)
    if not manager.is_downloaded(spec):
        pytest.skip(f"模型 {model_id} 未下载")


@pytest.mark.parametrize("model_id,scale", [
    ("realesr-animevideov3", 4),
    ("realesr-animevideov3", 2),  # 原生 x2（v2-animevideo-xsx2）
    pytest.param("realesrgan-x4plus", 4,
                 marks=pytest.mark.skipif(
                     not __import__("conftest").dml_available(),
                     reason="RRDB 大模型 CPU 回落太慢，仅在有 DML 的机器跑")),
])
def test_e2e_super_resolution(clip, model_id, scale):
    maybe_skip(model_id)
    spec = get_model(model_id)
    info = probe(clip)
    validate_m0(info)
    engine = OnnxSrEngine(
        model_file(spec, scale), scale, io=spec.io, tile=spec.tile_hint,
    )
    engine.load()
    out = TEMP_DIR / f"e2e_out_{model_id}_x{scale}.mp4"
    stats = __import__("asyncio").run(
        StreamPipeline(info, out, engine, EncodeOpts()).run()
    )
    assert stats.frames == info.total_frames
    o = probe(out)
    assert (o.width, o.height) == (320 * scale, 180 * scale)
    assert o.has_audio

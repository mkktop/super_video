"""图片序列输出：整视频超分后逐帧导出 PNG/JPG，帧图按 6 位编号命名（000001 起）。

覆盖：
- encoder_cmd 图片分支（%06d 模式 + -start_number 全局编号 + 不带音轨/封装参数）；
- SegmentedPipeline 每段直写最终目录、跨段编号连续、断点续跑不重写已完成帧；
- 全新任务复用同名目录时清理高帧号残留；JPG 档（mjpeg qscale 2）。
"""
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from sv.engines.onnx_engine import OnnxSrEngine
from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.probe import probe
from sv.pipeline.segmented import SegmentedPipeline
from sv.pipeline.stream import EncodeOpts, encoder_cmd
from sv.utils.process import WINDOWS_CREATE_FLAGS

BUNDLED_X4 = Path(__file__).parent.parent / "sv" / "models" / "bundled" / "RealESR-AnimeVideo-v3_x4.onnx"


def make_video(path: Path, w=160, h=90, duration=1, fps=24) -> None:
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}",
        "-t", str(duration), "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, check=True, creationflags=WINDOWS_CREATE_FLAGS)


@pytest.fixture(scope="module")
def sample():
    TEMP_DIR.mkdir(exist_ok=True)
    p = TEMP_DIR / "imgout_in.mp4"
    make_video(p)
    return p


def _engine():
    eng = OnnxSrEngine(BUNDLED_X4, scale=4, device="cpu")
    eng.load()
    return eng


def _names(d: Path, ext: str, total: int) -> list[str]:
    return sorted(f.name for f in d.glob(f"*.{ext}")) == [
        f"{i:06d}.{ext}" for i in range(1, total + 1)
    ]


def test_encoder_cmd_image():
    """图片分支：模式路径 + start_number 全局编号 + 不带音轨/封装参数。"""
    enc = EncodeOpts(out_kind="png")
    cmd = encoder_cmd(Path("in.mp4"), Path("o") / "%06d.png", 640, 360, 640, 360,
                      "24/1", enc, True, "aac", start_number=49)
    assert cmd[cmd.index("-start_number") + 1] == "49"
    assert cmd[-1].endswith("%06d.png")
    assert cmd[cmd.index("-f", cmd.index("-i")) + 1] == "image2"
    assert "-c:a" not in cmd and "-movflags" not in cmd
    assert "-c:v" not in cmd  # png 按扩展名用默认编码器


def test_encoder_cmd_image_jpg_scale():
    """JPG：mjpeg + qscale 2；目标尺寸≠引擎输出时编码器内 lanczos 缩放。"""
    cmd = encoder_cmd(Path("in.mp4"), Path("o") / "%06d.jpg", 640, 360, 320, 180,
                      "24/1", EncodeOpts(out_kind="jpg"), True, "aac")
    assert cmd[cmd.index("-c:v") + 1] == "mjpeg"
    assert cmd[cmd.index("-q:v") + 1] == "2"
    assert cmd[cmd.index("-vf") + 1] == "scale=320:180:flags=lanczos"


def test_segmented_png_e2e(sample):
    """PNG 序列端到端（CPU）：帧数=源帧数、跨段编号连续、尺寸=原生超分。"""
    eng = _engine()
    info = probe(sample)
    out = TEMP_DIR / "imgout_frames"
    stats = asyncio.run(SegmentedPipeline(
        info, out, eng, EncodeOpts(out_kind="png"),
        task_id="t_imgout", seg_frames=12, cleanup=False,
    ).run())
    assert _names(out, "png", info.total_frames)  # 24 帧 / 每段 12 → 两段编号连续
    assert stats.frames == info.total_frames
    assert stats.out_bytes > 0
    with Image.open(out / "000001.png") as im:
        assert im.size == (info.width * 4, info.height * 4)
        assert im.mode == "RGB"


def test_segmented_png_resume_numbering(sample):
    """断点续跑：只补缺失段，帧号全局连续，已完成帧不被重写。"""
    eng = _engine()
    info = probe(sample)
    out = TEMP_DIR / "imgout_frames"
    ckpt = TEMP_DIR / "segmented" / "t_imgout" / "checkpoint.json"
    first_hash = hashlib.sha256((out / "000001.png").read_bytes()).hexdigest()
    # 模拟中断：第二段帧图丢失、checkpoint 只记了第一段
    for i in range(13, info.total_frames + 1):
        (out / f"{i:06d}.png").unlink(missing_ok=True)
    ckpt.write_text(json.dumps({"done": [0]}))
    asyncio.run(SegmentedPipeline(
        info, out, eng, EncodeOpts(out_kind="png"),
        task_id="t_imgout", seg_frames=12, cleanup=False,
    ).run())
    assert _names(out, "png", info.total_frames)
    assert hashlib.sha256((out / "000001.png").read_bytes()).hexdigest() == first_hash


def test_stale_frames_pruned(sample):
    """全新任务复用同名目录：残留的高帧号旧图在开跑前被清掉。"""
    out = TEMP_DIR / "imgout_frames"
    (out / "999999.png").write_bytes(b"stale")
    eng = _engine()
    info = probe(sample)
    asyncio.run(SegmentedPipeline(
        info, out, eng, EncodeOpts(out_kind="png"),
        task_id="t_imgout2", seg_frames=12,
    ).run())
    assert not (out / "999999.png").exists()
    assert _names(out, "png", info.total_frames)


def test_segmented_jpg(sample):
    """JPG 序列端到端：编号连续、格式/尺寸正确。"""
    eng = _engine()
    info = probe(sample)
    out = TEMP_DIR / "imgout_jpg_frames"
    asyncio.run(SegmentedPipeline(
        info, out, eng, EncodeOpts(out_kind="jpg"),
        task_id="t_imgout_jpg", seg_frames=12,
    ).run())
    assert _names(out, "jpg", info.total_frames)
    with Image.open(out / "000001.jpg") as im:
        assert im.format == "JPEG"
        assert im.size == (info.width * 4, info.height * 4)

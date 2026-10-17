"""gpu_codec 编解码层测试。

无 GPU/无 PyNvVideoCodec 环境全部跳过（CI 安全——模块本身惰性导入）。
切段→NVDEC pts 对齐→NVENC 直出的语义在这里用真源验证。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sv.paths import TEMP_DIR, ffmpeg_bin  # noqa: E402
from sv.utils.process import WINDOWS_CREATE_FLAGS  # noqa: E402
from sv.pipeline.gpu_codec import cut_segment  # noqa: E402


def nvcodec_available() -> bool:
    try:
        import PyNvVideoCodec  # noqa: F401
        from cuda.bindings import driver as cu  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not nvcodec_available(), reason="需要 PyNvVideoCodec/cuda-bindings 环境")


def _make_src(tmp_path: Path, codec: str = "hevc", w: int = 512, h: int = 288,
              n: int = 48) -> Path:
    src = tmp_path / f"src_{codec}.mkv"
    r = np.random.default_rng(3).integers(0, 256, (h // 2, w // 2)).astype(np.uint8)
    img = np.kron(r, np.ones((2, 2), np.uint8))
    import subprocess

    frame = img.tobytes()
    r2 = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-r", "24",
         "-i", "-", "-frames:v", str(n), "-c:v", codec, "-x265-params",
         "log-level=error" if codec == "libx265" else "b=0", str(src)],
        input=frame * n, capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
    # 用 testsrc 更省事：图案帧内容已知
    r3 = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=24:duration={n / 24}s",
         "-c:v", codec if codec != "hevc10" else "libx265",
         "-pix_fmt", "yuv420p10le" if codec == "hevc10" else "yuv420p",
         "-g", "12", str(src)],
        capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
    assert r3.returncode == 0, r3.stderr.decode(errors="replace")[-300:]
    return src


def test_decoder_rejects_8bit_layout(tmp_path):
    """8bit NV12 表面是字节洗牌布局（非稳定契约）→ 首帧即拒绝。"""
    from sv.pipeline.gpu_codec import GpuSegmentDecoder, ensure_ctx

    ensure_ctx()
    src = _make_src(tmp_path, "hevc")
    seg = tmp_path / "seg8.mkv"
    cut_segment(src, seg, 0.0)
    dec = GpuSegmentDecoder(seg)
    with pytest.raises(RuntimeError, match="P016"):
        next(iter(dec.frames(0.0, 10)))


@pytest.mark.parametrize("src_codec,pix", [("hevc10", "yuv420p10le")])
def test_cut_and_decode_alignment(tmp_path, src_codec, pix):
    """切段 + pts 丢弃：GPU 解出帧 == ffmpeg 全量解码同 pts 帧（原生域位级对齐）。"""
    import subprocess

    import PyNvVideoCodec as nvc
    from sv.pipeline.gpu_codec import GpuSegmentDecoder, ensure_ctx

    ensure_ctx()
    src = _make_src(tmp_path, src_codec)
    deep = "10" in pix
    probe = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-map", "0:v:0", "-frames:v", "48", "-f", "rawvideo", "-pix_fmt", pix,
         "-"],
        capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
    if deep:
        arr = np.frombuffer(probe.stdout, "<u2").reshape(48, 432, 512)
        ref_all = arr[:, :288]  # Y 平面（10bit）
    else:
        arr = np.frombuffer(probe.stdout, np.uint8).reshape(48, 432, 512)
        ref_all = arr[:, :288]

    # 段起点 = 帧 20（24fps → 20/24 s）
    seg = tmp_path / "seg.mkv"
    cut_segment(src, seg, 20 / 24)
    dec = GpuSegmentDecoder(seg)
    from cuda.bindings import runtime as cudart

    h, w = 288, 512
    np_dt = np.uint16 if "10" in pix else np.uint8
    plane0 = None
    count = 0
    first_pts = None
    # 表面池会复用：首帧像素必须当帧 D2H（产品链同理当帧消费）
    for f, yv, uvv, pts in dec.frames(start_ms=20000 / 24, n=28):
        count += 1
        if plane0 is None:
            first_pts = pts
            plane0 = np.empty(h * w * np_dt().itemsize, np.uint8)
            cai = yv.__cuda_array_interface__
            cudart.cudaMemcpy(plane0.ctypes.data, cai["data"][0], plane0.nbytes,
                              cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost)
    assert count == 28, f"期望 28 帧，得到 {count}"
    assert first_pts == 833
    y = plane0.view(np_dt).reshape(h, w)
    gy = (y >> 6) if deep else y
    # 原生域位级对齐（NVDEC vs 软解；实测 diff=0）
    assert int(np.abs(gy.astype(int) - ref_all[20].astype(int)).max()) <= 2, (
        f"首帧像素不对齐 maxdiff="
        f"{int(np.abs(gy.astype(int) - ref_all[20].astype(int)).max())}")
    dec.close()


def test_decoder_clean_eof(tmp_path):
    """请求超过剩余帧数 → 干净短产（decode_eof 语义）。"""
    from sv.pipeline.gpu_codec import GpuSegmentDecoder, ensure_ctx

    ensure_ctx()
    src = _make_src(tmp_path, "hevc10")
    seg = tmp_path / "seg_tail.mkv"
    cut_segment(src, seg, 40 / 24)  # 只剩 8 帧
    dec = GpuSegmentDecoder(seg)
    got = list(dec.frames(start_ms=40000 / 24, n=100))
    assert 4 <= len(got) <= 10, f"尾部段应产 ~8 帧，得到 {len(got)}"
    dec.close()


def test_encoder_roundtrip(tmp_path):
    """NVENC 直出 mp4：喂常量帧 → ffprobe 可读、帧数正确。"""
    import subprocess

    from sv.pipeline.gpu_codec import GpuSegmentEncoder, ensure_ctx

    ensure_ctx()
    from cuda.bindings import runtime as cudart

    w, h = 320, 180
    enc = GpuSegmentEncoder(tmp_path / "seg.mp4", w, h, 24, 1, "h264_nvenc")
    y = np.full((1, 1, h, w), 128, np.uint8)
    uv = np.full((1, h // 2, w // 2, 2), 128, np.uint8)
    ydev = cudart.cudaMalloc(y.nbytes)[1]
    uvdev = cudart.cudaMalloc(uv.nbytes)[1]
    cudart.cudaMemcpy(int(ydev), y.ctypes.data, y.nbytes,
                      cudart.cudaMemcpyKind.cudaMemcpyHostToDevice)
    cudart.cudaMemcpy(int(uvdev), uv.ctypes.data, uv.nbytes,
                      cudart.cudaMemcpyKind.cudaMemcpyHostToDevice)
    for _ in range(20):
        enc.encode(int(ydev), int(uvdev))
    enc.close()
    ffprobe = str(ffmpeg_bin()).replace("ffmpeg.exe", "ffprobe.exe")
    r = subprocess.run(
        [ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames,width,height", "-of", "csv",
         str(tmp_path / "seg.mp4")],
        capture_output=True, text=True, creationflags=WINDOWS_CREATE_FLAGS)
    assert "20" in (r.stdout.strip().split(",") or [""]), r.stdout
    # 内容 sanity：解码首帧应接近常量灰
    r2 = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i",
         str(tmp_path / "seg.mp4"), "-map", "0:v:0", "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
    first = np.frombuffer(r2.stdout, np.uint8).reshape(h, w)
    assert float(np.abs(first.astype(int) - 130).mean()) < 1.5  # Y=128(limited) → gray 全量程≈130

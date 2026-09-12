"""收尾缩略图定版：任务结束不再把暗场帧留成全黑任务卡。

素材用「前段黑场 + 后段 testsrc2」复现用户场景：处理收尾恰逢黑场时，
滚动预览的最后一帧是全黑的；rewrite_final_previews 应挑到后段亮帧重写，
源侧同时间点成对更新；全黑素材回退最亮候选；失败时保留原预览文件。
"""
import subprocess
from pathlib import Path

from sv.paths import ffmpeg_bin
from sv.server.preview_final import _pick_time, rewrite_final_previews
from sv.utils.process import WINDOWS_CREATE_FLAGS


def _make_half_black(path: Path, black_s: float = 1.2, tail_s: float = 0.8):
    """前段黑场后段 testsrc2（共 2s）——收尾恰逢暗场的素材。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=black:size=64x36:rate=24:duration={black_s}",
         "-f", "lavfi", "-i", f"testsrc2=size=64x36:rate=24:duration={tail_s}",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
         "-map", "[v]", "-c:v", "libx264", "-crf", "22",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS)


def _make_all_black(path: Path, seconds: float = 2.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=black:size=64x36:rate=24:duration={seconds}",
         "-c:v", "libx264", "-crf", "22", "-pix_fmt", "yuv420p", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS)


def _jpg_gray_mean(path: Path) -> float:
    from PIL import Image, ImageStat

    return ImageStat.Stat(Image.open(path).convert("L")).mean[0]


def test_pick_time_skips_black_lead(tmp_path):
    v = tmp_path / "half.mp4"
    _make_half_black(v)
    t = _pick_time(v, 2.0)
    assert t is not None
    assert t >= 1.2  # 前 1.2s 全黑：选中的必须是后段亮画面


def test_rewrite_replaces_black_preview_with_bright_pair(tmp_path):
    out = tmp_path / "half.mp4"
    _make_half_black(out)
    src = tmp_path / "half_src.mp4"
    _make_half_black(src)
    out_jpg, src_jpg = tmp_path / "t.jpg", tmp_path / "t_src.jpg"
    out_jpg.write_bytes(b"stale-rolling-preview")  # 模拟滚动预览留下的黑帧产物
    rewrite_final_previews(out, src, out_jpg, src_jpg)
    assert _jpg_gray_mean(out_jpg) > 20  # 定版成亮帧
    assert _jpg_gray_mean(src_jpg) > 20  # 源侧同时间点成对
    assert not (tmp_path / "t.jpg.tmp").exists()  # 原子替换后无残留


def test_rewrite_all_black_still_picks_best_candidate(tmp_path):
    v = tmp_path / "black.mp4"
    _make_all_black(v)
    out_jpg = tmp_path / "t2.jpg"
    rewrite_final_previews(v, None, out_jpg, tmp_path / "t2_src.jpg")
    assert out_jpg.is_file()  # 全黑素材也定版（最亮候选，如实代表内容）
    assert not (tmp_path / "t2_src.jpg").exists()  # 源缺省时跳过源侧


def test_rewrite_failure_keeps_existing_preview(tmp_path):
    out_jpg = tmp_path / "t3.jpg"
    out_jpg.write_bytes(b"old-bytes")
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"garbage")  # probe 失败 → 整体放弃，保留原预览
    rewrite_final_previews(fake, None, out_jpg, tmp_path / "t3_src.jpg")
    assert out_jpg.read_bytes() == b"old-bytes"

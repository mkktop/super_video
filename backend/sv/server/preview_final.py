"""任务收尾缩略图定版：从成片挑一帧"正常画面"重写队列预览对。

处理中的预览每 5s 滚动覆盖、任务结束停在最后一帧——收尾恰逢暗场/转场时，
任务卡就是一张全黑图。done 事件前按候选时间点从成片探测亮度（复用
analyze 的 256 宽采样帧，单次 seek 解一帧开销极小），挑最亮的一帧，
同时间点再从源抽一帧，成对替换输出/源预览。

候选点位避开两端（片头片尾常是黑场或制作信息）、中段优先向两侧扩散；
不设黑场硬门槛取"最亮候选"：素材整体暗调时它也是最具代表性的一帧，
而普通素材里最亮候选足以跳出转场黑帧。任何失败保留滚动预览不动——
尽力而为，绝不影响主流程。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..paths import ffmpeg_bin
from ..utils.process import WINDOWS_CREATE_FLAGS

# 候选时间点（按时长比例）：中段优先向两侧扩散，避开 10% 边缘
_CANDIDATE_FRACS = (0.4, 0.55, 0.25, 0.7, 0.15, 0.85)
_THUMB_BOX = 960  # 与 pipeline.stream._save_jpg 同规格：预览长边上限


def _pick_time(video: Path, duration_s: float) -> float | None:
    """候选点位各抽一帧（256 宽采样），返回最亮一帧的时间戳；一帧都解不出返回 None。"""
    from ..pipeline.analyze import _decode_one_frame

    best_t: float | None = None
    best_mean = -1.0
    for frac in _CANDIDATE_FRACS:
        t = duration_s * frac
        frame = _decode_one_frame(video, t)
        if frame is None:
            continue
        mean = float(frame.mean())
        if mean > best_mean:
            best_t, best_mean = t, mean
    return best_t


def _save_frame_jpg(video: Path, t: float, dest: Path) -> bool:
    """t 时刻抽一帧写成 JPEG（长边 ≤960）；tmp+replace 防任务卡轮询读到半张图。"""
    tmp = dest.with_name(dest.name + ".tmp")
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{t:.3f}", "-i", str(video),
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-frames:v", "1",
        "-vf", f"scale={_THUMB_BOX}:{_THUMB_BOX}:force_original_aspect_ratio=decrease",
        "-q:v", "2", "-f", "image2", str(tmp),  # .tmp 后缀无封装可猜，显式 image2
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30,
                           creationflags=WINDOWS_CREATE_FLAGS)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


def rewrite_final_previews(out_video: Path, src_video: Path | None,
                           out_jpg: Path, src_jpg: Path) -> None:
    """任务收尾把滚动预览定版为挑好的一帧（输出 + 源同时间点成对）。"""
    try:
        from ..pipeline.probe import probe

        duration = probe(out_video).duration_s
        if duration <= 0:
            return
        t = _pick_time(out_video, duration)
        if t is None or not _save_frame_jpg(out_video, t, out_jpg):
            return
        if src_video is not None and Path(src_video).exists():
            _save_frame_jpg(Path(src_video), t, src_jpg)  # 源侧失败无妨：输出图已定版
    except Exception:
        pass  # 收尾预览尽力而为：失败保留滚动预览，不影响任务结果

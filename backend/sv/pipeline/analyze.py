"""源内容采样分析：动画/真人分类与隔行（梳状纹理）检测，供智能推荐。

不追求学术级准确——推荐是"帮用户把参数配到八成对"的辅助，规则可解释、
误判代价低（用户在推荐卡上肉眼可复核）。统计特征：

- flat_ratio：平坦像素占比（3x3 邻域内梯度≈0 的像素比例）。动画大色块
  平涂，占比显著高于真人实拍（皮肤/纹理/胶片颗粒处处有微差）。
- comb_frac：梳状像素占比（垂直二阶差分超阈值的像素比例，TDecomb 式
  隔行检测）。隔行源的相邻行来自不同场，梳齿处处存在；逐行源只有物体
  边缘少量像素超阈。与 ffprobe 声明的 field_order 互补（容器常误标）。

采样解码缩到 ~256 宽再出 rgb24：统计只关心区域分布特征，不需要全分辨率；
每帧独立 seek 取 4 个时间点（片头/片尾常是黑场或制作信息，避开 0% 与 100%）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from ..paths import ffmpeg_bin
from ..utils.process import WINDOWS_CREATE_FLAGS
from .probe import MediaInfo

# 采样与统计参数（8bit 量纲；对 10bit 源 ffmpeg 先转 8bit 再出 rgb24）
SAMPLE_W = 256          # 采样帧宽度（高度按比例取整）
N_SAMPLES = 4           # 采样帧数（按时长均匀分布，避开首尾 10%）
FLAT_TOL = 2            # 梯度容差：|d|<=2 视为平坦（压住轻噪/抖动）
COMB_THRESH = 20        # 梳状像素阈值（TDecomb 默认同量级）
COMB_MIN = 0.06         # comb_frac 超过此值判隔行（对齐 recommend.py 阈值）
FLAT_ANIME_MIN = 0.42   # flat_ratio 超过此值判动画


def _decode_one_frame(path: Path, t_s: float) -> np.ndarray | None:
    """seek 到 t_s 解码 1 帧并缩放到固定 256 宽的 rgb24，返回 HWC uint8。"""
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{t_s:.3f}", "-i", str(path),
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-frames:v", "1",
        "-vf", f"scale={SAMPLE_W}:-2:flags=bilinear",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30,
                           creationflags=WINDOWS_CREATE_FLAGS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    raw = np.frombuffer(r.stdout, dtype=np.uint8)
    h = raw.size // (SAMPLE_W * 3)
    if h < 8 or raw.size != h * SAMPLE_W * 3:
        return None  # 残包/异常尺寸按本帧失败处理
    return raw.reshape(h, SAMPLE_W, 3).copy()


def _frame_stats(frame: np.ndarray) -> tuple[float, float] | None:
    """单帧统计 → (flat_ratio, comb_frac)。"""
    g = frame.mean(axis=2)  # 灰度（统计用途，均值足够）
    dx = np.abs(np.diff(g, axis=1))
    dy = np.abs(np.diff(g, axis=0))
    flat = ((dx[:-1, :] <= FLAT_TOL) & (dy[:, :-1] <= FLAT_TOL)).mean()
    # 梳状：|f[y-1] + f[y+1] - 2*f[y]|（去边一列防 dx 边界效应干扰）
    f = g[:, 1:-1].astype(np.int16)
    d2 = np.abs(f[:-2] + f[2:] - 2 * f[1:-1])
    comb = float((d2 > COMB_THRESH).mean())
    return float(flat), comb


def sample_frame_stats(info: MediaInfo) -> dict | None:
    """对源采样 N 帧做内容统计；全部失败返回 None（调用方按"无推荐"处理）。"""
    duration = info.duration_s
    if duration <= 0:
        return None
    offsets = [duration * f for f in (0.1, 0.37, 0.63, 0.9)][:N_SAMPLES]
    flats: list[float] = []
    combs: list[float] = []
    for t in offsets:
        frame = _decode_one_frame(info.path, t)
        if frame is None:
            continue
        st = _frame_stats(frame)
        if st is not None:
            flats.append(st[0])
            combs.append(st[1])
    if not flats:
        return None
    return {
        "flat_ratio": round(sum(flats) / len(flats), 4),
        "comb_frac": round(sum(combs) / len(combs), 4),
        "frames": len(flats),
    }

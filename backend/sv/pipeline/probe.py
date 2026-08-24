"""ffprobe 封装：媒体信息探测 + 输入校验（HDR 拒绝；10bit/VFR 接受并转换）。"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from ..paths import ffprobe_bin
from ..utils.process import WINDOWS_CREATE_FLAGS


class UnsupportedMedia(Exception):
    """M0 范围外的输入（10bit / HDR / VFR / 无视频流等）。"""


@dataclass
class AudioStream:
    codec: str
    channels: int
    sample_rate: int


@dataclass
class MediaInfo:
    path: Path
    container: str
    duration_s: float
    width: int
    height: int
    fps: float
    fps_str: str  # 原始分数形式，如 "24000/1001"，编码时原样传递避免漂移
    vfr: bool
    video_codec: str
    pix_fmt: str
    bit_depth: int
    color_transfer: str
    audio: list[AudioStream] = field(default_factory=list)
    subtitle_count: int = 0
    subtitles: list[str] = field(default_factory=list)  # 字幕流编码名（subrip/hdmv_pgs_subtitle…）
    total_frames: int = 0

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)


def _ffprobe_json(args: list[str]) -> dict:
    proc = subprocess.run(
        [ffprobe_bin(), "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        creationflags=WINDOWS_CREATE_FLAGS,
    )
    if proc.returncode != 0:
        raise UnsupportedMedia(
            f"ffprobe 失败: {proc.stderr.decode('utf-8', 'replace').strip()[-500:]}"
        )
    return json.loads(proc.stdout)


def _bit_depth(pix_fmt: str) -> int:
    """从 pix_fmt 推断每通道位数：yuv420p=8, yuv420p10le=10, gray10le=10, rgb48le=16。"""
    pf = pix_fmt.lower()
    if pf.startswith(("p010",)):
        return 10
    if pf.startswith(("p016",)):
        return 16
    m = re.search(r"p(\d+)", pf)  # yuv420p10le / gbrp12le
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"[a-z]+(\d+)(?:le|be)?", pf)  # gray10le
    if m:
        return int(m.group(1))
    if "48" in pf:  # rgb48le / bgr48
        return 16
    return 8


# 探测缓存：向导选文件 probe 一次、提交任务再 probe 一次（含 -count_packets 全量
# 扫包），同一文件第二次就是纯浪费。按 路径+mtime+size+模式 失效，本地文件足够可靠。
# 返回对象只读共享（下游无变异），容量到顶整体清空（探测热点就几个文件，不值得 LRU）。
_probe_cache: dict[tuple[str, int, int, bool], MediaInfo] = {}
_PROBE_CACHE_MAX = 64


def probe(path: str | Path, exact_frames: bool = True) -> MediaInfo:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, exact_frames)
    cached = _probe_cache.get(key)
    if cached is not None:
        return cached
    data = _ffprobe_json(
        ["-print_format", "json", "-show_format", "-show_streams", str(path)]
    )
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        raise UnsupportedMedia(f"{path.name}: 没有视频流")

    r_rate = v.get("r_frame_rate", "0/1")
    avg_rate = v.get("avg_frame_rate", "0/1")
    fps = float(Fraction(r_rate)) if Fraction(r_rate) > 0 else float(Fraction(avg_rate))
    avg = float(Fraction(avg_rate)) if Fraction(avg_rate) > 0 else fps
    vfr = abs(fps - avg) / max(fps, 1e-6) > 0.02

    duration = float(data.get("format", {}).get("duration", 0) or v.get("duration", 0) or 0)
    audio = [
        AudioStream(
            codec=s.get("codec_name", "?"),
            channels=int(s.get("channels", 0)),
            sample_rate=int(s.get("sample_rate", 0)),
        )
        for s in data.get("streams", [])
        if s.get("codec_type") == "audio"
    ]
    subs = [s for s in data.get("streams", []) if s.get("codec_type") == "subtitle"]

    pix_fmt = v.get("pix_fmt", "yuv420p")
    info = MediaInfo(
        path=path,
        container=data.get("format", {}).get("format_name", "?"),
        duration_s=duration,
        width=int(v["width"]),
        height=int(v["height"]),
        fps=fps,
        fps_str=r_rate if Fraction(r_rate) > 0 else avg_rate,
        vfr=vfr,
        video_codec=v.get("codec_name", "?"),
        pix_fmt=pix_fmt,
        bit_depth=_bit_depth(pix_fmt),
        color_transfer=v.get("color_transfer", "bt709") or "bt709",
        audio=audio,
        subtitle_count=len(subs),
        subtitles=[s.get("codec_name", "?") for s in subs],
    )

    if vfr:
        # VFR 按平均帧率 CFR 化（解码端 -r 重采样），时基与总帧数都用 avg
        info.fps = avg
        info.fps_str = avg_rate

    if exact_frames:
        if vfr:
            info.total_frames = max(1, int(round(duration * avg)))
        else:
            info.total_frames = max(_count_frames(path), int(round(duration * fps)))
    else:
        info.total_frames = int(duration * info.fps)
    if len(_probe_cache) >= _PROBE_CACHE_MAX:
        _probe_cache.clear()
    _probe_cache[key] = info
    return info


def _count_frames(path: Path) -> int:
    """精确帧数：-count_packets 全量扫描；部分封装会少计 1~2 帧，与 duration×fps 取大。"""
    data = _ffprobe_json(
        [
            "-select_streams", "v:0", "-count_packets",
            "-show_entries", "stream=nb_read_packets",
            "-print_format", "json", str(path),
        ]
    )
    n = int(data["streams"][0].get("nb_read_packets", 0) or 0)
    return n


def validate_m0(info: MediaInfo) -> None:
    """M3 起接受 10bit（解码统一转 8bit SDR）与 VFR（按平均帧率 CFR 化）；HDR 仍拒绝。"""
    if info.color_transfer in ("smpte2084", "arib-std-b67"):
        raise UnsupportedMedia(
            f"{info.path.name}: HDR 片源（{info.color_transfer}），暂不支持；"
            f"请先在外部工具转 SDR 后再处理"
        )
    if info.total_frames <= 0:
        raise UnsupportedMedia(f"{info.path.name}: 无法确定总帧数")

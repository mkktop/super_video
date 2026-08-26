"""视频剪切（移植自 All_In_One_Video 的 smart cut，参数与兜底策略对齐）。

三种模式：
- fast  关键帧吸附纯复制：秒级完成、完全无损；起点吸附到入点前的最近关键帧
        （实际起点会提前，最多一个 GOP，通过 actual_start 返回给 UI 呈现）
- smart 智能剪切（默认）：起点不在关键帧时 头部转码段 + 尾部流复制段 + concat，
        帧级精确且只有头部付出转码代价；起点恰在关键帧则退化为纯复制
- exact 精确转码：整段重编码（源编码不支持无损分段/强制帧精确时的兜底）

smart 两段式的成立条件（任一不满足则整段转码并附 notice）：
- 视频编码为 h264/hevc（concat 复制段要求与转码段同编码家族）
- 无音轨或音轨为 aac（转码段音频固定转 aac，与复制段混合编码 concat 会坏）
"""
from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import TEMP_DIR, ffmpeg_bin, ffprobe_bin
from ..utils.process import WINDOWS_CREATE_FLAGS, kill_tree
from .probe import MediaInfo, probe

# 单段 ffmpeg 超时：驱动异常/坏文件挂起时不能永久堵死 trim 队列
FFMPEG_TIMEOUT_S = 1800.0


class TrimError(RuntimeError):
    pass


class TrimCanceled(Exception):
    """外部请求取消（cancel 端点已杀当前 ffmpeg 段）。"""


@dataclass
class TrimResult:
    output: Path
    requested_start: float
    actual_start: float  # fast 吸附后的真实起点；smart/exact == requested_start
    duration_s: float
    mode: str  # 实际执行的模式（smart 可能降级为 exact）
    notices: list[str] = field(default_factory=list)


# ---- 关键帧（逻辑移植自 AIOV shared/planner.ts）----


def scan_keyframes(path: Path) -> list[float]:
    """按 packets 的 K 标记扫关键帧时间点（升序）。"""
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
         "-show_packets", "-show_entries", "packet=pts_time,flags",
         "-of", "json", str(path)],
        capture_output=True, text=True, creationflags=WINDOWS_CREATE_FLAGS,
    )
    if r.returncode != 0:
        raise TrimError(f"关键帧扫描失败: {(r.stderr or '')[-300:]}")
    kfs: list[float] = []
    for p in json.loads(r.stdout or "{}").get("packets", []):
        t = p.get("pts_time")
        if t is not None and "K" in p.get("flags", ""):
            kfs.append(float(t))
    return sorted(kfs)


def _last_kf_at_or_before(kfs: list[float], t: float) -> float | None:
    ans = None
    for k in kfs:
        if k <= t:
            ans = k
        else:
            break
    return ans


def _first_kf_at_or_after(kfs: list[float], t: float) -> float | None:
    for k in kfs:
        if k >= t:
            return k
    return None


def _is_on_keyframe(kfs: list[float], t: float, half_frame: float) -> bool:
    return any(abs(k - t) <= half_frame for k in kfs)


def plan_segments(
    start: float, end: float, kfs: list[float], frame_dur: float, mode: str,
) -> tuple[list[tuple[str, float, float]], float]:
    """返回 ([(kind, s, e)], actual_start)。kind ∈ copy|encode。"""
    if not end > start:
        raise TrimError("入点必须早于出点")
    if mode == "exact":
        return [("encode", start, end)], start
    if mode == "fast":
        kf = _last_kf_at_or_before(kfs, start)
        copy_start = kf if kf is not None else 0.0
        return [("copy", copy_start, end)], copy_start
    # smart
    if _is_on_keyframe(kfs, start, frame_dur / 2):
        kf = _first_kf_at_or_after(kfs, start) or 0.0
        return [("copy", kf, end)], kf
    kf_after = _first_kf_at_or_after(kfs, start)
    if kf_after is None or kf_after >= end:
        return [("encode", start, end)], start
    return [("encode", start, kf_after), ("copy", kf_after, end)], start


# ---- ffmpeg 执行 ----


def _run_ffmpeg(cmd: list[str], seg_dur: float, progress_cb, p_from: float, p_to: float,
                on_spawn=None, cancel_check=None) -> None:
    """跑一段 ffmpeg；-progress pipe:1 的 out_time 映射到 [p_from, p_to) 进度区间。

    on_spawn(proc) 把进程句柄交给调用方（取消端点 kill 用）；
    看门狗线程兜底超时——stdout 挂起无输出时 for 循环醒不过来。
    cancel_check() 为真（外部取消杀了进程）抛 TrimCanceled——必须区别于
    TrimError，否则硬编回退逻辑会把"被取消"当成"硬编失败"再跑一遍软编。
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=WINDOWS_CREATE_FLAGS,
    )
    if on_spawn is not None:
        on_spawn(proc)
    timer = threading.Timer(FFMPEG_TIMEOUT_S, kill_tree, args=(proc.pid,))
    timer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.decode("utf-8", "replace").strip()
            if text.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    us = int(text.split("=", 1)[1])
                except ValueError:
                    continue
                done = min(1.0, (us / 1e6) / seg_dur) if seg_dur > 0 else 1.0
                if progress_cb:
                    progress_cb(p_from + (p_to - p_from) * done)
        proc.wait()
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        if proc.returncode != 0:
            if cancel_check is not None and cancel_check():
                raise TrimCanceled()
            raise TrimError(f"ffmpeg 失败(rc={proc.returncode}): {err[-400:]}")
    finally:
        timer.cancel()


def _copy_args(input: Path, start: float, dur: float, out: Path) -> list[str]:
    return [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-progress", "pipe:1",
        "-ss", f"{start:.6f}", "-i", str(input), "-t", f"{dur:.6f}",
        "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy", "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart", "-f", "mp4", str(out),
    ]


def _encoder_args(codec: str, nvenc: bool) -> tuple[str, list[str]]:
    """转码段编码器：NVENC 可用则硬编（剪切只是中间产物），否则 x264/x265。
    质量偏高（crf/cq 14）：头段是超分管线的输入，尽量少损失细节。"""
    if nvenc:
        if codec == "hevc":
            return "hevc_nvenc", ["-preset", "p1", "-tune", "hq", "-rc", "vbr", "-cq", "17"]
        return "h264_nvenc", ["-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "14"]
    if codec == "hevc":
        return "libx265", ["-preset", "veryfast", "-crf", "18"]
    return "libx264", ["-preset", "fast", "-crf", "14"]


def _encode_args(
    input: Path, start: float, dur: float, out: Path, info: MediaInfo, nvenc: bool,
) -> list[str]:
    enc_name, enc_args = _encoder_args(info.video_codec, nvenc)
    a = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-progress", "pipe:1",
        "-ss", f"{start:.6f}", "-i", str(input), "-t", f"{dur:.6f}",
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", enc_name, *enc_args,
    ]
    # 10bit 源保留原像素格式，避免与复制段位深不一致（8bit 强制 yuv420p）
    if "10" not in info.pix_fmt.lower():
        a += ["-pix_fmt", "yuv420p"]
    a += [
        "-fps_mode", "passthrough",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(out),
    ]
    return a


def _concat(a: Path, b: Path, out: Path, work: Path) -> None:
    lst = work / "concat.txt"
    def esc(p: Path) -> str:
        return str(p).replace("\\", "/").replace("'", "'\\''")
    lst.write_text(f"file '{esc(a)}'\nfile '{esc(b)}'\n", encoding="utf-8")
    r = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
         "-movflags", "+faststart", str(out)],
        capture_output=True, creationflags=WINDOWS_CREATE_FLAGS,
    )
    if r.returncode != 0:
        raise TrimError(f"concat 失败: {r.stderr.decode('utf-8', 'replace')[-400:]}")


def _encode_whole(
    input: Path, info: MediaInfo, start: float, end: float, out: Path,
    nvenc: bool, progress_cb, p_from: float, p_to: float, notices: list[str],
    on_spawn=None, cancel_check=None,
) -> None:
    try:
        _run_ffmpeg(_encode_args(input, start, end - start, out, info, nvenc),
                    end - start, progress_cb, p_from, p_to, on_spawn, cancel_check)
    except TrimCanceled:
        raise  # 取消不得触发硬编回退（回退=取消后再跑一遍软编）
    except TrimError:
        if not nvenc:
            raise
        notices.append("硬件编码失败，已回退软件编码")
        _run_ffmpeg(_encode_args(input, start, end - start, out, info, False),
                    end - start, progress_cb, p_from, p_to, on_spawn, cancel_check)


def run_trim(
    input_path: str | Path,
    start_s: float,
    end_s: float,
    mode: str,
    output: str | Path,
    nvenc: bool = False,
    progress_cb=None,  # cb(progress: 0~1)
    on_spawn=None,  # on_spawn(proc)：每段 ffmpeg 拉起时上报句柄（取消用）
    cancel_check=None,  # cancel_check() -> bool：外部请求取消
) -> TrimResult:
    if mode not in ("smart", "fast", "exact"):
        raise TrimError(f"未知剪切模式 {mode}")
    input_path = Path(input_path)
    output = Path(output)
    info = probe(input_path)
    end_s = min(end_s, info.duration_s)
    if not (0 <= start_s < end_s):
        raise TrimError("入点必须早于出点")
    frame_dur = 1 / max(info.fps, 1e-6)
    notices: list[str] = []
    progress_cb = progress_cb or (lambda p: None)

    kfs = scan_keyframes(input_path) if mode != "exact" else []
    segs, actual_start = plan_segments(start_s, end_s, kfs, frame_dur, mode)

    # smart 两段式成立条件
    kinds = {k for k, _, _ in segs}
    audio_codec = info.audio[0].codec if info.has_audio else None
    if kinds == {"encode", "copy"}:
        if info.video_codec not in ("h264", "hevc"):
            notices.append(f"源编码 {info.video_codec} 暂不支持无损分段，已整段转码")
            segs, actual_start = [("encode", start_s, end_s)], start_s
        elif audio_codec and audio_codec != "aac":
            notices.append(f"源音轨 {audio_codec} 非 AAC，已整段转码保证拼接一致")
            segs, actual_start = [("encode", start_s, end_s)], start_s

    output.parent.mkdir(parents=True, exist_ok=True)
    work = TEMP_DIR / "trim"
    work.mkdir(parents=True, exist_ok=True)

    enc_segs = [s for s in segs if s[0] == "encode"]
    cpy_segs = [s for s in segs if s[0] == "copy"]
    total_dur = end_s - actual_start

    # —— 纯复制（fast / 起点在关键帧的 smart）——
    if not enc_segs:
        s, e = cpy_segs[0][1], cpy_segs[0][2]
        _run_ffmpeg(_copy_args(input_path, s, e - s, output), e - s, progress_cb,
                    0.0, 1.0, on_spawn, cancel_check)
        return TrimResult(output, start_s, actual_start, e - s, mode if mode != "smart" else "fast", notices)

    # —— 纯转码（exact / 降级）——
    if not cpy_segs:
        _encode_whole(input_path, info, start_s, end_s, output, nvenc,
                      progress_cb, 0.0, 1.0, notices, on_spawn, cancel_check)
        return TrimResult(output, start_s, start_s, end_s - start_s, "exact", notices)

    # —— smart 两段式：B 复制(0→60%) → 实测校验 → A 转码(60→87%) → concat(87→100%) ——
    c_s, c_e = cpy_segs[0][1], cpy_segs[0][2]
    e_s, e_e = enc_segs[0][1], enc_segs[0][2]
    seg_b = work / "seg_b.mp4"
    seg_a = work / "seg_a.mp4"
    _run_ffmpeg(_copy_args(input_path, c_s, c_e - c_s, seg_b), c_e - c_s,
                progress_cb, 0.0, 0.6, on_spawn, cancel_check)
    b_dur = probe(seg_b).duration_s
    # 流复制按包边界截断，正常会有 ±0.2s 级漂移；差出 0.5s 以上才是切错了段
    if abs(b_dur - (c_e - c_s)) > 0.5:
        notices.append("复制段时长异常，已整段转码兜底")
        _encode_whole(input_path, info, start_s, end_s, output, nvenc,
                      progress_cb, 0.6, 1.0, notices, on_spawn, cancel_check)
        return TrimResult(output, start_s, start_s, end_s - start_s, "exact", notices)
    _run_ffmpeg(_encode_args(input_path, e_s, e_e - e_s, seg_a, info, nvenc),
                e_e - e_s, progress_cb, 0.6, 0.87, on_spawn, cancel_check)
    _concat(seg_a, seg_b, output, work)
    for f in (seg_a, seg_b):
        f.unlink(missing_ok=True)
    progress_cb(1.0)
    # 终检：产物时长应 ≈ 选择时长（复制段尾部包边界漂移 ~0.2s 属正常）
    out_dur = probe(output).duration_s
    if abs(out_dur - (end_s - start_s)) > max(0.6, 6 * frame_dur):
        notices.append(f"产物时长 {out_dur:.2f}s 与选择 {end_s - start_s:.2f}s 偏差较大，已整段转码兜底")
        _encode_whole(input_path, info, start_s, end_s, output, nvenc,
                      progress_cb, 0.0, 1.0, notices)
        return TrimResult(output, start_s, start_s, end_s - start_s, "exact", notices)
    return TrimResult(output, start_s, start_s, out_dur, "smart", notices)

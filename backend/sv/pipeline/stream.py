"""核心流式管线：ffmpeg 解码 → 逐帧推理 → ffmpeg 编码，全程管道不落盘。"""
from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..paths import ffmpeg_bin
from ..utils.process import WINDOWS_CREATE_FLAGS, kill_tree
from .probe import MediaInfo

_MP4_AUDIO_COPY_OK = {"aac", "mp3", "ac3", "eac3", "alac"}

# 文本字幕（可转 mov_text 进 mp4）；图形字幕（PGS/DVB 等）只能进 mkv 原样保留
_TEXT_SUBS = {
    "subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "sami",
    "microdvd", "subviewer", "vplayer", "realtext", "stl", "pjs", "jacosub",
    "mpl2", "subviewer1",
}

CONTAINERS = ("mp4", "mkv", "mov")


@dataclass
class EncodeOpts:
    codec: str = "h264"  # h264 | h265 | h264_nvenc | hevc_nvenc | av1_nvenc |
    #                    # h264_amf | hevc_amf | av1_svt
    crf: int = 18
    preset: str = "medium"
    audio_mode: str = "auto"  # auto: 兼容则 copy 否则 aac | copy | aac | flac(仅mkv) | none
    subtitle_mode: str = "none"  # none | auto（mkv 原样保留；mp4/mov 仅全文本字幕时转 mov_text）
    container: str = "mp4"  # mp4 | mkv | mov
    out_kind: str = "video"  # video | png | jpg——图片序列=整视频逐帧导出（无音轨）

    @property
    def mp4_family(self) -> bool:
        return self.container in ("mp4", "mov")


@dataclass
class RunStats:
    frames: int
    elapsed_s: float
    fps: float
    out_path: Path
    out_bytes: int


class PipelineError(RuntimeError):
    """管线失败，message 内含 ffmpeg 尾部日志。"""


class TaskCanceled(Exception):
    pass


_IMG_NAME_RE = re.compile(r"^(\d{6})\.(png|jpg)$")


def iter_image_frames(d: Path) -> list[tuple[int, Path]]:
    """目录内按帧号命名的图片（000001.png / 000002.jpg…），返回 (帧号, 路径) 升序。"""
    if not d.is_dir():
        return []
    out: list[tuple[int, Path]] = []
    for f in d.iterdir():
        m = _IMG_NAME_RE.match(f.name)
        if m and f.is_file():
            out.append((int(m.group(1)), f))
    out.sort()
    return out


def image_dir_bytes(d: Path) -> int:
    return sum(f.stat().st_size for _, f in iter_image_frames(d))


def prefilter_chain(deinterlace: bool = False, deband: bool = False) -> str | None:
    """解码前置滤镜链（超分前修复源画面缺陷），拼接进解码命令的 -vf。

    bwdif 必须用 mode=send_frame（每帧输入产一帧输出）——帧数不变是分段
    管线 checkpoint 语义的硬前提；默认的 send_field 会帧数翻倍，段产出
    帧数校验全炸。顺序固定：先反交错再去色带（交错梳齿会干扰 deband 的
    渐变检测）。两个开关都关时返回 None，解码命令完全不出现 -vf。
    """
    parts: list[str] = []
    if deinterlace:
        parts.append("bwdif=mode=send_frame")
    if deband:
        parts.append("deband")
    return ",".join(parts) or None


def decoder_cmd(
    input_path: Path,
    cfr_fps: str | None = None,
    seek_s: float | None = None,
    max_frames: int | None = None,
    hwaccel: str | None = None,
    vf: str | None = None,
) -> list[str]:
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
    ]
    if hwaccel:
        # 硬件解码：解码帧自动拷回系统内存后再 swscale 到 rgb24，管线其余零改动。
        # 可用性由 probe_hwaccel 用真实源预验证过，此处不做运行中回退
        cmd += ["-hwaccel", hwaccel]
    if seek_s is not None:
        # 输入 seek（-ss 在 -i 前）：CFR 源帧精确；VFR 源 ±1-2 帧偏差（分段续跑可接受）
        cmd += ["-ss", f"{seek_s:.6f}"]
    cmd += ["-i", str(input_path), "-map", "0:v:0", "-an", "-sn", "-dn"]
    if vf:
        # 前置滤镜在原始像素格式上跑（bwdif/deband 都为 yuv 设计），滤镜链尾部
        # 由 ffmpeg 自动插入格式转换到 rgb24；与 -hwaccel 组合同理安全（解码帧
        # 已在系统内存）。滤镜可用性与硬解组合由 probe_hwaccel(vf=...) 预验证
        cmd += ["-vf", vf]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24",
            "-sws_flags", "lanczos+accurate_rnd+full_chroma_int"]
    if cfr_fps:
        cmd += ["-r", cfr_fps]  # VFR 源按平均帧率 CFR 化（补/丢帧）
    if max_frames is not None:
        cmd += ["-frames:v", str(max_frames)]
    cmd.append("-")
    return cmd


_VCODECS = {
    "h264": "libx264",
    "h265": "libx265",
    "h264_nvenc": "h264_nvenc",
    "hevc_nvenc": "hevc_nvenc",
    "av1_nvenc": "av1_nvenc",
    "h264_amf": "h264_amf",
    "hevc_amf": "hevc_amf",
    "av1_svt": "libsvtav1",
}

# x264 preset 名 → SVT-AV1 preset 数字（0-13，大=快）；未知按 medium=6
_SVT_PRESET = {
    "veryslow": "4", "slower": "5", "slow": "5", "medium": "6",
    "fast": "8", "faster": "9", "veryfast": "10",
}


def video_codec_args(enc: EncodeOpts) -> list[str]:
    """视频编码器参数（三家硬编 + 两款软编 + SVT-AV1），CRF 统一语义。"""
    vcodec = _VCODECS.get(enc.codec, "libx264")
    if vcodec.endswith("_nvenc"):
        args = ["-c:v", vcodec, "-preset", "p5", "-rc", "vbr",
                "-cq", str(enc.crf), "-b:v", "0"]
        if vcodec != "av1_nvenc":  # av1_nvenc 无 hq tune
            args += ["-tune", "hq"]
        return args + ["-pix_fmt", "yuv420p"]
    if vcodec.endswith("_amf"):
        # AMF 无 CRF，用 cqp 近似：I 帧取 crf、P 帧略高保持码率效率
        return ["-c:v", vcodec, "-quality", "balanced", "-rc", "cqp",
                "-qp_i", str(enc.crf), "-qp_p", str(min(51, enc.crf + 2)),
                "-pix_fmt", "yuv420p"]
    if vcodec == "libsvtav1":
        # SVT-AV1 的 CRF 量程 0-63（x264 是 0-51），等比映射保持 UI 单一滑杆
        svt_crf = min(63, round(enc.crf * 63 / 51))
        return ["-c:v", vcodec, "-crf", str(svt_crf),
                "-preset", _SVT_PRESET.get(enc.preset, "6"),
                "-pix_fmt", "yuv420p"]
    return ["-c:v", vcodec, "-crf", str(enc.crf), "-preset", enc.preset,
            "-pix_fmt", "yuv420p"]


def fps_double(fps_str: str) -> str:
    """'30000/1001' -> '60000/1001'（补帧 x2 时编码帧率翻倍）。"""
    from fractions import Fraction

    return str(Fraction(fps_str) * 2)


def audio_args(enc: EncodeOpts, mp4_family: bool, audio_codecs: list[str]) -> list[str]:
    """音轨参数（源全部音轨一并映射，audio_codecs=各轨编码）：
    - 显式 copy/aac/flac：全轨统一
    - auto：mkv 全轨 copy；mp4 全轨可混则 copy、全不可混则统一 aac，
      混合源逐轨定（不因单轨不兼容整体重编码）
    """
    if enc.audio_mode == "none" or not audio_codecs:
        return []
    if enc.audio_mode == "copy":
        return ["-c:a", "copy"]
    if enc.audio_mode == "flac":
        return ["-c:a", "flac"]
    if enc.audio_mode == "aac":
        return ["-c:a", "aac", "-b:a", "192k"]
    if not mp4_family:
        # mkv 混流能力宽（flac/mp3/dts/truehd…皆可），auto 尽量无损 copy 少一代有损
        return ["-c:a", "copy"]
    copyable = [c in _MP4_AUDIO_COPY_OK for c in audio_codecs]
    if all(copyable):
        return ["-c:a", "copy"]
    if not any(copyable):
        return ["-c:a", "aac", "-b:a", "192k"]
    # 混合源：逐轨定（可混 copy / 不可混 aac），不因单轨不兼容整体重编码
    args: list[str] = []
    for i, ok in enumerate(copyable):
        if ok:
            args += [f"-c:a:{i}", "copy"]
        else:
            args += [f"-c:a:{i}", "aac", f"-b:a:{i}", "192k"]
    return args


def subtitle_args(enc: EncodeOpts, sub_codecs: list[str]) -> list[str]:
    """字幕轨映射（源文件=第二输入，下标 1）：
    - mkv：全部字幕流原样保留（含图形字幕 PGS/DVB），并带上内嵌字体附件
      （ASS 特效字依赖；mp4 家族挂附件会直接混流失败，故仅 mkv）
    - mp4/mov：仅当全部为文本字幕时转 mov_text，否则整体丢弃（避免 PGS 混流失败）
    """
    if enc.subtitle_mode != "auto" or not sub_codecs:
        return []
    if enc.mp4_family:
        if all(c in _TEXT_SUBS for c in sub_codecs):
            return ["-map", "1:s?", "-c:s", "mov_text"]
        return []
    return ["-map", "1:s?", "-c:s", "copy", "-map", "1:t?"]


def encoder_cmd(
    input_path: Path,
    output_path: Path,
    frame_w: int,
    frame_h: int,   # 推理输出帧尺寸（rawvideo 输入声明）
    target_w: int,
    target_h: int,  # 最终目标尺寸；与 frame 不同则编码器内 lanczos 缩放
    fps_str: str,
    enc: EncodeOpts,
    with_audio: bool,  # 挂第二输入（源文件，取音轨/字幕）
    audio_codecs: list[str] | None,  # 源各音轨编码（全部映射；None/空=不挂音轨）
    sub_codecs: list[str] | None = None,
    start_number: int = 1,  # 图片序列：本段首帧的全局帧号（分段续跑全局编号）
) -> list[str]:
    if enc.out_kind != "video":
        # 图片序列：image2 复用器一帧一图；无音轨/封装参数
        cmd = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-video_size", f"{frame_w}x{frame_h}", "-framerate", fps_str, "-i", "pipe:0",
        ]
        if (target_w, target_h) != (frame_w, frame_h):
            cmd += ["-vf", f"scale={target_w}:{target_h}:flags=lanczos"]
        if enc.out_kind == "jpg":
            cmd += ["-c:v", "mjpeg", "-q:v", "2"]  # qscale 2 ≈ 高质量
        cmd += ["-start_number", str(start_number), "-f", "image2", str(output_path)]
        return cmd
    subs = sub_codecs or []
    tracks = list(audio_codecs or [])
    mux_source = with_audio and (bool(tracks) or bool(subs))
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-video_size", f"{frame_w}x{frame_h}", "-framerate", fps_str, "-i", "pipe:0",
    ]
    if mux_source:
        cmd += ["-i", str(input_path)]
    cmd += ["-map", "0:v:0"]
    if mux_source:
        if tracks:
            cmd += ["-map", "1:a?"]  # 全部音轨（此前仅 1:a:0，多轨源会静默丢轨）
        cmd += subtitle_args(enc, subs)
        cmd += ["-map_chapters", "1"]  # 章节从源继承（显式声明；默认取"首个含章节的输入"）

    cmd += video_codec_args(enc)
    if (target_w, target_h) != (frame_w, frame_h):
        cmd += ["-vf", f"scale={target_w}:{target_h}:flags=lanczos"]

    if mux_source:
        if tracks:
            cmd += audio_args(enc, enc.mp4_family, tracks)
        else:
            cmd += ["-an"]
    if enc.mp4_family:
        cmd += ["-movflags", "+faststart"]
    cmd += [str(output_path)]
    return cmd

class StreamPipeline:
    """逐帧流式超分管线。

    transformer 需要提供：`.scale: int` 与 `.process(frame_rgb: uint8 HWC) -> uint8 HWC`。
    """

    def __init__(
        self,
        info: MediaInfo,
        output_path: str | Path,
        transformer,
        encode: EncodeOpts | None = None,
        progress_cb=None,  # cb(frames, total, fps, eta_s)
        cancel_event: asyncio.Event | None = None,
        preview_path: Path | None = None,
        src_preview_path: Path | None = None,  # 源首帧截图（对比预览用）
        preview_interval_s: float = 5.0,
        target_scale: int | None = None,  # 目标倍率；小于引擎倍率时编码器缩放
        target_size: tuple[int, int] | None = None,  # 精确目标宽高；优先于 target_scale（仍只缩不放）
        interp=None,  # Rife2x 补帧引擎；非 None 时输出帧率 x2
        seek_s: float | None = None,  # 分段续跑：解码从该时间点开始（输入 seek）
        max_frames: int | None = None,  # 分段续跑：最多解码帧数
        with_audio: bool = True,  # 分段编码不挂音轨（最终 concat 时统一合成）
        seg_start: int = 0,  # 分段续跑：本段在总输出帧中的起始偏移（进度上报用）
        seg_total: int | None = None,  # 分段续跑：总输出帧数覆盖（进度上报用）
        frame_start: int = 1,  # 图片序列分段：本段首帧全局帧号（000001 起编号）
        decode_hwaccel: str | None = None,  # 硬解 '-hwaccel' 值（None=软解；probe_hwaccel 预验证过）
        decode_vf: str | None = None,  # 解码前置滤镜链（反交错/去色带；帧数不变式见 prefilter_chain）
    ):
        self.info = info
        self.output_path = Path(output_path)
        self.tx = transformer
        self.enc = encode or EncodeOpts()
        self.progress_cb = progress_cb
        self.cancel_event = cancel_event
        self.preview_path = preview_path
        self.src_preview_path = src_preview_path
        self.preview_interval_s = preview_interval_s
        self.target_scale = target_scale
        self.target_size = target_size
        self.interp = interp
        self.seek_s = seek_s
        self.max_frames = max_frames
        self.with_audio = with_audio
        self.seg_start = seg_start
        self.seg_total = seg_total
        self.frame_start = frame_start
        self.decode_hwaccel = decode_hwaccel
        self.decode_vf = decode_vf
        # 分段计时（性能剖析用；SegmentedPipeline 汇总落盘，平时无人读）：
        # read=解码管道读取 infer=process() 写=编码管道写入+drain preview=预览 JPEG
        # qin_wait/qout_wait=协程等队列（饥饿时间） enc_finish=段尾编码进程收尾
        self.stage_stats: dict[str, float] = {}

    async def run(self) -> RunStats:
        _t_entry = time.perf_counter()
        info, enc, tx = self.info, self.enc, self.tx
        frame_w, frame_h = info.width * tx.scale, info.height * tx.scale
        if self.target_size is not None:
            target_w, target_h = self.target_size
        else:
            target = self.target_scale or tx.scale
            target_w, target_h = info.width * target, info.height * target
        output_path = self.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        audio_codecs = ([a.codec for a in info.audio]
                        if info.has_audio and enc.audio_mode != "none" else [])
        sub_codecs = list(getattr(info, "subtitles", []) or [])
        out_fps_str = fps_double(info.fps_str) if self.interp is not None else info.fps_str

        dec = asyncio.create_subprocess_exec(
            *decoder_cmd(info.path, cfr_fps=info.fps_str if info.vfr else None,
                         seek_s=self.seek_s, max_frames=self.max_frames,
                         hwaccel=self.decode_hwaccel, vf=self.decode_vf),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=WINDOWS_CREATE_FLAGS,
        )
        enc_proc = asyncio.create_subprocess_exec(
            *encoder_cmd(info.path, output_path, frame_w, frame_h, target_w, target_h,
                         out_fps_str, enc, self.with_audio,
                         audio_codecs, sub_codecs, start_number=self.frame_start),
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=WINDOWS_CREATE_FLAGS,
        )
        p_dec, p_enc = await asyncio.gather(dec, enc_proc)
        dec_err: deque[str] = deque(maxlen=30)
        enc_err: deque[str] = deque(maxlen=30)
        drain_dec = asyncio.create_task(self._drain(p_dec.stderr, dec_err))
        drain_enc = asyncio.create_task(self._drain(p_enc.stderr, enc_err))

        frame_size = info.width * info.height * 3
        total = self.seg_total if self.seg_total is not None else info.total_frames * (2 if self.interp is not None else 1)
        local_total = total - self.seg_start  # 本段应产出的帧数（分段续跑时 < total）
        t0 = time.perf_counter()
        self.stage_stats["setup"] = t0 - _t_entry  # ffmpeg 解码/编码进程拉起耗时
        batch = max(1, int(getattr(tx, "batch", 1) or 1))
        assert p_enc.stdin is not None and p_dec.stdout is not None

        # 三协程重叠：读下一帧 / 推理(线程) / 写上一帧 并行（BENCH.md 2026-08-25：
        # uint8 包装之上再 +30%；session.run 释放 GIL，推理挪 executor 不堵事件循环）。
        # batch>1 的推理保持同步调用（DML 批量路径在线程池里有死锁前科）。
        q_in: asyncio.Queue = asyncio.Queue(maxsize=3)
        q_out: asyncio.Queue = asyncio.Queue(maxsize=3)
        loop = asyncio.get_running_loop()

        async def reader() -> None:
            """解码帧搬运：干净 EOF 放行哨兵；异常直接抛（外层取消同伴，不会卡死）。"""
            t_read = 0.0
            n_read = 0
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise TaskCanceled()
                items: list[np.ndarray] = []
                while len(items) < batch:
                    try:
                        _t = time.perf_counter()
                        buf = await p_dec.stdout.readexactly(frame_size)
                        t_read += time.perf_counter() - _t
                    except asyncio.IncompleteReadError as e:
                        if len(e.partial) == 0:
                            # 干净 EOF：先放行攒了不满一批的尾帧（总帧数 % batch != 0
                            # 时最多 batch-1 帧），再放哨兵——直接 return 会静默丢尾批
                            if items:
                                await q_in.put(items if batch > 1 else items[0])
                            await q_in.put(None)
                            self.stage_stats["read"] = t_read
                            # 分段请求帧数没拿满就干净 EOF = 真实片尾提前（probe 按
                            # 容器 duration 估总帧数，MKV BDRemux 元数据偏大实测
                            # +247 帧）；产满 max_frames 后解码器正常退出同样走
                            # 这里，不算——只有"没拿满"才是文件真到尾的可靠信号
                            if self.max_frames is not None and n_read < self.max_frames:
                                self.stage_stats["decode_eof"] = 1.0
                            return
                        tail = " | ".join(dec_err)
                        raise PipelineError(
                            f"解码流在帧中间截断({len(e.partial)}/{frame_size}字节): {tail}"
                        ) from e
                    items.append(np.frombuffer(buf, dtype=np.uint8).reshape(info.height, info.width, 3))
                    n_read += 1
                await q_in.put(items if batch > 1 else items[0])

        async def inferer() -> None:
            prev_out: np.ndarray | None = None  # 补帧用：上一张超分输出帧
            t_qin = t_infer = 0.0
            while True:
                _t = time.perf_counter()
                item = await q_in.get()
                t_qin += time.perf_counter() - _t
                if item is None:
                    self.stage_stats["qin_wait"] = t_qin
                    self.stage_stats["infer"] = t_infer
                    if self.interp is not None and prev_out is not None:
                        await q_out.put((prev_out, None))  # 末帧复制，凑满 2N 保持时长/音画同步
                    await q_out.put(None)
                    return
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise TaskCanceled()
                frames = list(item) if batch > 1 else [item]
                if batch > 1:
                    outs = tx.process_batch(np.stack(frames))  # [N,H',W',3]
                    outs_list = [outs[i] for i in range(outs.shape[0])]
                elif getattr(tx, "main_thread_only", False):
                    # CUGAN×DML 围栏：该组合下 session.run 离开进程主线程即
                    # GPU 栈死锁（onnx_engine.main_thread_only 注释/BENCH §13）。
                    # 放弃读-写重叠、在事件循环线程（主线程）内联推理，正确性优先。
                    f0 = frames[0]
                    _t = time.perf_counter()
                    outs_list = [tx.process(f0)]
                    t_infer += time.perf_counter() - _t
                else:
                    f0 = frames[0]
                    _t = time.perf_counter()
                    outs_list = [await loop.run_in_executor(None, lambda f=f0: tx.process(f))]
                    t_infer += time.perf_counter() - _t
                for i, out_frame in enumerate(outs_list):
                    if out_frame.shape != (frame_h, frame_w, 3):
                        raise PipelineError(
                            f"变换输出尺寸 {out_frame.shape} 与预期 {(frame_h, frame_w, 3)} 不符"
                        )
                    if self.interp is not None and prev_out is not None:
                        # 两帧之间插一张，帧率翻倍；末尾补一张凑满 2N
                        a, b = prev_out, out_frame
                        inter = await loop.run_in_executor(
                            None, lambda x=a, y=b: self.interp.interpolate(x, y))
                        await q_out.put((inter, None))
                    await q_out.put((out_frame, frames[i]))
                    prev_out = out_frame

        async def writer() -> int:
            frames = 0
            last_cb = 0.0
            last_preview = 0.0
            t_qout = t_write = t_preview = 0.0
            while True:
                _t = time.perf_counter()
                item = await q_out.get()
                t_qout += time.perf_counter() - _t
                if item is None:
                    self.stage_stats["qout_wait"] = t_qout
                    self.stage_stats["write"] = t_write
                    self.stage_stats["preview"] = t_preview
                    self.stage_stats["frames"] = float(frames)
                    return frames
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise TaskCanceled()
                out_frame, src_frame = item
                _t = time.perf_counter()
                p_enc.stdin.write(out_frame.tobytes())
                await p_enc.stdin.drain()
                t_write += time.perf_counter() - _t
                frames += 1
                now = time.perf_counter()
                # 源图与输出图必须同帧成对保存（对比滑块左右一致）
                if (src_frame is not None
                        and (self.preview_path is not None or self.src_preview_path is not None)
                        and (frames == 1 or now - last_preview >= self.preview_interval_s)):
                    _t = time.perf_counter()
                    if self.preview_path is not None:
                        self._save_preview(out_frame)
                    if self.src_preview_path is not None:
                        self._save_jpg(src_frame, self.src_preview_path)
                    t_preview += time.perf_counter() - _t
                    last_preview = now
                if self.progress_cb and (now - last_cb >= 0.5 or frames == local_total):
                    elapsed = now - t0
                    fps = frames / elapsed if elapsed > 0 else 0.0
                    eta = (total - self.seg_start - frames) / fps if fps > 0 else 0.0
                    self.progress_cb(self.seg_start + frames, total, fps, eta)
                    last_cb = now

        tasks = [asyncio.create_task(reader()),
                 asyncio.create_task(inferer()),
                 asyncio.create_task(writer())]
        try:
            results = await asyncio.gather(*tasks)
            n_frames = results[2]
            p_enc.stdin.close()
            _t = time.perf_counter()
            enc_rc = await p_enc.wait()
            self.stage_stats["enc_finish"] = time.perf_counter() - _t
            dec_rc = await p_dec.wait()
            if enc_rc != 0:
                raise PipelineError(f"编码进程失败(rc={enc_rc}): {' | '.join(enc_err)}")
            if dec_rc != 0:
                raise PipelineError(f"解码进程失败(rc={dec_rc}): {' | '.join(dec_err)}")
        except (asyncio.IncompleteReadError, BrokenPipeError) as e:
            await self._reap(p_dec, p_enc)
            tail = " | ".join(dec_err) + " || " + " | ".join(enc_err)
            raise PipelineError(f"管线中断({type(e).__name__}): {tail}") from e
        except TaskCanceled:
            await self._reap(p_dec, p_enc)
            raise
        except Exception:
            await self._reap(p_dec, p_enc)
            raise
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for task in (drain_dec, drain_enc):
                task.cancel()
            await asyncio.gather(drain_dec, drain_enc, return_exceptions=True)

        elapsed = time.perf_counter() - t0
        self.stage_stats["wall"] = elapsed + self.stage_stats.get("setup", 0.0)
        stats = RunStats(
            frames=n_frames,
            elapsed_s=elapsed,
            fps=n_frames / elapsed if elapsed > 0 else 0.0,
            out_path=output_path,
            out_bytes=output_path.stat().st_size if output_path.exists() else 0,
        )
        return stats

    async def _drain(self, stream: asyncio.StreamReader, sink: deque[str]) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            sink.append(line.decode("utf-8", "replace").rstrip())

    async def _reap(self, *procs: asyncio.subprocess.Process) -> None:
        for p in procs:
            if p.returncode is None:
                kill_tree(p.pid)
                try:
                    await asyncio.wait_for(p.wait(), timeout=5)
                except Exception:
                    pass

    def _save_preview(self, frame: np.ndarray) -> None:
        self._save_jpg(frame, self.preview_path)

    def _save_jpg(self, frame: np.ndarray, path: Path | None) -> None:
        if path is None:
            return
        try:
            from PIL import Image

            img = Image.fromarray(frame)
            img.thumbnail((960, 960))
            path.parent.mkdir(parents=True, exist_ok=True)
            img.save(path, quality=88)
        except Exception:
            pass  # 预览失败不影响主流程

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


@dataclass
class EncodeOpts:
    codec: str = "h264"  # h264 | h265 | h264_nvenc | hevc_nvenc
    crf: int = 18
    preset: str = "medium"
    audio_mode: str = "auto"  # auto: 兼容则 copy，否则转 aac；none: 丢弃音轨
    out_kind: str = "video"  # video | png | jpg——图片序列=整视频逐帧导出（无音轨）


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


def decoder_cmd(
    input_path: Path,
    cfr_fps: str | None = None,
    seek_s: float | None = None,
    max_frames: int | None = None,
) -> list[str]:
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
    ]
    if seek_s is not None:
        # 输入 seek（-ss 在 -i 前）：CFR 源帧精确；VFR 源 ±1-2 帧偏差（分段续跑可接受）
        cmd += ["-ss", f"{seek_s:.6f}"]
    cmd += ["-i", str(input_path), "-map", "0:v:0", "-an", "-sn", "-dn",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
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
}


def fps_double(fps_str: str) -> str:
    """'30000/1001' -> '60000/1001'（补帧 x2 时编码帧率翻倍）。"""
    from fractions import Fraction

    return str(Fraction(fps_str) * 2)


def audio_args(enc: EncodeOpts, mp4_family: bool, audio_codec: str | None) -> list[str]:
    """音轨参数：mp4 家族且编码可拷则 copy，否则转 aac 192k；audio_mode=none 不带。"""
    if enc.audio_mode == "none":
        return []
    copyable = mp4_family and audio_codec in _MP4_AUDIO_COPY_OK
    if copyable and enc.audio_mode in ("auto", "copy"):
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "192k"]


def encoder_cmd(
    input_path: Path,
    output_path: Path,
    frame_w: int,
    frame_h: int,   # 推理输出帧尺寸（rawvideo 输入声明）
    target_w: int,
    target_h: int,  # 最终目标尺寸；与 frame 不同则编码器内 lanczos 缩放
    fps_str: str,
    enc: EncodeOpts,
    has_audio: bool,
    audio_codec: str | None,
    mp4_family: bool,
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
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-video_size", f"{frame_w}x{frame_h}", "-framerate", fps_str, "-i", "pipe:0",
    ]
    if has_audio:
        cmd += ["-i", str(input_path)]
    cmd += ["-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "1:a:0?"]

    vcodec = _VCODECS.get(enc.codec, "libx264")
    if vcodec.endswith("_nvenc"):
        cmd += [
            "-c:v", vcodec,
            "-preset", "p5", "-tune", "hq",
            "-rc", "vbr", "-cq", str(enc.crf), "-b:v", "0",
            "-pix_fmt", "yuv420p",
        ]
    else:
        cmd += [
            "-c:v", vcodec, "-crf", str(enc.crf), "-preset", enc.preset,
            "-pix_fmt", "yuv420p",
        ]
    if (target_w, target_h) != (frame_w, frame_h):
        cmd += ["-vf", f"scale={target_w}:{target_h}:flags=lanczos"]

    if has_audio:
        cmd += audio_args(enc, mp4_family, audio_codec)
    if mp4_family:
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

    async def run(self) -> RunStats:
        info, enc, tx = self.info, self.enc, self.tx
        frame_w, frame_h = info.width * tx.scale, info.height * tx.scale
        if self.target_size is not None:
            target_w, target_h = self.target_size
        else:
            target = self.target_scale or tx.scale
            target_w, target_h = info.width * target, info.height * target
        output_path = self.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        mp4_family = output_path.suffix.lower() in (".mp4", ".m4v", ".mov")
        audio_codec = info.audio[0].codec if info.has_audio else None
        out_fps_str = fps_double(info.fps_str) if self.interp is not None else info.fps_str

        dec = asyncio.create_subprocess_exec(
            *decoder_cmd(info.path, cfr_fps=info.fps_str if info.vfr else None,
                         seek_s=self.seek_s, max_frames=self.max_frames),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=WINDOWS_CREATE_FLAGS,
        )
        enc_proc = asyncio.create_subprocess_exec(
            *encoder_cmd(info.path, output_path, frame_w, frame_h, target_w, target_h,
                         out_fps_str, enc, self.with_audio and info.has_audio,
                         audio_codec, mp4_family, start_number=self.frame_start),
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
        frames = 0
        last_cb = 0.0
        last_preview = 0.0
        batch = max(1, int(getattr(tx, "batch", 1) or 1))

        async def read_frame() -> np.ndarray | None:
            """读一帧；干净 EOF 返回 None，帧中截断抛错。"""
            try:
                buf = await p_dec.stdout.readexactly(frame_size)
            except asyncio.IncompleteReadError as e:
                if len(e.partial) == 0:
                    return None
                tail = " | ".join(dec_err)
                raise PipelineError(
                    f"解码流在帧中间截断({len(e.partial)}/{frame_size}字节): {tail}"
                ) from e
            return np.frombuffer(buf, dtype=np.uint8).reshape(info.height, info.width, 3)

        prev_out: np.ndarray | None = None  # 补帧用：上一张超分输出帧

        def emit_frame(out_frame: np.ndarray, src_frame: np.ndarray | None) -> None:
            """写一帧到编码器并处理预览/进度（src 仅真实帧有，补帧产物跳过预览）。"""
            nonlocal frames, last_preview, last_cb
            p_enc.stdin.write(out_frame.tobytes())
            frames += 1
            now = time.perf_counter()
            # 源图与输出图必须同帧成对保存（对比滑块左右一致）
            if (src_frame is not None
                    and (self.preview_path is not None or self.src_preview_path is not None)
                    and (frames == 1 or now - last_preview >= self.preview_interval_s)):
                if self.preview_path is not None:
                    self._save_preview(out_frame)
                if self.src_preview_path is not None:
                    self._save_jpg(src_frame, self.src_preview_path)
                last_preview = now
            if self.progress_cb and (now - last_cb >= 0.5 or frames == local_total):
                elapsed = now - t0
                fps = frames / elapsed if elapsed > 0 else 0.0
                eta = (total - self.seg_start - frames) / fps if fps > 0 else 0.0
                self.progress_cb(self.seg_start + frames, total, fps, eta)
                last_cb = now

        try:
            assert p_enc.stdin is not None and p_dec.stdout is not None
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise TaskCanceled()

                if batch > 1:
                    pending: list[np.ndarray] = []
                    while len(pending) < batch:
                        f = await read_frame()
                        if f is None:
                            break
                        pending.append(f)
                    if not pending:
                        break  # 干净 EOF
                    outs = tx.process_batch(np.stack(pending))  # [N,H',W',3]
                    srcs = pending
                else:
                    f = await read_frame()
                    if f is None:
                        break  # 干净 EOF
                    outs = tx.process(f)[None]
                    srcs = [f]

                for i in range(outs.shape[0]):
                    out_frame = outs[i]
                    if out_frame.shape != (frame_h, frame_w, 3):
                        raise PipelineError(
                            f"变换输出尺寸 {out_frame.shape} 与预期 {(frame_h, frame_w, 3)} 不符"
                        )
                    if self.interp is not None and prev_out is not None:
                        # 两帧之间插一张，帧率翻倍；末尾补一张凑满 2N
                        emit_frame(self.interp.interpolate(prev_out, out_frame), None)
                    emit_frame(out_frame, srcs[i])
                    prev_out = out_frame
                await p_enc.stdin.drain()

            if self.interp is not None and prev_out is not None:
                emit_frame(prev_out, None)  # 末帧复制，凑满 2N 保持时长/音画同步

            p_enc.stdin.close()
            enc_rc = await p_enc.wait()
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
            raise
        except Exception:
            await self._reap(p_dec, p_enc)
            raise
        finally:
            for task in (drain_dec, drain_enc):
                task.cancel()
            await asyncio.gather(drain_dec, drain_enc, return_exceptions=True)

        elapsed = time.perf_counter() - t0
        stats = RunStats(
            frames=frames,
            elapsed_s=elapsed,
            fps=frames / elapsed if elapsed > 0 else 0.0,
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

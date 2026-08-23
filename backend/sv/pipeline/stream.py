"""核心流式管线：ffmpeg 解码 → 逐帧推理 → ffmpeg 编码，全程管道不落盘。"""
from __future__ import annotations

import asyncio
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
    codec: str = "h264"  # h264 | h265
    crf: int = 18
    preset: str = "medium"
    audio_mode: str = "auto"  # auto: 兼容则 copy，否则转 aac；none: 丢弃音轨


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


def decoder_cmd(input_path: Path) -> list[str]:
    return [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(input_path), "-map", "0:v:0", "-an", "-sn", "-dn",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-sws_flags", "lanczos+accurate_rnd+full_chroma_int",
        "-",
    ]


def encoder_cmd(
    input_path: Path,
    output_path: Path,
    out_w: int,
    out_h: int,
    fps_str: str,
    enc: EncodeOpts,
    has_audio: bool,
    audio_codec: str | None,
    mp4_family: bool,
) -> list[str]:
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-video_size", f"{out_w}x{out_h}", "-framerate", fps_str, "-i", "pipe:0",
    ]
    if has_audio:
        cmd += ["-i", str(input_path)]
    cmd += ["-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "1:a:0?"]

    vcodec = "libx264" if enc.codec == "h264" else "libx265"
    cmd += [
        "-c:v", vcodec, "-crf", str(enc.crf), "-preset", enc.preset,
        "-pix_fmt", "yuv420p",
    ]

    if has_audio:
        keep = enc.audio_mode != "none"
        copyable = mp4_family and audio_codec in _MP4_AUDIO_COPY_OK
        if keep and copyable and enc.audio_mode in ("auto", "copy"):
            cmd += ["-c:a", "copy"]
        elif keep:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
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
        preview_interval_s: float = 5.0,
    ):
        self.info = info
        self.output_path = Path(output_path)
        self.tx = transformer
        self.enc = encode or EncodeOpts()
        self.progress_cb = progress_cb
        self.cancel_event = cancel_event
        self.preview_path = preview_path
        self.preview_interval_s = preview_interval_s

    async def run(self) -> RunStats:
        info, enc, tx = self.info, self.enc, self.tx
        out_w, out_h = info.width * tx.scale, info.height * tx.scale
        output_path = self.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        mp4_family = output_path.suffix.lower() in (".mp4", ".m4v", ".mov")
        audio_codec = info.audio[0].codec if info.has_audio else None

        dec = asyncio.create_subprocess_exec(
            *decoder_cmd(info.path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=WINDOWS_CREATE_FLAGS,
        )
        enc_proc = asyncio.create_subprocess_exec(
            *encoder_cmd(info.path, output_path, out_w, out_h, info.fps_str,
                         enc, info.has_audio, audio_codec, mp4_family),
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
        total = info.total_frames
        t0 = time.perf_counter()
        frames = 0
        last_cb = 0.0
        last_preview = 0.0

        try:
            assert p_enc.stdin is not None and p_dec.stdout is not None
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise TaskCanceled()
                try:
                    buf = await p_dec.stdout.readexactly(frame_size)
                except asyncio.IncompleteReadError as e:
                    if len(e.partial) == 0:
                        break  # 干净 EOF：解码器正常结束
                    tail = " | ".join(dec_err)
                    raise PipelineError(
                        f"解码流在帧中间截断({len(e.partial)}/{frame_size}字节): {tail}"
                    ) from e
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(info.height, info.width, 3)
                out_frame = tx.process(frame)
                if out_frame.shape != (out_h, out_w, 3):
                    raise PipelineError(
                        f"变换输出尺寸 {out_frame.shape} 与预期 {(out_h, out_w, 3)} 不符"
                    )
                p_enc.stdin.write(out_frame.tobytes())
                await p_enc.stdin.drain()
                frames += 1

                now = time.perf_counter()
                if self.preview_path and now - last_preview >= self.preview_interval_s:
                    self._save_preview(out_frame)
                    last_preview = now
                if self.progress_cb and (now - last_cb >= 0.5 or frames == total):
                    elapsed = now - t0
                    fps = frames / elapsed if elapsed > 0 else 0.0
                    eta = (total - frames) / fps if fps > 0 else 0.0
                    self.progress_cb(frames, total, fps, eta)
                    last_cb = now

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
        try:
            from PIL import Image

            img = Image.fromarray(frame)
            img.thumbnail((960, 960))
            self.preview_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(self.preview_path, quality=88)
        except Exception:
            pass  # 预览失败不影响主流程

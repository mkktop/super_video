"""分块管线（torch/扩散引擎用）：整段解码 PNG → 分块推理 → checkpoint 续跑 → 序列编码。

与流式管线的取舍：分块需要落盘临时目录（磁盘占用 = 帧数×输出分辨率×~1.5MB），
换来按块断点续跑——中断/取消/崩溃后重跑自动跳过已完成块。工作目录按任务 id
持久化（TEMP_DIR/chunked/<task_id>），任务成功后清理，取消/失败保留供续跑。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image

from ..paths import TEMP_DIR, ffmpeg_bin
from ..utils.process import WINDOWS_CREATE_FLAGS
from .probe import MediaInfo
from .stream import (
    EncodeOpts,
    PipelineError,
    RunStats,
    TaskCanceled,
    audio_args,
    image_dir_bytes,
    iter_image_frames,
    subtitle_args,
    video_codec_args,
)


class ChunkedPipeline:
    def __init__(
        self,
        info: MediaInfo,
        output_path: str | Path,
        transformer,
        encode: EncodeOpts | None = None,
        *,
        task_id: str,
        chunk: int = 32,
        overlap: int = 0,  # 扩散模型的滑窗重叠；逐帧模型用 0
        progress_cb=None,
        cancel_event: asyncio.Event | None = None,
        preview_path: Path | None = None,
        src_preview_path: Path | None = None,
        target_size: tuple[int, int] | None = None,  # 精确目标宽高；与引擎输出不同则编码时缩放
    ):
        self.info = info
        self.output_path = Path(output_path)
        self.tx = transformer
        self.enc = encode or EncodeOpts()
        self.chunk = max(1, chunk)
        self.overlap = overlap
        self.progress_cb = progress_cb
        self.cancel_event = cancel_event
        self.preview_path = preview_path
        self.src_preview_path = src_preview_path
        self.target_size = target_size
        self.work_dir = TEMP_DIR / "chunked" / task_id

    def _run_ffmpeg(self, cmd: list[str]) -> None:
        p = subprocess.run(cmd, capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
        if p.returncode != 0:
            raise PipelineError(
                f"ffmpeg 失败(rc={p.returncode}): "
                f"{p.stderr.decode('utf-8', 'replace')[-500:]}"
            )

    def _decode_stage(self, src_dir: Path) -> int:
        """整段解码为 PNG 序列；已解码（done 标记）则跳过。返回帧数。"""
        marker = self.work_dir / "decoded.json"
        if marker.exists():
            return json.loads(marker.read_text())["frames"]
        if src_dir.exists():
            for f in src_dir.glob("*.png"):
                f.unlink()
        src_dir.mkdir(parents=True, exist_ok=True)
        self._run_ffmpeg([
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(self.info.path), "-map", "0:v:0", "-an", "-sn", "-dn",
            str(src_dir / "f%06d.png"),
        ])
        n = len(list(src_dir.glob("f*.png")))
        if n == 0:
            raise PipelineError("解码没有得到任何帧")
        marker.write_text(json.dumps({"frames": n}))
        return n

    def _save_ckpt(self, done: set[int]) -> None:
        tmp = self.work_dir / "checkpoint.json.tmp"
        tmp.write_text(json.dumps({"done": sorted(done)}))
        tmp.replace(self.work_dir / "checkpoint.json")

    async def run(self) -> RunStats:
        info, enc, tx = self.info, self.enc, self.tx
        t0 = time.perf_counter()
        src_dir = self.work_dir / "src"
        out_dir = self.work_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        n = self._decode_stage(src_dir)
        ckpt_file = self.work_dir / "checkpoint.json"
        done: set[int] = set(json.loads(ckpt_file.read_text())["done"]) if ckpt_file.exists() else set()
        done_at_start = bool(done)  # 图片序列：非续跑时清掉最终目录的旧编号图

        starts = list(range(0, n, self.chunk))
        frames_done = sum(min(s + self.chunk, n) - s for s in done)

        def report(force=False):
            if self.progress_cb:
                elapsed = time.perf_counter() - t0
                fps = frames_done / elapsed if elapsed > 0 else 0.0
                eta = (n - frames_done) / fps if fps > 0 else 0.0
                self.progress_cb(frames_done, n, fps, eta)

        for ci, s in enumerate(starts):
            if s in done:
                continue
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise TaskCanceled()
            e = min(s + self.chunk, n)
            for i in range(s, e):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise TaskCanceled()
                img = np.asarray(Image.open(src_dir / f"f{i+1:06d}.png").convert("RGB"))
                frame = img[..., ::-1].copy()  # RGB -> BGR
                out = tx.process(frame)
                if s == 0 and i == 0 and self.preview_path is not None:
                    Image.fromarray(out[..., ::-1]).save(self.preview_path, quality=90)
                if s == 0 and i == 0 and self.src_preview_path is not None:
                    Image.fromarray(img).save(self.src_preview_path, quality=90)
                Image.fromarray(out[..., ::-1]).save(out_dir / f"f{i+1:06d}.png")
                frames_done += 1
                if frames_done % 8 == 0:
                    report()
            done.add(s)
            self._save_ckpt(done)
            report()

        # ---- 输出：图片序列（PNG 直通/转 JPG）或视频 ----
        if enc.out_kind != "video":
            final_dir = self.output_path  # 图片序列输出是目录
            final_dir.mkdir(parents=True, exist_ok=True)
            if not done_at_start:
                for _, f in iter_image_frames(final_dir):
                    f.unlink()
            cmd = [
                ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                "-framerate", info.fps_str, "-i", str(out_dir / "f%06d.png"),
            ]
            scale_needed = self.target_size is not None and self.target_size != (
                info.width * tx.scale, info.height * tx.scale
            )
            if scale_needed:
                cmd += ["-vf", f"scale={self.target_size[0]}:{self.target_size[1]}:flags=lanczos"]
            if enc.out_kind == "jpg":
                cmd += ["-c:v", "mjpeg", "-q:v", "2"]
            elif not scale_needed:
                cmd += ["-c:v", "copy"]  # PNG→PNG 直通，不重压
            cmd += ["-f", "image2", str(final_dir / f"%06d.{enc.out_kind}")]
            await asyncio.get_event_loop().run_in_executor(None, self._run_ffmpeg, cmd)
            for num, f in iter_image_frames(final_dir):  # 裁掉历史残留高帧号
                if num > n:
                    f.unlink()
            report(force=True)
            shutil.rmtree(self.work_dir, ignore_errors=True)  # 成功后清理
            return RunStats(
                frames=n, elapsed_s=time.perf_counter() - t0,
                fps=n / (time.perf_counter() - t0),
                out_path=final_dir, out_bytes=image_dir_bytes(final_dir),
            )

        # ---- 编码：PNG 序列 + 源音轨/字幕 ----
        output_path = self.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        audio_codecs = ([a.codec for a in info.audio]
                        if info.has_audio and enc.audio_mode != "none" else [])
        has_audio = bool(audio_codecs)
        subs = list(getattr(info, "subtitles", []) or [])
        cmd = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", info.fps_str, "-i", str(out_dir / "f%06d.png"),
        ]
        if has_audio or subs:
            cmd += ["-i", str(info.path)]
        cmd += ["-map", "0:v:0"]
        if has_audio or subs:
            if has_audio:
                cmd += ["-map", "1:a?"]  # 全部音轨（此前仅 1:a:0，多轨源会静默丢轨）
            cmd += subtitle_args(enc, subs)
            cmd += ["-map_chapters", "1"]
        cmd += video_codec_args(enc)
        if self.target_size is not None:
            tw, th = self.target_size
            if (tw, th) != (info.width * tx.scale, info.height * tx.scale):
                cmd += ["-vf", f"scale={tw}:{th}:flags=lanczos"]
        if has_audio:
            cmd += audio_args(enc, enc.mp4_family, audio_codecs)
        if enc.mp4_family:
            cmd += ["-movflags", "+faststart"]
        cmd += [str(output_path)]
        await asyncio.get_event_loop().run_in_executor(None, self._run_ffmpeg, cmd)

        report(force=True)
        shutil.rmtree(self.work_dir, ignore_errors=True)  # 成功后清理
        return RunStats(
            frames=n, elapsed_s=time.perf_counter() - t0,
            fps=n / (time.perf_counter() - t0),
            out_path=output_path, out_bytes=output_path.stat().st_size,
        )

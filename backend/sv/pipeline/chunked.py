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
from .stream import EncodeOpts, PipelineError, RunStats, TaskCanceled, _VCODECS


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

        # ---- 编码：PNG 序列 + 源音轨 ----
        output_path = self.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        mp4_family = output_path.suffix.lower() in (".mp4", ".m4v", ".mov")
        audio_codec = info.audio[0].codec if info.has_audio else None
        cmd = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", info.fps_str, "-i", str(out_dir / "f%06d.png"),
        ]
        if info.has_audio:
            cmd += ["-i", str(info.path)]
        cmd += ["-map", "0:v:0"]
        if info.has_audio:
            cmd += ["-map", "1:a:0?"]
        vcodec = _VCODECS.get(enc.codec, "libx264")
        if vcodec.endswith("_nvenc"):
            cmd += ["-c:v", vcodec, "-preset", "p5", "-tune", "hq",
                    "-rc", "vbr", "-cq", str(enc.crf), "-b:v", "0",
                    "-pix_fmt", "yuv420p"]
        else:
            cmd += ["-c:v", vcodec, "-crf", str(enc.crf), "-preset", enc.preset,
                    "-pix_fmt", "yuv420p"]
        if info.has_audio and enc.audio_mode != "none":
            copyable = mp4_family and audio_codec in ("aac", "mp3", "ac3", "eac3", "alac")
            if copyable and enc.audio_mode in ("auto", "copy"):
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
        if mp4_family:
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

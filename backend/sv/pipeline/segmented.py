"""分段流式管线：GAN(ONNX) 任务断点续跑。

把整段解码-推理-编码拆成若干独立段（每段 = 一次 StreamPipeline 子运行，
解码从段起点输入 seek + 限帧数，只编视频），每完成一段原子写入 checkpoint；
全部完成后 concat 视频段 + 源音轨合成最终文件。

取消/崩溃只丢当前段，续跑从 checkpoint 之后继续（模式与 chunked.py 的 torch
续跑一致：工作目录在 TEMP_DIR/segmented/<task_id>，成功才删除）。
已知取舍：
- VFR 源按时间 seek，段边界可能 ±1-2 帧偏差（CFR 源帧精确）；
- 补帧任务段间少一对插帧，以末帧复制补足 2N（视觉无感）。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

from ..paths import TEMP_DIR, ffmpeg_bin
from ..utils.process import WINDOWS_CREATE_FLAGS
from .probe import MediaInfo
from .stream import (
    EncodeOpts,
    PipelineError,
    RunStats,
    StreamPipeline,
    TaskCanceled,
    audio_args,
)


class SegmentedPipeline:
    """分段流式超分管线（支持断点续跑）。接口与 StreamPipeline 对齐。"""

    def __init__(
        self,
        info: MediaInfo,
        output_path: str | Path,
        transformer,
        encode: EncodeOpts | None = None,
        task_id: str = "t",
        progress_cb=None,
        cancel_event: asyncio.Event | None = None,
        preview_path: Path | None = None,
        src_preview_path: Path | None = None,
        target_scale: int | None = None,
        interp=None,
        seg_frames: int | None = None,  # 测试/调优：固定每段输入帧数
        cleanup: bool = True,  # 成功后删除工作目录（测试保留以便构造续跑场景）
    ):
        self.info = info
        self.output_path = Path(output_path)
        self.tx = transformer
        self.enc = encode or EncodeOpts()
        self.task_id = task_id
        self.progress_cb = progress_cb
        self.cancel_event = cancel_event
        self.preview_path = preview_path
        self.src_preview_path = src_preview_path
        self.target_scale = target_scale
        self.interp = interp
        self.seg_frames = seg_frames
        self.cleanup = cleanup

    async def run(self) -> RunStats:
        info, tx = self.info, self.tx
        total_in = info.total_frames
        if total_in <= 0:
            raise PipelineError("无法确定总帧数，不能分段处理")
        work = TEMP_DIR / "segmented" / self.task_id
        work.mkdir(parents=True, exist_ok=True)
        # 分段数自适应：约 8 段（小视频至少 60 帧/段，大视频每段 ≤600 帧 ≈ 3-5 分钟工作量）
        seg = self.seg_frames or min(600, max(60, total_in // 8))
        factor = 2 if self.interp is not None else 1
        total_out = total_in * factor

        ckpt_file = work / "checkpoint.json"
        done: set[int] = set()
        if ckpt_file.exists():
            try:
                done = set(json.loads(ckpt_file.read_text())["done"])
            except (ValueError, KeyError):
                pass  # 损坏的 checkpoint 从头跑

        starts = list(range(0, total_in, seg))
        t0 = time.perf_counter()
        for s in starts:
            if s in done:
                continue  # 续跑：跳过已完成段
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise TaskCanceled()
            n_in = min(seg, total_in - s)
            pipe = StreamPipeline(
                info, work / f"seg_{s:06d}.mp4", tx, self.enc,
                progress_cb=self.progress_cb,
                cancel_event=self.cancel_event,
                preview_path=self.preview_path,
                src_preview_path=self.src_preview_path,
                target_scale=self.target_scale,
                interp=self.interp,
                seek_s=(s / info.fps) if s > 0 else None,
                max_frames=n_in,
                with_audio=False,  # 段只编视频，音轨最后统一合成
                seg_start=s * factor,
                seg_total=total_out,
            )
            await pipe.run()
            done.add(s)
            self._save_ckpt(work, ckpt_file, done)

        # 最终合成：视频段 concat + 源音轨（与 encoder_cmd 同款 copy/aac 逻辑）
        seglist = work / "segments.txt"
        seglist.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in sorted(work.glob("seg_*.mp4"))),
            encoding="utf-8",
        )
        await asyncio.get_running_loop().run_in_executor(None, self._concat, seglist)

        if self.cleanup:
            shutil.rmtree(work, ignore_errors=True)
        elapsed = time.perf_counter() - t0
        return RunStats(
            frames=total_out,
            elapsed_s=elapsed,
            fps=total_out / elapsed if elapsed > 0 else 0.0,
            out_path=self.output_path,
            out_bytes=self.output_path.stat().st_size if self.output_path.exists() else 0,
        )

    def _save_ckpt(self, work: Path, ckpt_file: Path, done: set[int]) -> None:
        """原子写 checkpoint（tmp + replace，与 chunked.py 同款防半写）。"""
        tmp = work / "checkpoint.json.tmp"
        tmp.write_text(json.dumps({"done": sorted(done)}))
        tmp.replace(ckpt_file)

    def _concat(self, seglist: Path) -> None:
        """concat 视频段 + 源音轨 → 最终输出（-c:v copy，秒级）。"""
        mp4_family = self.output_path.suffix.lower() in (".mp4", ".m4v", ".mov")
        audio_codec = self.info.audio[0].codec if self.info.has_audio else None
        has_audio = self.info.has_audio and self.enc.audio_mode != "none"
        cmd = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "0", "-i", str(seglist),
        ]
        if has_audio:
            cmd += ["-i", str(self.info.path)]
        cmd += ["-map", "0:v:0"]
        if has_audio:
            cmd += ["-map", "1:a:0?"]
        cmd += ["-c:v", "copy"]
        if has_audio:
            cmd += audio_args(self.enc, mp4_family, audio_codec)
        if mp4_family:
            cmd += ["-movflags", "+faststart"]
        cmd += [str(self.output_path)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=WINDOWS_CREATE_FLAGS)
        if r.returncode != 0:
            raise PipelineError(
                f"最终合成失败(rc={r.returncode}): {(r.stderr or '')[-500:]}"
            )

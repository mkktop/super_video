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
import os
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
    image_dir_bytes,
    iter_image_frames,
    subtitle_args,
)


def shard_starts(starts: list[int], shard: int | None, nshards: int) -> list[int]:
    """分段交错分片：第 i 段归第 i%nshards 路（两路均衡收尾，优于前后对半）。"""
    if shard is None or nshards <= 1:
        return starts
    return [s for i, s in enumerate(starts) if i % nshards == shard]


def _write_eof_marker(work: Path, real_total_in: int) -> None:
    """真实片尾落盘（原子写）：另一路与续跑据此跳过界外段。

    取更小值覆盖：先撞尾的一路若只拿到"空段起点"这种粗上界（0 帧段），
    另一路随后发现的精确值（短段起点+实得帧数）应覆盖它。tmp 名带 pid——
    双路两进程共用目录，同名 tmp 会互踩（checkpoint 单文件时代的旧坑）。
    """
    m = work / "eof.json"
    if m.exists():
        old = read_eof_marker(work)
        if old is not None and old <= real_total_in:
            return
    tmp = work / f"eof.json.tmp{os.getpid()}"
    tmp.write_text(json.dumps({"real_total_in": int(real_total_in)}))
    tmp.replace(m)


def read_eof_marker(work: Path) -> int | None:
    """读真实片尾标记（输入帧口径）；无/损坏按 None（继续按估算总长跑）。"""
    m = work / "eof.json"
    if not m.exists():
        return None
    try:
        return int(json.loads(m.read_text())["real_total_in"])
    except (ValueError, KeyError, OSError):
        return None


def _concat_line(p: Path) -> str:
    """concat 清单一行：file '路径'——路径内的单引号按 demuxer 语法转义成 '\\''。"""
    return "file '" + p.as_posix().replace("'", "'\\''") + "'"


def concat_segments(work: Path, info: MediaInfo, enc: EncodeOpts,
                    output_path: Path) -> None:
    """concat 视频段 + 源音轨/字幕 → 最终输出（-c:v copy，秒级）。

    双路并行时由协调 worker 在两路都完成后调用（各分片 worker 不做合成）。
    音轨全部映射（auto 逐轨定 copy/aac），章节与 mkv 字体附件一并从源继承。
    """
    audio_codecs = ([a.codec for a in info.audio]
                    if info.has_audio and enc.audio_mode != "none" else [])
    has_audio = bool(audio_codecs)
    subs = list(getattr(info, "subtitles", []) or [])
    seglist = work / "segments.txt"
    seglist.write_text(
        "\n".join(_concat_line(p) for p in sorted(work.glob("seg_*.mp4"))),
        encoding="utf-8",
    )
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "concat", "-safe", "0", "-i", str(seglist),
    ]
    if has_audio or subs:
        cmd += ["-i", str(info.path)]
    cmd += ["-map", "0:v:0"]
    if has_audio or subs:
        if has_audio:
            cmd += ["-map", "1:a?"]  # 全部音轨（此前仅 1:a:0，多轨源会静默丢轨）
        cmd += subtitle_args(enc, subs)
        cmd += ["-map_chapters", "1"]
    cmd += ["-c:v", "copy"]
    if has_audio:
        cmd += audio_args(enc, enc.mp4_family, audio_codecs)
    if enc.mp4_family:
        cmd += ["-movflags", "+faststart"]
    cmd += [str(output_path)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       creationflags=WINDOWS_CREATE_FLAGS)
    if r.returncode != 0:
        raise PipelineError(
            f"最终合成失败(rc={r.returncode}): {(r.stderr or '')[-500:]}"
        )


def finalize_image_dir(img_dir: Path, total_out: int) -> None:
    """图片序列收尾：裁掉可能残留的高帧号图（双路时由协调者调用一次）。"""
    for num, f in iter_image_frames(img_dir):
        if num > total_out:
            f.unlink()


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
        target_size: tuple[int, int] | None = None,  # 精确目标宽高；优先于 target_scale
        interp=None,
        seg_frames: int | None = None,  # 测试/调优：固定每段输入帧数
        cleanup: bool = True,  # 成功后删除工作目录（测试保留以便构造续跑场景）
        shard: int | None = None,  # 双路并行：本 worker 处理第 shard 路（0 基）
        nshards: int = 1,
        decode_hwaccel: str | None = None,  # 硬解 '-hwaccel' 值（None=软解；worker 预验证过）
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
        self.target_size = target_size
        self.interp = interp
        self.seg_frames = seg_frames
        self.cleanup = cleanup
        self.shard = shard
        self.nshards = nshards
        self.decode_hwaccel = decode_hwaccel

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

        starts = shard_starts(list(range(0, total_in, seg)), self.shard, self.nshards)
        sharded = self.nshards > 1  # 分片模式：只处理自己那段，合成/清理由协调者做
        t0 = time.perf_counter()
        processed_out = 0
        img_mode = self.enc.out_kind != "video"
        img_dir: Path | None = None
        if img_mode:
            # 图片序列：每段直写最终目录（-start_number 全局续编号），无需 concat。
            # 全新任务先清掉旧编号图（上次更长的运行可能留下高帧号残帧）
            img_dir = self.output_path
            if not done and not sharded:
                img_dir.mkdir(parents=True, exist_ok=True)
                for _, f in iter_image_frames(img_dir):
                    f.unlink()
        local_done = 0  # 本路已完成段的输出帧数（分片模式进度按局部口径上报）
        tot: dict[str, float] = {}  # 分段计时累计（剖析用，写 perf_stages.jsonl）
        gap_prev_end = None
        real_total_in: int | None = None  # 解码 EOF 证实的真实总长（probe 估算可能偏大）
        try:
            for s in starts:
                if s in done:
                    continue  # 续跑：跳过已完成段
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise TaskCanceled()
                if real_total_in is None:
                    real_total_in = read_eof_marker(work)  # 另一路/上次运行已发现片尾
                if real_total_in is not None and s >= real_total_in:
                    break  # starts 升序，其后全在真实片尾外：不解码、不入 checkpoint
                n_in = min(seg, total_in - s)
                if self.shard is not None and self.progress_cb is not None:
                    # StreamPipeline 上报的是全局帧号（seg_start+local）；分片模式下
                    # 换算成本路累计（全局号在交错段间有跳跃，直接累计会把段间隙
                    # 误当进度，两路相加就超 100%）
                    def _cb(frames, total, fps, eta, _loc=local_done, _glob=s * factor):
                        self.progress_cb(_loc + frames - _glob, total, fps, eta)
                else:
                    _cb = self.progress_cb
                seg_out = (img_dir / f"%06d.{self.enc.out_kind}") if img_mode else work / f"seg_{s:06d}.mp4"
                _t_gap0 = time.perf_counter()
                if gap_prev_end is not None:
                    tot["gap"] = tot.get("gap", 0.0) + (_t_gap0 - gap_prev_end)
                pipe = StreamPipeline(
                    info, seg_out, tx, self.enc,
                    progress_cb=_cb,
                    cancel_event=self.cancel_event,
                    preview_path=self.preview_path,
                    src_preview_path=self.src_preview_path,
                    target_scale=self.target_scale,
                    target_size=self.target_size,
                    interp=self.interp,
                    seek_s=(s / info.fps) if s > 0 else None,
                    max_frames=n_in,
                    with_audio=False,  # 段只编视频，音轨最后统一合成
                    seg_start=s * factor,
                    seg_total=total_out,
                    frame_start=s * factor + 1 if img_mode else 1,
                    decode_hwaccel=self.decode_hwaccel,
                )
                await pipe.run()
                stats = pipe.stage_stats
                # 段产出帧数校验：非末段短产=解码/probe 异常，硬失败不入 checkpoint
                # （静默固化的短段会让 concat 总帧数 < 预期 → 音画漂移且续跑无法自愈）；
                # 末段短产容忍（VFR/probe 帧数估算偏差的已知取舍），记录缺口供剖析。
                # 例外——解码器干净 EOF 且未产满请求帧数 = 文件真到尾了（probe 按
                # 容器 duration 估帧数，MKV BDRemux 元数据偏大：139813 估 / 139566 实，
                # 尾段 139200 产出 366/600 曾被误判"解码异常"，正式版两次复现）。
                expect = n_in * factor
                got = int(stats.get("frames", 0.0))
                eof_tail = bool(stats.get("decode_eof")) and got < expect
                if got != expect and not eof_tail:
                    last_seg = s + n_in >= total_in
                    if not last_seg or got > expect:
                        raise PipelineError(
                            f"段 {s} 产出 {got} 帧 ≠ 预期 {expect} 帧（解码异常，本段未入 checkpoint，重试可恢复）"
                        )
                    tot["tail_short"] = tot.get("tail_short", 0.0) + (expect - got)
                if eof_tail and got == 0:
                    # 段起点已在真实片尾之外（双路另一路先撞尾）：无产物不入
                    # checkpoint（入了会让续跑跳过"看似完成"的空段），只落粗上界
                    real_total_in = s
                    _write_eof_marker(work, real_total_in)
                    break
                for k, v in stats.items():
                    tot[k] = tot.get(k, 0.0) + v
                tot["n_segs"] = tot.get("n_segs", 0.0) + 1
                with (work / "perf_stages.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"seg": s, **{k: round(v, 3) for k, v in pipe.stage_stats.items()}},
                                       ensure_ascii=False) + "\n")
                gap_prev_end = time.perf_counter()
                done.add(s)
                local_done += got
                processed_out += got
                self._save_ckpt(work, ckpt_file, done)
                if eof_tail:
                    # 真实片尾 = 段起点 + 实得输入帧数（补帧 factor 下输出恰好 2N）
                    real_total_in = s + got // factor
                    _write_eof_marker(work, real_total_in)
                    break  # 本路 starts 升序，其后段全在真实片尾外
        finally:
            if tot:
                frames = tot.get("frames", 0.0)
                rec = {"summary": True, **{k: round(v, 3) for k, v in tot.items()},
                       "fps": round(frames / tot["wall"], 2) if tot.get("wall") else 0.0,
                       "ms_per_frame": {k: round(v * 1000 / frames, 2) for k, v in tot.items()
                                        if k not in ("frames", "n_segs", "wall", "decode_eof")
                                        and frames > 0}}
                with (work / "perf_stages.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        if real_total_in is not None:
            # probe 估算被解码 EOF 证伪：进度与产物口径改用真实帧数
            total_out = real_total_in * factor

        if sharded:
            # 分片 worker 到此为止：不 trim/不 concat/不清理（协调者统一做）
            elapsed = time.perf_counter() - t0
            return RunStats(
                frames=processed_out,
                elapsed_s=elapsed,
                fps=processed_out / elapsed if elapsed > 0 else 0.0,
                out_path=self.output_path,
                out_bytes=0,
            )

        if img_mode:
            # 成功后裁掉可能残留的高帧号图；统计目录体积作为产物大小
            assert img_dir is not None
            finalize_image_dir(img_dir, total_out)
            elapsed = time.perf_counter() - t0
            if self.cleanup:
                shutil.rmtree(work, ignore_errors=True)
            return RunStats(
                frames=total_out,
                elapsed_s=elapsed,
                fps=total_out / elapsed if elapsed > 0 else 0.0,
                out_path=self.output_path,
                out_bytes=image_dir_bytes(img_dir),
            )

        # 最终合成：视频段 concat + 源音轨（与 encoder_cmd 同款 copy/aac 逻辑）
        await asyncio.get_running_loop().run_in_executor(
            None, concat_segments, work, info, self.enc, self.output_path)

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
        """原子写 checkpoint（tmp + replace，与 chunked.py 同款防半写）。

        双路并行时两 worker 各写各的段：先并入磁盘上已有的 done 集（另一路
        写入的段不能丢，否则崩溃续跑会把别人做完的段重跑一遍）。
        """
        if self.nshards > 1 and ckpt_file.exists():
            try:
                done |= set(json.loads(ckpt_file.read_text())["done"])
            except (ValueError, KeyError, OSError):
                pass  # 损坏按空集处理，本路集合照写
        tmp = work / "checkpoint.json.tmp"
        tmp.write_text(json.dumps({"done": sorted(done)}))
        tmp.replace(ckpt_file)

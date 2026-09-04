"""任务 Worker：独立进程执行单个超分任务，stdout 输出 JSON 事件行。

由 runner 通过 `python -m sv.server.worker <task_id>` 拉起。
事件协议（每行一个 JSON）：
  {"type":"started","total_frames":n,"output":...}
  {"type":"progress","frames":n,"total":n,"fps":f,"eta_sec":e}
  {"type":"log","line":"..."}
  {"type":"done","frames":n,"elapsed":s,"out_bytes":b}
  {"type":"failed","error":"..."}
退出码：0 成功 / 1 失败 / 2 任务不存在 / 3 已取消
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sv.engines.onnx_engine import OnnxSrEngine
from sv.engines.rife import Rife2x
from sv.models import manager
from sv.models.fp16 import ensure_fp16_file
from sv.models.registry import get_model, model_file
from sv.paths import SR_LOG_DIR, TEMP_DIR
from sv.pipeline.probe import (
    DECODERS,
    UnsupportedMedia,
    probe,
    probe_hwaccel,
    validate_m0,
)
from sv.pipeline.segmented import SegmentedPipeline, read_eof_marker
from sv.pipeline.stream import (
    TEXT_SUBS,
    EncodeOpts,
    PipelineError,
    RunStats,
    TaskCanceled,
    prefilter_chain,
)
from sv.server import db, settings

from sv.server.worker_common import (  # noqa: F401 — 事件/性能日志协议（含测试 re-export）
    SR_LOG_DIR,
    _prof_collect,
    _prof_enabled,
    _prof_write,
    _prof_write_video_header,
    emit,
    emit_failed,
)
from sv.server.worker_engine import (  # noqa: F401 — 引擎装配（含测试 re-export）
    _cugan_alt_hint,
    _load_onnx_engine,
)
from sv.server.worker_image import (  # noqa: F401 — 图片作业（含测试 re-export）
    _batch_heights,
    _run_image_job,
)


def _encode_opts(params: dict, out_kind: str) -> EncodeOpts:
    """任务参数 → 编码选项（app.py 已做过白名单校验，此处 defensive 再夹一层默认值）。"""
    return EncodeOpts(
        codec=params.get("codec", "h264"),
        crf=int(params.get("crf", 18)),
        preset=params.get("preset", "medium"),
        audio_mode=params.get("audio_mode", "auto"),
        subtitle_mode=params.get("subtitle_mode", "auto"),
        container=params.get("container", "mp4"),
        out_kind=out_kind,
    )


def _spawn_shard_child(task_id: str):
    """双路并行：拉起子进程跑第 2 路分段（与 runner 拉本进程同款入口形态）。"""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "worker", task_id, "--shard", "1", "2"]
    else:
        cmd = [sys.executable, "-m", "sv.server.worker", task_id, "--shard", "1", "2"]
    from sv.utils.process import WINDOWS_CREATE_FLAGS

    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=WINDOWS_CREATE_FLAGS, env={**os.environ, "PYTHONUNBUFFERED": "1"})


def _read_child_events(proc, state: dict, started_cb=None) -> None:
    """子分片进程 stdout 事件泵：聚合帧数/转发日志/捕获成败（守护线程）。

    frames 是该路的全局帧号（含段起点偏移），用增量累计成本路完成数，
    与协调者同口径相加才是真实总进度（直接相加两个全局号会翻倍）。
    """
    assert proc.stdout is not None
    last = 0
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue  # 库 print 等噪音：分片失败以 failed 事件为准
        et = ev.get("type")
        if et == "progress":
            f = ev.get("frames", last)
            state["frames"] += max(0, f - last)
            last = f
        elif et == "log":
            emit({"type": "log", "line": ev.get("line", "")})
        elif et == "done":
            state["done"] = True
        elif et == "failed":
            state["error"] = ev.get("error", "并行分片失败")


def main(task_id: str, shard: int | None = None, nshards: int = 1,
         serve: bool = False) -> int:
    """任务入口。serve=常驻模式（runner 拉起时带 --serve）：

    任务成功（rc=0）后不退出，从 stdin 读下一行 task_id 继续处理——
    进程内引擎缓存（worker_engine._ENGINE_CACHE）让同签名任务的
    「加载+预热」从每任务一次降为每签名一次（TRT 反序列化 5-8s/路）。
    非 0 退出码（failed/canceled）直接返回：失败/取消后会话健康度不可信
    （OOM 即损坏级联），runner 收到非 done 终态会关闭 stdin 丢弃本进程。
    stdin EOF（runner 关闭/死亡）= 正常退出。
    """
    while True:
        rc = _main_once(task_id, shard, nshards)
        if not serve or rc != 0:
            return rc
        try:
            line = sys.stdin.readline()
        except OSError:
            return rc
        if not line.strip():
            return rc  # runner 关闭 stdin：优雅退出
        task_id = line.strip()


def _main_once(task_id: str, shard: int | None = None, nshards: int = 1) -> int:
    task = db.get_task(task_id)
    if task is None:
        emit({"type": "failed", "error": f"任务 {task_id} 不存在"})
        return 2

    params = task["params"]
    model_id = task["model_id"]
    input_path = Path(task["input_path"])
    output_path = Path(task["output_path"])

    try:
        spec = get_model(model_id)
    except KeyError:
        emit({"type": "failed", "error": f"未知模型 {model_id}"})
        return 1

    # 图片任务：单帧专用路径（无分段/checkpoint/补帧/音频），在视频探测前分岔
    if params.get("kind") == "image":
        return _run_image_job(task, params, spec)

    try:
        info = probe(input_path)
        validate_m0(info)
    except (UnsupportedMedia, FileNotFoundError) as e:
        emit({"type": "failed", "error": str(e)})
        return 1

    scale = int(params.get("scale") or max(spec.scale))
    if scale not in spec.scale:
        emit({"type": "failed", "error": f"模型不支持 x{scale}"})
        return 1
    target = int(params.get("target_scale") or scale)
    if not (1 <= target <= scale):
        emit({"type": "failed", "error": f"目标倍率 x{target} 无效（1 ~ x{scale}）"})
        return 1
    # 精确目标分辨率（自定义分辨率模式）：优先于整数倍率，仍只允许"原生超分后缩小"
    tw = params.get("target_w")
    th = params.get("target_h")
    if tw is not None or th is not None:
        if not (isinstance(tw, int) and isinstance(th, int)):
            emit({"type": "failed", "error": "target_w/target_h 需同时提供整数宽高"})
            return 1
        if not (info.width <= tw <= info.width * scale and info.height <= th <= info.height * scale):
            emit({"type": "failed", "error": (
                f"目标分辨率 {tw}x{th} 超出范围（源 {info.width}x{info.height} ~ "
                f"原生上限 {info.width * scale}x{info.height * scale}）"
            )})
            return 1
        target_size = (tw, th)
    else:
        target_size = None
    interp_mode = params.get("interp", "off")
    if interp_mode not in ("off", "rife2x"):
        emit({"type": "failed", "error": f"未知补帧模式 {interp_mode}"})
        return 1
    out_kind = params.get("out_kind", "video")
    if out_kind not in ("video", "png", "jpg"):
        emit({"type": "failed", "error": f"未知输出类型 {out_kind}"})
        return 1
    # 字幕保留但容器装不下图形字幕（PGS/DVB 只能进 mkv）：明确告知丢弃，
    # 不静默——用户看到"字幕开关开着"却找不到轨，会以为功能坏了
    if (out_kind == "video" and params.get("subtitle_mode", "auto") == "auto"
            and info.subtitles
            and params.get("container", "mp4") in ("mp4", "mov")
            and not all(c in TEXT_SUBS for c in info.subtitles)):
        emit({"type": "log", "line":
              "源含图形字幕（PGS/DVB 等），MP4/MOV 容器无法封装，本次不保留字幕；"
              "如需保留请将封装容器改为 MKV"})
    denoise = params.get("denoise")  # real-cugan 降噪档：0/1/2/3 → 对应变体权重
    variant = f"denoise{int(denoise)}" if denoise is not None else None
    if variant is None:
        from sv.models.registry import auto_variant
        variant = auto_variant(spec, scale, info.height)  # MangaJaNai 系按源高度选权重
        if variant:
            emit({"type": "log", "line": f"按源高度 {info.height}p 自动选择权重档: {variant}"})

    try:
        from sv.models.registry import file_for_scale
        need = file_for_scale(spec, scale, variant)  # 只下本任务用到的权重
        manager.ensure_files(spec, [need])
    except Exception as e:  # 下载/校验失败
        emit({"type": "failed", "error": f"模型文件不可用: {e}"})
        return 1

    interp = None
    if interp_mode == "rife2x":
        try:
            rife_spec = get_model("rife-v4.26")
            manager.ensure_downloaded(rife_spec)
            precision = settings.load().get("precision", "fp32")
            rife_weight = model_file(rife_spec, 1, precision)
            # spec.fp16=False（转换不可用）必须尊重——曾在此无条件强转，DML 加载
            # 崩溃被下面的 except 吞掉，用户开的补帧无声失效
            if (precision == "fp16" and rife_spec.fp16
                    and not rife_weight.stem.endswith("_fp16")):
                rife_weight = ensure_fp16_file(rife_weight)
            interp = Rife2x(rife_weight)
            interp.load()
        except Exception as e:  # noqa: BLE001 — 补帧是增强项，失败降级为不补帧
            emit({"type": "log", "line": f"补帧不可用，按无补帧继续: {e}"})
            interp = None

    out_total = info.total_frames * (2 if interp is not None else 1)
    emit({
        "type": "started",
        "total_frames": out_total,
        "src_w": info.width, "src_h": info.height,
        "out_w": target_size[0] if target_size else info.width * target,
        "out_h": target_size[1] if target_size else info.height * target,
        "output": str(output_path),
    })

    # ---- 解码器：任务参数选择 + 真实源预验证（验证失败回退软解） ----
    # 预验证必须做在分段开始前：分段中途硬解初始化失败会被当成"帧中间截断"，
    # 段落落入 checkpoint 但内容缺失，破坏断点续跑语义。硬解是性能选项，
    # 创建后环境变化（换卡/驱动更新）不值得让任务硬失败，回退 + 日志说明
    # 前置滤镜串进同一验证命令：滤镜×硬解组合兼容性必须与真实解码一致地测
    decode_vf = prefilter_chain(bool(params.get("deinterlace")),
                                bool(params.get("deband")))
    if decode_vf:
        emit({"type": "log", "line": f"画面预处理已启用：{decode_vf}"})
    decoder_param = params.get("decoder", "sw")
    decode_hwaccel = None
    if decoder_param != "sw":
        hw_dec = DECODERS.get(decoder_param)
        if hw_dec and probe_hwaccel(input_path, hw_dec, info.video_codec, vf=decode_vf):
            decode_hwaccel = hw_dec
            emit({"type": "log", "line": f"硬件解码已启用（{decoder_param.upper()}）"})
        else:
            emit({"type": "log", "line": (
                f"{decoder_param} 硬件解码不可用（编码 {info.video_codec} 不支持"
                f" 或本机驱动不可用），本次使用软件解码")})

    # 批处理仅对小分辨率有益（≤720p 实测 +6%）；大分辨率单帧已喂饱，批了反而慢 9%
    batch = int(params.get("batch") or spec.io.get("batch_hint", 1) or 1)
    if info.width * info.height > 1280 * 720:
        batch = 1
    # u8 图手术（前后处理 GPU 化，BENCH 3.8x）只支持 batch=1；batch_hint 的
    # 批量收益（+6%）远小于禁用包装的代价——非用户显式指定且无分块时强制单帧。
    # 实测 960x720：batch4 无包装 104ms/帧 vs batch1 包装 26ms/帧（4x）。
    if batch > 1 and not params.get("batch") and not (params.get("tile") or spec.tile_hint):
        batch = 1

    t0 = time.perf_counter()
    prof = _prof_enabled()
    precision = settings.load().get("precision", "fp32")
    if spec.engine == "torch":
        # ---- torch 路径：分块管线 + checkpoint 续跑（PyTorch CUDA 环境）----
        from sv.engines.torch_engine import TorchSrEngine
        from sv.pipeline.chunked import ChunkedPipeline

        t_load = time.perf_counter()
        engine = TorchSrEngine(
            model_file(spec, scale), scale, io=spec.io,
            tile=int(params.get("tile") or 512),
        )
        engine.load()
        if prof:
            _prof_write_video_header(task_id, {
                "model": spec.id, "provider": str(getattr(engine, "provider_used", "?")),
                "precision": "fp16-autocast", "u8": False,
                "engine_setting": "cuda(torch)", "tile": int(params.get("tile") or 512),
                "batch": 1, "interp": interp_mode,
                "decoder": decoder_param, "prefilter": decode_vf,
                "src_w": info.width, "src_h": info.height, "fps": info.fps,
                "frames": info.total_frames,
                "target": (f"{target_size[0]}x{target_size[1]}" if target_size
                           else f"x{target}（{info.width * target}x{info.height * target}）"),
                "load_s": time.perf_counter() - t_load,
                "seg": int(params.get("chunk") or 32), "n_segs": 0,
                "parallel": False,
                "resumed": (TEMP_DIR / "chunked" / task_id).exists(),
            })
        emit({"type": "loaded", "provider": engine.provider_used, "precision": "fp16-autocast"})

        preview_dir = TEMP_DIR / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)

        def on_progress(frames, total, fps, eta):
            emit({
                "type": "progress", "frames": frames, "total": total,
                "fps": round(fps, 2), "eta_sec": int(eta),
            })

        pipeline = ChunkedPipeline(
            info, output_path, engine,
            _encode_opts(params, out_kind),
            task_id=task_id, chunk=int(params.get("chunk") or 32),
            progress_cb=on_progress,
            preview_path=preview_dir / f"{task_id}.jpg",
            src_preview_path=preview_dir / f"{task_id}_src.jpg",
            target_size=target_size,
            decode_hwaccel=decode_hwaccel,
            decode_vf=decode_vf,
        )
        try:
            stats = asyncio.run(pipeline.run())
        except TaskCanceled:
            emit({"type": "canceled"})
            return 3
        except PipelineError as e:
            emit({"type": "failed", "error": str(e)})
            return 1
        except Exception as e:  # noqa: BLE001
            emit({"type": "failed", "error": f"{type(e).__name__}: {e}"})
            return 1
        if prof:
            _fps = stats.frames / stats.elapsed_s if stats.elapsed_s > 0 else 0
            _prof_write(task_id, f"==== 运行结束 {stats.frames}帧 · {stats.elapsed_s:.1f}s"
                        f" · 平均 {_fps:.2f} fps（端到端口径） ====\n\n")
        emit({
            "type": "done", "frames": stats.frames,
            "elapsed": round(stats.elapsed_s, 1),
            "out_bytes": stats.out_bytes,
            "preview": str(preview_dir / f"{task_id}.jpg"),
            "src_preview": str(preview_dir / f"{task_id}_src.jpg"),
        })
        return 0

    try:
        weight = model_file(spec, scale, precision, variant)
        tile = int(params.get("tile") or spec.tile_hint)
        t_load = time.perf_counter()
        engine, used_precision = _load_onnx_engine(
            weight, spec, scale, variant, precision, tile,
            (info.height, info.width), batch=batch, log=emit)
    except Exception as e:  # noqa: BLE001 — 与图片路径同：失败带堆栈落 sidecar.log
        emit_failed("引擎加载失败", e)
        return 1
    load_s = time.perf_counter() - t_load
    emit({"type": "loaded", "provider": engine.provider_used, "precision": used_precision})

    preview_dir = TEMP_DIR / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{task_id}.jpg"
    src_preview_path = preview_dir / f"{task_id}_src.jpg"

    # ---- 双路并行判定（协调者身份才分叉；分片子进程只跑自己的段防递归）----
    # 条件：开关开 + onnx 模型 + 无补帧 + 视频输出 + 至少 8 段——每路要各自
    # 加载一遍推理引擎（TRT 反序列化 ~5-8s），段太少时双路省的计算抵不过
    # 双倍的引擎加载（91 帧小视频实测 51fps → 29fps 反而变慢）
    seg_est = min(600, max(60, info.total_frames // 8))
    n_segs = -(-info.total_frames // seg_est)  # ceil(total/seg)
    parallel = (
        shard is None
        and settings.load().get("parallel_streams") is True
        and spec.engine == "onnx"
        and interp is None
        and out_kind == "video"
        and n_segs >= 8
    )
    child = None
    child_state: dict = {"frames": 0, "done": False, "error": None}

    if prof and shard is None:
        _prof_write_video_header(task_id, {
            "model": spec.id,
            "provider": "/".join(engine.provider_used or []) or "?",
            "precision": used_precision,
            "u8": bool(getattr(engine, "u8_wrapped", False)),
            "engine_setting": settings.load().get("engine", "auto"),
            "tile": tile, "batch": batch, "interp": interp_mode,
            "decoder": decoder_param, "prefilter": decode_vf,
            "src_w": info.width, "src_h": info.height, "fps": info.fps,
            "frames": info.total_frames,
            "target": (f"{target_size[0]}x{target_size[1]}" if target_size
                       else f"x{target}（{info.width * target}x{info.height * target}）"),
            "load_s": load_s, "seg": seg_est, "n_segs": n_segs,
            "parallel": parallel,
            "resumed": (TEMP_DIR / "segmented" / task_id / "checkpoint.json").exists(),
        })

    if parallel:
        child = _spawn_shard_child(task_id)
        threading.Thread(target=_read_child_events, args=(child, child_state),
                         daemon=True).start()
        emit({"type": "log", "line": "双路并行已启用：两个进程分段同时处理"})

        own = {"last": 0, "done": 0}  # 本路帧号 → 增量累计完成数

        def on_progress(frames, total, fps, eta):
            # 两路各自增量累计之和 = 真实总进度（全局帧号直接相加会翻倍）
            own["done"] += max(0, frames - own["last"])
            own["last"] = frames
            f = own["done"] + child_state["frames"]
            el = time.perf_counter() - t0
            fps_all = f / el if el > 0 else 0.0
            emit({"type": "progress", "frames": f, "total": total,
                  "fps": round(fps_all, 2),
                  "eta_sec": int((total - f) / fps_all) if fps_all > 0 else 0})
    else:
        def on_progress(frames, total, fps, eta):
            emit({
                "type": "progress", "frames": frames, "total": total,
                "fps": round(fps, 2), "eta_sec": int(eta),
            })

    # 分段管线：取消/崩溃后"继续"可跳过已完成段（checkpoint 在 .tmp/segmented/<task_id>）
    # 协调者=第 0 路（其余段由子进程跑），分片子进程只跑自己的段；两者都不做
    # 收尾合成——双路都完成后由协调者统一 concat + 清理
    my_shard: int | None = shard
    my_nshards = nshards
    if parallel:
        my_shard, my_nshards = 0, 2
    sharded = my_shard is not None
    pipeline = SegmentedPipeline(
        info, output_path, engine,
        _encode_opts(params, out_kind),
        task_id=task_id,
        progress_cb=on_progress,
        preview_path=None if sharded else preview_path,
        src_preview_path=None if sharded else src_preview_path,
        target_scale=None if target_size else target,
        target_size=target_size,
        interp=interp,
        shard=my_shard, nshards=my_nshards,
        # 剖析模式：成功后不删工作目录，等 _prof_collect 把分段耗时并入日志再清
        cleanup=(not sharded) and not prof,
        decode_hwaccel=decode_hwaccel,
        decode_vf=decode_vf,
    )
    work_dir = TEMP_DIR / "segmented" / task_id
    try:
        stats = asyncio.run(pipeline.run())
        if parallel:
            # 双路收尾：等子进程退出 + 校验成败 + 合成 + 清理
            if child_state["error"]:
                raise PipelineError(f"并行分片失败: {child_state['error']}")
            try:
                child.wait(timeout=600)
            except subprocess.TimeoutExpired:
                raise PipelineError("并行分片超时未退出")
            if child.returncode != 0 or not child_state["done"]:
                raise PipelineError(f"并行分片异常退出 (rc={child.returncode})")
            from sv.pipeline.segmented import concat_segments

            concat_segments(work_dir, info, _encode_opts(params, out_kind),
                            output_path)
            if prof:
                _prof_collect(task_id, work_dir)
            # 真实片尾修正（probe 高估被 EOF 证伪）：done 帧数/进度按实际值；
            # 先读标记再清目录
            real_in = read_eof_marker(work_dir)
            real_out = (real_in * (2 if interp is not None else 1)
                        if real_in is not None else out_total)
            shutil.rmtree(work_dir, ignore_errors=True)
            stats = RunStats(
                frames=real_out,
                elapsed_s=time.perf_counter() - t0,
                fps=real_out / max(time.perf_counter() - t0, 1e-6),
                out_path=output_path,
                out_bytes=output_path.stat().st_size if output_path.exists() else 0,
            )
    except TaskCanceled:
        emit({"type": "canceled"})
        return 3
    except PipelineError as e:
        emit({"type": "failed", "error": str(e)})
        return 1
    except Exception as e:  # noqa: BLE001 — worker 兜底，任何异常都要上报
        emit({"type": "failed", "error": f"{type(e).__name__}: {e}"})
        return 1
    finally:
        if child is not None and child.poll() is None:
            from sv.utils.process import kill_tree

            kill_tree(child.pid)  # 本路失败/取消时带走分片子进程

    if prof and shard is None:
        if not parallel:
            # 单路成功收尾：并入分段耗时明细后清掉工作目录（剖析模式 pipeline 未自清）
            _prof_collect(task_id, work_dir)
            shutil.rmtree(work_dir, ignore_errors=True)
        fps_e2e = stats.frames / stats.elapsed_s if stats.elapsed_s > 0 else 0
        _prof_write(task_id,
                    f"==== 运行结束 {stats.frames}帧 · {stats.elapsed_s:.1f}s"
                    f" · 平均 {fps_e2e:.2f} fps（端到端口径：含引擎加载与合成） ====\n\n")

    emit({
        "type": "done", "frames": stats.frames,
        "total_frames": stats.frames,  # 真实片尾修正后回填 DB（旧事件无此键按估算）
        "elapsed": round(stats.elapsed_s, 1),
        "out_bytes": stats.out_bytes, "preview": str(preview_path),
        "src_preview": str(src_preview_path),
    })
    return 0

if __name__ == "__main__":
    import argparse

    # Windows 管道默认走系统 locale：中文系统 GBK 尚可、英文系统 cp1252 直接
    # UnicodeEncodeError（一行事件都吐不出）。runner 真实链路带 PYTHONIOENCODING；
    # 直跑（-m / 调试 / 测试）由此兜底，与 cli.main 同款
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser(prog="worker")
    ap.add_argument("task_id")
    ap.add_argument("--shard", nargs=2, type=int, metavar=("I", "N"),
                    default=None, help="[内部] 双路并行：本进程跑第 I 路(0基)/共 N 路")
    ap.add_argument("--serve", action="store_true",
                    help="[内部] runner 常驻模式：任务成功后经 stdin 接续下一任务")
    args = ap.parse_args()
    sh = args.shard[0] if args.shard else None
    ns = args.shard[1] if args.shard else 1
    sys.exit(main(args.task_id, sh, ns, serve=args.serve))

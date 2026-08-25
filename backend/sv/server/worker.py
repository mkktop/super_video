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
from sv.paths import TEMP_DIR
from sv.pipeline.probe import UnsupportedMedia, probe, validate_m0
from sv.pipeline.segmented import SegmentedPipeline
from sv.pipeline.stream import EncodeOpts, PipelineError, RunStats, TaskCanceled
from sv.server import db, settings


def _encode_opts(params: dict, out_kind: str) -> EncodeOpts:
    """任务参数 → 编码选项（app.py 已做过白名单校验，此处 defensive 再夹一层默认值）。"""
    return EncodeOpts(
        codec=params.get("codec", "h264"),
        crf=int(params.get("crf", 18)),
        preset=params.get("preset", "medium"),
        audio_mode=params.get("audio_mode", "auto"),
        subtitle_mode=params.get("subtitle_mode", "none"),
        container=params.get("container", "mp4"),
        out_kind=out_kind,
    )


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


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
        # started 等其余事件忽略（本进程已上报过）


def main(task_id: str, shard: int | None = None, nshards: int = 1) -> int:
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
    denoise = params.get("denoise")  # real-cugan 降噪档：0/1/2/3 → 对应变体权重
    variant = f"denoise{int(denoise)}" if denoise is not None else None

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
            if precision == "fp16" and not rife_weight.stem.endswith("_fp16"):
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
    precision = settings.load().get("precision", "fp32")
    if spec.engine == "torch":
        # ---- torch 路径：分块管线 + checkpoint 续跑（PyTorch CUDA 环境）----
        from sv.engines.torch_engine import TorchSrEngine
        from sv.pipeline.chunked import ChunkedPipeline

        engine = TorchSrEngine(
            model_file(spec, scale), scale, io=spec.io,
            tile=int(params.get("tile") or 512),
        )
        engine.load()
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
        emit({
            "type": "done", "frames": stats.frames,
            "elapsed": round(stats.elapsed_s, 1),
            "out_bytes": stats.out_bytes,
            "preview": str(preview_dir / f"{task_id}.jpg"),
            "src_preview": str(preview_dir / f"{task_id}_src.jpg"),
        })
        return 0

    weight = model_file(spec, scale, precision, variant)
    # 惰性补转 fp16（一次），失败回退 fp32；spec.fp16=False 的模型（如 real-cugan，
    # 转换后 ShapeInference 崩）直接用 fp32 原件
    if precision == "fp16" and spec.fp16 and not weight.stem.endswith("_fp16"):
        emit({"type": "log", "line": f"生成 fp16 变体: {weight.name}"})
        weight = ensure_fp16_file(weight)
    used_precision = "fp16" if weight.stem.endswith("_fp16") else "fp32"

    def _oom(e: Exception) -> bool:
        t = str(e).lower()
        return "memory" in t or "alloc" in t or "oom" in t

    tile = int(params.get("tile") or spec.tile_hint)
    # 推理后端：设置 engine=trt 时走 TensorRT 链（TRT 不可用引擎层自动回退）
    ort_device = "trt" if settings.load().get("engine") == "trt" else "auto"
    if ort_device == "trt":
        if getattr(sys, "frozen", False):
            # 安装版：激活 TRT 组件（GPU 版 onnxruntime 重定向）。必须在
            # 进程内首次 import onnxruntime 之前（OnnxSrEngine.load 惰性导入）；
            # 组件缺失/不兼容返回 False → 引擎层自然回退 DML
            from sv.engines.trt_runtime import activate_component

            if activate_component():
                emit({"type": "log", "line": "TRT 组件已激活（GPU 版运行时）"})
        emit({"type": "log", "line":
              "TensorRT 引擎加载中（新模型/新分辨率首次编译需 1~2 分钟，之后秒级启动）"})
    engine = None
    for _ in range(3):  # 显存不足自动降档：tile 逐步减半
        try:
            engine = OnnxSrEngine(
                weight, scale, io=spec.io, tile=tile, batch=batch,
                device=ort_device)
            engine.load()
            import numpy as np
            # 预热兼显存探测。必须用源帧真实尺寸：DML 会话一旦跑过 64x64 这类
            # 小形状，后续真实尺寸的执行路径被拖慢且不可逆（实测 960x720：
            # 25.7ms -> 38.5ms/帧，+50%；先小后大也无法自愈）
            engine.process(np.zeros((info.height, info.width, 3), dtype=np.uint8))
            break
        except Exception as e:  # noqa: BLE001
            if not _oom(e) or tile in (1,):
                raise
            new_tile = 256 if tile == 0 else max(64, tile // 2)
            emit({"type": "log", "line": f"显存不足，tile {tile or '关'} -> {new_tile} 重试"})
            tile = new_tile
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

    if parallel:
        child = _spawn_shard_child(task_id)
        threading.Thread(target=_read_child_events, args=(child, child_state),
                         daemon=True).start()
        emit({"type": "log", "line": "双路并行已启用（两个进程分段同时推理）"})

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
        cleanup=not sharded,
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
            shutil.rmtree(work_dir, ignore_errors=True)
            stats = RunStats(
                frames=out_total,
                elapsed_s=time.perf_counter() - t0,
                fps=out_total / max(time.perf_counter() - t0, 1e-6),
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

    emit({
        "type": "done", "frames": stats.frames,
        "elapsed": round(stats.elapsed_s, 1),
        "out_bytes": stats.out_bytes, "preview": str(preview_path),
        "src_preview": str(src_preview_path),
    })
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(prog="worker")
    ap.add_argument("task_id")
    ap.add_argument("--shard", nargs=2, type=int, metavar=("I", "N"),
                    default=None, help="[内部] 双路并行：本进程跑第 I 路(0基)/共 N 路")
    args = ap.parse_args()
    sh = args.shard[0] if args.shard else None
    ns = args.shard[1] if args.shard else 1
    sys.exit(main(args.task_id, sh, ns))

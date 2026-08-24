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
import sys
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
from sv.pipeline.stream import EncodeOpts, PipelineError, TaskCanceled
from sv.server import db, settings


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main(task_id: str) -> int:
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
    denoise = params.get("denoise")  # real-cugan 降噪档：3 → denoise3 变体
    variant = f"denoise{int(denoise)}" if denoise else None

    try:
        manager.ensure_downloaded(spec)
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
            EncodeOpts(
                codec=params.get("codec", "h264"),
                crf=int(params.get("crf", 18)),
                preset=params.get("preset", "medium"),
                out_kind=out_kind,
            ),
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
    if precision == "fp16" and not weight.stem.endswith("_fp16"):
        emit({"type": "log", "line": f"生成 fp16 变体: {weight.name}"})
        weight = ensure_fp16_file(weight)  # 惰性补转（一次），失败回退 fp32
    used_precision = "fp16" if weight.stem.endswith("_fp16") else "fp32"

    def _oom(e: Exception) -> bool:
        t = str(e).lower()
        return "memory" in t or "alloc" in t or "oom" in t

    tile = int(params.get("tile") or spec.tile_hint)
    engine = None
    for _ in range(3):  # 显存不足自动降档：tile 逐步减半
        try:
            engine = OnnxSrEngine(weight, scale, io=spec.io, tile=tile, batch=batch)
            engine.load()
            import numpy as np
            engine.process(np.zeros((64, 64, 3), dtype=np.uint8))  # 预热兼显存探测
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

    def on_progress(frames, total, fps, eta):
        emit({
            "type": "progress", "frames": frames, "total": total,
            "fps": round(fps, 2), "eta_sec": int(eta),
        })

    # 分段管线：取消/崩溃后"继续"可跳过已完成段（checkpoint 在 .tmp/segmented/<task_id>）
    pipeline = SegmentedPipeline(
        info, output_path, engine,
        EncodeOpts(
            codec=params.get("codec", "h264"),
            crf=int(params.get("crf", 18)),
            preset=params.get("preset", "medium"),
            out_kind=out_kind,
        ),
        task_id=task_id,
        progress_cb=on_progress, preview_path=preview_path,
        src_preview_path=src_preview_path,
        target_scale=None if target_size else target,
        target_size=target_size,
        interp=interp,
    )
    try:
        stats = asyncio.run(pipeline.run())
    except TaskCanceled:
        emit({"type": "canceled"})
        return 3
    except PipelineError as e:
        emit({"type": "failed", "error": str(e)})
        return 1
    except Exception as e:  # noqa: BLE001 — worker 兜底，任何异常都要上报
        emit({"type": "failed", "error": f"{type(e).__name__}: {e}"})
        return 1

    emit({
        "type": "done", "frames": stats.frames,
        "elapsed": round(stats.elapsed_s, 1),
        "out_bytes": stats.out_bytes, "preview": str(preview_path),
        "src_preview": str(src_preview_path),
    })
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m sv.server.worker <task_id>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))

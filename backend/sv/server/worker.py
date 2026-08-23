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
from sv.models import manager
from sv.models.registry import get_model, model_file
from sv.paths import TEMP_DIR
from sv.pipeline.probe import UnsupportedMedia, probe, validate_m0
from sv.pipeline.stream import EncodeOpts, PipelineError, StreamPipeline, TaskCanceled
from sv.server import db


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

    try:
        manager.ensure_downloaded(spec)
    except Exception as e:  # 下载/校验失败
        emit({"type": "failed", "error": f"模型文件不可用: {e}"})
        return 1

    emit({
        "type": "started",
        "total_frames": info.total_frames,
        "src_w": info.width, "src_h": info.height,
        "out_w": info.width * target, "out_h": info.height * target,
        "output": str(output_path),
    })

    # 批处理仅对小分辨率有益（≤720p 实测 +6%）；大分辨率单帧已喂饱，批了反而慢 9%
    batch = int(params.get("batch") or spec.io.get("batch_hint", 1) or 1)
    if info.width * info.height > 1280 * 720:
        batch = 1

    t0 = time.perf_counter()
    engine = OnnxSrEngine(
        model_file(spec, scale), scale, io=spec.io,
        tile=int(params.get("tile") or spec.tile_hint),
        batch=batch,
    )
    engine.load()
    emit({"type": "loaded", "provider": engine.provider_used})

    import numpy as np

    engine.process(np.zeros((64, 64, 3), dtype=np.uint8))  # 预热

    preview_dir = TEMP_DIR / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{task_id}.jpg"
    src_preview_path = preview_dir / f"{task_id}_src.jpg"

    def on_progress(frames, total, fps, eta):
        emit({
            "type": "progress", "frames": frames, "total": total,
            "fps": round(fps, 2), "eta_sec": int(eta),
        })

    pipeline = StreamPipeline(
        info, output_path, engine,
        EncodeOpts(
            codec=params.get("codec", "h264"),
            crf=int(params.get("crf", 18)),
            preset=params.get("preset", "medium"),
        ),
        progress_cb=on_progress, preview_path=preview_path,
        src_preview_path=src_preview_path,
        target_scale=target,
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

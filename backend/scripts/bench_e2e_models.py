"""端到端吞吐基准：真管线（解码→推理→编码）× 全模型 × 后端（RTX 5080）。

与「全模型基准」（bench_all_models.py，纯推理口径）互补：本脚本走产品同款
StreamPipeline——ffmpeg 解码管道 → engine.process 逐帧 → ffmpeg libx264 crf18
编码成 MP4（默认链路），fps 为 RunStats.fps = 成品帧数 / 整条管线 wall，
即用户在任务卡上看到的那类真实吞吐。

口径：samples/动漫测试1.mp4 无损剪出 6 秒片段（一次性预裁），预热后整段跑完；
每格附带 stage_stats（read/infer/write 各阶段秒数）。RIFE 是成对帧补帧引擎、
不走单流超分管线，不在本表；torch 引擎入列但注意其颜色契约与 RGB 管线不
一致（BGR 直喂），仅吞吐数据有效。

用法：
  .venv/Scripts/python.exe      scripts/bench_e2e_models.py --device auto --json out.jsonl
  .venv-cuda/Scripts/python.exe scripts/bench_e2e_models.py --device trt  --json out.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sv.pipeline.probe import probe
from sv.pipeline.stream import EncodeOpts, StreamPipeline
from sv.utils.process import WINDOWS_CREATE_FLAGS

from bench_all_models import H, W, SR_ENTRIES  # noqa: E402  复用矩阵定义

CLIP_S = 6


def ensure_clip() -> Path:
    """从样例片无损剪 CLIP_S 秒做统一测试源（重复执行幂等）。"""
    from sv.paths import TEMP_DIR

    src = Path(__file__).resolve().parents[2] / "samples" / "动漫测试1.mp4"
    dst = TEMP_DIR / "bench_20260827" / f"clip{CLIP_S}s.mp4"
    if dst.exists():
        return dst
    from sv.paths import ffmpeg_bin

    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-y", "-loglevel", "error",
         "-i", str(src), "-t", str(CLIP_S), "-c", "copy", str(dst)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )
    return dst


def bench_one(info, spec_model: dict, model_id: str, scale, variant,
              label: str, device: str, out_dir: Path,
              *, part: int = 0, parts: int = 1, codec: str = "h264",
              tag: str = "") -> dict:
    from sv.engines.onnx_engine import OnnxSrEngine
    from sv.models.fp16 import ensure_fp16_file
    from sv.models.registry import model_file

    spec = spec_model[model_id]
    # 与 worker 同款的 CUGAN×DML 拒绝（BENCH §13：DML 逐帧泄漏必崩，TRT/CPU 可用）
    if "cugan" in spec.id.lower() and device == "auto":
        return {"model": model_id, "status": "skipped",
                "reason": "CUGAN×DML 显存泄漏必崩（BENCH §13），需 TRT/CPU 后端"}
    weight = model_file(spec, scale, "fp16", variant)
    if not weight.exists():
        return {"model": model_id, "status": "skipped", "reason": f"缺权重 {weight}"}
    if spec.fp16 and not weight.stem.endswith("_fp16"):
        weight = ensure_fp16_file(weight)
    tile = int((spec.engine_kwargs(scale, 0).get("tile")) or 0)

    import os
    eng = OnnxSrEngine(weight, scale, io=spec.io, tile=tile, batch=1,
                       device=device, validate_hw=(info.height, info.width),
                       manifest_allow_wrap=getattr(spec, "u8_wrap", True),
                       u8_wrap=not os.environ.get("SV_BENCH_NOWRAP"))
    eng.load()
    import numpy as np
    eng.process(np.zeros((info.height, info.width, 3), dtype=np.uint8))  # 预热真实尺寸

    # 分片（双路并行口径）：按帧均分，各分片独立编码无音轨，外层负责拼接混音
    cfps = info.fps
    total = info.total_frames
    per = (total + parts - 1) // parts if parts > 1 else total
    start_f = part * per
    n = max(0, min(per, total - start_f))
    suffix = f".{tag}p{part}" if parts > 1 else ""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{model_id}_{scale}{suffix}.mp4"
    pipe = StreamPipeline(info, out, eng, encode=EncodeOpts(codec=codec),
                          seek_s=(start_f / cfps) if n else None,
                          max_frames=n or None,
                          with_audio=parts == 1,
                          seg_start=start_f, seg_total=total)
    t0 = time.perf_counter()
    stats = asyncio.run(pipe.run())
    wall = time.perf_counter() - t0
    stages = {k: round(v, 2) for k, v in pipe.stage_stats.items()}
    row = {
        "model": model_id, "label": label, "scale": scale, "variant": variant,
        "tile": tile, "status": "ok", "mode": "dual" if parts > 1 else "single",
        "part": f"{part}/{parts}", "codec": codec,
        "provider": eng.provider_used[0] if eng.provider_used else "?",
        "precision_used": "fp16" if weight.stem.endswith("_fp16") else "fp32",
        "u8_wrapped": bool(getattr(eng, "u8_wrapped", False)),
        "frames": stats.frames,
        "e2e_fps": round(stats.frames / wall, 1),
        "wall_s": round(wall, 2),
        "out_mb": round(stats.out_bytes / 1048576, 1),
        "stages": stages,
    }
    del eng
    gc.collect()
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", choices=["auto", "trt"], default="auto")
    ap.add_argument("--json", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--clip", default="", help="覆盖默认测试片源路径")
    ap.add_argument("--parts", type=int, default=1, help="分片数（2=双路并行口径）")
    ap.add_argument("--part", type=int, default=0)
    ap.add_argument("--codec", default="h264")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    from sv.models.registry import load_registry

    specs = load_registry()
    clip = Path(args.clip) if args.clip else ensure_clip()
    info = probe(clip)
    print(f"# device={args.device} codec={args.codec} clip={clip.name} "
          f"{info.width}x{info.height}@{info.fps:.3f} frames={info.total_frames} "
          f"parts={args.parts}", flush=True)

    sink = Path(args.json).open("a", encoding="utf-8") if args.json else None

    def emit(row: dict) -> None:
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if sink:
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            sink.flush()

    only = args.only.strip().lower()
    for model_id, scale, variant, label in SR_ENTRIES:
        if only and only not in model_id.lower() and only not in label.lower():
            continue
        try:
            emit(bench_one(info, specs, model_id, scale, variant, label,
                           args.device, clip.parent / "out",
                           part=args.part, parts=args.parts,
                           codec=args.codec, tag=args.tag))
        except Exception as e:  # noqa: BLE001
            emit({"model": model_id, "label": label, "status": "error",
                  "reason": f"{type(e).__name__}: {str(e)[:260]}"})

    # torch 引擎：CUDA 环境才有；颜色契约与 RGB 管线不一致，仅吞吐口径有效。
    # 分片（双路并行）模式不属于单模型单进程口径，直接结束。
    if args.parts == 1 and (not only or "torch" in only):
        try:
            from sv.engines.nvidia_dlls import register_nvidia_dlls

            register_nvidia_dlls()
            import torch  # noqa: F401

            if torch.cuda.is_available():
                from sv.engines.torch_engine import TorchSrEngine
                from sv.models.registry import model_file as mf

                spec = specs["realesrgan-x4plus-torch"]
                eng = TorchSrEngine(mf(spec, 4), 4, io=spec.io, tile=256)
                eng.load()
                out = clip.parent / "out" / "torch_x4plus.mp4"
                pipe = StreamPipeline(info, out, eng, encode=EncodeOpts())
                t0 = time.perf_counter()
                stats = asyncio.run(pipe.run())
                wall = time.perf_counter() - t0
                emit({"model": "realesrgan-x4plus-torch", "tile": 256,
                      "status": "ok", "device": eng.device,
                      "frames": stats.frames,
                      "e2e_fps": round(stats.frames / wall, 1),
                      "wall_s": round(wall, 2)})
        except Exception as e:  # noqa: BLE001
            emit({"model": "realesrgan-x4plus-torch", "status": "error",
                  "reason": f"{type(e).__name__}: {str(e)[:260]}"})

    if sink:
        sink.close()
        print(f"# 已写出 {args.json}", flush=True)


if __name__ == "__main__":
    main()

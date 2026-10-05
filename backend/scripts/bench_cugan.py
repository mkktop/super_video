"""Real-CUGAN 真实权重基准：产品同款引擎 + 解码/超分/HEVC NVENC 管线。

每个倍率独立进程运行，冷启动与热推理分开记录，不改应用设置。
  ../.venv-cuda/Scripts/python.exe scripts/bench_cugan.py --scale 2
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from sv.models.registry import file_for_scale, get_model, model_file
from sv.paths import ROOT, ffmpeg_bin
from sv.pipeline.probe import probe
from sv.pipeline.stream import EncodeOpts, StreamPipeline
from sv.server import settings
from sv.server.worker_engine import _load_onnx_engine
from sv.utils.process import WINDOWS_CREATE_FLAGS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=int, choices=[2, 3, 4], required=True)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--frames", type=int, default=48)
    parser.add_argument("--inference-frames", type=int, default=20)
    parser.add_argument("--tile", type=int, default=0, help="0 为整帧，正数为分块尺寸")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".tmp" / "cugan-bench-20261005")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # 仅当前基准进程覆盖读取，不持久化，也不影响运行中的应用。
    current = settings.load()
    settings.load = lambda: {**current, "engine": "trt", "precision": "fp16"}
    spec = get_model("real-cugan")
    weight = model_file(spec, args.scale)
    if not weight.is_file():
        raise RuntimeError(f"请先下载 {file_for_scale(spec, args.scale)['name']}")
    expected = file_for_scale(spec, args.scale)["sha256"]
    digest = hashlib.sha256(weight.read_bytes()).hexdigest()
    if digest != expected:
        raise RuntimeError("权重 sha256 校验失败")
    src = args.output_dir / f"source_{args.width}x{args.height}_{args.frames}f.mp4"
    if not src.exists():
        subprocess.run([
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={args.width}x{args.height}:rate=24",
            "-frames:v", str(args.frames), "-c:v", "libx264", "-crf", "18",
            "-pix_fmt", "yuv420p", str(src),
        ], check=True, creationflags=WINDOWS_CREATE_FLAGS)
    print(f"START x{args.scale}: {weight.name}, {args.width}x{args.height}", flush=True)
    t0 = time.perf_counter()
    engine, precision = _load_onnx_engine(
        weight, spec, args.scale, None, "fp16", args.tile,
        (args.height, args.width), batch=1,
        log=lambda ev: print(ev.get("line", ""), flush=True),
    )
    load_s = time.perf_counter() - t0
    providers = engine.provider_used
    if not providers or providers[0] != "TensorrtExecutionProvider":
        raise RuntimeError(f"实际后端 {providers}，不能将回退结果报告为 TensorRT 速度")
    print(f"ENGINE_READY: {load_s:.2f}s providers={providers} tile={engine.tile}", flush=True)
    gy, gx = np.mgrid[0:args.height, 0:args.width]
    frame = np.stack([(gx * 255) // args.width, (gy * 255) // args.height,
                      (gx + gy) % 256], axis=-1).astype(np.uint8)
    for _ in range(3):
        engine.process(frame)
    times = []
    for _ in range(args.inference_frames):
        started = time.perf_counter()
        out = engine.process(frame)
        times.append(time.perf_counter() - started)
    assert out.shape == (args.height * args.scale, args.width * args.scale, 3)
    assert out.dtype == np.uint8 and float(out.std()) > 6
    inference_fps = len(times) / sum(times)
    print(f"INFERENCE: {inference_fps:.2f}fps", flush=True)
    suffix = f"_tile{args.tile}" if args.tile else ""
    dst = args.output_dir / f"real-cugan_x{args.scale}_{args.width}x{args.height}{suffix}.mp4"
    stats = asyncio.run(StreamPipeline(
        probe(src), dst, engine,
        EncodeOpts(codec="hevc_nvenc", crf=18, audio_mode="none"),
        progress_cb=lambda frames, total, fps, eta: print(
            f"PIPELINE: {frames}/{total} frames {fps:.2f}fps", flush=True
        ) if frames == 1 or frames % 12 == 0 or frames == total else None,
    ).run())
    output = probe(dst)
    assert stats.frames == args.frames
    assert output.total_frames == args.frames
    assert (output.width, output.height) == (args.width * args.scale, args.height * args.scale)
    result = {
        "model": spec.id, "variant": "conservative", "scale": args.scale,
        "weight": str(weight), "sha256": digest, "source": str(src),
        "input_size": [args.width, args.height], "output_size": [output.width, output.height],
        "providers": providers, "weight_precision": precision,
        "trt_fp16": engine.trt_fp16, "tile": engine.tile, "u8_wrapped": engine.u8_wrapped,
        "load_and_warmup_s": load_s, "inference_frames": len(times),
        "inference_fps": inference_fps, "inference_median_ms": float(np.median(times)) * 1000,
        "pipeline_frames": stats.frames, "pipeline_elapsed_s": stats.elapsed_s,
        "pipeline_fps": stats.fps, "encoder": "hevc_nvenc", "crf": 18,
        "output": str(dst), "output_bytes": stats.out_bytes,
    }
    report = args.output_dir / f"x{args.scale}_{args.width}x{args.height}{suffix}.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

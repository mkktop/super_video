"""全模型 × 推理后端基准（RTX 5080，2026-08-27）。

矩阵：11 个已装 ONNX 超分权重 × {DML-fp16, DML-fp32, TRT-fp16} + RIFE 补帧
（DML/CUDA 各一）+ x4plus torch 引擎（CUDA）。固定输入 960x540 合成自然图，
每条目 预热 3 帧（首帧含 u8 A/B 校验与 TRT 引擎构建）+ 计时 20 帧。

计时口径：engine.process() 单帧 wall 的均值（=总 elapsed/N，防聚合虚高口径）
与逐帧中位数双报。加载复用产品同款路径等价物：fp16 惰性补转、u8 图手术包装
（tile=0 动态尺寸）、provider 链选择均走 OnnxSrEngine 本体。

用法：
  .venv/Scripts/python.exe        scripts/bench_all_models.py --device auto --precision fp16 --json out.jsonl
  .venv-cuda/Scripts/python.exe   scripts/bench_all_models.py --device trt  --precision fp16 --json out.jsonl
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from sv.engines.onnx_engine import OnnxSrEngine
from sv.models.fp16 import ensure_fp16_file
from sv.models.registry import load_registry, model_file

H, W = 540, 960
WARMUP = 3
FRAMES = 20

# (model_id, scale, variant, 展示名)
SR_ENTRIES = [
    ("realesr-animevideov3", 2, None, "AnimeVideo xs x2"),
    ("realesr-animevideov3", 4, None, "AnimeVideo v3 x4"),
    ("animejanai-v2-l1", 2, None, "AnimeJaNai V2 L1"),
    ("animejanai-v2-l2", 2, None, "AnimeJaNai V2 L2"),
    ("animejanai-v2-l3", 2, None, "AnimeJaNai V2 L3"),
    ("animejanai-v3-hd-l1", 2, None, "AnimeJaNai V3 HD L1"),
    ("animejanai-v3-hd-l2", 2, None, "AnimeJaNai V3 HD L2"),
    ("animejanai-v3-hd-l3", 2, None, "AnimeJaNai V3 HD L3"),
    ("real-cugan", 2, "denoise0", "Real-CUGAN x2 无降噪"),
    ("real-cugan", 4, "denoise3", "Real-CUGAN x4 降噪3"),
    ("realesrgan-x4plus", 4, None, "Real-ESRGAN x4plus(动态)"),
]


def synth_frame() -> np.ndarray:
    """合成自然图：渐变 + 噪声（与 FP16 基准同风格，种子固定可复现）。"""
    rng = np.random.default_rng(7)
    gx = np.linspace(0, 200, W, dtype=np.float32)
    gy = np.linspace(0, 240, H, dtype=np.float32)
    base = gy[:, None] + gx[None, :]
    img = np.stack([base, base * 0.82, base * 0.6], axis=-1)
    img += rng.normal(0, 12, (H, W, 3)).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def bench_sr(specs, model_id: str, scale: int, variant, device: str,
             precision: str) -> dict:
    spec = specs[model_id]
    want_fp16 = precision == "fp16" and spec.fp16
    weight = model_file(spec, scale, "fp16" if want_fp16 else "fp32", variant)
    if not weight.exists():
        return {"model": model_id, "scale": scale, "variant": variant,
                "status": "skipped", "reason": f"权重不存在: {weight}"}
    if want_fp16 and not weight.stem.endswith("_fp16"):
        weight = ensure_fp16_file(weight)

    # tile 取产品同款口径：显式 0 时按 manifest tile_hint 兜底（x4plus=512 分块，
    # 不参与 u8 包装；其余动态小模型保持 0 全帧直推）
    tile = int((spec.engine_kwargs(scale, 0).get("tile")) or 0)
    eng = OnnxSrEngine(weight, scale, io=spec.io, tile=tile, batch=1,
                       device=device, validate_hw=(H, W), manifest_allow_wrap=getattr(spec, "u8_wrap", True))
    t0 = time.perf_counter()
    eng.load()
    frame = synth_frame()
    for _ in range(WARMUP):          # 首帧含 u8 校验 / TRT 引擎构建
        out = eng.process(frame)
    first_s = time.perf_counter() - t0

    times = []
    wall0 = time.perf_counter()
    for _ in range(FRAMES):
        ts = time.perf_counter()
        eng.process(frame)
        times.append(time.perf_counter() - ts)
    wall_s = time.perf_counter() - wall0

    row = {
        "model": model_id, "label": None, "scale": scale, "variant": variant,
        "weight": weight.name, "status": "ok", "tile": tile,
        "provider": eng.provider_used[0] if eng.provider_used else "?",
        "precision_used": "fp16" if weight.stem.endswith("_fp16") else "fp32",
        "u8_wrapped": bool(getattr(eng, "u8_wrapped", False)),
        "fixed_hw": getattr(eng, "fixed_hw", None),
        "hw_in": f"{W}x{H}", "hw_out": f"{out.shape[1]}x{out.shape[0]}",
        "first_call_s": round(first_s, 2),
        "mean_ms": round(wall_s / FRAMES * 1000, 1),
        "median_ms": round(float(np.median(times)) * 1000, 1),
        "fps_mean": round(FRAMES / wall_s, 1),
    }
    del eng
    gc.collect()
    return row


def bench_rife(device_chain_note: str) -> dict:
    from sv.engines.rife import Rife2x

    w = Path("models_store/rife-v4.26/rife_v4.26.onnx")
    if not w.exists():
        w = next(iter(Path(__file__).resolve().parents[2].glob(
            "models_store/rife-v4.26/*.onnx")), None)
        if w is None:
            return {"model": "rife-v4.26", "status": "skipped", "reason": "权重缺失"}
    r = Rife2x(w)
    r.load()
    a, b = synth_frame(), synth_frame()
    for _ in range(WARMUP):
        mid = r.interpolate(a, b)
    times = []
    wall0 = time.perf_counter()
    for _ in range(FRAMES):
        ts = time.perf_counter()
        r.interpolate(a, b)
        times.append(time.perf_counter() - ts)
    wall_s = time.perf_counter() - wall0
    return {
        "model": "rife-v4.26", "scale": 2, "status": "ok",
        "note": device_chain_note, "hw_in": f"{W}x{H}",
        "hw_out": f"{mid.shape[1]}x{mid.shape[0]}",
        "provider": r.provider_used[0] if r.provider_used else "?",
        "mean_ms": round(wall_s / FRAMES * 1000, 1),
        "median_ms": round(float(np.median(times)) * 1000, 1),
        "fps_mean": round(FRAMES / wall_s, 1),
    }


def bench_torch(specs) -> dict:
    from sv.engines.torch_engine import TorchSrEngine

    spec = specs["realesrgan-x4plus-torch"]
    weight = model_file(spec, 4)
    if not weight.exists():
        return {"model": "realesrgan-x4plus-torch", "status": "skipped",
                "reason": "pth 缺失"}
    eng = TorchSrEngine(weight, 4, io=spec.io, tile=256)
    t0 = time.perf_counter()
    eng.load()
    frame = synth_frame()
    for _ in range(WARMUP):
        out = eng.process(frame)
    first_s = time.perf_counter() - t0
    times = []
    wall0 = time.perf_counter()
    for _ in range(FRAMES):
        ts = time.perf_counter()
        eng.process(frame)
        times.append(time.perf_counter() - ts)
    wall_s = time.perf_counter() - wall0
    return {
        "model": "realesrgan-x4plus-torch", "scale": 4, "tile": 256,
        "status": "ok", "device": eng.device,
        "hw_in": f"{W}x{H}", "hw_out": f"{out.shape[1]}x{out.shape[0]}",
        "first_call_s": round(first_s, 2),
        "mean_ms": round(wall_s / FRAMES * 1000, 1),
        "median_ms": round(float(np.median(times)) * 1000, 1),
        "fps_mean": round(FRAMES / wall_s, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", choices=["auto", "trt"], default="auto")
    ap.add_argument("--precision", choices=["fp16", "fp32"], default="fp16")
    ap.add_argument("--json", default="")
    ap.add_argument("--only", default="",
                    help="只跑名称含此子串的条目（SR 条目或 torch/rife 关键字）")
    args = ap.parse_args()

    from sv.paths import MODELS_DIR  # noqa: F401  确认数据目录定位正常
    specs = load_registry()
    rows: list[dict] = []
    sink = Path(args.json).open("a", encoding="utf-8") if args.json else None
    print(f"# device={args.device} precision={args.precision} "
          f"input={W}x{H} warmup={WARMUP} frames={FRAMES}", flush=True)

    def emit(row: dict) -> None:
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if sink:
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            sink.flush()

    def run_one(model_id: str, scale: int, variant, label: str) -> None:
        try:
            row = bench_sr(specs, model_id, scale, variant, args.device, args.precision)
        except Exception as e:  # noqa: BLE001
            row = {"model": model_id, "scale": scale, "variant": variant,
                   "status": "error", "reason": str(e)[:300]}
        row["label"] = label
        emit(row)

    only = args.only.strip()

    def wanted(*keys: str) -> bool:
        return not only or any(only.lower() in k.lower() for k in keys if k)

    for model_id, scale, variant, label in SR_ENTRIES:
        if not wanted(model_id, label):
            continue
        run_one(model_id, scale, variant, label)

    # RIFE / torch 属于链路专属后端：哪个解释器有对应库就在哪趟顺带测出
    if wanted("rife"):
        try:
            emit(bench_rife("vs-mlrt 七通道版；provider 由引擎内置链自选"))
        except Exception as e:  # noqa: BLE001
            emit({"model": "rife-v4.26", "status": "error", "reason": str(e)[:300]})
    if wanted("torch"):
        try:
            from sv.engines.nvidia_dlls import register_nvidia_dlls

            register_nvidia_dlls()  # pip 分发的 cuda/cudnn DLL 目录（torch 导入依赖）
            import torch  # noqa: F401

            if torch.cuda.is_available():
                emit(bench_torch(specs))
        except Exception as e:  # noqa: BLE001
            emit({"model": "realesrgan-x4plus-torch", "status": "error",
                  "reason": f"{type(e).__name__}: {str(e)[:260]}"})

    if sink:
        sink.close()
        print(f"# 已写出 {args.json}", flush=True)


if __name__ == "__main__":
    main()

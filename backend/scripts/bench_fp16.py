"""fp32 vs fp16 基准 + 数值一致性（DML 实测）。

每个 (模型, 分辨率)：3 次预热 + 10 次计时取中位；PSNR 为两版输出全帧对比。
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

MODELS_DIR = Path(__file__).resolve().parents[1] / "sv" / "models" / "bundled"

CASES = [
    # (模型文件, [输入分辨率列表])
    ("RealESRGANv2-animevideo-xsx2.onnx", [(640, 360), (1280, 720), (1920, 1080)]),
    ("RealESR-AnimeVideo-v3_x4.onnx", [(480, 270), (640, 360), (960, 540)]),
]


def make_input(w: int, h: int) -> np.ndarray:
    """自然图像近似：平滑渐变 + 纹理噪声，覆盖数值动态范围。"""
    rng = np.random.default_rng(42)
    yy, xx = np.mgrid[0:h, 0:w] / max(w, h)
    img = np.stack([
        0.3 + 0.4 * xx + 0.1 * np.sin(yy * 20),
        0.5 + 0.2 * yy,
        0.2 + 0.3 * (xx * yy) + 0.1 * np.cos(xx * 15),
    ], axis=0).astype(np.float32)
    img += rng.normal(0, 0.02, img.shape).astype(np.float32)
    return np.clip(img, 0, 1)[None]  # 1CHW


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return float("inf") if mse == 0 else 10 * np.log10(1.0 / mse)


def bench_session(path: Path, x: np.ndarray, warmup: int = 3, iters: int = 10) -> tuple[float, np.ndarray]:
    so = ort.SessionOptions()
    sess = ort.InferenceSession(str(path), so, providers=["DmlExecutionProvider"])
    inp = sess.get_inputs()[0].name
    for _ in range(warmup):
        sess.run(None, {inp: x})
    times = []
    out = None
    for _ in range(iters):
        t0 = time.perf_counter()
        out = sess.run(None, {inp: x})[0]
        times.append(time.perf_counter() - t0)
    return statistics.median(times), out


def main() -> None:
    for name, sizes in CASES:
        fp32_path = MODELS_DIR / name
        fp16_path = MODELS_DIR / name.replace(".onnx", "_fp16.onnx")
        print(f"\n== {name} ==")
        print(f"{'输入':>12} {'fp32 ms':>9} {'fp16 ms':>9} {'加速':>7} {'PSNR':>8}")
        for w, h in sizes:
            x = make_input(w, h)
            t32, o32 = bench_session(fp32_path, x)
            t16, o16 = bench_session(fp16_path, x)
            p = psnr(o32, o16)
            ps = f"{p:.1f}" if p != float("inf") else "inf"
            print(f"{f'{w}x{h}':>12} {t32*1000:9.1f} {t16*1000:9.1f} {t32/t16:6.2f}x {ps:>8}")


if __name__ == "__main__":
    sys.exit(main())

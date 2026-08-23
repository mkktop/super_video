"""决定性 IO 校准：testsrc2 真彩帧 4x 缩小 → 各(通道序,数值范围)组合超分 → 与原图比 PSNR。

正确组合的 PSNR 显著更高。用法: python scripts/calibrate_color.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from sv.engines.onnx_engine import OnnxSrEngine
from sv.paths import MODELS_DIR, TEMP_DIR, ffmpeg_bin
from sv.utils.process import WINDOWS_CREATE_FLAGS

CASES = [
    ("animevideov3", MODELS_DIR / "realesr-animevideov3" / "RealESR-AnimeVideo-v3_x4.onnx", 4, 0),
    ("x4plus", MODELS_DIR / "realesrgan-x4plus" / "RealESRGAN_x4plus.onnx", 4, 64),
]


def ff(*args: str) -> None:
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


def load_png(path: Path) -> np.ndarray:
    from PIL import Image

    return np.asarray(Image.open(path).convert("RGB"))


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return 99.0 if mse == 0 else 10 * np.log10(255.0**2 / mse)


def main():
    TEMP_DIR.mkdir(exist_ok=True)
    ref = TEMP_DIR / "calib_ref.png"
    low = TEMP_DIR / "calib_low.png"
    # 真彩参考帧 + 4 倍缩小帧
    ff("-f", "lavfi", "-i", "testsrc2=size=512x288:rate=1", "-frames:v", "1", str(ref))
    ff("-i", str(ref), "-vf", "scale=128:72:flags=lanczos", str(low))

    ref_img = load_png(ref)
    low_img = load_png(low)

    for name, path, scale, tile in CASES:
        print(f"== {name} ==")
        results = []
        for color in ("rgb", "bgr"):
            for rng in ("0-255", "0-1"):
                eng = OnnxSrEngine(
                    path, scale,
                    io={"color": color, "range": rng, "pad": 4}, tile=tile,
                )
                eng.load()
                out = eng.process(low_img)
                score = psnr(ref_img, out)
                results.append((score, color, rng))
                print(f"  color={color:3s} range={rng:5s}  PSNR={score:6.2f} dB")
        best = max(results)
        print(f"  --> 最优: color={best[1]}, range={best[2]} (PSNR {best[0]:.2f} dB)\n")


if __name__ == "__main__":
    main()

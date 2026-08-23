"""M0 基准：模型速度（fps）+ 内存曲线平稳性。用法: python scripts/bench.py"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).parent.parent))

from sv.engines.onnx_engine import OnnxSrEngine
from sv.models.registry import get_model, local_files
from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.probe import probe, validate_m0
from sv.pipeline.stream import EncodeOpts, StreamPipeline
from sv.utils.process import WINDOWS_CREATE_FLAGS


def make_clip(path: Path, w: int, h: int, duration: float):
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", str(duration), "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


class MemorySampler:
    def __init__(self):
        self.proc = psutil.Process()
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            rss = self.proc.memory_info().rss
            for c in self.proc.children(recursive=True):
                try:
                    rss += c.memory_info().rss
                except psutil.Error:
                    pass
            self.samples.append(rss / 1e6)
            time.sleep(1.0)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=3)


def bench(model_id: str, clip: Path, label: str):
    spec = get_model(model_id)
    info = probe(clip)
    validate_m0(info)

    t0 = time.perf_counter()
    engine = OnnxSrEngine(local_files(spec)[0], 4, io=spec.io, tile=spec.tile_hint)
    engine.load()
    import numpy as np

    engine.process(np.zeros((64, 64, 3), dtype=np.uint8))  # 预热
    load_s = time.perf_counter() - t0

    out = TEMP_DIR / f"bench_{model_id}_{label}.mp4"
    with MemorySampler() as mem:
        stats = asyncio.run(StreamPipeline(info, out, engine, EncodeOpts()).run())

    drift = (mem.samples[-1] - mem.samples[min(5, len(mem.samples) - 1)]
             if len(mem.samples) > 6 else 0.0)
    return {
        "model": model_id, "src": label, "frames": stats.frames,
        "fps": stats.fps, "elapsed": stats.elapsed_s,
        "load_s": load_s, "ep": ",".join(engine.provider_used),
        "peak_mb": max(mem.samples), "drift_mb": drift,
    }


def main():
    TEMP_DIR.mkdir(exist_ok=True)
    jobs = []
    clip480 = TEMP_DIR / "bench_480p.mp4"
    clip720 = TEMP_DIR / "bench_720p.mp4"
    make_clip(clip480, 854, 480, 10)
    make_clip(clip720, 1280, 720, 10)

    jobs.append(("realesr-animevideov3", clip480, "854x480"))
    jobs.append(("realesr-animevideov3", clip720, "1280x720"))
    jobs.append(("realesrgan-x4plus", clip480, "854x480(3s)"))

    # x4plus 较慢，用 3 秒子集
    clip480_short = TEMP_DIR / "bench_480p_short.mp4"
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-ss", "0", "-i", str(clip480), "-t", "3", "-c", "copy", str(clip480_short)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )
    jobs[-1] = ("realesrgan-x4plus", clip480_short, "854x480-3s")

    rows = []
    for mid, clip, label in jobs:
        print(f">>> {mid} @ {label} ...", flush=True)
        rows.append(bench(mid, clip, label))

    print("\n| 模型 | 输入 | 帧数 | fps | 耗时s | 加载s | EP | 峰值内存MB | 内存漂移MB |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['model']} | {r['src']} | {r['frames']} | {r['fps']:.2f} | "
            f"{r['elapsed']:.1f} | {r['load_s']:.1f} | {r['ep']} | "
            f"{r['peak_mb']:.0f} | {r['drift_mb']:+.0f} |"
        )


if __name__ == "__main__":
    main()

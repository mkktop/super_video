"""双路并行推理实验：填满 GPU 空隙（BENCH.md 2026-08-25 后续）。

现状：u8 包装 + 单路重叠后 GPU 利用率 73%，仍有调度/IO 空隙。三个形态对比：
  ov1     单 session 读推写重叠（基线，~23fps）
  ov2p    同进程两个独立 session，各跑半段（各配独立解码/编码进程）
  ov2x    两个独立进程各跑半段（DML 设备完全隔离，最重形态）

用法:
  .venv/Scripts/python.exe scripts/bench_dual.py --src <video>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from sv.paths import TEMP_DIR

WINDOWS_CREATE_FLAGS = 0x08000000


class U8Engine:
    def __init__(self, sess, scale: int):
        self.sess = sess
        self.scale = scale
        self.batch = 1

    def process(self, frame: np.ndarray) -> np.ndarray:
        return self.sess.run(None, {self.sess.get_inputs()[0].name: frame[None]})[0][0]


def _setup(args):
    import onnxruntime as ort

    from sv.engines.u8_wrap import wrap_u8
    from sv.models.fp16 import ensure_fp16_file
    from sv.models.registry import get_model, local_files
    from sv.pipeline.probe import probe

    info = probe(Path(args.src))
    spec = get_model(args.model)
    src = local_files(spec)[0]
    if spec.fp16 and not src.stem.endswith("_fp16"):
        src = ensure_fp16_file(src)
    out_dir = TEMP_DIR / "bench_fs"
    out_dir.mkdir(parents=True, exist_ok=True)
    wrap = out_dir / "wrap_full.onnx"
    if not wrap.exists():
        wrap_u8(src, wrap, color=spec.io.get("color", "bgr"),
                range_01=spec.io.get("range", "0-1") == "0-1")

    def make_session():
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        return ort.InferenceSession(str(wrap), so, providers=["DmlExecutionProvider"])

    return info, spec, out_dir, make_session


async def main_async(args):
    from bench_fullspeed import Sampler, run_overlap
    from sv.pipeline.stream import EncodeOpts

    info, spec, out_dir, make_session = _setup(args)
    mid = info.total_frames // 2
    seek = mid / float(Fraction(info.fps_str))
    enc = EncodeOpts(codec=args.codec)
    scale = max(spec.scale)
    report: dict = {}
    out_dir.mkdir(parents=True, exist_ok=True)

    with Sampler() as sampler:
        # 基线：单 session 重叠跑全片
        if args.only in (None, "ov1"):
            e1 = U8Engine(make_session(), scale)
            with sampler.phase("ov1_单路重叠"):
                t0 = time.perf_counter()
                r = await run_overlap(info, e1, enc, out_dir / "dual_a.mp4")
                wall = time.perf_counter() - t0
            report["ov1"] = {"wall": round(wall, 2), "frames": r["frames"],
                             "fps": round(r["frames"] / wall, 1)}
            print("ov1", report["ov1"], flush=True)

        # 同进程双独立 session：已实测 DML 双 session 并发触发 0x887A0005
        # （GPU 设备移除，驱动级崩溃），默认不再跑
        if args.only == "ov2p":
            ea = U8Engine(make_session(), scale)
            eb = U8Engine(make_session(), scale)
            with sampler.phase("ov2p_同进程双session"):
                t0 = time.perf_counter()
                rs = await asyncio.gather(
                    run_overlap(info, ea, enc, out_dir / "dual_a.mp4", max_frames=mid),
                    run_overlap(info, eb, enc, out_dir / "dual_b.mp4", seek_s=seek),
                )
                wall = time.perf_counter() - t0
            n = sum(r["frames"] for r in rs)
            report["ov2p"] = {"wall": round(wall, 2), "frames": n, "fps": round(n / wall, 1)}
            print("ov2p", report["ov2p"], flush=True)

    # 跨进程双实例：各自独占 DML 设备/队列
    me = Path(__file__).resolve()
    with Sampler() as sampler:
        with sampler.phase("ov2x_跨进程双实例"):
            t0 = time.perf_counter()
            procs = [subprocess.Popen(
                [sys.executable, str(me), "--src", args.src, "--model", args.model,
                 "--codec", args.codec, "--child", str(i), str(mid), f"{seek:.6f}"],
                stdout=subprocess.PIPE, creationflags=WINDOWS_CREATE_FLAGS)
                for i in range(2)]
            outs = []
            for p in procs:
                o, _ = p.communicate()
                outs.append(o.decode("utf-8", "replace"))
            wall = time.perf_counter() - t0
    lasts = []
    for o in outs:
        lines = [json.loads(l) for l in o.splitlines()
                 if l.strip().startswith("{\"loop\"")]
        lasts.append(lines[-1])
    steady_fps = sum(x["frames"] for x in lasts) / max(x["wall"] for x in lasts)
    report["ov2x"] = {"wall_cold": round(wall, 2),
                      "loops": lasts,
                      "steady_fps": round(steady_fps, 1)}
    print("ov2x 稳态(末轮):", report["ov2x"], flush=True)

    print("\n== GPU 采样 ==", flush=True)
    for k in sorted(sampler.samples):
        s = sampler.summary(k)
        print(f"  {k:22s} gpu_avg={s['gpu_util_avg']}% max={s['gpu_util_max']}% "
              f"pwr={s['pwr_avg']}W wall={s['wall_s']}s", flush=True)
    (out_dir / "bench_dual.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))


def child(args):
    """子进程模式：idx=0 前半（限帧），idx=1 后半（seek）。

    连跑 3 遍输出每轮 wall/frames——首轮含 DML 着色器预热，稳态取末轮；
    父进程用两子进程末轮的 max(wall) 算并发吞吐（排除进程启动/加载污染）。
    """
    from bench_fullspeed import run_overlap
    from sv.pipeline.stream import EncodeOpts

    info, spec, out_dir, make_session = _setup(args)
    idx, mid, seek = int(args.child[0]), int(args.child[1]), float(args.child[2])
    eng = U8Engine(make_session(), max(spec.scale))

    async def _run():
        kw = {"max_frames": mid} if idx == 0 else {"seek_s": seek}
        for i in range(3):
            t0 = time.perf_counter()
            r = await run_overlap(info, eng, EncodeOpts(codec=args.codec),
                                  out_dir / f"dual_x{idx}.mp4", **kw)
            print(json.dumps({"loop": i, "wall": round(time.perf_counter() - t0, 2),
                              "frames": r["frames"]}), flush=True)

    asyncio.run(_run())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--model", default="animejanai-v3-hd-l2")
    ap.add_argument("--codec", default="h264_nvenc")
    ap.add_argument("--child", nargs=3, default=None, help="内部用: <idx> <mid> <seek>")
    ap.add_argument("--only", default=None, help="只跑指定相位: ov1|ov2p|ov2x")
    args = ap.parse_args()
    if args.child:
        child(args)
        return
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

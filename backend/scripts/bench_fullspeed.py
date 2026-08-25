"""全链路吞吐基准：定位超分瓶颈 + 优化变体对比（2026-08 吃满硬件研究）。

分五个相位，全部跑在真实片源上：
  decode  解码隔离：软解 vs cuvid 硬解，读满整段 rawvideo 的耗时/CPU
  infer   推理隔离：精度 × batch 组合的逐帧耗时；session.run 裸时间 vs process() 全程
          （差值 = CPU 侧 pad/transpose/类型转换开销）
  encode  编码隔离：4K 帧喂各编码器（libx264/libx265/nvenc 系）的吞吐/CPU
  full    整管线（复刻 StreamPipeline 逻辑 + 分段计时 read/infer/write）
  overlap 重叠实验：读-推-写三协程重叠（推理挪线程）vs 串行；双段并行 par2

哪个解释器跑就用哪个推理后端：.venv=DML / .venv-cuda=CUDA。
用法:
  .venv/Scripts/python.exe scripts/bench_fullspeed.py --src <video>
  .venv-cuda/Scripts/python.exe scripts/bench_fullspeed.py --src <video> --phases infer,full
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from sv.engines.onnx_engine import OnnxSrEngine
from sv.models.fp16 import ensure_fp16_file
from sv.models.registry import get_model, local_files
from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.probe import probe
from sv.pipeline.stream import EncodeOpts, decoder_cmd, encoder_cmd
from sv.utils.process import WINDOWS_CREATE_FLAGS

try:
    import psutil
except ImportError:
    psutil = None

BENCH_DIR = TEMP_DIR / "bench_fs"


# ---- 采样线程：GPU 利用率/功耗 + 系统 CPU，按相位归档 ----

class Sampler:
    def __init__(self):
        self.samples: dict[str, list[tuple]] = defaultdict(list)
        self._label = "idle"
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        if psutil:
            psutil.cpu_percent(interval=None)  # 打底
        while not self._stop.is_set():
            util = pwr = None
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,power.draw",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, timeout=3, creationflags=WINDOWS_CREATE_FLAGS,
                )
                if out.returncode == 0:
                    parts = out.stdout.decode().strip().split(",")
                    util, pwr = float(parts[0]), float(parts[1])
            except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
                pass
            cpu = psutil.cpu_percent(interval=None) if psutil else None
            self.samples[self._label].append((time.time(), util, pwr, cpu))
            self._stop.wait(0.5)

    @contextmanager
    def phase(self, label: str):
        old = self._label
        self._label = label
        t0 = time.perf_counter()
        try:
            yield
        finally:
            time.sleep(0.3)  # 相位边界处的样本别串台（仍记在本相位下）
            self._label = old
            self.samples[label].append(("wall", time.perf_counter() - t0, None, None))

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=3)

    def summary(self, label: str) -> dict:
        rows = [s for s in self.samples.get(label, []) if s[0] != "wall"]
        wall = next((s[1] for s in self.samples.get(label, []) if s[0] == "wall"), None)
        utils = [r[1] for r in rows if r[1] is not None]
        pwrs = [r[2] for r in rows if r[2] is not None]
        cpus = [r[3] for r in rows if r[3] is not None]
        return {
            "wall_s": round(wall, 2) if wall else None,
            "gpu_util_avg": round(sum(utils) / len(utils), 1) if utils else None,
            "gpu_util_max": round(max(utils), 1) if utils else None,
            "pwr_avg": round(sum(pwrs) / len(pwrs), 1) if pwrs else None,
            "cpu_avg": round(sum(cpus) / len(cpus), 1) if cpus else None,
            "n": len(rows),
        }


# ---- 通用 ----

async def drain_stderr(stream, sink: list[str]):
    while True:
        line = await stream.readline()
        if not line:
            return
        sink.append(line.decode("utf-8", "replace").rstrip())


async def reap(*procs):
    from sv.utils.process import kill_tree

    for p in procs:
        if p.returncode is None:
            kill_tree(p.pid)
            try:
                await asyncio.wait_for(p.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                pass


def spawn(cmd: list[str], *, stdin_pipe=False):
    return asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_pipe else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=WINDOWS_CREATE_FLAGS,
    )


def load_frames(info, n: int | None, *, cuvid=False, sampler=None) -> list[np.ndarray]:
    """同步解码前 n 帧（或全部）到内存。"""
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin"]
    if cuvid:
        cmd += ["-c:v", f"{info.video_codec}_cuvid"]
    cmd += ["-i", str(info.path), "-map", "0:v:0", "-an", "-sn", "-dn",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    if n is not None:
        cmd.insert(-1, "-frames:v"); cmd.insert(-1, str(n))
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
    buf = r.stdout
    fs = info.width * info.height * 3
    frames = [np.frombuffer(buf[i:i + fs], np.uint8).reshape(info.height, info.width, 3)
              for i in range(0, len(buf) - fs + 1, fs)]
    return frames, time.perf_counter() - t0


def build_engine(spec, scale, precision, batch, tile=None):
    weight = local_files(spec)[0]
    if precision == "fp16" and spec.fp16 and not weight.stem.endswith("_fp16"):
        weight = ensure_fp16_file(weight)
    eng = OnnxSrEngine(weight, scale, io=spec.io, tile=spec.tile_hint if tile is None else tile,
                       batch=batch)
    eng.load()
    eng.process(np.zeros((64, 64, 3), np.uint8))  # 预热
    return eng


# ---- 相位实现 ----

def phase_decode(info, sampler: Sampler) -> dict:
    out = {}
    for name, cuvid in (("sw", False), ("cuvid", True)):
        with sampler.phase(f"decode_{name}"):
            frames, t = load_frames(info, None, cuvid=cuvid)
        out[name] = {"frames": len(frames), "sec": round(t, 2),
                     "fps": round(len(frames) / t, 1)}
    return out


def phase_infer(info, spec, sampler, frames, precisions, batches) -> dict:
    scale = max(spec.scale)
    out = {}
    for prec in precisions:
        eng = build_engine(spec, scale, prec, 1)
        provider = eng.provider_used[0]
        # session.run 裸时间（输入已备好）→ 与 process() 差值即 CPU 前后处理开销
        h, w = info.height, info.width
        x = np.zeros((1, 3, h, w), np.float16 if eng._in_fp16 else np.float32)
        for _ in range(3):
            eng.session.run(eng._out_names, {eng._in_name: x})
        t0 = time.perf_counter()
        for _ in range(20):
            eng.session.run(eng._out_names, {eng._in_name: x})
        raw_ms = (time.perf_counter() - t0) / 20 * 1000

        for b in batches:
            label = f"infer_{prec}_b{b}"
            eb = eng if b == 1 else build_engine(spec, scale, prec, b)
            n = len(frames)
            with sampler.phase(label):
                t0 = time.perf_counter()
                if b == 1:
                    for f in frames:
                        eb.process(f)
                else:
                    for i in range(0, n - b + 1, b):
                        eb.process_batch(np.stack(frames[i:i + b]))
                sec = time.perf_counter() - t0
            out[label] = {
                "provider": (eb.provider_used[0] if eb.provider_used else "?"),
                "in_ms": round(sec / n * 1000, 1), "fps": round(n / sec, 1),
                "run_raw_ms": round(raw_ms, 1),
            }
    out["_provider"] = provider
    return out


def phase_encode(info, frames, sampler, codecs, n_frames=60) -> dict:
    scale = 2
    fw, fh = info.width * scale, info.height * scale
    big = [np.repeat(np.repeat(f, scale, axis=0), scale, axis=1) for f in frames[:n_frames]]
    out = {}
    for codec in codecs:
        label = f"encode_{codec}"
        with sampler.phase(label):
            t0 = time.perf_counter()

            async def _run():
                enc = EncodeOpts(codec=codec)
                p = await spawn(encoder_cmd(info.path, BENCH_DIR / f"enc_{codec}.mp4",
                                            fw, fh, fw, fh, info.fps_str, enc,
                                            False, None), stdin_pipe=True)
                for f in big:
                    p.stdin.write(f.tobytes())
                    await p.stdin.drain()
                p.stdin.close()
                return await p.wait()

            rc = asyncio.run(_run())
            sec = time.perf_counter() - t0
        out[codec] = {"rc": rc, "sec": round(sec, 2), "fps": round(len(big) / sec, 1)}
        (BENCH_DIR / f"enc_{codec}.mp4").unlink(missing_ok=True)
    return out


# ---- 复刻 StreamPipeline 的串行管线 + 分段计时 ----

async def run_seq(info, engine, enc: EncodeOpts, out: Path, *, seek_s=None,
                  max_frames=None, timers=None):
    scale = engine.scale
    fw, fh = info.width * scale, info.height * scale
    dec = await spawn(decoder_cmd(info.path, cfr_fps=info.fps_str if info.vfr else None,
                                   seek_s=seek_s, max_frames=max_frames))
    encp = await spawn(encoder_cmd(info.path, out, fw, fh, fw, fh, info.fps_str,
                                   enc, False, None), stdin_pipe=True)
    dec_err: list[str] = []
    enc_err: list[str] = []
    drains = [asyncio.create_task(drain_stderr(dec.stderr, dec_err)),
              asyncio.create_task(drain_stderr(encp.stderr, enc_err))]
    fs = info.width * info.height * 3
    t_read = t_infer = t_write = 0.0
    n = 0
    batch = max(1, int(getattr(engine, "batch", 1) or 1))
    t0 = time.perf_counter()
    try:
        eof = False
        while not eof:
            pend = []
            t = time.perf_counter()
            while len(pend) < batch:
                try:
                    buf = await dec.stdout.readexactly(fs)
                except asyncio.IncompleteReadError as e:
                    if len(e.partial) == 0:
                        eof = True
                        break
                    raise RuntimeError(f"解码截断: {' | '.join(dec_err[-3:])}") from e
                pend.append(np.frombuffer(buf, np.uint8).reshape(info.height, info.width, 3))
            t_read += time.perf_counter() - t
            if not pend:
                break
            t = time.perf_counter()
            outs = (engine.process(pend[0])[None] if batch == 1
                    else engine.process_batch(np.stack(pend)))
            t_infer += time.perf_counter() - t
            t = time.perf_counter()
            for o in outs:
                encp.stdin.write(o.tobytes())
                n += 1
            await encp.stdin.drain()
            t_write += time.perf_counter() - t
        encp.stdin.close()
        rc_enc = await encp.wait()
        await dec.wait()
    except Exception:
        await reap(dec, encp)
        raise
    finally:
        for d in drains:
            d.cancel()
        await asyncio.gather(*drains, return_exceptions=True)
    wall = time.perf_counter() - t0
    if timers is not None:
        timers.update(read=t_read, infer=t_infer, write=t_write, frames=n)
    assert rc_enc == 0, f"encoder rc={rc_enc}: {' | '.join(enc_err[-3:])}"
    return {"wall": wall, "frames": n, "fps": n / wall}


async def run_overlap(info, engine, enc: EncodeOpts, out: Path, *, qdepth=3):
    """读-推-写三协程重叠：推理挪到线程（session.run 释放 GIL），读下一帧/写上一帧并行。"""
    scale = engine.scale
    fw, fh = info.width * scale, info.height * scale
    dec = await spawn(decoder_cmd(info.path, cfr_fps=info.fps_str if info.vfr else None))
    encp = await spawn(encoder_cmd(info.path, out, fw, fh, fw, fh, info.fps_str,
                                   enc, False, None), stdin_pipe=True)
    dec_err: list[str] = []
    enc_err: list[str] = []
    drains = [asyncio.create_task(drain_stderr(dec.stderr, dec_err)),
              asyncio.create_task(drain_stderr(encp.stderr, enc_err))]
    fs = info.width * info.height * 3
    loop = asyncio.get_running_loop()
    q_in: asyncio.Queue = asyncio.Queue(maxsize=qdepth)
    q_out: asyncio.Queue = asyncio.Queue(maxsize=qdepth)
    t_infer = 0.0
    failed: BaseException | None = None
    batch = max(1, int(getattr(engine, "batch", 1) or 1))

    async def reader():
        nonlocal failed
        try:
            while True:
                try:
                    buf = await dec.stdout.readexactly(fs)
                except asyncio.IncompleteReadError as e:
                    if len(e.partial) == 0:
                        await q_in.put(None)
                        return
                    raise RuntimeError(f"解码截断: {' | '.join(dec_err[-3:])}") from e
                await q_in.put(np.frombuffer(buf, np.uint8).reshape(info.height, info.width, 3))
        except Exception as e:  # noqa: BLE001 — 异常也要放行哨兵，防 inferer 卡死
            failed = e
            await q_in.put(None)

    async def inferer():
        while True:
            items = [await q_in.get()]
            if items[0] is None:
                await q_out.put(None)
                return
            while len(items) < batch:
                nxt = await q_in.get()
                if nxt is None:
                    eof_seen = True  # noqa: F841
                    break
                items.append(nxt)
            stack = items[0] if batch == 1 else np.stack(items)
            t = time.perf_counter()
            outs = (await loop.run_in_executor(None, lambda: engine.process(stack))
                    if batch == 1 else
                    await loop.run_in_executor(None, lambda: engine.process_batch(stack)))
            nonlocal t_infer  # noqa: PLW0150
            t_infer += time.perf_counter() - t
            for o in (outs if batch > 1 else [outs]):
                await q_out.put(o)

    async def writer():
        n = 0
        while True:
            o = await q_out.get()
            if o is None:
                break
            encp.stdin.write(o.tobytes())
            n += 1
            await encp.stdin.drain()
        encp.stdin.close()
        return n

    t0 = time.perf_counter()
    try:
        results = await asyncio.gather(reader(), inferer(), writer())
        n = results[2]
        rc = await encp.wait()
        await dec.wait()
        if failed is not None:
            raise failed
        assert rc == 0, f"encoder rc={rc}: {' | '.join(enc_err[-3:])}"
    except Exception:
        await reap(dec, encp)
        raise
    finally:
        for d in drains:
            d.cancel()
        await asyncio.gather(*drains, return_exceptions=True)
    wall = time.perf_counter() - t0
    return {"wall": wall, "frames": n, "fps": n / wall, "infer_busy_s": t_infer}


def log(msg: str):
    print(msg, flush=True)


def phase_full(info, spec, sampler, combos, out_prefix="full") -> dict:
    scale = max(spec.scale)
    results = {}
    for prec, codec, b, mode in combos:
        label = f"{out_prefix}_{prec}_{codec}_b{b}" + (f"_{mode}" if mode else "")
        log(f"  ... {label}")
        engine = build_engine(spec, scale, prec, b)
        enc = EncodeOpts(codec=codec)
        out = BENCH_DIR / f"{label}.mp4"
        timers = {}
        with sampler.phase(label):
            try:
                if mode == "ov":  # 重叠
                    r = asyncio.run(asyncio.wait_for(
                        run_overlap(info, engine, enc, out), timeout=300))
                elif mode == "par2":  # 双段并行（各半视频，共享 session）
                    from fractions import Fraction

                    mid = info.total_frames // 2
                    seek = mid / float(Fraction(info.fps_str))

                    async def two():
                        parts = [
                            run_seq(info, engine, enc, BENCH_DIR / f"{label}_a.mp4",
                                    seek_s=None, max_frames=mid, timers={}),
                            run_seq(info, engine, enc, BENCH_DIR / f"{label}_b.mp4",
                                    seek_s=seek, max_frames=None, timers={}),
                        ]
                        return await asyncio.gather(*parts)

                    r = asyncio.run(asyncio.wait_for(two(), timeout=300))
                    r = {"wall": max(x["wall"] for x in r),
                         "frames": sum(x["frames"] for x in r),
                         "fps": sum(x["frames"] for x in r) / max(x["wall"] for x in r)}
                else:
                    r = asyncio.run(asyncio.wait_for(
                        run_seq(info, engine, enc, out, timers=timers), timeout=300))
            except TimeoutError:
                results[label] = {"error": "TIMEOUT>300s（疑似死锁，跳过）"}
                log(f"  !!! {label} 超时跳过")
                continue
        results[label] = {
            "fps": round(r["fps"], 1), "frames": r["frames"],
            "provider": engine.provider_used[0] if engine.provider_used else "?",
            **{f"t_{k}": round(v, 2) for k, v in timers.items()},
            **({"infer_busy_s": round(r["infer_busy_s"], 2)} if "infer_busy_s" in r else {}),
        }
        for f in BENCH_DIR.glob(f"{label}*.mp4"):
            f.unlink(missing_ok=True)
    return results


# ---- main ----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--model", default="animejanai-v3-hd-l2")
    ap.add_argument("--phases", default="decode,infer,encode,full,overlap")
    ap.add_argument("--precisions", default="fp32,fp16")
    ap.add_argument("--batches", default="1,4")
    ap.add_argument("--codecs", default="h264,h265,h264_nvenc,hevc_nvenc,av1_nvenc")
    args = ap.parse_args()

    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    info = probe(Path(args.src))
    spec = get_model(args.model)
    print(f"== {info.width}x{info.height}@{info.fps_str} {info.total_frames}f | "
          f"model={args.model} scale={max(spec.scale)} → "
          f"{info.width * max(spec.scale)}x{info.height * max(spec.scale)} ==")

    report: dict = {"src": str(args.src), "model": args.model,
                    "hw": {"w": info.width, "h": info.height,
                           "frames": info.total_frames}}
    phases = set(args.phases.split(","))
    with Sampler() as sampler:
        if "decode" in phases:
            report["decode"] = phase_decode(info, sampler)
            print("decode:", json.dumps(report["decode"]))
            frames = None
        if "infer" in phases or "encode" in phases:
            frames, _ = load_frames(info, 60)
        if "infer" in phases:
            report["infer"] = phase_infer(info, spec, sampler, frames,
                                          args.precisions.split(","),
                                          [int(b) for b in args.batches.split(",")])
            print("infer:", json.dumps(report["infer"], ensure_ascii=False))
        if "encode" in phases:
            report["encode"] = phase_encode(info, frames, sampler, args.codecs.split(","))
            print("encode:", json.dumps(report["encode"]))
        if "full" in phases:
            combos = [
                ("fp16", "h264", 1, None),        # 产品基线（软编）
                ("fp16", "h264_nvenc", 1, None),
                ("fp16", "h264_nvenc", 4, None),
                ("fp32", "h264_nvenc", 1, None),  # fp16 管线内收益
                ("fp16", "hevc_nvenc", 1, None),
            ]
            report["full"] = phase_full(info, spec, sampler, combos)
            print("full:", json.dumps(report["full"], ensure_ascii=False))
        if "overlap" in phases:
            combos = [
                ("fp16", "h264_nvenc", 1, None),   # 串行对照
                ("fp16", "h264_nvenc", 1, "ov"),   # 读推写重叠
                ("fp16", "h264_nvenc", 1, "par2"),  # 双段并行
            ]
            report["overlap"] = phase_full(info, spec, sampler, combos, out_prefix="ov")
            print("overlap:", json.dumps(report["overlap"], ensure_ascii=False))

    report["sampler"] = {k: sampler.summary(k) for k in sampler.samples}
    out_json = BENCH_DIR / f"bench_{report.get('infer', {}).get('_provider', 'x')}.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\n== JSON → {out_json} ==")
    for k in sorted(sampler.samples):
        s = sampler.summary(k)
        print(f"  {k:32s} gpu_avg={s['gpu_util_avg']}% max={s['gpu_util_max']}% "
              f"pwr={s['pwr_avg']}W cpu={s['cpu_avg']}% wall={s['wall_s']}s")


if __name__ == "__main__":
    main()

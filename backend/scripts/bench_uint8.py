"""uint8 直进直出 ONNX 图实验：把前后处理(Cast/Transpose/Div/Mul/Clip)塞进模型图。

现状：engine.process() = GPU 推理 45-84ms + CPU 前后处理 ~75-100ms（4K 输出
95MB fp32 数组的 transpose/mul/clip/astype 全在 CPU）。GPU 只吃到 ~30%。

本实验不改产品代码：onnx 图手术包装模型——
  输入 uint8[N,H,W,3](ffmpeg rgb24 帧零拷贝) → Cast f32 → Div255 → Transpose NCHW
  → [原模型] → (fp16 则 Cast f32) → Mul255 → Clip → Transpose NHWC → Cast uint8 输出
后处理全在 GPU，D2H 只传 24.9MB uint8(而非 95MB fp32)。

对照三档：plain(现产品路径) / post-only(只包装输出侧) / full(输入输出全包装)。
用法:
  .venv/Scripts/python.exe scripts/bench_uint8.py --model animejanai-v3-hd-l2 --src <video>
  .venv-cuda/Scripts/python.exe scripts/bench_uint8.py ...   (CUDA EP 对照)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper

sys.path.insert(0, str(Path(__file__).parent.parent))

from sv.models.registry import get_model, local_files
from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.utils.process import WINDOWS_CREATE_FLAGS


def wrap_model(src: Path, dst: Path, *, wrap_input: bool) -> Path:
    """图手术：输入 uint8 NHWC（可选）+ 输出 uint8 NHWC（必做）。"""
    m = onnx.load(str(src))
    g = m.graph
    opset = next((o.version for o in m.opset_import if o.domain in ("", "ai.onnx")), 0)
    assert opset >= 11, f"opset {opset} < 11, Clip 输入式不可用"

    old_in = g.input[0]
    inner_dtype = old_in.type.tensor_type.elem_type  # 1=fp32 10=fp16
    out_dtype = g.output[0].type.tensor_type.elem_type
    f32 = TensorProto.FLOAT
    sfx = "full" if wrap_input else "post"

    # ---- 输出侧：原输出改名 -> 内部，追加 Mul/Clip/Transpose/Cast ----
    old_out_name = g.output[0].name
    # fp16 转换模型的末端 fp32→fp16 Cast 在 DML 上是真崩溃核（作为输出节点时走
    # 拷贝路径不执行，被追加节点消费就会执行）——绕过它，直接取其 fp32 输入
    tail_node = next((n for n in g.node if old_out_name in n.output), None)
    if (tail_node is not None and tail_node.op_type == "Cast"
            and out_dtype == TensorProto.FLOAT16):
        raw_out = tail_node.input[0]
        g.node.remove(tail_node)
    else:
        raw_out = f"{old_out_name}_raw"
        for n in g.node:
            n.output[:] = [raw_out if o == old_out_name else o for o in n.output]
    g.output.remove(g.output[0])

    nodes = []
    post_src = raw_out  # 绕过末端 Cast 后天然 fp32；否则可能仍是 fp16 需补 Cast
    if out_dtype != f32 and not (tail_node is not None and tail_node.op_type == "Cast"):
        nodes.append(helper.make_node("Cast", [raw_out], [f"pf32_{sfx}"], to=f32))
        post_src = f"pf32_{sfx}"
    nodes += [
        helper.make_node("Mul", [post_src, "c255"], [f"scaled_{sfx}"]),
        helper.make_node("Clip", [f"scaled_{sfx}", "c0", "c255"], [f"clipped_{sfx}"]),
        helper.make_node("Transpose", [f"clipped_{sfx}"], [f"hwc_{sfx}"], perm=[0, 2, 3, 1]),
        helper.make_node("Cast", [f"hwc_{sfx}"], ["out_u8"], to=TensorProto.UINT8),
    ]

    # ---- 输入侧（可选）：新增 uint8 NHWC 输入，原输入改名 -> 内部 ----
    pre_nodes: list = []
    if wrap_input:
        old_in_name = old_in.name
        old_in.name = "inner_in"
        for n in g.node:
            n.input[:] = ["inner_in" if i == old_in_name else i for i in n.input]
        # 原输入改为由前置链生产：从图输入删除，避免 SSA 冲突
        g.input.remove(old_in)
        g.input.insert(0, helper.make_tensor_value_info("frame_u8", TensorProto.UINT8,
                                                        ["n", "h", "w", 3]))
        pre_nodes = [
            helper.make_node("Cast", ["frame_u8"], [f"u8f32_{sfx}"], to=f32),
            helper.make_node("Div", [f"u8f32_{sfx}", "c255"], [f"normed_{sfx}"]),
            helper.make_node("Transpose", [f"normed_{sfx}"], [f"nchw_{sfx}"], perm=[0, 3, 1, 2]),
        ]
        if inner_dtype != f32:
            pre_nodes.append(helper.make_node("Cast", [f"nchw_{sfx}"], ["inner_in"],
                                              to=inner_dtype))
        else:
            pre_nodes[-1].output[0] = "inner_in"
        for i, n in enumerate(pre_nodes):
            g.node.insert(i, n)
    g.node.extend(nodes)
    g.output.append(helper.make_tensor_value_info("out_u8", TensorProto.UINT8,
                                                  ["n", "h", "w", 3]))
    for name, val in (("c255", 255.0), ("c0", 0.0)):
        g.initializer.append(helper.make_tensor(name, f32, [], [val]))
    onnx.checker.check_model(m)
    onnx.save(m, str(dst))
    return dst


def decode_frames(path: Path, n: int):
    from sv.pipeline.probe import probe

    info = probe(path)
    r = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-map", "0:v:0", "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, creationflags=WINDOWS_CREATE_FLAGS,
    )
    fs = info.width * info.height * 3
    frames = [np.frombuffer(r.stdout[i:i + fs], np.uint8).reshape(info.height, info.width, 3)
              for i in range(0, len(r.stdout) - fs + 1, fs)]
    return info, frames


class U8Engine:
    """包装模型的引擎适配器：uint8 HWC 直进直出，兼容 run_seq 的 transformer 协议。"""

    def __init__(self, sess, scale: int):
        self.sess = sess
        self.scale = scale
        self.batch = 1
        self.provider_used = sess.get_providers()

    def process(self, frame: np.ndarray) -> np.ndarray:
        return self.sess.run(None, {self.sess.get_inputs()[0].name: frame[None]})[0][0]


def e2e_run(info, engine, codec: str, sampler, label: str, out_dir: Path,
            mode: str = "seq") -> dict:
    import asyncio
    from fractions import Fraction

    from bench_fullspeed import run_overlap, run_seq
    from sv.pipeline.stream import EncodeOpts

    enc = EncodeOpts(codec=codec)
    out = out_dir / f"e2e_{label}.mp4"
    with sampler.phase(label):
        if mode == "ov":
            r = asyncio.run(asyncio.wait_for(run_overlap(info, engine, enc, out), timeout=300))
            timers = {"infer_busy": r.get("infer_busy_s")}
        elif mode == "par2":
            mid = info.total_frames // 2
            seek = mid / float(Fraction(info.fps_str))

            async def two():
                return await asyncio.gather(
                    run_seq(info, engine, enc, out_dir / f"{label}_a.mp4",
                            seek_s=None, max_frames=mid, timers={}),
                    run_seq(info, engine, enc, out_dir / f"{label}_b.mp4",
                            seek_s=seek, max_frames=None, timers={}),
                )

            parts = asyncio.run(asyncio.wait_for(two(), timeout=300))
            r = {"fps": sum(p["frames"] for p in parts) / max(p["wall"] for p in parts)}
            timers = {}
        else:
            timers = {}
            r = asyncio.run(asyncio.wait_for(
                run_seq(info, engine, enc, out, timers=timers), timeout=300))
    for f in out_dir.glob(f"e2e_{label}*.mp4"):
        f.unlink(missing_ok=True)
    return {"fps": round(r["fps"], 1), "mode": mode,
            **{f"t_{k}": round(v, 2) for k, v in timers.items()}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="animejanai-v3-hd-l2")
    ap.add_argument("--src", required=True)
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--precision", choices=("fp16", "fp32"), default="fp16")
    ap.add_argument("--device", choices=("auto", "trt", "cuda", "cpu"), default="auto")
    ap.add_argument("--e2e", action="store_true", help="端到端管线 A/B（plain vs full 包装）")
    args = ap.parse_args()

    import onnxruntime as ort

    from sv.engines.onnx_engine import OnnxSrEngine

    spec = get_model(args.model)
    src = local_files(spec)[0]
    if args.precision == "fp16" and spec.fp16 and not src.stem.endswith("_fp16"):
        from sv.models.fp16 import ensure_fp16_file

        src = ensure_fp16_file(src)
    scale = max(spec.scale)
    out_dir = TEMP_DIR / "bench_fs"
    out_dir.mkdir(parents=True, exist_ok=True)
    info, frames = decode_frames(Path(args.src), args.frames)
    print(f"frames={len(frames)} {info.width}x{info.height} src={src.name}", flush=True)

    eng = OnnxSrEngine(src, scale, io=spec.io, tile=0, batch=1, device=args.device)
    eng.load()
    eng.process(frames[0])
    t0 = time.perf_counter()
    ref = [eng.process(f) for f in frames]
    plain_ms = (time.perf_counter() - t0) / len(frames) * 1000
    print(f"plain     {plain_ms:7.1f} ms/frame  provider={eng.provider_used}", flush=True)

    def wrap_providers() -> list:
        """TRT 时带引擎缓存选项，其余后端原样。"""
        ps: list = list(eng.provider_used)
        if "TensorrtExecutionProvider" in ps:
            from sv.engines.onnx_engine import _trt_provider_options

            ps = _trt_provider_options() + [
                p for p in ps if p != "TensorrtExecutionProvider"]
        return ps

    for mode, wrap_in in (("post", False), ("full", True)):
        dst = wrap_model(src, out_dir / f"wrap_{mode}.onnx", wrap_input=wrap_in)
        so = ort.SessionOptions()
        # 追加尾节点后，全量图优化会重排 fp16 转换模型内部被输出边界保护的 Cast/Clip
        # （DML 上 0x8007023E 崩）；降到 BASIC 挡住该融合
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        sess = ort.InferenceSession(str(dst), so, providers=wrap_providers())
        iname = sess.get_inputs()[0].name
        want_fp16 = sess.get_inputs()[0].type == "tensor(float16)"

        if wrap_in:
            def run(f):
                return sess.run(None, {iname: f[None]})[0]
        else:
            def run(f):
                x = np.ascontiguousarray(f.transpose(2, 0, 1)[None].astype(np.float32) / 255.0)
                if want_fp16:
                    x = x.astype(np.float16)
                return sess.run(None, {iname: x})[0]

        run(frames[0])
        t0 = time.perf_counter()
        outs = [run(f) for f in frames]
        ms = (time.perf_counter() - t0) / len(frames) * 1000
        diffs = [int(np.abs(a.astype(np.int16) - b.astype(np.int16)).max())
                 for a, b in zip(outs[:5], ref[:5])]
        print(f"{mode:9s} {ms:7.1f} ms/frame  shape={outs[0].shape} "
              f"dtype={outs[0].dtype} maxdiff[:5]={diffs}", flush=True)

    if args.e2e:
        from bench_fullspeed import Sampler

        dst = out_dir / "wrap_full.onnx"
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        sess = ort.InferenceSession(str(dst), so, providers=wrap_providers())
        u8 = U8Engine(sess, scale)
        with Sampler() as sampler:
            r_plain = e2e_run(info, eng, "h264_nvenc", sampler, "e2e_plain", out_dir)
            r_wrap = e2e_run(info, u8, "h264_nvenc", sampler, "e2e_u8full", out_dir)
            r_ov = e2e_run(info, u8, "h264_nvenc", sampler, "e2e_u8_ov", out_dir,
                           mode="ov")
            r_par = e2e_run(info, u8, "h264_nvenc", sampler, "e2e_u8_par2", out_dir,
                            mode="par2")
        print(f"e2e plain  {r_plain}", flush=True)
        print(f"e2e u8full {r_wrap}", flush=True)
        print(f"e2e u8-ov  {r_ov}", flush=True)
        print(f"e2e u8-par2 {r_par}", flush=True)
        for k in sorted(sampler.samples):
            s = sampler.summary(k)
            print(f"  {k:16s} gpu_avg={s['gpu_util_avg']}% max={s['gpu_util_max']}% "
                  f"pwr={s['pwr_avg']}W cpu={s['cpu_avg']}% wall={s['wall_s']}s", flush=True)


if __name__ == "__main__":
    main()

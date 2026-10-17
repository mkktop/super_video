"""全 GPU 零拷贝链 API 尖刺：NVDEC → 图内 YUV→RGB→SR→NV12 → NVENC。

验证三件事（Phase 0 闸门）：
1. PyNvVideoCodec 2.x 在本机（RTX 5070 Ti / cp314 / win）的解码/编码/交接 API 可用性；
2. YUV 直进直出的 ONNX 图手术可被 TRT EP 导入，且各级毫秒在预算内；
3. e2e 单路 ≥55fps、转换层对 ffmpeg rgb24 PSNR ≥45dB。

用法（必须用 .venv-cuda 解释器）：
  ../.venv-cuda/Scripts/python.exe scripts/bench_gpu_chain.py <src> [--frames N]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sv.paths import TEMP_DIR, ffmpeg_bin  # noqa: E402

MODEL = (Path(r"D:/super_video/models_store/animejanai-v3-hd-l2")
         / "2x_AnimeJaNai_HD_V3_UltraCompact_fp16.onnx")

# BT.709 / BT.601 limited-range：RGB ← Y,U,V（系数作用于 [0,1] 域）
MATRICES = {
    "709": {
        "lin": (  # (yk, uk, vk) per R/G/B
            (1.164383, 0.0, 1.792741),
            (1.164383, -0.212639, -0.532909),
            (1.164383, 2.112402, 0.0),
        ),
        "ey": (0.2126, 0.7152, 0.0722),
    },
    "601": {
        "lin": (
            (1.164383, 0.0, 1.596027),
            (1.164383, -0.391762, -0.812968),
            (1.164383, 2.017232, 0.0),
        ),
        "ey": (0.299, 0.587, 0.114),
    },
}


class WrapBuilder:
    """y_in/uv_in(P016 10bit) → BT.709 RGB f16 → 原模型 → NV12 packed u8 包装图。

    节点全部按 opset 17 规范写（Slice/Clip/Unsqueeze/Resize 用输入传参）。
    子采样在浮点域完成后再 Cast u8（ReduceMean 不吃 uint8）。
    """

    def __init__(self, model_path: Path, h: int, w: int, matrix: dict, scale: int = 2):
        m = onnx.load(str(model_path))
        self.g = m.graph
        self.in_name = self.g.input[0].name
        self.out_name = self.g.output[0].name
        self.h, self.w, self.m, self.scale = h, w, matrix, scale
        self.pre_nodes: list[onnx.Node] = []   # 前链：插表头（产生模型输入）
        self.post_nodes: list[onnx.Node] = []  # 后链：插表尾（消费模型输出）
        self._target = self.pre_nodes
        self.inits: dict[str, onnx.TensorProto] = {}

    def _k(self, name: str, arr) -> str:
        if name not in self.inits:
            self.inits[name] = numpy_helper.from_array(np.asarray(arr), name)
        return name

    def _f(self, name: str, v: float) -> str:
        return self._k(name, np.array([v], np.float32))

    def _i(self, name: str, arr) -> str:
        return self._k(name, np.asarray(arr, np.int64))

    def N(self, op: str, ins: list, out: str, **kw) -> str:
        name = f"wrap_{len(self.pre_nodes) + len(self.post_nodes)}_{op.lower()}"
        self._target.append(helper.make_node(op, ins, [out], name=name, **kw))
        return out

    def build(self) -> onnx.ModelProto:
        h, w, m, S = self.h, self.w, self.m, self.scale
        H2, W2, h2, w2 = h * S, w * S, h * S // 2, w * S // 2
        N = self.N

        def lin3(y, u, v, ks, tag):
            prods = [N("Mul", [y, self._f(f"yk_{tag}{i}", ks[i][0])], f"{tag}_a{i}")
                     for i in range(3)]
            prods += [N("Mul", [u, self._f(f"uk_{tag}{i}", ks[i][1])], f"{tag}_b{i}")
                      for i in range(3)]
            prods += [N("Mul", [v, self._f(f"vk_{tag}{i}", ks[i][2])], f"{tag}_c{i}")
                      for i in range(3)]
            s = N("Add", [prods[0], prods[1]], f"{tag}_01")
            s = N("Add", [s, prods[2]], f"{tag}_12")
            s = N("Add", [s, prods[3]], f"{tag}_3")
            s = N("Add", [s, prods[4]], f"{tag}_4")
            return N("Add", [s, prods[5]], f"{tag}_rgb")

        # ---- 输入侧：P016 → [0,1] full ----
        cmin, cmax = self._f("cmin", 0.0), self._f("cmax", 1.0)
        inv64 = self._f("inv64", 1.0 / 64.0)
        lo = self._f("lim_lo", 64.0)

        def to01(t, tag, rng_c):
            t = N("Cast", [t], f"{tag}_f", to=TensorProto.FLOAT)
            t = N("Mul", [t, inv64], f"{tag}_10")
            t = N("Sub", [t, lo], f"{tag}_s")
            t = N("Div", [t, self._f(f"rng_{tag}", rng_c)], f"{tag}_01")
            return N("Clip", [t, cmin, cmax], f"{tag}_c")

        # 量化范围：Y=219 系（10bit 876）、色度=224 系（10bit 896）——swscale 口径
        y = to01("y_in", "y", 876.0)
        uv = to01("uv_in", "uv", 896.0)
        u = N("Slice", [uv, self._i("su0", [0, 0, 0, 0]), self._i("su1", [1, h // 2, w // 2, 1]),
                        self._i("su2", [0, 1, 2, 3])], "u_c")
        v = N("Slice", [uv, self._i("sv0", [0, 0, 0, 1]), self._i("sv1", [1, h // 2, w // 2, 2]),
                        self._i("sv2", [0, 1, 2, 3])], "v_c")
        u = N("Squeeze", [u, self._i("sq_uv", [3])], "u_s")
        v = N("Squeeze", [v, self._i("sq_uv2", [3])], "v_s")
        roi = self._k("roi_e", np.zeros((0,), np.float32))
        scales = self._k("scales_uv", np.array([1, 2, 2], np.float32))
        for src, tag in ((u, "u"), (v, "v")):
            src = N("Resize", [src, roi, scales], f"{tag}_up", mode="linear",
                    coordinate_transformation_mode="half_pixel")
            globals()[tag] = src
        u05 = N("Sub", [globals()["u"], self._f("half", 0.5)], "u_cen")
        v05 = N("Sub", [globals()["v"], self._f("half2", 0.5)], "v_cen")
        ks = m["lin"]
        chans = []
        for tag in ("r", "g", "b"):
            c = lin3(y, u05, v05, ks, tag)
            chans.append(N("Unsqueeze", [c, self._i(f"{tag}_ax1", [1])], f"{tag}_4d"))
        rgb = N("Concat", chans, "rgb_chw", axis=1)
        rgb = N("Clip", [rgb, cmin, cmax], "rgb_clip")
        # 模型 IO 为 fp32（内部自 Cast f16）——与实测权重一致
        N("Cast", [rgb], self.in_name, to=TensorProto.FLOAT)

        # ---- 输出侧：模型 f16 → NV12 packed u8（后链，追加表尾）----
        self._target = self.post_nodes
        o = N("Cast", [self.out_name], "o_f", to=TensorProto.FLOAT)
        ch = []
        for i, tag in enumerate("rgb"):
            ch.append(N("Slice", [o, self._i(f"o{i}0", [0, i]), self._i(f"o{i}1", [1, i + 1]),
                                  self._i(f"o{i}2", [0, 1])], f"o_{tag}"))
        r, g, b = ch
        ey = m["ey"]
        prods = [N("Mul", [c, self._f(f"ey{i}", 219 * ey[i])], f"ey_x{i}") for i, c in enumerate(ch)]
        s = N("Add", [prods[0], prods[1]], "ey_01")
        s = N("Add", [s, prods[2]], "ey_012")
        cy = N("Add", [s, self._f("yoff", 16.0)], "Y_255")
        ey_f = N("Div", [N("Sub", [cy, self._f("m16", 16.0)], "ym16"),
                         self._f("d219", 219.0)], "EY_full")
        chroma = []
        for src, kdiv, tag in ((b, 224 / 1.8556, "cb"), (r, 224 / 1.5748, "cr")):
            d = N("Sub", [src, ey_f], f"{tag}_d")
            mm = N("Mul", [d, self._f(f"kdiv_{tag}", kdiv)], f"{tag}_m")
            oo = N("Add", [mm, self._f(f"{tag}off", 128.0)], f"{tag}_255")
            chroma.append(N("Clip", [oo, self._f("c255a", 0.0), self._f("c255b", 255.0)],
                            f"{tag}_c"))
        # Y 平面
        y_out = N("Clip", [cy, self._f("c255c", 0.0), self._f("c255d", 255.0)], "Y_c")

        # 2x2 均值下采样（浮点域）→ 交错 → UV 平面 [1,h2,w2,2]
        def sub4(t, tag):
            r5 = N("Reshape", [t, self._i(f"{tag}_d5", [1, 1, h2, 2, w2, 2])], f"{tag}_r5")
            mm = N("ReduceMean", [r5], f"{tag}_mean", axes=[3, 5], keepdims=0)
            return N("Reshape", [mm, self._i(f"{tag}_d2", [1, h2, w2, 1])], f"{tag}_4d")

        uvp = N("Concat", [sub4(chroma[0], "cbs"), sub4(chroma[1], "crs")],
                "uv_pl", axis=3)

        # TRT 不吃 uint8 中间张量：浮点域算完各自平面，边界直接 Cast 产出。
        # （打包成单块 NV12 由管线侧两笔 D2D 完成，避免 Concat(u8) 导入失败）
        y_u8 = N("Cast", [y_out], "out_y", to=TensorProto.UINT8)
        uv_u8 = N("Cast", [uvp], "out_uv", to=TensorProto.UINT8)

        # ---- 组装：前链插表头、后链接表尾；输入换人；输出补 out_nv12 ----
        for node in reversed(self.pre_nodes):
            self.g.node.insert(0, node)
        self.g.node.extend(self.post_nodes)
        del self.g.input[:]
        self.g.input.extend([
            helper.make_tensor_value_info("y_in", TensorProto.UINT16, [1, h, w]),
            helper.make_tensor_value_info("uv_in", TensorProto.UINT16, [1, h // 2, w // 2, 2]),
        ])
        # 原输出 "output" 保持导出（内部张量，不绑定即不产生宿主拷贝）
        self.g.output.append(helper.make_tensor_value_info(
            "out_y", TensorProto.UINT8, [1, 1, H2, W2]))
        self.g.output.append(helper.make_tensor_value_info(
            "out_uv", TensorProto.UINT8, [1, h2, w2, 2]))
        self.g.initializer.extend(self.inits.values())
        model = helper.make_model(self.g, opset_imports=[helper.make_opsetid("", 17)])
        model.ir_version = 9
        return model


def np_yuv_to_rgb(y: np.ndarray, uv: np.ndarray, lin, nearest: bool = True) -> np.ndarray:
    """numpy 参考：与图同数学（nearest=True 时用最近邻替代 half_pixel bilinear）。"""
    yf = np.clip((y.astype(np.float32) / 64.0 - 64.0) / 876.0, 0, 1)
    u = (uv[..., 0].astype(np.float32) / 64.0 - 64.0) / 896.0 - 0.5
    v = (uv[..., 1].astype(np.float32) / 64.0 - 64.0) / 896.0 - 0.5
    if nearest:
        u = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1)
        v = np.repeat(np.repeat(v, 2, axis=0), 2, axis=1)
    else:  # half_pixel bilinear 参考（与 Resize(linear, half_pixel) 对齐）
        def up(x):
            h, w = x.shape
            # 行方向 ×2
            r = np.empty((h * 2, w), np.float32)
            r[0::2] = 0.75 * x + 0.25 * np.vstack([x[:1], x[:-1]])
            r[1::2] = 0.25 * x + 0.75 * np.vstack([x[1:], x[-1:]])
            # 列方向 ×2
            out = np.empty((h * 2, w * 2), np.float32)
            left = np.hstack([r[:, :1], r[:, :-1]])
            right = np.hstack([r[:, 1:], r[:, -1:]])
            out[:, 0::2] = 0.75 * r + 0.25 * left
            out[:, 1::2] = 0.25 * r + 0.75 * right
            return out
        u, v = up(u), up(v)
    ks = lin
    outs = [ks[i][0] * yf + ks[i][1] * u + ks[i][2] * v for i in range(3)]
    rgb = np.stack(outs, axis=-1)
    return np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    d = a.astype(np.float64) - b.astype(np.float64)
    mse = float((d * d).mean())
    return 99.0 if mse == 0 else 10 * np.log10(255 * 255 / mse)


class EncFrame:
    """Encode 设备输入协议：.cuda() → [CAIMemoryView(Y), CAIMemoryView(UV)]。

    视图直接挂在 ORT 输出 OrtValue 的设备指针上——真零拷贝。
    注意 CAIMemoryView 构造序：(shape, strides, typestr, gpuIdx, ptr, readonly)。
    """

    def __init__(self, nvc, y_ptr: int, uv_ptr: int, h: int, w: int):
        self._views = [
            nvc.CAIMemoryView([h, w, 1], [w, 1, 1], "|u1", 0, y_ptr, False),
            nvc.CAIMemoryView([h // 2, w // 2, 2], [w, 2, 1], "|u1", 0, uv_ptr, False),
        ]

    def cuda(self):
        return self._views


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--no-encode", action="store_true")
    ap.add_argument("--matrix", default="709", choices=["709", "601"])
    args = ap.parse_args()

    from sv.engines.nvidia_dlls import register_nvidia_dlls
    register_nvidia_dlls()
    import PyNvVideoCodec as nvc
    import onnxruntime as ort
    from cuda.bindings import runtime as cudart
    from cuda.bindings import driver as cu

    cu.cuInit(0)
    _, _ctx = cu.cuDevicePrimaryCtxRetain(0)
    cu.cuCtxSetCurrent(_ctx)  # Encode 的 cuPointerGetAttribute 需要本线程持上下文

    dm = nvc.CreateDemuxer(args.src)
    dc = nvc.CreateDecoder(gpuid=0, codec=dm.GetNvCodecId(),
                           usedevicememory=True)  # 帧驻显存（默认宿主内存不可 D2D）
    W, H = dm.Width(), dm.Height()
    fps_n, fps_d = dm.AvgFrameRateNum(), dm.AvgFrameRateDen()
    print(f"[probe] {W}x{H} codec={dm.GetNvCodecId()} fps={fps_n}/{fps_d} "
          f"surface={dc.GetPixelFormat().name}")

    cache = TEMP_DIR / "trt_gpu_spike_cache"
    cache.mkdir(parents=True, exist_ok=True)
    onnx_path = TEMP_DIR / f"gpu_spike_{H}x{W}_{args.matrix}.onnx"
    onnx.save(WrapBuilder(MODEL, H, W, MATRICES[args.matrix]).build(), str(onnx_path))
    onnx.checker.check_model(str(onnx_path))
    print(f"[graph] wrapped → {onnx_path.name}")

    providers = [
        ("TensorrtExecutionProvider", {
            "device_id": 0, "trt_engine_cache_enable": True,
            "trt_engine_cache_path": str(cache), "trt_timing_cache_enable": True,
            "trt_fp16_enable": True, "trt_max_workspace_size": 10 << 30,
        }),
        "CUDAExecutionProvider", "CPUExecutionProvider",
    ]
    t0 = time.perf_counter()
    sess = ort.InferenceSession(str(onnx_path), providers=providers)
    print(f"[ort] session in {time.perf_counter()-t0:.1f}s providers={sess.get_providers()}")
    H2, W2 = H * 2, W * 2
    ov_y = ort.OrtValue.ortvalue_from_shape_and_type([1, H, W], np.uint16, "cuda", 0)
    ov_uv = ort.OrtValue.ortvalue_from_shape_and_type(
        [1, H // 2, W // 2, 2], np.uint16, "cuda", 0)
    ov_y_out = ort.OrtValue.ortvalue_from_shape_and_type(
        [1, 1, H2, W2], np.uint8, "cuda", 0)
    ov_uv_out = ort.OrtValue.ortvalue_from_shape_and_type(
        [1, H2 // 2, W2 // 2, 2], np.uint8, "cuda", 0)
    bind = sess.io_binding()
    bind.bind_ortvalue_input("y_in", ov_y)
    bind.bind_ortvalue_input("uv_in", ov_uv)
    bind.bind_ortvalue_output("out_y", ov_y_out)
    bind.bind_ortvalue_output("out_uv", ov_uv_out)
    # NVENC 要求 Y/UV 同一块连续显存（"u plane must follow yplane"）：
    # 打包缓冲 + 每帧两笔 D2D（Y 8.3MB + UV 4.1MB ≈ 0.2ms）
    packed = int(cudart.cudaMalloc(H2 * W2 * 3 // 2)[1])

    enc = mux = None
    if not args.no_encode:
        enc = nvc.CreateEncoder(W2, H2, "NV12", False, codec="h264")
        out_file = TEMP_DIR / "gpu_spike_out.mp4"
        if out_file.exists():
            out_file.unlink()
        mux = nvc.FFmpegMuxer(str(out_file), nvc.MP4, "h264", W2, H2,
                              fps_num=fps_n, fps_den=fps_d,
                              extradata=enc.GetSequenceParams())
        print(f"[enc] NVENC h264 {W2}x{H2} → {out_file.name}")

    stats = {"d2d": 0.0, "infer": 0.0, "encode": 0.0}
    n = muxed = 0
    ref_yuv = None
    first_out_nv12 = None
    t_all = time.perf_counter()
    while n < args.frames:
        pkt = dm.Demux()
        if pkt is None:
            break
        for f in dc.Decode(pkt):
            n += 1
            views = f.cuda()
            yv = views[0].__cuda_array_interface__
            uvv = views[1].__cuda_array_interface__
            if n == 150:
                ref_yuv = (
                    np.frombuffer(_d2h(yv["data"][0], H * W * 2), np.uint16).reshape(H, W).copy(),
                    np.frombuffer(_d2h(uvv["data"][0], H // 2 * W * 2), np.uint16).reshape(
                        H // 2, W // 2, 2).copy())
            t = time.perf_counter()
            dc.SyncOnCUStream()  # 解码写完才可被其他流访问
            cudart.cudaMemcpy2DAsync(
                ov_y.data_ptr(), W * 2, yv["data"][0], yv["strides"][0] * 2,
                W * 2, H, cudart.cudaMemcpyKind.cudaMemcpyDeviceToDevice, 0)
            cudart.cudaMemcpy2DAsync(
                ov_uv.data_ptr(), W * 2, uvv["data"][0], uvv["strides"][0] * 2,
                W * 2, H // 2, cudart.cudaMemcpyKind.cudaMemcpyDeviceToDevice, 0)
            cudart.cudaStreamSynchronize(0)
            stats["d2d"] += time.perf_counter() - t
            t = time.perf_counter()
            sess.run_with_iobinding(bind)
            stats["infer"] += time.perf_counter() - t
            if n == 150:
                first_out_nv12 = np.concatenate([
                    ov_y_out.numpy().flatten(), ov_uv_out.numpy().flatten()],
                ).reshape(H2 * 3 // 2, W2).copy()  # 一次性 D2H 供 V2
            if enc is not None:
                t = time.perf_counter()
                cudart.cudaMemcpy2DAsync(
                    packed, W2, ov_y_out.data_ptr(), W2, W2, H2,
                    cudart.cudaMemcpyKind.cudaMemcpyDeviceToDevice, 0)
                cudart.cudaMemcpy2DAsync(
                    packed + H2 * W2, W2, ov_uv_out.data_ptr(), W2, W2, H2 // 2,
                    cudart.cudaMemcpyKind.cudaMemcpyDeviceToDevice, 0)
                cudart.cudaStreamSynchronize(0)
                frame = EncFrame(nvc, packed, packed + H2 * W2, H2, W2)
                for p in enc.Encode(frame):
                    ts = muxed * (90000 * fps_d) // fps_n
                    dur = (90000 * fps_d) // fps_n
                    mux.Mux(p["data"], ts, ts, dur, 0)
                    muxed += 1
                stats["encode"] += time.perf_counter() - t
    wall = time.perf_counter() - t_all
    if enc is not None:
        for p in enc.EndEncode():
            ts = muxed * (90000 * fps_d) // fps_n
            mux.Mux(p["data"], ts, ts, (90000 * fps_d) // fps_n, 0)
            muxed += 1
        mux.Finalize()
        del mux

    print(f"[e2e] {n} frames in {wall:.2f}s = {n / wall:.1f} fps (muxed {muxed})")
    for k, v in stats.items():
        print(f"  {k:8s}: {v * 1000 / max(n, 1):7.2f} ms/frame")
    print(f"  other    : {(wall - sum(stats.values())) * 1000 / max(n, 1):7.2f} ms/frame")

    checks(args, n, H, W, ref_yuv, first_out_nv12, sess)
    return 0


def _d2h(ptr: int, nbytes: int) -> bytes:
    from cuda.bindings import runtime as cudart
    host = np.empty(nbytes, np.uint8)
    cudart.cudaMemcpy(host.ctypes.data, ptr, nbytes,
                      cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost)
    return host.tobytes()


def np_rgb_to_nv12(rgb01: np.ndarray, ey: tuple) -> np.ndarray:
    """RGB [0,1] HWC → NV12 packed（与图同数学，供 V2 同域参考）。"""
    r, g, b = rgb01[..., 0], rgb01[..., 1], rgb01[..., 2]
    eyv = ey[0] * r + ey[1] * g + ey[2] * b
    y = np.clip(16 + 219 * eyv, 0, 255)
    cb = np.clip(128 + (b - eyv) * 224 / 1.8556, 0, 255)
    cr = np.clip(128 + (r - eyv) * 224 / 1.5748, 0, 255)
    h2, w2 = y.shape[0] // 2, y.shape[1] // 2
    cb = cb.reshape(h2, 2, w2, 2).mean(axis=(1, 3))
    cr = cr.reshape(h2, 2, w2, 2).mean(axis=(1, 3))
    nv12 = np.zeros((h2 * 2 + h2, y.shape[1]), np.uint8)
    nv12[:h2 * 2] = y.astype(np.uint8)
    nv12[h2 * 2:, 0::2] = cb.astype(np.uint8)
    nv12[h2 * 2:, 1::2] = cr.astype(np.uint8)
    return nv12


def checks(args, n, H, W, ref_yuv, out_nv12, sess) -> None:
    """V1: 转换层数学 vs ffmpeg rgb24；V2: 全链 SR vs CPU 标准路径。"""
    import onnxruntime as ort
    if ref_yuv is None:
        print("[check] 帧数不足，跳过")
        return
    ff = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
         "-i", args.src, "-map", "0:v:0", "-an", "-sn", "-dn",
         "-frames:v", "150", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-sws_flags", "lanczos+accurate_rnd+full_chroma_int", "-"],
        capture_output=True)
    arr = np.frombuffer(ff.stdout, np.uint8)
    if arr.size < 150 * H * W * 3:
        print(f"[check] ffmpeg 参考不足（{arr.size}B）: {ff.stderr.decode(errors='replace')[-200:]}")
        return
    ref6 = arr.reshape(-1, H, W, 3)
    m = MATRICES[args.matrix]
    mine = np_yuv_to_rgb(ref_yuv[0], ref_yuv[1], m["lin"], nearest=False)
    print(f"[V1] 转换层 PSNR(frame5, vs ffmpeg) = {psnr(ref6[149], mine):.2f} dB "
          f"maxdiff={int(np.abs(ref6[149].astype(int) - mine.astype(int)).max())}")
    mine_n = np_yuv_to_rgb(ref_yuv[0], ref_yuv[1], m["lin"], nearest=True)
    print(f"[V1-nn] 最近邻口径 PSNR = {psnr(ref6[149], mine_n):.2f} dB")

    # V2：CPU 标准推理（ffmpeg rgb24 → f16 NCHW → 原模型 → uint8）vs GPU NV12 输出
    if out_nv12 is None:
        return
    src_rgb = ref6[149].astype(np.float32) / 255.0
    x = np.transpose(src_rgb, (2, 0, 1))[None].astype(np.float32)  # 模型 IO 为 fp32（内部自 Cast f16）
    plain = ort.InferenceSession(str(MODEL), providers=["CUDAExecutionProvider"])
    y = plain.run(None, {plain.get_inputs()[0].name: x})[0]  # f16 NCHW
    rgb_out = np.clip(np.transpose(y[0], (1, 2, 0)).astype(np.float32), 0, 1)
    ref_sr = (rgb_out * 255 + 0.5).clip(0, 255).astype(np.uint8)
    nv = out_nv12  # 已是 (1.5H2, W2) 2D
    H2, W2 = ref_sr.shape[:2]
    # 同域对比：CPU SR 输出也过一遍同款 RGB→NV12（4:2:0 量化两边对等），
    # 剩下的就是 SR 执行差异（TRT fp16 vs CUDA EP fp16）本身
    ref_nv12 = np_rgb_to_nv12(rgb_out, m["ey"])
    ry, ruv = ref_nv12[:H2], ref_nv12[H2:]
    gy, guv = nv[:H2], nv[H2:]

    def p8(a, b):
        d = a.astype(np.float64) - b.astype(np.float64)
        mse = (d * d).mean()
        return 99.0 if mse == 0 else 10 * np.log10(255 * 255 / mse)

    print(f"[V2] 同域 SR 差异: Y PSNR={p8(ry, gy):.2f} dB (maxdiff="
          f"{int(np.abs(ry.astype(int) - gy.astype(int)).max())}), "
          f"UV PSNR={p8(ruv, guv):.2f} dB (maxdiff="
          f"{int(np.abs(ruv.astype(int) - guv.astype(int)).max())})")


if __name__ == "__main__":
    sys.exit(main())

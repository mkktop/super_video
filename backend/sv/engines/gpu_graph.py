"""GPU 直连链图手术：YUV 平面(NV12/P016) → 模型 → NV12 平面，帧数据全程显存。

承接 u8_wrap 的边界法则（TRT 不接受 uint8/uint16 中间张量）：
- 输入侧 y_in/uv_in 为 u8(8bit 源)/u16(10bit 源) 平面，仅出现在图边界，
  第一步即 Cast 进浮点域（转换子图因 u16 边界自动落 CUDA EP，实测代价 ~1.6ms）；
- 输出侧 out_y/out_uv 各自由浮点域边界 Cast 产出（NVENC 要求 Y/UV 同一连续
  分配，打包由管线侧两笔 D2D 完成）。

色彩口径与产品 ffmpeg 链（swscale lanczos+accurate_rnd+full_chroma_int → rgb24）
位级对齐——BENCH 尖刺实测 V1 104dB / maxdiff=1：
- P016(10bit)：MSB 对齐（>>6）；Y 量化 219 系（/876），色度 224 系（/896）
- NV12(8bit)：无移位；Y /219，色度 /224（中心 128 → 0）
- 色度上采样 half_pixel bilinear（与 swscale 默认一致）
- 矩阵 BT.709（HD 源；未标记源沿用产品链既有行为）

模型 IO dtype 自适应：按模型声明输入 Cast（fp32-IO 转换版与原生 fp16-IO 均可）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

from ..paths import TEMP_DIR

# BT.709 limited-range：R/G/B ← (Y, U-0.5, V-0.5)（系数作用于 [0,1] 域）
BT709_LIN = (
    (1.164383, 0.0, 1.792741),
    (1.164383, -0.212639, -0.532909),
    (1.164383, 2.112402, 0.0),
)
BT709_EY = (0.2126, 0.7152, 0.0722)

MATRICES = {"709": (BT709_LIN, BT709_EY)}

# 位深相关量化参数：移位、Y 量距、色度量距（swscale 口径）
_BITS = {
    8: dict(dtype=TensorProto.UINT8, np_dtype=np.uint8, shift_div=1.0,
            y_lo=16.0, y_rng=219.0, c_lo=16.0, c_rng=224.0),
    10: dict(dtype=TensorProto.UINT16, np_dtype=np.uint16, shift_div=64.0,
             y_lo=64.0, y_rng=876.0, c_lo=64.0, c_rng=896.0),
}

WRAP_DIR = TEMP_DIR / "gpu_wrap"


def _elem_dtype(tt) -> int:
    return tt.elem_type


def wrap_model(model_path: Path, h: int, w: int, bit_depth: int,
               matrix: str = "709", scale: int = 2) -> onnx.ModelProto:
    """生成 yuv 直进直出包装图。h/w 为源尺寸（须偶数）；scale 为模型放大倍率。"""
    if h % 2 or w % 2:
        raise ValueError(f"GPU 直连链要求偶数尺寸，收到 {w}x{h}")
    if bit_depth not in _BITS:
        raise ValueError(f"不支持的位深: {bit_depth}")
    lin, ey = MATRICES[matrix]
    q = _BITS[bit_depth]
    m = onnx.load(str(model_path))
    g = m.graph
    in_name = g.input[0].name
    out_name = g.output[0].name
    in_elem = _elem_dtype(g.input[0].type.tensor_type)
    H2, W2, h2, w2 = h * scale, w * scale, h * scale // 2, w * scale // 2

    pre: list[onnx.Node] = []
    post: list[onnx.Node] = []
    inits: dict[str, onnx.TensorProto] = {}

    def N(target, op, ins, out, **kw):
        target.append(helper.make_node(op, ins, [out], **kw))
        return out

    def k(name, arr):
        if name not in inits:
            inits[name] = numpy_helper.from_array(np.asarray(arr), name)
        return name

    def f(name, v):
        return k(name, np.array([v], np.float32))

    def i(name, arr):
        return k(name, np.asarray(arr, np.int64))

    # ---- 前链：平面 → [0,1] full → RGB f?（模型 IO dtype） ----
    cmin, cmax = f("cmin", 0.0), f("cmax", 1.0)

    def to01(t, tag, lo, rng):
        t = N(pre, "Cast", [t], f"{tag}_f", to=TensorProto.FLOAT)
        t = N(pre, "Mul", [t, f(f"sd_{tag}", 1.0 / q["shift_div"])],
              f"{tag}_10")
        t = N(pre, "Sub", [t, f(f"lo_{tag}", lo)], f"{tag}_s")
        t = N(pre, "Div", [t, f(f"rg_{tag}", rng)], f"{tag}_01")
        return N(pre, "Clip", [t, cmin, cmax], f"{tag}_c")

    y = to01("y_in", "y", q["y_lo"], q["y_rng"])
    # 色度反洗牌：PyNvVideoCodec 设备表面的 UV 平面是「前后半 riffle」排列
    # （值 k 在槽 2k、后半值 N/2+k 在槽 2k+1；元素粒度 = u16(10bit)/u8(8bit)；
    # 多文件×分辨率×编码在内容帧上实证位级成立——黑帧色度恒定不可判）。
    # 还原用「reshape 成对 + 切末轴」——CUDA EP 的带步长 Slice 实测结果错误。
    uv_src = N(pre, "Cast", ["uv_in"], "uv_slots_f", to=TensorProto.FLOAT)
    uv_pairs = N(pre, "Reshape", [uv_src, i("uvpair_d", [1, -1, 2])], "uv_pairs")
    big = i("sl_max", [2 ** 62])
    ev3 = N(pre, "Slice", [uv_pairs, i("ev_s", [0, 0, 0]),
                           i("ev_e", [1, 2 ** 62, 1]), i("ev_ax", [0, 1, 2])], "uv_ev3")
    od3 = N(pre, "Slice", [uv_pairs, i("od_s", [0, 0, 1]),
                           i("od_e", [1, 2 ** 62, 2]), i("od_ax", [0, 1, 2])], "uv_od3")
    ev = N(pre, "Squeeze", [ev3, i("ev_sq", [2])], "uv_ev")
    od = N(pre, "Squeeze", [od3, i("od_sq", [2])], "uv_od")
    uv = N(pre, "Concat", [ev, od], "uv_cat", axis=1)
    uv = N(pre, "Reshape", [uv, i("uv_d4", [1, h // 2, w // 2, 2])], "uv_unr")
    uv = N(pre, "Mul", [uv, f("sd_uv", 1.0 / q["shift_div"])], "uv_10")
    uv = N(pre, "Sub", [uv, f("lo_uv", q["c_lo"])], "uv_s")
    uv = N(pre, "Div", [uv, f("rg_uv", q["c_rng"])], "uv_01")
    uv = N(pre, "Clip", [uv, cmin, cmax], "uv_c")
    u = N(pre, "Slice", [uv, i("su0", [0, 0, 0, 0]), i("su1", [1, h // 2, w // 2, 1]),
                         i("su2", [0, 1, 2, 3])], "u_c")
    v = N(pre, "Slice", [uv, i("sv0", [0, 0, 0, 1]), i("sv1", [1, h // 2, w // 2, 2]),
                         i("sv2", [0, 1, 2, 3])], "v_c")
    u = N(pre, "Squeeze", [u, i("squ", [3])], "u_s")
    v = N(pre, "Squeeze", [v, i("sqv", [3])], "v_s")
    roi = k("roi_e", np.zeros((0,), np.float32))
    scales = k("scales_uv", np.array([1, 2, 2], np.float32))
    u = N(pre, "Resize", [u, roi, scales], "u_up", mode="linear",
          coordinate_transformation_mode="half_pixel")
    v = N(pre, "Resize", [v, roi, scales], "v_up", mode="linear",
          coordinate_transformation_mode="half_pixel")
    u05 = N(pre, "Sub", [u, f("half", 0.5)], "u_cen")
    v05 = N(pre, "Sub", [v, f("half2", 0.5)], "v_cen")

    def lin3(tag, yk, uk, vk):
        p = [N(pre, "Mul", [y, f(f"yk_{tag}", yk)], f"{tag}_a0"),
             N(pre, "Mul", [u05, f(f"uk_{tag}", uk)], f"{tag}_b0"),
             N(pre, "Mul", [v05, f(f"vk_{tag}", vk)], f"{tag}_c0")]
        s = N(pre, "Add", [p[0], p[1]], f"{tag}_01")
        return N(pre, "Add", [s, p[2]], f"{tag}_rgb")

    chans = []
    for idx, tag in enumerate("rgb"):
        c = lin3(tag, *lin[idx])
        chans.append(N(pre, "Unsqueeze", [c, i(f"{tag}_ax", [1])], f"{tag}_4d"))
    rgb = N(pre, "Concat", chans, "rgb_chw", axis=1)
    rgb = N(pre, "Clip", [rgb, cmin, cmax], "rgb_clip")
    N(pre, "Cast", [rgb], in_name, to=in_elem)

    # ---- 后链：模型输出 → NV12 平面（浮点域收尾，边界 Cast） ----
    o = N(post, "Cast", [out_name], "o_f", to=TensorProto.FLOAT)
    ch = []
    for idx, tag in enumerate("rgb"):
        ch.append(N(post, "Slice", [o, i(f"o{tag}0", [0, idx]), i(f"o{tag}1", [1, idx + 1]),
                                    i(f"o{tag}2", [0, 1])], f"o_{tag}"))
    prods = [N(post, "Mul", [c, f(f"ey{i}", 219 * ey[i])], f"ey_x{i}")
             for i, c in enumerate(ch)]
    s = N(post, "Add", [prods[0], prods[1]], "ey_01")
    s = N(post, "Add", [s, prods[2]], "ey_012")
    cy = N(post, "Add", [s, f("yoff", 16.0)], "Y_255")
    ey_f = N(post, "Div", [N(post, "Sub", [cy, f("m16", 16.0)], "ym16"),
                           f("d219", 219.0)], "EY_full")
    chroma = []
    for src, kdiv, tag in ((ch[2], 224 / 1.8556, "cb"), (ch[0], 224 / 1.5748, "cr")):
        d = N(post, "Sub", [src, ey_f], f"{tag}_d")
        mm = N(post, "Mul", [d, f(f"kd_{tag}", kdiv)], f"{tag}_m")
        oo = N(post, "Add", [mm, f(f"co_{tag}", 128.0)], f"{tag}_255")
        chroma.append(N(post, "Clip", [oo, f("ca", 0.0), f("cb255", 255.0)], f"{tag}_c"))
    y_out = N(post, "Clip", [cy, f("cc0", 0.0), f("cc255", 255.0)], "Y_c")

    def sub4(t, tag):
        r5 = N(post, "Reshape", [t, i(f"{tag}_d5", [1, 1, h2, 2, w2, 2])], f"{tag}_r5")
        mm = N(post, "ReduceMean", [r5], f"{tag}_mean", axes=[3, 5], keepdims=0)
        return N(post, "Reshape", [mm, i(f"{tag}_d2", [1, h2, w2, 1])], f"{tag}_4d")

    uvp = N(post, "Concat", [sub4(chroma[0], "cbs"), sub4(chroma[1], "crs")],
            "uv_pl", axis=3)
    N(post, "Cast", [y_out], "out_y", to=TensorProto.UINT8)
    N(post, "Cast", [uvp], "out_uv", to=TensorProto.UINT8)

    # ---- 组装 ----
    for node in reversed(pre):
        g.node.insert(0, node)
    g.node.extend(post)
    del g.input[:]
    g.input.extend([
        helper.make_tensor_value_info("y_in", q["dtype"], [1, h, w]),
        helper.make_tensor_value_info("uv_in", q["dtype"], [1, h // 2, w // 2, 2]),
    ])
    # 原模型输出转内部张量（不导出——省一次物化），只出 NV12 双平面
    for o_ in list(g.output):
        if o_.name == out_name:
            g.output.remove(o_)
    g.output.append(helper.make_tensor_value_info(
        "out_y", TensorProto.UINT8, [1, 1, H2, W2]))
    g.output.append(helper.make_tensor_value_info(
        "out_uv", TensorProto.UINT8, [1, h2, w2, 2]))
    g.initializer.extend(inits.values())
    model = helper.make_model(g, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    return model


# 图结构版本盐：图手术逻辑演进时 bump，防旧缓存复用（权重 mtime 不变的
# 代码改动也需失效）
_WRAP_VER = 3


def wrapped_path(weight: Path, h: int, w: int, bit_depth: int,
                 matrix: str, scale: int) -> Path:
    """缓存路径；源权重 mtime 变化即失效（同 u8_wrap 惯例）。"""
    key = f"{weight.stem}_{h}x{w}_bd{bit_depth}_{matrix}_x{scale}_v{_WRAP_VER}"
    return WRAP_DIR / f"{key}.onnx"


def ensure_wrapped(weight: Path, h: int, w: int, bit_depth: int,
                   matrix: str = "709", scale: int = 2) -> Path:
    """惰性生成/复用包装图缓存。"""
    dst = wrapped_path(weight, h, w, bit_depth, matrix, scale)
    if dst.exists() and dst.stat().st_mtime >= weight.stat().st_mtime:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    onnx.save(wrap_model(weight, h, w, bit_depth, matrix, scale), str(tmp))
    tmp.replace(dst)
    return dst


# ---- numpy 参考实现（与图同数学；单测对拍 + GPU 链校验基准） ----

def np_unriffle(uv_slots: np.ndarray) -> np.ndarray:
    """色度平面反洗牌（与图内 Strided-Slice+Concat 同变换）。

    槽 2k = 值 k（前半），槽 2k+1 = 值 N/2+k（后半）；元素粒度随位深
    （10bit=u16 槽、8bit=u8 字节）。返回紧致值序列，reshape 由调用方做。
    """
    flat = uv_slots.reshape(-1)
    return np.concatenate([flat[0::2], flat[1::2]])


def np_yuv_to_rgb(y: np.ndarray, uv: np.ndarray, bit_depth: int,
                  lin=BT709_LIN, riffle: bool = True) -> np.ndarray:
    """numpy 参考（与图同数学）。

    riffle=True：uv 为解码表面「洗牌槽」形态（产品链路）；
    riffle=False：uv 为 ffmpeg 裸解的紧致平面（参考基准用）。
    """
    q = _BITS[bit_depth]
    sd = q["shift_div"]
    uv_tight = (np_unriffle(uv) if riffle else uv.reshape(-1)
                ).reshape(y.shape[0] // 2, y.shape[1] // 2, 2)
    yf = np.clip((y.astype(np.float32) / sd - q["y_lo"]) / q["y_rng"], 0, 1)
    u = (uv_tight[..., 0].astype(np.float32) / sd - q["c_lo"]) / q["c_rng"] - 0.5
    v = (uv_tight[..., 1].astype(np.float32) / sd - q["c_lo"]) / q["c_rng"] - 0.5

    def up(x):  # half_pixel bilinear（与 Resize(linear, half_pixel) 对齐）
        hh, ww = x.shape
        r = np.empty((hh * 2, ww), np.float32)
        r[0::2] = 0.75 * x + 0.25 * np.vstack([x[:1], x[:-1]])
        r[1::2] = 0.25 * x + 0.75 * np.vstack([x[1:], x[-1:]])
        out = np.empty((hh * 2, ww * 2), np.float32)
        left = np.hstack([r[:, :1], r[:, :-1]])
        right = np.hstack([r[:, 1:], r[:, -1:]])
        out[:, 0::2] = 0.75 * r + 0.25 * left
        out[:, 1::2] = 0.25 * r + 0.75 * right
        return out

    u, v = up(u), up(v)
    outs = [lin[i][0] * yf + lin[i][1] * u + lin[i][2] * v for i in range(3)]
    return np.clip(np.stack(outs, axis=-1) * 255.0 + 0.5, 0, 255).astype(np.uint8)


def np_nv12_to_rgb(y: np.ndarray, uv: np.ndarray, lin=BT709_LIN) -> np.ndarray:
    """NV12 平面(u8 limited) → RGB uint8 HWC（预览/校验用逆变换）。"""
    yf = (y.astype(np.float32) - 16.0) / 219.0
    u = (uv[..., 0].astype(np.float32) - 128.0) / 224.0
    v = (uv[..., 1].astype(np.float32) - 128.0) / 224.0
    u = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1)
    v = np.repeat(np.repeat(v, 2, axis=0), 2, axis=1)
    outs = [lin[i][0] * yf + lin[i][1] * u + lin[i][2] * v for i in range(3)]
    return np.clip(np.stack(outs, axis=-1) * 255.0 + 0.5, 0, 255).astype(np.uint8)


def np_rgb_to_nv12(rgb01: np.ndarray, ey=BT709_EY) -> np.ndarray:
    """RGB [0,1] HWC → NV12 packed u8（与图后链同数学）。"""
    r, g, b = rgb01[..., 0], rgb01[..., 1], rgb01[..., 2]
    eyv = ey[0] * r + ey[1] * g + ey[2] * b
    yv = np.clip(16 + 219 * eyv, 0, 255)
    cb = np.clip(128 + (b - eyv) * 224 / 1.8556, 0, 255)
    cr = np.clip(128 + (r - eyv) * 224 / 1.5748, 0, 255)
    hh, ww = yv.shape[0] // 2, yv.shape[1] // 2
    cb = cb.reshape(hh, 2, ww, 2).mean(axis=(1, 3))
    cr = cr.reshape(hh, 2, ww, 2).mean(axis=(1, 3))
    nv12 = np.zeros((hh * 2 + hh, yv.shape[1]), np.uint8)
    nv12[:hh * 2] = yv.astype(np.uint8)
    nv12[hh * 2:, 0::2] = cb.astype(np.uint8)
    nv12[hh * 2:, 1::2] = cr.astype(np.uint8)
    return nv12


def psnr_u8(a: np.ndarray, b: np.ndarray) -> float:
    d = a.astype(np.float64) - b.astype(np.float64)
    mse = float((d * d).mean())
    return 99.0 if mse == 0 else 10 * np.log10(255 * 255 / mse)

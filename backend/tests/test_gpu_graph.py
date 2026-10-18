"""gpu_graph 图手术单测：纯 CPU 可跑（图内算子全在 CPU EP 成立）。

用 toy 模型（Resize 最近邻 2x 上采样）做「YUV→RGB→SR→NV12」全数学对拍：
图输出 vs numpy 参考实现（np_yuv_to_rgb + np_rgb_to_nv12）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import pytest
from onnx import TensorProto, helper, numpy_helper

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sv.engines.gpu_graph import (  # noqa: E402
    ensure_wrapped, np_rgb_to_nv12, np_yuv_to_rgb, wrap_model, wrapped_path,
)


def _toy_model(io_elem: int = TensorProto.FLOAT, name: str = "_toy_up2.onnx") -> Path:
    """2x 最近邻上采样 toy「SR 模型」，IO dtype 参数化（具体尺寸 64x96→128x192）。"""
    x = helper.make_tensor_value_info("input", io_elem, [1, 3, 64, 96])
    y = helper.make_tensor_value_info("output", io_elem, [1, 3, 128, 192])
    node = helper.make_node(
        "Resize", ["input", "roi_z", "scales_t"], ["output"],
        mode="nearest", coordinate_transformation_mode="asymmetric",
        nearest_mode="floor")
    inits = [
        numpy_helper.from_array(np.zeros((0,), np.float32), "roi_z"),
        numpy_helper.from_array(np.array([1, 1, 2, 2], np.float32), "scales_t"),
    ]
    g = helper.make_graph([node], "toy", [x], [y], inits)
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 17)])
    m.ir_version = 9
    p = Path(__file__).parent / name
    onnx.save(m, str(p))
    return p


@pytest.fixture(scope="module")
def toy():
    return _toy_model()


@pytest.fixture(scope="module")
def yuv_planes():
    """平滑渐变合成平面（梯度+低频正弦），色度按解码表面「riffle 槽」形态给出。

    纯随机/块状数据对色度插值实现差异过敏（half_pixel 双线性在硬边处
    的微观差异天然大）；平滑数据下 numpy 参考与 ORT Resize 应高度一致。
    """
    h, w = 64, 96
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    y_f = 0.5 + 0.35 * np.sin(xx / 11.0) * np.cos(yy / 9.0) + 0.1 * xx / w
    u_f = 0.5 + 0.3 * np.cos(xx / 13.0 + 1.0) * np.sin(yy / 8.0)
    v_f = 0.5 + 0.3 * np.sin(xx / 10.0 + 2.0) * np.cos(yy / 11.0)
    y10 = np.clip(64 + y_f * 876, 64, 940).astype(np.uint16) << 6
    uv10_t = np.stack([
        np.clip(64 + (u_f[::2, ::2] - 0.5) * 896 + 512 - 256, 64, 960),
        np.clip(64 + (v_f[::2, ::2] - 0.5) * 896 + 512 - 256, 64, 960),
    ], axis=-1).astype(np.uint16) << 6

    def riffle(uv_tight):
        """槽 2k=值 k（前半），槽 2k+1=后半值——与解码表面一致。"""
        flat = uv_tight.reshape(-1)
        n = len(flat)
        slots = np.empty_like(flat)
        slots[0::2] = flat[:n // 2]
        slots[1::2] = flat[n // 2:]
        return slots.reshape(uv_tight.shape)

    uv10 = riffle(uv10_t)
    y8 = ((y10 >> 6) >> 2).astype(np.uint8)
    uv8 = riffle(((uv10_t >> 6) >> 2).astype(np.uint8))
    return (h, w), y10, uv10, y8, uv8


def _run_wrapped(model_path, h, w, bit_depth, y, uv):
    mp = wrap_model(model_path, h, w, bit_depth)
    onnx.checker.check_model(mp)
    sess = ort.InferenceSession(
        mp.SerializeToString(), providers=["CPUExecutionProvider"])
    oy, ouv = sess.run(None, {"y_in": y[None], "uv_in": uv[None]})
    return oy[0, 0], ouv[0]


@pytest.mark.parametrize("bit_depth", [8, 10])
def test_math_parity(toy, yuv_planes, bit_depth):
    """图输出 vs numpy 参考对拍（PSNR 口径）。

    numpy 参考的 half_pixel 双线性与 ORT Resize 实现在块边界有微观差异，
    位级对齐由 GPU-gated 的 ffmpeg 神谕测试把关（BENCH 实测 104dB/maxdiff=1）；
    此处 PSNR 门槛足以拦截矩阵/量程/通道排序等结构性回归（错即 <30dB）。
    """
    (h, w), y10, uv10, y8, uv8 = yuv_planes
    y, uv = (y8, uv8) if bit_depth == 8 else (y10, uv10)
    gy, guv = _run_wrapped(toy, h, w, bit_depth, y, uv)
    ref_rgb = np_yuv_to_rgb(y, uv, bit_depth)
    sr = np.repeat(np.repeat(ref_rgb, 2, axis=0), 2, axis=1)  # toy = 2x 最近邻
    ref_nv12 = np_rgb_to_nv12(sr.astype(np.float32) / 255.0)
    H2 = h * 2
    ruv = ref_nv12[H2:].reshape(H2 // 2, w, 2)  # 输出色度 H2/2 × w（交错对）
    dy = np.abs(gy.astype(int) - ref_nv12[:H2].astype(int))
    duv = np.abs(guv.astype(int) - ruv.astype(int))
    assert psnr(gy, ref_nv12[:H2]) >= 45.0, f"Y PSNR {psnr(gy, ref_nv12[:H2]):.1f}"
    assert psnr(guv, ruv) >= 38.0
    assert dy.mean() <= 1.5 and duv.mean() <= 3.0  # 无系统性偏置


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    d = a.astype(np.float64) - b.astype(np.float64)
    mse = float((d * d).mean())
    return 99.0 if mse == 0 else 10 * np.log10(255 * 255 / mse)


def test_odd_dims_rejected(toy):
    with pytest.raises(ValueError):
        wrap_model(toy, 63, 96, 8)


def test_bad_bitdepth_rejected(toy):
    with pytest.raises(ValueError):
        wrap_model(toy, 64, 96, 12)


def test_f16_io_model_cast_branch(yuv_planes):
    """模型声明 f16 输入时，前链末端 Cast 到 f16。"""
    p16 = _toy_model(TensorProto.FLOAT16, "_toy_up2_f16io.onnx")
    mp = wrap_model(p16, 64, 96, 8)
    cast = [n for n in mp.graph.node if n.op_type == "Cast" and "input" in n.output]
    assert cast, "应有产出模型输入名的 Cast"
    to = [onnx.helper.get_attribute_value(a) for a in cast[0].attribute
          if a.name == "to"][0]
    assert to == TensorProto.FLOAT16


def test_cache_roundtrip(toy, tmp_path, monkeypatch):
    from sv.engines import gpu_graph
    monkeypatch.setattr(gpu_graph, "WRAP_DIR", tmp_path)
    p1 = ensure_wrapped(toy, 64, 96, 8)
    assert p1.exists()
    stat1 = p1.stat().st_mtime_ns
    p2 = ensure_wrapped(toy, 64, 96, 8)
    assert p2 == p1 and p2.stat().st_mtime_ns == stat1  # 命中缓存不重建
    fut = toy.stat().st_mtime + 10  # 源更新 → 失效重建
    os.utime(toy, (fut, fut))
    p3 = ensure_wrapped(toy, 64, 96, 8)
    assert p3 == p1 and p3.stat().st_mtime_ns != stat1


def test_cache_tmp_pid_suffixed(toy, tmp_path, monkeypatch):
    """tmp 写入名必须带 pid：双路并行两 worker 进程首次包装同一 (权重,分辨率)
    时，固定 tmp 名会互踩——交错写坏后 replace 成坏缓存，且 mtime 失效条件
    永不触发，该键的 GPU 链从此静默回退（回归：曾用固定 .tmp 名）。"""
    from sv.engines import gpu_graph
    monkeypatch.setattr(gpu_graph, "WRAP_DIR", tmp_path)
    saved: list[str] = []
    real_save = onnx.save
    monkeypatch.setattr(gpu_graph.onnx, "save",
                        lambda m, p: (saved.append(Path(p).name),
                                      real_save(m, p))[1])
    ensure_wrapped(toy, 64, 96, 8)
    assert saved and saved[0].endswith(f".tmp{os.getpid()}"), saved
    assert not list(tmp_path.glob("*.tmp*")), "成功落盘后不得残留 tmp"


def test_wrapped_path_keying():
    a = wrapped_path(Path("m.onnx"), 1080, 1920, 10, "709", 2)
    b = wrapped_path(Path("m.onnx"), 1080, 1920, 8, "709", 2)
    c = wrapped_path(Path("m.onnx"), 1080, 1920, 10, "709", 3)
    assert len({a.name, b.name, c.name}) == 3

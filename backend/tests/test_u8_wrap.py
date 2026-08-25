"""uint8 直进直出图手术：包装生成 / 引擎 A/B 逐位一致 / io 约定矩阵 / 失败回退。

背景（BENCH.md 2026-08-25）：前后处理 GPU 化后推理 3.8x，输出与原路径逐位一致。
引擎 load() 内部已有 A/B 校验（不过则自动回退），此处独立复验并覆盖矩阵。
"""
from pathlib import Path

import numpy as np
import pytest

from sv.engines.onnx_engine import OnnxSrEngine
from sv.engines.u8_wrap import wrap_u8
from sv.paths import TEMP_DIR

BUNDLED = Path(__file__).parent.parent / "sv" / "models" / "bundled"
V3 = BUNDLED / "RealESR-AnimeVideo-v3_x4.onnx"
V3_FP16 = BUNDLED / "RealESR-AnimeVideo-v3_x4_fp16.onnx"
IO_V3 = {"color": "bgr", "range": "0-1"}  # animevideov3 约定（BENCH.md 实测校准）


def _frames():
    rng = np.random.default_rng(11)
    return [
        np.zeros((96, 128, 3), np.uint8),
        rng.integers(0, 256, (96, 128, 3), dtype=np.uint8),
        rng.integers(0, 256, (95, 127, 3), dtype=np.uint8),  # 奇尺寸：pad+裁剪路径
    ]


def _assert_close(a: np.ndarray, b: np.ndarray, tol: int = 1) -> None:
    assert a.shape == b.shape and a.dtype == b.dtype == np.uint8
    diff = int(np.abs(a.astype(np.int16) - b.astype(np.int16)).max())
    assert diff <= tol, f"输出不一致 maxdiff={diff}"


def _build_tiny_model(path: Path) -> None:
    """最小 2x 上采样模型：Conv3→12(1x1) + PixelShuffle(2)，fp32 动态尺寸。"""
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    rng = np.random.default_rng(3)
    w = (rng.standard_normal((12, 3, 3, 3)) * 0.05).astype(np.float32)
    nodes = [
        helper.make_node("Conv", ["x", "W", "B"], ["c"], kernel_shape=[3, 3],
                         pads=[1, 1, 1, 1]),
        helper.make_node("DepthToSpace", ["c"], ["y"], blocksize=2, mode="CRD"),
    ]
    graph = helper.make_graph(
        nodes, "tiny_sr",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, ["n", 3, "h", "w"])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, ["n", 3, "h2", "w2"])],
        [numpy_helper.from_array(w, "W"),
         numpy_helper.from_array(np.zeros(12, np.float32), "B")],
    )
    m = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    m.ir_version = 8
    onnx.checker.check_model(m)
    onnx.save(m, str(path))


def test_wrap_file_generates(tmp_path):
    """图手术产物是合法 ONNX（checker 通过），且可建 session。"""
    pytest.importorskip("onnx")
    import onnx
    import onnxruntime as ort

    dst = tmp_path / "v3_u8.onnx"
    wrap_u8(V3, dst, color="bgr", range_01=True)
    assert dst.exists()
    onnx.checker.check_model(onnx.load(str(dst)))
    sess = ort.InferenceSession(str(dst), providers=["CPUExecutionProvider"])
    assert sess.get_inputs()[0].type == "tensor(uint8)"
    assert sess.get_outputs()[0].type == "tensor(uint8)"


def test_engine_ab_fp32():
    """真实模型（bgr/0-1）：包装开/关输出一致；load 内建校验保证 u8_wrapped=True。"""
    plain = OnnxSrEngine(V3, 4, io=IO_V3, u8_wrap=False)
    plain.load()
    wrapped = OnnxSrEngine(V3, 4, io=IO_V3, u8_wrap=True)
    wrapped.load()
    assert wrapped.u8_wrapped is True, "满足条件时包装必须生效（否则校验失败被静默回退）"
    for f in _frames():
        _assert_close(plain.process(f), wrapped.process(f))


def test_engine_ab_fp16_terminal_cast():
    """fp16 转换模型（末端 fp32→fp16 Cast）：绕末端 Cast 路径输出一致。"""
    plain = OnnxSrEngine(V3_FP16, 4, io=IO_V3, u8_wrap=False)
    plain.load()
    wrapped = OnnxSrEngine(V3_FP16, 4, io=IO_V3, u8_wrap=True)
    wrapped.load()
    assert wrapped.u8_wrapped is True
    for f in _frames():
        _assert_close(plain.process(f), wrapped.process(f))


@pytest.mark.parametrize("color,range_", [
    ("rgb", "0-1"), ("bgr", "0-1"), ("rgb", "0-255"), ("bgr", "0-255"),
])
def test_io_convention_matrix(tmp_path, color, range_):
    """合成小模型 × 全部 io 约定组合：通道反转与值域缩放的图内接线正确。"""
    pytest.importorskip("onnx")
    tiny = tmp_path / f"tiny_{color}_{range_}.onnx"
    _build_tiny_model(tiny)
    io = {"color": color, "range": range_}
    plain = OnnxSrEngine(tiny, 2, io=io, u8_wrap=False)
    plain.load()
    wrapped = OnnxSrEngine(tiny, 2, io=io, u8_wrap=True)
    wrapped.load()
    assert wrapped.u8_wrapped is True
    for f in _frames():
        _assert_close(plain.process(f), wrapped.process(f))


def test_wrap_marker_rebuild_and_uint8_io():
    """清标记强制走完整校验链：包装 session 必须 uint8 直进（防回归：曾误把包装
    session 建到原始 float 模型文件上，marker 存在时静默潜伏到首帧才炸）。"""
    from sv.paths import TEMP_DIR

    for f in (TEMP_DIR / "u8_wrap").glob(f"{V3.stem}_u8.ok.*"):
        f.unlink()
    wrapped = OnnxSrEngine(V3, 4, io=IO_V3, u8_wrap=True)
    wrapped.load()
    assert wrapped.u8_wrapped is True
    assert wrapped._u8_sess.get_inputs()[0].type == "tensor(uint8)"
    out = wrapped.process(np.zeros((96, 128, 3), np.uint8))
    assert out.shape == (384, 512, 3) and out.dtype == np.uint8


def test_wrap_failure_falls_back(tmp_path, monkeypatch):
    """图手术失败（如 onnx 图不兼容）：静默回退原路径，功能不受影响。"""
    pytest.importorskip("onnx")
    import sv.engines.u8_wrap as u8w

    def boom(*a, **k):
        raise RuntimeError("mock: 手术失败")

    monkeypatch.setattr(u8w, "wrap_u8", boom)
    tiny = tmp_path / "tiny_fb.onnx"
    _build_tiny_model(tiny)
    io = {"color": "rgb", "range": "0-1"}
    plain = OnnxSrEngine(tiny, 2, io=io, u8_wrap=False)
    plain.load()
    fb = OnnxSrEngine(tiny, 2, io=io, u8_wrap=True)
    fb.load()
    assert fb.u8_wrapped is False, "失败必须回退"
    rng = np.random.default_rng(5)
    f = rng.integers(0, 256, (64, 66, 3), dtype=np.uint8)
    _assert_close(plain.process(f), fb.process(f))


def test_batch_engine_not_wrapped():
    """batch>1 引擎不包装（走原 session 路径，避免线程池批量死锁前科）。"""
    plain = OnnxSrEngine(V3, 4, io=IO_V3, batch=4, u8_wrap=False)
    plain.load()
    wrapped = OnnxSrEngine(V3, 4, io=IO_V3, batch=4, u8_wrap=True)
    wrapped.load()
    assert wrapped.u8_wrapped is False
    rng = np.random.default_rng(6)
    frames = np.stack([rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
                       for _ in range(4)])
    _assert_close(plain.process_batch(frames), wrapped.process_batch(frames))


@pytest.fixture(scope="module", autouse=True)
def _bench_tmp():
    TEMP_DIR.mkdir(exist_ok=True)
    yield

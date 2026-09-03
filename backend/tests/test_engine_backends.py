"""推理引擎后端（项2/项1 支撑）：原生 fp16 ONNX 支持 + TensorRT provider 回退链。"""
import sys
import types

import numpy as np
import pytest

from sv.engines.onnx_engine import OnnxSrEngine


def test_native_fp16_model(tmp_path):
    """AnimeJaNai 类 fp16 本体模型（IO 也是 float16）：输入自动转 fp16，输出回 float32 语义。"""
    pytest.importorskip("onnx")
    import onnx
    from onnx import TensorProto, helper

    # 最小 Identity 图，IO 均为 FLOAT16（模拟 AnimeJaNai 原生导出）
    node = helper.make_node("Identity", ["input"], ["output"])
    graph = helper.make_graph(
        [node], "g",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT16, ["n", 3, "h", "w"])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT16, ["n", 3, "h", "w"])],
    )
    m16 = tmp_path / "native_fp16.onnx"
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    onnx.save(model, str(m16))

    eng = OnnxSrEngine(m16, 1, io={"color": "rgb", "range": "0-1"}, device="cpu")
    eng.load()
    assert eng._in_fp16 is True
    out = eng.process(np.full((32, 32, 3), 128, dtype=np.uint8))
    assert out.shape == (32, 32, 3)
    assert np.allclose(out, 127)  # Identity + fp16 舍入：128 -> 0.50196 -> 127.99 -> 127


class _FakeIn:
    name = "input"
    type = "tensor(float)"
    shape = ["b", 3, "h", "w"]


class _FakeOut:
    name = "output"


class _FakeSession:
    """TRT 在 providers 里且模拟失败时抛错，其余成功。"""

    fail_trt = True
    calls: list = []

    def __init__(self, model_path, so, providers=None):
        names = [p if isinstance(p, str) else p[0] for p in (providers or [])]
        self._names = names
        _FakeSession.calls.append(names)
        if _FakeSession.fail_trt and "TensorrtExecutionProvider" in names:
            raise RuntimeError("mock: TensorRT libraries not found")

    def get_providers(self):
        return self._names

    def get_inputs(self):
        return [_FakeIn()]

    def get_outputs(self):
        return [_FakeOut()]


def test_trt_fallback_chain(monkeypatch):
    """device=trt：TRT 建链失败 → 去 TRT 重试（CUDA/DML 继续），不直接死。"""
    fake = types.SimpleNamespace(
        get_available_providers=lambda: [
            "TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
        SessionOptions=lambda: types.SimpleNamespace(),
        GraphOptimizationLevel=types.SimpleNamespace(
            ORT_ENABLE_BASIC=1, ORT_DISABLE_ALL=0),
        InferenceSession=_FakeSession,
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    _FakeSession.calls = []
    eng = OnnxSrEngine("whatever.onnx", 2, device="trt")
    eng.load()
    assert _FakeSession.calls[0][0] == "TensorrtExecutionProvider"
    assert _FakeSession.calls[1] == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    assert eng.provider_used == ["CUDAExecutionProvider", "CPUExecutionProvider"]


def test_probe_model_picks_existing_bundled(monkeypatch, tmp_path):
    """CUDA/TRT 探测的校验模型必须动态取 bundled 现存文件。

    回归：探测曾硬编码 RealESR-AnimeVideo-v3_x4.onnx，内置集换血 V3.1 后
    文件不再随包，engine=trt/cuda 在安装版必报「缺少校验用模型」恒回落
    DirectML（v0.4.0+ 实锤）。"""
    import sv.server.engine_select as es

    m = es._probe_model()
    assert m is not None and m.suffix == ".onnx" and m.exists()

    # bundled 空目录 → None（探测层报「缺少校验用模型」而非拿死路径）
    monkeypatch.setattr(es, "BUNDLED_DIR", tmp_path)
    assert es._probe_model() is None
    (tmp_path / "w.onnx").write_bytes(b"x")
    assert es._probe_model() == tmp_path / "w.onnx"


def test_trt_provider_options_attached(monkeypatch):
    """TRT 成功路径：provider 以 (name, options) 元组传入，含引擎缓存开关。"""
    _FakeSession.fail_trt = False
    try:
        fake = types.SimpleNamespace(
            get_available_providers=lambda: [
                "TensorrtExecutionProvider", "CUDAExecutionProvider"],
            SessionOptions=lambda: types.SimpleNamespace(),
            GraphOptimizationLevel=types.SimpleNamespace(
                ORT_ENABLE_BASIC=1, ORT_DISABLE_ALL=0),
            InferenceSession=_FakeSession,
        )
        monkeypatch.setitem(sys.modules, "onnxruntime", fake)
        _FakeSession.calls = []
        eng = OnnxSrEngine("whatever.onnx", 2, device="trt")
        eng.load()
        first = _FakeSession.calls[0]
        assert len(_FakeSession.calls) == 1  # 一次成功，无回退
        # 捕获的 providers 是原始参数（含元组）——从引擎侧重验
        from sv.engines.onnx_engine import _trt_provider_options

        name, opts = _trt_provider_options()[0]
        assert name == "TensorrtExecutionProvider"
        assert opts["trt_engine_cache_enable"] is True
        assert opts["trt_fp16_enable"] is True
    finally:
        _FakeSession.fail_trt = True

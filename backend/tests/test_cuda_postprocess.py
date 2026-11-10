"""Independent output conversion must preserve the existing quantization contract."""
from types import SimpleNamespace

import numpy as np
import pytest

from sv.engines import cuda_postprocess
from sv.engines.onnx_engine import OnnxSrEngine


@pytest.mark.parametrize("dtype", [np.float32, np.float16])
@pytest.mark.parametrize("color,normalized,affine", [
    ("rgb", True, None), ("bgr", False, None), ("rgb", True, (0.7, 0.15)),
])
def test_output_graph_matches_reference_math(monkeypatch, dtype, color, normalized, affine):
    import onnxruntime as ort

    # Execute the actual graph on CPU in CI. GPU availability/placement is
    # checked separately; this covers dtype conversion, clipping and channels.
    original = ort.InferenceSession

    def cpu_session(model, options, providers):
        assert providers[0] == "CUDAExecutionProvider"
        session = original(model, options, providers=["CPUExecutionProvider"])
        session.get_providers = lambda: ["CUDAExecutionProvider"]
        return session

    monkeypatch.setattr(ort, "InferenceSession", cpu_session)
    kind = "tensor(float16)" if dtype == np.float16 else "tensor(float)"
    session = cuda_postprocess.create_session(ort, kind, color=color,
                                             range_01=normalized, affine=affine)
    raw = np.random.default_rng(8).uniform(-0.2, 1.2, (1, 3, 13, 17)).astype(dtype)
    if not normalized:
        raw *= 255
    expected = raw[0].astype(np.float32)
    if affine:
        expected -= affine[1]
        expected /= affine[0]
    if normalized:
        expected *= 255
    expected = np.clip(expected, 0, 255).transpose(1, 2, 0)
    if color == "bgr":
        expected = expected[..., ::-1]
    expected = expected.astype(np.uint8)
    actual = session.run(None, {"raw": raw})[0][0]
    np.testing.assert_array_equal(actual, expected)


def test_missing_cuda_is_rejected(monkeypatch):
    import onnxruntime as ort

    monkeypatch.setattr(ort, "InferenceSession", lambda *a, **k:
                        SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"]))
    with pytest.raises(RuntimeError, match="未启用 GPU"):
        cuda_postprocess.create_session(ort, "tensor(float)", color="rgb", range_01=True)


@pytest.mark.parametrize("mismatch", [False, True])
def test_validation_enables_only_matching_outputs(monkeypatch, capsys, mismatch):
    engine = OnnxSrEngine("unused.onnx", 2, io={"color": "rgb", "range": "0-1"},
                          device="trt", tile=32, validate_hw=(99, 101))
    original = SimpleNamespace(get_outputs=lambda: [SimpleNamespace(type="tensor(float)")])
    candidate = object()
    engine.session = original
    monkeypatch.setattr(cuda_postprocess, "create_session", lambda *a, **kw: candidate)
    seen = []

    def reference(frame):
        seen.append(frame.shape[:2])
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)

    def output(frame, session):
        assert session is candidate
        result = np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)
        if mismatch:
            result[0, 0] = 100
        return result

    monkeypatch.setattr(engine, "_infer_plain", reference)
    monkeypatch.setattr(engine, "_infer_cuda_post", output)
    engine._try_cuda_postprocess()
    assert engine.session is original
    assert engine.cuda_postprocessed is not mismatch
    assert engine._cuda_post_sess is (None if mismatch else candidate)
    if mismatch:
        assert "maxdiff=100" in capsys.readouterr().out
    else:
        assert seen == [(32, 32), (32, 32), (31, 31)]
        engine._infer(np.zeros((7, 9, 3), np.uint8))


def test_binding_keeps_float_output_on_device():
    raw = object()
    actions = []
    pixels = np.zeros((1, 12, 16, 3), np.uint8)

    class Binding:
        def bind_cpu_input(self, name, tensor):
            actions.append(("input", name, tensor.shape))

        def bind_output(self, name, device):
            actions.append(("output", name, device))

        def get_outputs(self):
            return [raw]

        def bind_ortvalue_input(self, name, value):
            assert value is raw
            actions.append(("device_input", name))

        def copy_outputs_to_cpu(self):
            actions.append(("copy_pixels",))
            return [pixels]

    model = SimpleNamespace(io_binding=Binding, run_with_iobinding=lambda b: actions.append(("model",)))
    post = SimpleNamespace(io_binding=Binding, run_with_iobinding=lambda b: actions.append(("post",)))
    output = cuda_postprocess.run(model, "x", "y", np.zeros((1, 3, 6, 8), np.float32), post)
    assert output.shape == (12, 16, 3)
    assert actions == [("input", "x", (1, 3, 6, 8)), ("output", "y", "cuda"),
                       ("model",), ("device_input", "raw"), ("output", "pixels", "cuda"),
                       ("post",), ("copy_pixels",)]


def test_fixed_shape_probe_clamped_to_model_input(monkeypatch):
    """定长导出（DAT2 256×256）的探针必须夹到模型输入上限，整页 validate_hw 会超限被拒。"""
    engine = OnnxSrEngine("unused.onnx", 4, io={"color": "rgb", "range": "0-1"},
                          device="trt", validate_hw=(1816, 1280))
    engine.fixed_hw = (256, 256)
    engine.session = SimpleNamespace(get_outputs=lambda: [SimpleNamespace(type="tensor(float)")])
    candidate = object()
    monkeypatch.setattr(cuda_postprocess, "create_session", lambda *a, **kw: candidate)
    seen = []

    def reference(frame):
        seen.append(frame.shape[:2])
        return np.zeros((frame.shape[0] * 4, frame.shape[1] * 4, 3), np.uint8)

    def output(frame, session):
        assert session is candidate
        return np.zeros((frame.shape[0] * 4, frame.shape[1] * 4, 3), np.uint8)

    monkeypatch.setattr(engine, "_infer_plain", reference)
    monkeypatch.setattr(engine, "_infer_cuda_post", output)
    engine._try_cuda_postprocess()
    assert engine.cuda_postprocessed
    assert seen and all(h <= 256 and w <= 256 for h, w in seen)


def test_cuda_path_not_gated_by_u8_wrap_flag(monkeypatch):
    """manifest u8_wrap=false（DAT2 现状）不得连带跳过 CUDA 后处理。"""
    engine = OnnxSrEngine("unused.onnx", 4, io={"cuda_postprocess": True}, device="trt")
    engine.u8_wrap_enabled = False
    engine.provider_used = ["TensorrtExecutionProvider"]
    calls = []

    def cuda():
        calls.append("cuda")
        engine.cuda_postprocessed = True

    monkeypatch.setattr(engine, "_try_cuda_postprocess", cuda)
    monkeypatch.setattr(engine, "_try_u8_wrap", lambda: calls.append("wrap"))
    engine._setup_gpu_paths({"CUDAExecutionProvider", "TensorrtExecutionProvider"})
    assert calls == ["cuda"]


def test_cuda_failure_falls_back_to_wrap_when_allowed(monkeypatch):
    """CUDA 对拍失败且包装未被 manifest 关断时，兜底尝试包装。"""
    engine = OnnxSrEngine("unused.onnx", 4, io={"cuda_postprocess": True}, device="trt")
    engine.u8_wrap_enabled = True
    engine.provider_used = ["TensorrtExecutionProvider"]
    calls = []
    monkeypatch.setattr(engine, "_try_cuda_postprocess", lambda: calls.append("cuda"))
    monkeypatch.setattr(engine, "_try_u8_wrap", lambda: calls.append("wrap"))
    engine._setup_gpu_paths({"CUDAExecutionProvider", "TensorrtExecutionProvider"})
    assert calls == ["cuda", "wrap"]

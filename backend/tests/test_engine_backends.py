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


def test_trt_cpu_chain_skips_cuda(monkeypatch):
    """device=trt_cpu：TRT 主链保留、CUDA EP 被剔除（TRT 编不了的节点落 CPU）。

    MangaJaNai 系 MxNet 导出的 Resize 在 CUDA kernel 有 fast_divmod 断言
    崩溃缺陷（TRT 回退 CUDA 即触发），换链避开而不放弃 TRT 主图。"""
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
    _FakeSession.fail_trt = False  # TRT 本身可用，链按声明生效
    eng = OnnxSrEngine("whatever.onnx", 2, device="trt_cpu")
    eng.load()
    assert _FakeSession.calls[0] == ["TensorrtExecutionProvider", "CPUExecutionProvider"]
    _FakeSession.fail_trt = True  # 恢复默认（同文件其他测试依赖它模拟 TRT 失败）


def test_worker_falls_back_on_cuda_kernel_fault(monkeypatch, tmp_path):
    """worker 加载遇 CUDA 内核崩溃（非显存）：engine=trt 自动换 TRT+CPU 链重试。

    回归：MangaJaNai 在 TRT/CUDA 后端 warmup 即崩（Resize fast_divmod 断言），
    任务直接失败；换链后主图仍 TRT、坏节点落 CPU。"""
    from sv.server import worker
    from sv.server import worker_engine

    calls: list[str] = []

    class _BoomThenOk:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None):
            calls.append(device)
            if len(calls) == 1:
                raise RuntimeError(
                    "[ONNXRuntimeError] : 6 : RUNTIME_EXCEPTION : Resize node. "
                    "E:\_work\1\s\onnxruntime\core/providers/cuda/shared_inc/"
                    "fast_divmod.h:50 onnxruntime::cuda::DivMod<int>::DivMod "
                    "d_ >= 1 was false.")

        def load(self):
            pass

        def process(self, frame):
            return frame

    spec = types.SimpleNamespace(io={}, fp16=False, id="mangajanai")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _BoomThenOk)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "trt"}))
    eng, prec = worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 2, None, "fp32", 0, (64, 64), log=lambda ev: None)
    assert calls == ["trt", "trt_cpu"], f"应自动换 TRT+CPU 链重试: {calls}"
    assert prec == "fp32"


def test_worker_cuda_engine_fault_falls_to_cpu(monkeypatch, tmp_path):
    """engine=cuda 场景同一崩溃：无 GPU 替代链，退 CPU 保出片。"""
    from sv.server import worker
    from sv.server import worker_engine

    calls: list[str] = []

    class _BoomThenOk:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None):
            calls.append(device)
            if len(calls) == 1:
                raise RuntimeError(
                    "RUNTIME_EXCEPTION providers/cuda/shared_inc/fast_divmod.h:50 "
                    "DivMod d_ >= 1 was false.")

        def load(self):
            pass

        def process(self, frame):
            return frame

    spec = types.SimpleNamespace(io={}, fp16=False, id="mangajanai")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _BoomThenOk)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "cuda"}))
    eng, prec = worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 2, None, "fp32", 0, (64, 64), log=lambda ev: None)
    # engine=cuda 映射的 ort_device 是 auto（provider 链自动含 CUDA），降级即 cpu
    assert calls == ["auto", "cpu"], f"应退 CPU 重试: {calls}"


def test_worker_unicode_masked_oom_falls_back_tile(monkeypatch, tmp_path):
    """中文 Windows 上 DML 显存不足被 UnicodeDecodeError 掩盖（ORT 错误消息
    bytes 内嵌系统 GBK 描述，Python 绑定层 utf-8 硬解失败）——_oom 必须能从
    e.args 的原始 bytes 识别 0x8007000E，降 tile 重试不被假象骗过直接失败。"""
    from sv.server import worker
    from sv.server import worker_engine

    calls: list[tuple[str, int]] = []

    class _OomThenOk:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None):
            calls.append((device, tile))
            if len(calls) == 1:
                # 现场形态：args[1] 是含 GBK 错误描述的原始 bytes
                raise UnicodeDecodeError(
                    "utf-8",
                    b"[ONNXRuntimeError] : 6 : RUNTIME_EXCEPTION ... 8007000E "
                    b"\xc7\xf3\xb4\xe6\xb4\xe6\xb4\xa6\xb2\xbb\xd7\xe3\xa1\xad",
                    240, 241, "invalid continuation byte")

        def load(self):
            pass

        def process(self, frame):
            return frame

    spec = types.SimpleNamespace(io={}, fp16=False, id="mangajanai")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _OomThenOk)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "auto"}))
    eng, prec = worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 4, None, "fp32", 0, (2133, 1510), log=lambda ev: None)
    assert [c[1] for c in calls] == [512, 256], \
        "先预设 512（大图预算），UnicodeDecodeError 形态 OOM 再降至 256 重试"


def test_worker_large_output_preset_tile(monkeypatch, tmp_path):
    """大图输出像素超预算（2133p x4≈51M > 36M）自动预设 512 分块：
    干净会话一次到位，避免全尺寸 OOM 后同进程 DML 损坏只能退 CPU。"""
    from sv.server import worker
    from sv.server import worker_engine

    calls: list[int] = []

    class _Ok:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None):
            calls.append(tile)

        def load(self):
            pass

        def process(self, frame):
            return frame

    spec = types.SimpleNamespace(io={}, fp16=False, id="x")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _Ok)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "auto"}))
    # 大图：预设 512；1080p x4（33M）不动保持全尺寸性能
    worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 4, None, "fp32", 0, (2133, 1510), log=lambda ev: None)
    assert calls == [512]
    calls.clear()
    worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 4, None, "fp32", 0, (1080, 1920), log=lambda ev: None)
    assert calls == [0]

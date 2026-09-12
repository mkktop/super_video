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


# ---- TRT 混合精度输出损坏（DAT2 黑图）：manifest 关 fp16 + 灰图探测降链 ----

def test_trt_fp16_flag_parsed_and_cache_split():
    """io.trt_fp16=false 落到引擎开关；显式参数可覆盖（降链重建用）。"""
    eng = OnnxSrEngine("w.onnx", 4, io={"color": "rgb", "range": "0-1", "trt_fp16": False})
    assert eng.trt_fp16 is False
    assert OnnxSrEngine("w.onnx", 4, io={"color": "rgb", "range": "0-1"}).trt_fp16 is True
    # 显式参数优先于 io 声明
    assert OnnxSrEngine("w.onnx", 4, io={"trt_fp16": True}, trt_fp16=False).trt_fp16 is False

    from sv.engines.onnx_engine import _trt_provider_options

    _, opts_on = _trt_provider_options(True)[0]
    _, opts_off = _trt_provider_options(False)[0]
    assert opts_on["trt_fp16_enable"] is True and opts_off["trt_fp16_enable"] is False
    # workspace ≥10GB：全图大图 RRDB 的 builder 需求超 4GB 会 EP_FAIL 静默回落
    # CUDA（891x1280 实测 29s/张 vs TRT 0.4s）
    assert opts_on["trt_max_workspace_size"] == 10 * 1024**3
    # 精度分家：坏 fp16 引擎缓存绝不与 fp32 链同目录（缓存键不保证区分精度开关）
    assert opts_on["trt_engine_cache_path"] != opts_off["trt_engine_cache_path"]
    assert opts_off["trt_engine_cache_path"].endswith("trt_cache_fp32")


def test_dat2_manifest_declares_trt_fp32():
    """illustrationjanai-4x-dat2（transformer）manifest 必须声明 trt_fp16=false：
    TRT fp16 实测 100% NaN→纯黑（v0.5.0 用户 53 张黑图），声明缺失即回归。"""
    from sv.models.registry import get_model

    io = get_model("illustrationjanai-4x-dat2").io
    assert io.get("trt_fp16") is False


def test_worker_trt_garbage_disables_trt_fp16(monkeypatch, tmp_path):
    """灰图探测发现 TRT fp16 输出全黑：同链改 fp32 精度重建（DAT2 实测恢复）。"""
    from sv.server import worker
    from sv.server import worker_engine

    calls: list[tuple[str, bool | None]] = []

    class _BlackOnFp16:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None, trt_fp16=None):
            calls.append((device, trt_fp16))
            self.fp16 = trt_fp16 is None or trt_fp16  # None=默认开

        def load(self):
            pass

        def process(self, frame):
            import numpy as np

            return np.zeros_like(frame) if self.fp16 else frame

    spec = types.SimpleNamespace(io={}, fp16=False, id="illustrationjanai-4x-dat2")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _BlackOnFp16)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "trt"}))
    eng, prec = worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 4, None, "fp32", 256, (64, 64), log=lambda ev: None)
    assert calls == [("trt", None), ("trt", False)], \
        f"应先 TRT fp16 探测失败、再同链关 fp16 重建: {calls}"


def test_worker_garbage_falls_through_to_cpu(monkeypatch, tmp_path):
    """降链全黑到底：512 分块 → fp32 原件（无）→ TRT 关 fp16 → auto → CPU，
    逐级换变量保出片。"""
    from sv.server import worker
    from sv.server import worker_engine

    calls: list[tuple[str, int]] = []

    class _BlackUntilCpu:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None, trt_fp16=None):
            calls.append((device, tile))
            self.device = device

        def load(self):
            pass

        def process(self, frame):
            import numpy as np

            return frame if self.device == "cpu" else np.zeros_like(frame)

    spec = types.SimpleNamespace(io={}, fp16=False, id="x")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _BlackUntilCpu)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "trt"}))
    eng, prec = worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 4, None, "fp32", 0, (64, 64), log=lambda ev: None)
    assert [d for d, _ in calls] == ["trt", "trt", "trt", "auto", "cpu"], \
        f"应按 分块→关TRT-fp16→auto→cpu 逐级降链: {calls}"
    assert calls[0][1] == 0 and calls[1][1] == 512, "第一步先启用 512 分块"


def test_worker_garbage_big_image_enables_tiling(monkeypatch, tmp_path):
    """DML 大图全图数值损坏（1500x1078 实测灰雾/近黑，512 分块即救）：
    auto 后端 tile=0 时第一步降档就是启用分块，不出 GPU。"""
    from sv.server import worker
    from sv.server import worker_engine

    calls: list[int] = []

    class _BlackOnlyFullFrame:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None, trt_fp16=None):
            calls.append(tile)
            self.tile = tile

        def load(self):
            pass

        def process(self, frame):
            import numpy as np

            return frame if self.tile else np.zeros_like(frame)

    spec = types.SimpleNamespace(io={}, fp16=False, id="realesrgan-x4plus-anime")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _BlackOnlyFullFrame)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "auto"}))
    eng, prec = worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 4, None, "fp32", 0, (1078, 1500), log=lambda ev: None)
    assert calls == [0, 512], f"应只走 全图→512分块 两步: {calls}"
    assert prec == "fp32"


def test_worker_garbage_fp16_file_falls_to_fp32(monkeypatch, tmp_path):
    """转换版 fp16 权重在当前后端数值损坏：分块救不了时换 fp32 原件同后端重试，
    返回精度口径同步落为 fp32。"""
    from sv.server import worker
    from sv.server import worker_engine

    fp32_file = tmp_path / "m.onnx"
    fp32_file.write_bytes(b"x")
    (tmp_path / "m_fp16.onnx").write_bytes(b"x")
    calls: list[str] = []

    class _BlackOnFp16File:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None, trt_fp16=None):
            calls.append(weight.name)
            self.fp16_file = weight.stem.endswith("_fp16")

        def load(self):
            pass

        def process(self, frame):
            import numpy as np

            return np.zeros_like(frame) if self.fp16_file else frame

    spec = types.SimpleNamespace(io={}, fp16=True, id="x")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _BlackOnFp16File)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "auto"}))
    eng, prec = worker._load_onnx_engine(
        tmp_path / "m_fp16.onnx", spec, 4, None, "fp32", 0, (64, 64), log=lambda ev: None)
    assert calls == ["m_fp16.onnx", "m_fp16.onnx", "m.onnx"], \
        f"应先试分块、再换 fp32 原件: {calls}"
    assert prec == "fp32"


# ---- 显存不足（GBK 掩盖形态）耗尽降 tile 后的 CPU 兜底 + 错误解蔽 ----

_OOM_BYTES = (b"[ONNXRuntimeError] : 6 : RUNTIME_EXCEPTION ... 8007000E "
              b"\xc4\xe6\xb4\xe6\xd7\xca\xd4\xb4\xb2\xbb\xd7\xe3")


def test_worker_oom_exhausted_falls_to_cpu(monkeypatch, tmp_path):
    """OOM 后同进程 DML 会话损坏（降 tile 重建接着爆，2026-09-12 实测）：tile 降到底
    仍不足时回退 CPU 保出片，不再直接失败——失败任务后 runner 丢弃进程，下一单
    自然恢复 GPU（用户实测：整幅失败→手动 256 分块新进程成功）。"""
    from sv.server import worker
    from sv.server import worker_engine

    calls: list[tuple[str, int]] = []

    class _OomUntilCpu:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None, trt_fp16=None):
            calls.append((device, tile))
            self.device = device

        def load(self):
            pass

        def process(self, frame):
            if self.device != "cpu":
                raise UnicodeDecodeError("utf-8", _OOM_BYTES, 240, 1,
                                         "invalid continuation byte")
            return frame

    spec = types.SimpleNamespace(io={}, fp16=False, id="mangajanai")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _OomUntilCpu)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "auto"}))
    eng, prec = worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 2, None, "fp32", 0, (1280, 891), log=lambda ev: None)
    tiles = [t for _, t in calls]
    assert tiles == [0, 256, 128, 64, 0], f"应 降tile到底→CPU整幅: {calls}"
    assert calls[-1][0] == "cpu"


def test_worker_masked_error_decoded_on_total_failure(monkeypatch, tmp_path):
    """全链失败时错误必须解蔽：上报解码后的真实错误（含 HRESULT），不再让用户
    对着 UnicodeDecodeError 猜病因。"""
    from sv.server import worker
    from sv.server import worker_engine

    class _AlwaysOom:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None, trt_fp16=None):
            pass

        def load(self):
            pass

        def process(self, frame):
            raise UnicodeDecodeError("utf-8", _OOM_BYTES, 240, 1,
                                     "invalid continuation byte")

    spec = types.SimpleNamespace(io={}, fp16=False, id="x")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _AlwaysOom)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "auto"}))
    with pytest.raises(RuntimeError, match="8007000E"):
        worker._load_onnx_engine(
            tmp_path / "w.onnx", spec, 2, None, "fp32", 0, (64, 64), log=lambda ev: None)


# ---- GPU 设备移除（TDR/驱动重置 887A0005/6/7）：等待重试 + CPU 兜底 + 不进常驻缓存 ----

_DEV_REMOVED_MSG = ("[ONNXRuntimeError] : 1 : FAIL : ...DmlExecutionProvider\\src\\"
                    "ExecutionProvider.cpp(952)... Exception(6) tid(8e00) "
                    "887A0005 GPU 设备实例已经暂停。")

# GBK 掩盖形态：UnicodeDecodeError.args 挂原始 bytes（同 _OOM_BYTES 手法）
_DEV_REMOVED_BYTES = (b"[ONNXRuntimeError] : 1 : FAIL : ...DmlExecutionProvider ... "
                      b"887A0006 \xc9\xe8\xb1\xb8\xd2\xd1\xb4\xd3\xb7\xfe\xce\xf1")


def test_worker_dev_removed_waits_then_recovers(monkeypatch, tmp_path):
    """设备移除是环境瞬态：配置一动不动等 3s 重试，驱动恢复后原配置继续跑。"""
    from sv.server import worker
    from sv.server import worker_engine

    calls: list[tuple[str, int]] = []
    boom = {"n": 0}

    class _DevRemovedThenOk:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None, trt_fp16=None):
            calls.append((device, tile))
            self.device = device

        def load(self):
            if boom["n"] < 1:
                boom["n"] += 1
                raise RuntimeError(_DEV_REMOVED_MSG)

        def process(self, frame):
            return frame

    sleeps: list[float] = []
    monkeypatch.setattr(worker_engine.time, "sleep", lambda s: sleeps.append(s))
    spec = types.SimpleNamespace(io={}, fp16=False, id="mangajanai")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _DevRemovedThenOk)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "auto"}))
    eng, prec = worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 2, None, "fp32", 0, (64, 64), log=lambda ev: None)
    assert [d for d, _ in calls] == ["auto", "auto"], calls  # 后端/分块零改动
    assert all(t == 0 for _, t in calls)
    assert sleeps == [3.0]


def test_worker_dev_removed_persistent_falls_to_cpu(monkeypatch, tmp_path):
    """持续移除（驱动反复重置）：两轮等待后回退 CPU 保出片；瞬态病因不进
    常驻缓存——下一个任务重新从 GPU 起链，驱动恢复即回全速。"""
    from sv.server import worker
    from sv.server import worker_engine

    calls: list[tuple[str, int]] = []

    class _DevRemovedUntilCpu:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None, trt_fp16=None):
            calls.append((device, tile))
            self.device = device

        def load(self):
            if self.device != "cpu":
                raise RuntimeError(_DEV_REMOVED_MSG)

        def process(self, frame):
            return frame

    sleeps: list[float] = []
    monkeypatch.setattr(worker_engine.time, "sleep", lambda s: sleeps.append(s))
    worker_engine._ENGINE_CACHE.clear()
    spec = types.SimpleNamespace(io={}, fp16=False, id="mangajanai")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _DevRemovedUntilCpu)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "auto"}))
    eng, prec = worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 2, None, "fp32", 0, (64, 64), log=lambda ev: None)
    assert [d for d, _ in calls] == ["auto", "auto", "auto", "cpu"], calls
    assert calls[-1][1] == 0  # CPU 整幅，不带分块
    assert sleeps == [3.0, 3.0]
    assert worker_engine._ENGINE_CACHE == {}  # 兜底引擎未缓存


def test_worker_dev_removed_masked_bytes_detected(monkeypatch, tmp_path):
    """中文系统 GBK 掩盖形态（UnicodeDecodeError.args 带 bytes）：设备移除
    同样被识别并走等待重试。"""
    from sv.server import worker
    from sv.server import worker_engine

    calls: list[str] = []
    boom = {"n": 0}

    class _MaskedDevRemoved:
        def __init__(self, weight, scale, io=None, tile=0, batch=1,
                     device="auto", validate_hw=None, trt_fp16=None):
            calls.append(device)
            self.device = device

        def load(self):
            pass

        def process(self, frame):
            if boom["n"] < 2:
                boom["n"] += 1
                raise UnicodeDecodeError("utf-8", _DEV_REMOVED_BYTES, 240, 1,
                                         "invalid continuation byte")
            return frame

    sleeps: list[float] = []
    monkeypatch.setattr(worker_engine.time, "sleep", lambda s: sleeps.append(s))
    spec = types.SimpleNamespace(io={}, fp16=False, id="x")
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _MaskedDevRemoved)
    monkeypatch.setattr(worker_engine, "settings",
                        types.SimpleNamespace(load=lambda: {"engine": "auto"}))
    eng, prec = worker._load_onnx_engine(
        tmp_path / "w.onnx", spec, 2, None, "fp32", 0, (64, 64), log=lambda ev: None)
    assert calls == ["auto", "auto", "auto"], calls
    assert sleeps == [3.0, 3.0]

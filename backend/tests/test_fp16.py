"""FP16 集成测试：精度选择 / 缺失回退 / 自动转换 / 引擎跑通。"""
import os
from pathlib import Path

import pytest

from sv.models.fp16 import ensure_fp16, ensure_fp16_file, fp16_path
from sv.models.registry import BUNDLED_DIR, ModelSpec, model_file
from sv.paths import TEMP_DIR
from sv.server import settings

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_fp16.db"))


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    """precision 写盘测试指向临时文件，不碰开发机真实设置。"""
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")


def _toy_fp32_onnx(path: Path, ch: int = 64) -> Path:
    """fp32 玩具卷积模型：fp16 转换链路的测试输入。

    内置权重已换成 AnimeJaNai V3.1（原生 fp16），仓库不再随包 fp32 权重，
    转换语义用玩具模型覆盖——权重刻意做大（~14KB），fp16 化后体积约减半的
    断言才不被 protobuf 固定开销淹没。
    """
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    rng = np.random.default_rng(3).standard_normal
    inits = [
        numpy_helper.from_array(rng((ch, 3, 3, 3)).astype(np.float32), "w1"),
        numpy_helper.from_array(rng((3, ch, 3, 3)).astype(np.float32), "w2"),
    ]
    nodes = [
        helper.make_node("Conv", ["input", "w1"], ["c1"], pads=[1, 1, 1, 1]),
        helper.make_node("Conv", ["c1", "w2"], ["output"], pads=[1, 1, 1, 1]),
    ]
    graph = helper.make_graph(
        nodes, "toy",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 64, 64])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, 64, 64])],
        inits,
    )
    onnx.save(helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)]), str(path))
    return path


def test_bundled_weights_shipped():
    """内置权重随包分发：AnimeJaNai V3.1 HD 四件（原生 fp16，无需 fp16 变体）。"""
    from sv.models.registry import load_registry

    for mid in (
        "animejanai-v31-hd-performance", "animejanai-v31-hd-performance-sharp",
        "animejanai-v31-hd-balanced", "animejanai-v31-hd-balanced-sharp",
    ):
        spec = load_registry()[mid]
        assert spec.fp16 is False, f"{mid} 应声明原生 fp16（不再转换）"
        for f in spec.files:
            assert (BUNDLED_DIR / f["name"]).exists(), f"{f['name']} 未随包"


def _fake_spec(id_: str = "fake-x2") -> ModelSpec:
    return ModelSpec(
        id=id_, name="fake", engine="onnx", scale=[2], content=[],
        speed="fast", vram_gb=1, files=[{"name": "m.onnx"}],
    )


def test_model_file_precision_pick(tmp_path, monkeypatch):
    """worker 真实调用链：model_file(precision) + 缺失时惰性补转（fp32 落 store）。"""
    import sv.models.registry as reg

    spec = _fake_spec("fake-pick")
    store = tmp_path / "models" / spec.id
    store.mkdir(parents=True)
    _toy_fp32_onnx(store / "m.onnx")
    monkeypatch.setattr(reg, "MODELS_DIR", tmp_path / "models")

    p32 = model_file(spec, 2)
    weight = ensure_fp16_file(model_file(spec, 2, "fp16"))
    assert weight.stem.endswith("_fp16") and weight.exists()
    assert weight.parent == p32.parent
    assert not p32.stem.endswith("_fp16")  # fp32 请求不受影响


def test_store_overrides_bundled(monkeypatch, tmp_path):
    """models_store 的 fp32 优先于 bundled；fp16 兄弟缺失时回退 fp32。"""
    import sv.models.registry as reg

    spec = _fake_spec()
    store = tmp_path / "models" / spec.id
    store.mkdir(parents=True)
    _toy_fp32_onnx(store / "m.onnx")
    monkeypatch.setattr(reg, "MODELS_DIR", tmp_path / "models")

    base = reg.model_file(spec, 2)
    assert base == store / "m.onnx"
    assert reg.model_file(spec, 2, "fp16") == base  # 无 fp16 兄弟 -> 回退

    made = ensure_fp16(spec)
    assert made == [fp16_path(base)] and fp16_path(base).exists()
    assert fp16_path(base).stat().st_size < base.stat().st_size  # 权重减半
    assert reg.model_file(spec, 2, "fp16") == fp16_path(base)


def test_settings_precision_validation():
    assert settings.DEFAULTS["precision"] == "fp16"
    with pytest.raises(ValueError):
        settings.save({"precision": "int8"})
    data = settings.save({"precision": "fp32"})
    assert data["precision"] == "fp32"
    settings.save({"precision": "fp16"})  # 还原默认


def test_engine_fp16_run():
    """内置 V3.1（原生 fp16）真实跑一帧：输出形状/数值正常。"""
    from sv.engines.onnx_engine import OnnxSrEngine
    from sv.models.registry import get_model

    spec = get_model("animejanai-v31-hd-balanced")
    eng = OnnxSrEngine(model_file(spec, 2), 2, io=spec.io)
    eng.load()
    import numpy as np

    frame = np.random.default_rng(7).integers(0, 256, (64, 64, 3), dtype=np.uint8)
    out = eng.process(frame)
    assert out.shape == (128, 128, 3) and out.dtype == np.uint8
    assert int(out.std()) > 5  # 非全黑/全白

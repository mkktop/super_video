"""FP16 集成测试：精度选择 / 缺失回退 / 自动转换 / 引擎跑通。"""
import os
import shutil
from pathlib import Path

import pytest

from sv.models.fp16 import ensure_fp16, ensure_fp16_file, fp16_path
from sv.models.registry import BUNDLED_DIR, ModelSpec, model_file
from sv.paths import TEMP_DIR
from sv.server import settings

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_fp16.db"))

BASE = BUNDLED_DIR / "RealESRGANv2-animevideo-xsx2.onnx"


def test_bundled_fp16_shipped():
    """bundled 两模型的 fp16 变体随仓库分发，选择时优先命中。"""
    for name in ("RealESRGANv2-animevideo-xsx2.onnx", "RealESR-AnimeVideo-v3_x4.onnx"):
        assert fp16_path(BUNDLED_DIR / name).exists(), f"{name} 缺 fp16 变体"


def test_model_file_precision_pick():
    """worker 真实调用链：model_file(precision) + 缺失时惰性补转。"""
    p32 = model_file(_spec_animevideo(), 4)
    weight = ensure_fp16_file(model_file(_spec_animevideo(), 4, "fp16"))
    assert weight.stem.endswith("_fp16") and weight.exists()
    assert weight.parent == p32.parent
    assert not p32.stem.endswith("_fp16")  # fp32 请求不受影响


def _spec_animevideo():
    from sv.models.registry import get_model

    return get_model("realesr-animevideov3")


def test_store_overrides_bundled(monkeypatch, tmp_path):
    """models_store 的 fp32 优先于 bundled；fp16 兄弟缺失时回退 fp32。"""
    import sv.models.registry as reg

    spec = ModelSpec(
        id="fake-x2", name="fake", engine="onnx", scale=[2], content=[],
        speed="fast", vram_gb=1, files=[{"name": "m.onnx"}],
    )
    store = tmp_path / "models" / spec.id
    store.mkdir(parents=True)
    shutil.copy(BASE, store / "m.onnx")
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
    """fp16 权重在 DML 上真实跑一帧：输出形状/数值正常。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    spec = _spec_animevideo()
    eng = OnnxSrEngine(
        model_file(spec, 2, "fp16"), 2,
        io={"color": "bgr", "range": "0-1", "batch_hint": 1},
    )
    eng.load()
    import numpy as np

    frame = np.random.default_rng(7).integers(0, 256, (64, 64, 3), dtype=np.uint8)
    out = eng.process(frame)
    assert out.shape == (128, 128, 3) and out.dtype == np.uint8
    assert int(out.std()) > 5  # 非全黑/全白

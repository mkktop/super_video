"""批处理正确性：批量推理与逐帧推理输出一致（允许浮点归约级别的微小差异）。"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from sv.engines.onnx_engine import OnnxSrEngine
from sv.models.registry import BUNDLED_DIR

MODEL = BUNDLED_DIR / "RealESR-AnimeVideo-v3_x4.onnx"
IO = {"color": "bgr", "range": "0-1"}


@pytest.mark.skipif(not MODEL.exists(), reason="bundled 模型缺失")
@pytest.mark.parametrize("scale", [2, 4])
def test_batch_matches_single(scale):
    model = BUNDLED_DIR / (
        "RealESRGANv2-animevideo-xsx2.onnx" if scale == 2
        else "RealESR-AnimeVideo-v3_x4.onnx"
    )
    if not model.exists():
        pytest.skip("缺少该倍率模型")
    single = OnnxSrEngine(model, scale, io=IO, batch=1)
    single.load()
    batched = OnnxSrEngine(model, scale, io=IO, batch=4)
    batched.load()
    if batched.batch != 4:
        pytest.skip(f"该导出版 batch 轴固定({batched.max_batch})，无法批处理")

    rng = np.random.default_rng(7)
    frames = rng.integers(0, 256, (6, 68, 118, 3), dtype=np.uint8)  # 含奇数尺寸触发补边
    out_single = np.stack([single.process(f) for f in frames])
    out_batch = batched.process_batch(frames)

    assert out_batch.shape == (
        6, frames.shape[1] * scale, frames.shape[2] * scale, 3
    )
    diff = np.abs(out_single.astype(np.int16) - out_batch.astype(np.int16))
    assert diff.max() <= 2, f"批量与单帧输出偏差过大: max={diff.max()}"

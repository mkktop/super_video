"""批处理正确性：批量推理与逐帧推理输出一致（允许浮点归约级别的微小差异）。

专测 RealESR AnimeVideo 系（导出时 batch 轴动态）。该系列已改为下载模型
（内置换成 AnimeJaNai V3.1，batch 轴固定 1 测不了批量路径），权重从
models_store 解析——本地下载过 realesr-animevideov3 才有条件跑，否则跳过。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from sv.engines.onnx_engine import OnnxSrEngine
from sv.models.registry import model_dir

_NAMES = {2: "RealESRGANv2-animevideo-xsx2.onnx", 4: "RealESR-AnimeVideo-v3_x4.onnx"}
IO = {"color": "bgr", "range": "0-1"}


def _weight(scale: int) -> Path:
    return model_dir("realesr-animevideov3") / _NAMES[scale]


@pytest.mark.parametrize("scale", [2, 4])
def test_batch_matches_single(scale):
    model = _weight(scale)
    if not model.exists():
        pytest.skip(f"models_store 缺 {_NAMES[scale]}（下载 realesr-animevideov3 后可跑）")
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

"""低复制前后处理须与原来的浮点舍入、颜色、裁剪路径逐像素一致。"""
from types import SimpleNamespace

import numpy as np
import pytest

from sv.engines.onnx_engine import OnnxSrEngine


@pytest.mark.parametrize("fp16", [False, True])
@pytest.mark.parametrize("color", ["rgb", "bgr"])
@pytest.mark.parametrize("value_range,affine", [("0-255", None), ("0-1", None), ("0-1", (0.7, 0.15))])
@pytest.mark.parametrize("fixed", [False, True])
def test_prepost_preserves_reference_pixels_and_inputs(fp16, color, value_range, affine, fixed):
    rng = np.random.default_rng(11)
    frame = rng.integers(0, 256, (9, 13, 3), dtype=np.uint8)[::2, ::2]
    original = frame.copy()
    h, w = frame.shape[:2]
    eng = OnnxSrEngine("fake.onnx", 2, io={"color": color, "range": value_range, "pad": 4})
    eng.affine = affine
    eng._in_fp16 = fp16
    eng._in_name = "input"
    eng._out_names = ["output"]
    if fixed:
        eng.fixed_hw = (8, 12)
        ph, pw = 8 - h, 12 - w
    else:
        ph, pw = (4 - h % 4) % 4, (4 - w % 4) % 4
    x = np.pad(frame, ((0, ph), (0, pw), (0, 0)), mode="edge")
    if color == "bgr":
        x = x[..., ::-1]
    x = np.ascontiguousarray(x.transpose(2, 0, 1)[None].astype(np.float32))
    if value_range == "0-1":
        x = x / 255.0
    if affine:
        a, b = affine
        x = x * a + b
    if fp16:
        x = x.astype(np.float16)
    dtype = np.float16 if fp16 else np.float32
    raw = rng.normal(0.5 if value_range == "0-1" else 128, 0.6 if value_range == "0-1" else 150,
                     (1, 3, (h + ph) * 2, (w + pw) * 2)).astype(dtype)
    raw_before = raw.copy()
    def run(names, inputs):
        assert inputs["input"].flags.c_contiguous
        np.testing.assert_array_equal(inputs["input"], x)
        return [raw]
    eng.session = SimpleNamespace(run=run)
    y = raw[0].transpose(1, 2, 0).astype(np.float32)
    if affine:
        a, b = affine
        y = (y - b) / a
    if value_range == "0-1":
        y = y * 255.0
    y = np.clip(y, 0, 255).astype(np.uint8)
    if color == "bgr":
        y = y[..., ::-1]
    expected = np.ascontiguousarray(y[:h * 2, :w * 2])
    actual = eng._infer_plain(frame)
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(frame, original)
    np.testing.assert_array_equal(raw, raw_before)
    assert actual.flags.c_contiguous


"""worker GPU 直连链门控单测（纯逻辑，CPU 可跑）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sv.pipeline.stream_gpu import gpu_chain_gate  # noqa: E402

OK = dict(
    setting=True, spec_engine="onnx", interp=None, out_kind="video",
    codec="h264_nvenc", decode_vf=None, burn_subtitle=None, watermark=None,
    target_size=None, tile=0, batch=1, src_codec="hevc", bit_depth=10,
    width=1920, height=1080, io={},
)


def _ok(**over):
    kw = {**OK, **over}
    return gpu_chain_gate(**kw)


def test_all_conditions_met():
    assert _ok() == (True, "")


def test_setting_off():
    ok, reason = _ok(setting=False)
    assert not ok and "gpu_zero_copy" in reason


def test_engine_not_onnx():
    ok, reason = _ok(spec_engine="torch")
    assert not ok and "torch" in reason


def test_interp_rejected():
    ok, reason = _ok(interp=object())
    assert not ok and "补帧" in reason


def test_image_output_rejected():
    ok, reason = _ok(out_kind="png")
    assert not ok and "图片" in reason


def test_codec_gate():
    for codec in ("h264", "libx264", "av1_nvenc", "h264_amf"):
        ok, reason = _ok(codec=codec)
        assert not ok, codec
    assert _ok(codec="hevc_nvenc")[0]


def test_prefilters_rejected():
    ok, reason = _ok(decode_vf="bwdif=mode=send_frame")
    assert not ok and "滤镜" in reason


def test_burn_watermark_rejected():
    ok, reason = _ok(burn_subtitle=object())
    assert not ok and "字幕" in reason
    ok, reason = _ok(watermark=object())
    assert not ok and "水印" in reason


def test_target_size_rejected():
    ok, reason = _ok(target_size=(3840, 2160))
    assert not ok and "目标尺寸" in reason


def test_tile_batch_rejected():
    ok, reason = _ok(tile=512)
    assert not ok and "tile" in reason
    ok, reason = _ok(batch=4)
    assert not ok and "批处理" in reason


def test_source_constraints():
    ok, reason = _ok(src_codec="vp9")
    assert not ok and "vp9" in reason
    ok, reason = _ok(bit_depth=8)
    assert not ok and "8bit" in reason
    ok, reason = _ok(width=1919)
    assert not ok and "奇数" in reason
    ok, reason = _ok(height=1079)
    assert not ok and "奇数" in reason


def test_model_io_constraints():
    ok, reason = _ok(io={"color": "y"})
    assert not ok and "IO" in reason
    ok, reason = _ok(io={"affine": [0.7, 0.15]})
    assert not ok
    ok, reason = _ok(io={"fixed_hw": [256, 256]})
    assert not ok

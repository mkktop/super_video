"""第一梯队新模型（Real-CUGAN Pro / Ani4K v2）与引擎 io 扩展。

覆盖：注册表 manifest 语义（变体映射/仿射/pad 分档/sha256）、引擎 affine
前后处理数学、pad 按倍率分档解析、affine 与 u8 包装互斥、latest x3 pad 修复。
真实权重的画质/速度验证在 BENCH.md 口径下人工跑（见 .tmp/models_vset 探测脚本）。
"""
import re
from pathlib import Path

import numpy as np
import pytest

from sv.models.registry import file_for_scale, load_registry
from sv.paths import TEMP_DIR

REGISTRY_DIR = Path(__file__).parent.parent / "sv" / "models" / "registry_json"


# ---- 注册表 manifest ----

def test_registry_loads_new_entries():
    specs = load_registry()
    assert "real-cugan-pro" in specs
    assert "ani4k-v2-compact" in specs
    assert "ani4k-v2-ultracompact" in specs


def test_animejanai_v31_entries():
    """V3.1 四变体：两档速度 × Standard/Sharp 独立权重，原生 fp16 单帧。"""
    ids = {
        "animejanai-v31-hd-performance": "fastest",
        "animejanai-v31-hd-performance-sharp": "fastest",
        "animejanai-v31-hd-balanced": "fast",
        "animejanai-v31-hd-balanced-sharp": "fast",
    }
    specs = load_registry()
    sharp_hashes, std_hashes = set(), set()
    for mid, speed in ids.items():
        spec = specs[mid]
        assert spec.scale == [2] and spec.engine == "onnx" and spec.speed == speed
        # 原生 fp16 权重不再转换（V2 家族惯例），单帧固定 batch
        assert spec.fp16 is False and spec.io.get("batch_hint") == 1
        assert spec.io["color"] == "rgb" and spec.io["range"] == "0-1"
        assert "denoise" not in spec.io  # 无降噪变体概念
        assert len(spec.files) == 1
        h = spec.files[0]["sha256"]
        (sharp_hashes if "Sharp1" in spec.files[0]["name"] else std_hashes).add(h)
    # Sharp 与 Standard 是不同权重（实测哈希不同），且各自唯一
    assert len(sharp_hashes) == len(std_hashes) == 2
    assert sharp_hashes.isdisjoint(std_hashes)


def test_cugan_pro_variant_mapping():
    """denoise 档 → 权重映射：None=保守（同倍率第一个）、0=no-denoise3x、3=denoise3x。"""
    spec = load_registry()["real-cugan-pro"]
    assert spec.scale == [2, 3]
    for sc in (2, 3):
        assert file_for_scale(spec, sc)["name"] == f"pro-conservative-up{sc}x.onnx"
        assert file_for_scale(spec, sc, "denoise0")["name"] == f"pro-no-denoise3x-up{sc}x.onnx"
        assert file_for_scale(spec, sc, "denoise3")["name"] == f"pro-denoise3x-up{sc}x.onnx"
    # denoise1/2 上游未发布，档位集合应为 [0, 3]（前端据此只列两项）
    levels = sorted({
        int(f["variant"][7:]) for f in spec.files
        if str(f.get("variant", "")).startswith("denoise")
        and str(f["variant"])[7:].isdigit()
    })
    assert levels == [0, 3]
    # 与 latest 相同的家族围栏：fp16 转换前科 + DML 泄漏禁包装
    assert spec.fp16 is False and spec.u8_wrap is False
    assert spec.io["affine"] == [0.7, 0.15]


def test_cugan_pro_pad_per_scale():
    """up2x 需偶数、up3x 需 4 的倍数（实测 30x30 拒跑 32x32 通过）。"""
    io = load_registry()["real-cugan-pro"].io
    assert io["pad"] == {"2": 2, "3": 4}


def test_ani4k_entries():
    for mid, speed in (("ani4k-v2-compact", "fast"), ("ani4k-v2-ultracompact", "fastest")):
        spec = load_registry()[mid]
        assert spec.scale == [2] and spec.engine == "onnx"
        assert spec.speed == speed
        assert spec.fp16 is True  # fp16 转换数值已实测（u8 域 ≤1/255）
        assert spec.io["color"] == "rgb" and spec.io["range"] == "0-1"
        assert len(spec.files) == 1
    # NC 许可必须显式记录（免费工具可再分发，但条款要可见）
    assert "BY-NC" in load_registry()["ani4k-v2-compact"].license


def test_all_manifest_files_wellformed():
    """全注册表：sha256 合法；主源指 ModelScope 镜像（国内加速），models-v1 降为备用源。"""
    for spec in load_registry().values():
        for f in spec.files:
            if "sha256" in f:
                assert re.fullmatch(r"[0-9a-f]{64}", f["sha256"]), (spec.id, f["name"])
            url = f.get("url", "")
            if url:
                assert url == (
                    "https://modelscope.cn/models/mengkaikun/super-video-models/resolve/master/"
                    + f["name"]
                ), (spec.id, f["name"])
                assert f.get("mirror_urls") == [
                    "https://github.com/mkktop/super_video/releases/download/models-v1/"
                    + f["name"]
                ], (spec.id, f["name"])


def test_latest_cugan_x3_pad_fix():
    """latest x3 整除性是 4 不是 3（30x30 曾崩溃）；x2/x4 行为保持不变。"""
    io = load_registry()["real-cugan"].io
    assert io["pad"] == {"2": 2, "3": 4, "4": 4}


# ---- 引擎 io 扩展 ----

def _mul_model(tmp_path, k=0.5):
    """y = x * k 的动态尺寸 ONNX（N,C,H,W 全动态），验证前后处理管线的探针模型。"""
    pytest.importorskip("onnx")
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    x = helper.make_tensor_value_info("input", TensorProto.FLOAT, [None, 3, None, None])
    y = helper.make_tensor_value_info("output", TensorProto.FLOAT, [None, 3, None, None])
    kvec = numpy_helper.from_array(np.array([k], dtype=np.float32), name="k")
    node = helper.make_node("Mul", ["input", "k"], ["output"])
    graph = helper.make_graph([node], "probe", [x], [y], [kvec])
    m = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    p = tmp_path / f"probe_mul_{k}.onnx"
    onnx.save(m, str(p))
    return p


@pytest.fixture(scope="module")
def _engine_env():
    import os
    import uuid

    # 引擎惰性缓存目录（u8 包装/trt）指到测试临时区，避免污染真实缓存
    os.environ.setdefault("SV_DB", str(TEMP_DIR / f"test_new_models_{uuid.uuid4().hex[:6]}.db"))


def test_pad_dict_resolution():
    """io.pad 字典按运行时 scale 取档；int 保持原语义。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    io = {"pad": {"2": 2, "3": 4}}
    assert OnnxSrEngine("m.onnx", 2, io=io).pad == 2
    assert OnnxSrEngine("m.onnx", 3, io=io).pad == 4
    # 缺档回退 scale 本值
    assert OnnxSrEngine("m.onnx", 4, io=io).pad == 4
    assert OnnxSrEngine("m.onnx", 3, io={}).pad == 3  # 默认 = scale
    assert OnnxSrEngine("m.onnx", 3, io={"pad": 8}).pad == 8


def test_affine_plumbing_single(_engine_env, tmp_path):
    """affine 必须精确包住网络：出图 = ((入图*a+b 经网络) - b)/a。

    探针网络 y=0.5x：引擎端到端输出应为 ((v/255*0.7+0.15)*0.5-0.15)/0.7*255。
    """
    from sv.engines.onnx_engine import OnnxSrEngine

    model = _mul_model(tmp_path)
    eng = OnnxSrEngine(model, 1, io={
        "color": "rgb", "range": "0-1", "pad": 1, "affine": [0.7, 0.15],
    }, device="cpu")
    eng.load()
    frame = np.arange(0, 256, 8, dtype=np.uint8)[:30].reshape(5, 6, 1).repeat(3, axis=2)
    out = eng.process(frame)
    v = frame[..., 0].astype(np.float32)
    expect = np.clip(((v / 255 * 0.7 + 0.15) * 0.5 - 0.15) / 0.7 * 255, 0, 255)
    np.testing.assert_allclose(out[..., 0].astype(np.float32), expect, atol=1.0)
    assert eng.u8_wrapped is False, "affine 模型必须跳过 u8 包装"


def test_affine_plumbing_batch(_engine_env, tmp_path):
    from sv.engines.onnx_engine import OnnxSrEngine

    model = _mul_model(tmp_path)
    eng = OnnxSrEngine(model, 1, io={
        "color": "rgb", "range": "0-1", "pad": 1, "affine": [0.7, 0.15],
    }, device="cpu", batch=2)
    eng.load()
    frames = np.full((2, 5, 6, 3), 200, dtype=np.uint8)
    out = eng.process_batch(frames)
    v = 200.0
    expect = ((v / 255 * 0.7 + 0.15) * 0.5 - 0.15) / 0.7 * 255
    assert out.shape == (2, 5, 6, 3)
    np.testing.assert_allclose(out[..., 0].astype(np.float32), expect, atol=1.0)


def test_affine_disables_u8_wrap(_engine_env, tmp_path):
    """同图同 io，无 affine 时 CPU 探针可包装；带 affine 必须关闭。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    model = _mul_model(tmp_path, k=1.0)  # y=x，包装 A/B 可通过
    plain = OnnxSrEngine(model, 1, io={"color": "rgb", "range": "0-1", "pad": 1},
                         device="cpu")
    plain.load()
    assert plain.u8_wrapped is True
    aff = OnnxSrEngine(model, 1, io={"color": "rgb", "range": "0-1", "pad": 1,
                                     "affine": [0.7, 0.15]}, device="cpu")
    aff.load()
    assert aff.u8_wrapped is False and aff._u8_sess is None


# ---- V3.1 真实权重端到端（权重经 models_store 或下载缓存；无权重/DML 缺失则跳过）----

def _v31_weight(mid):
    from sv.models.registry import load_registry, model_file

    w = model_file(load_registry()[mid], 2)
    if not w.exists():
        pytest.skip(f"{mid} 权重未就绪")
    return w


@pytest.mark.parametrize("mid", [
    "animejanai-v31-hd-performance", "animejanai-v31-hd-performance-sharp",
    "animejanai-v31-hd-balanced", "animejanai-v31-hd-balanced-sharp",
])
@pytest.mark.skipif(not __import__("conftest").dml_available(),
                    reason="无 DML 设备")
def test_v31_real_weights_dml(mid):
    """DML 下原生 fp16 权重走通：u8 包装生效（含 Sharp 容差 2 案例）且无原生崩溃。

    Sharp1 Performance 曾在包装校验差 2/255 时回退、回退后首帧 DML 原生崩
    （包装 session 被 GC 析构损坏设备）——引擎保留引用后此场景必须消失。
    """
    from sv.engines.onnx_engine import OnnxSrEngine

    w = _v31_weight(mid)
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 256, (47, 63, 3), dtype=np.uint8)
    eng = OnnxSrEngine(w, 2, io=load_registry()[mid].io,
                       device="auto", validate_hw=(47, 63))
    eng.load()
    out = eng.process(frame)
    assert out.shape == (94, 126, 3)
    assert "Dml" in eng.provider_used[0]
    assert eng.u8_wrapped is True, "V3.1 四权重在 DML 下包装必须全通过（容差 ≤2）"

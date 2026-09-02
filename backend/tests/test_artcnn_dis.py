"""ArtCNN（亮度 doubler）与 DIS 新模型：注册表语义 + 引擎单通道 Y 路径。

ArtCNN 主力线是单通道 Y 的 2x doubler（上游官方 Inferencer 实证 0-1 域），
引擎 io.color="y" 走 RGB→Y(BT.601 全域)过模型 + 色度双三次放大合并；
DIS 是三通道原生 fp16 的 2x 修复系（补真人向 2x 空档）。
真权重的 DML 端到端在本机有 DML 时跑（同 test_new_models 的 V3.1 惯例）。
"""
from pathlib import Path

import numpy as np
import pytest

from sv.models.registry import load_registry, model_file

REGISTRY_DIR = Path(__file__).parent.parent / "sv" / "models" / "registry_json"


# ---- 注册表 manifest ----

def test_registry_loads_artcnn_dis():
    specs = load_registry()
    for mid in ("artcnn-c4f16", "artcnn-c4f16-dn", "artcnn-r8f64",
                "dis-2x-balanced", "dis-2x-fast"):
        assert mid in specs, mid


def test_artcnn_entries_semantics():
    """单通道亮度 doubler 家族：color=y、免对齐 pad=1、单帧 batch、MIT。"""
    tiers = {"artcnn-c4f16": "fast", "artcnn-c4f16-dn": "fast",
             "artcnn-r8f64": "balanced"}
    specs = load_registry()
    for mid, speed in tiers.items():
        spec = specs[mid]
        assert spec.scale == [2] and spec.engine == "onnx" and spec.kind == "sr"
        assert spec.speed == speed
        assert spec.license == "MIT"  # 商业无顾虑的新家族
        assert spec.io["color"] == "y" and spec.io["range"] == "0-1"
        assert spec.io["pad"] == 1 and spec.io.get("batch_hint") == 1
        assert len(spec.files) == 1
        assert spec.content == ["anime"]
    # DN 与标准是不同权重（哈希不同）
    h = {mid: specs[mid].files[0]["sha256"] for mid in
         ("artcnn-c4f16", "artcnn-c4f16-dn")}
    assert h["artcnn-c4f16"] != h["artcnn-c4f16-dn"]


def test_dis_entries_semantics():
    """DIS：三通道 0-1 原生 fp16（不再二次转换），Apache-2.0，真人/通用。"""
    specs = load_registry()
    for mid, speed in (("dis-2x-balanced", "balanced"), ("dis-2x-fast", "fast")):
        spec = specs[mid]
        assert spec.scale == [2] and spec.engine == "onnx"
        assert spec.speed == speed
        assert spec.license == "Apache-2.0"
        assert spec.fp16 is False  # 权重本体即 fp16（同 AnimeJaNai 惯例）
        assert spec.io["color"] == "rgb" and spec.io["range"] == "0-1"
        assert spec.io["pad"] == 1  # 实测 47x63 奇数边长直跑
        assert spec.content == ["general"]
    h = {mid: specs[mid].files[0]["sha256"] for mid in
         ("dis-2x-balanced", "dis-2x-fast")}
    assert h["dis-2x-balanced"] != h["dis-2x-fast"]


# ---- 引擎单通道 Y 路径（探针模型，无真实权重依赖）----

def _y_probe_model(tmp_path, scale: int, hw: tuple[int, int] | None = None):
    """单通道恒等探针：Y 原样输出（scale=1）或最近邻 2x（scale=2）。

    hw=None 动态尺寸（ArtCNN 官方导出口径）；给 hw 则定长（探非法组合用）。
    """
    pytest.importorskip("onnx")
    import onnx
    from onnx import TensorProto, helper

    dims = [1, 1, *(hw if hw else (None, None))]
    x = helper.make_tensor_value_info("input", TensorProto.FLOAT, dims)
    y = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 1, None, None])
    if scale == 1:
        nodes = [helper.make_node("Identity", ["input"], ["output"])]
    else:
        # 最近邻 2x：轴 2/3 各自拼接自身（Concat 平铺，数学上即 nearest 2x）
        nodes = [
            helper.make_node("Concat", ["input", "input"], ["rows"], axis=2),
            helper.make_node("Concat", ["rows", "rows"], ["output"], axis=3),
        ]
    graph = helper.make_graph(nodes, "yprobe", [x], [y], [])
    m = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    p = tmp_path / f"yprobe{scale}x.onnx"
    onnx.save(m, str(p))
    return p


def test_y_path_roundtrip_identity(tmp_path):
    """scale=1 恒等 Y 探针：引擎输出 ≈ 输入（YCbCr 往返 + 合并的数学验证）。

    误差来源只有 uint8 量化与矩阵舍入（实测 <2/255）。
    """
    from sv.engines.onnx_engine import OnnxSrEngine

    model = _y_probe_model(tmp_path, 1)
    eng = OnnxSrEngine(model, 1, io={"color": "y", "range": "0-1", "pad": 1},
                       device="cpu")
    eng.load()
    rng = np.random.default_rng(5)
    frame = rng.integers(0, 256, (33, 47, 3), dtype=np.uint8)
    out = eng.process(frame)
    assert out.shape == (33, 47, 3)
    diff = np.abs(out.astype(np.int16) - frame.astype(np.int16))
    assert diff.max() <= 2, f"Y 通道往返误差过大: {diff.max()}"


def test_y_path_2x_shape_and_luma(tmp_path):
    """scale=2 最近邻探针：输出 2x、Y 内容恒等放大（色度插值只影响色彩平滑度）。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    model = _y_probe_model(tmp_path, 2)
    eng = OnnxSrEngine(model, 2, io={"color": "y", "range": "0-1", "pad": 1},
                       device="cpu")
    eng.load()
    frame = np.full((16, 24, 3), 128, dtype=np.uint8)
    out = eng.process(frame)
    assert out.shape == (32, 48, 3)
    # 中灰（R=G=B）经 YCbCr 是零色度：输出仍应接近中灰
    assert abs(float(out.mean()) - 128.0) <= 1.0


def test_y_path_disables_u8_wrap(tmp_path):
    """color=y 不进 u8 包装（三通道图手术语义不适用）。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    model = _y_probe_model(tmp_path, 1)
    eng = OnnxSrEngine(model, 1, io={"color": "y", "range": "0-1", "pad": 1},
                       device="cpu")
    eng.load()
    assert eng.u8_wrapped is False and eng._u8_sess is None


def test_y_static_shape_rejected_at_load(tmp_path):
    """y 模型 + 定长导出组合：load 期即报清晰错误。

    _infer_y 的分发在 fixed_hw 判断之前，定长 y 模型若放行会在首帧撞
    ORT shape mismatch 的晦涩报错——防线前移到加载期。
    """
    from sv.engines.onnx_engine import OnnxSrEngine

    model = _y_probe_model(tmp_path, 1, hw=(32, 32))
    eng = OnnxSrEngine(model, 1, io={"color": "y", "range": "0-1", "pad": 1},
                       device="cpu")
    with pytest.raises(ValueError, match="定长导出"):
        eng.load()


# ---- 真实权重端到端（权重在 models_store；无权重跳过，DML 门控同 V3.1 惯例）----

def _weight(mid: str) -> Path:
    w = model_file(load_registry()[mid], 2)
    if not w.exists():
        pytest.skip(f"{mid} 权重未就绪")
    return w


@pytest.mark.parametrize("mid", ["artcnn-c4f16", "artcnn-c4f16-dn", "artcnn-r8f64"])
def test_artcnn_real_weights_cpu(mid):
    """真权重 CPU：2x 输出、奇数边长免对齐、恒定黑帧不发散。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    rng = np.random.default_rng(7)
    eng = OnnxSrEngine(_weight(mid), 2, io=load_registry()[mid].io, device="cpu")
    eng.load()
    out = eng.process(rng.integers(0, 256, (47, 63, 3), dtype=np.uint8))
    assert out.shape == (94, 126, 3)
    black = eng.process(np.zeros((32, 32, 3), np.uint8))
    assert black.mean() <= 2.0, "黑帧应保持黑（0-1 域/单通道接线正确）"


@pytest.mark.parametrize("mid", ["dis-2x-balanced", "dis-2x-fast"])
def test_dis_real_weights_cpu(mid):
    """真权重 CPU（fp16 模型在 CPU EP 由 ORT 内部转型跑通）：2x、奇数边长。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    rng = np.random.default_rng(7)
    eng = OnnxSrEngine(_weight(mid), 2, io=load_registry()[mid].io, device="cpu")
    eng.load()
    assert eng._in_fp16, "DIS 权重本体应为 fp16 输入"
    out = eng.process(rng.integers(0, 256, (47, 63, 3), dtype=np.uint8))
    assert out.shape == (94, 126, 3)


@pytest.mark.parametrize("mid", ["artcnn-c4f16", "artcnn-c4f16-dn", "artcnn-r8f64",
                                 "dis-2x-balanced", "dis-2x-fast"])
@pytest.mark.skipif(not __import__("conftest").dml_available(), reason="无 DML 设备")
def test_new_models_real_weights_dml(mid):
    """DML 端到端：Y 路径不包装直跑、DIS 包装生效（A/B 内建校验），奇数边长。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    rng = np.random.default_rng(7)
    eng = OnnxSrEngine(_weight(mid), 2, io=load_registry()[mid].io,
                       device="auto", validate_hw=(47, 63))
    eng.load()
    out = eng.process(rng.integers(0, 256, (47, 63, 3), dtype=np.uint8))
    assert out.shape == (94, 126, 3)
    assert "Dml" in eng.provider_used[0]
    spec_io = load_registry()[mid].io
    if spec_io["color"] == "y":
        assert eng.u8_wrapped is False
    else:
        assert eng.u8_wrapped is True, "DIS 在 DML 下 u8 包装应通过 A/B 校验"

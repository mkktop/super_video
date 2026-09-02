"""图片/漫画超分扩容（2026-09）：MangaJaNai 漫画专模 + IllustrationJaNai 彩色页
+ HAT/SwinIR 真人 4x 画质档 + x4v3/anime_6B + SeemoRe 3x + 社区精选三款。

关键语义：
- mangajanai 按 io.auto_variant="height" 按源高度自动选权重（14 文件 = 2x/4x ×
  7 个设计高度档，网点频率对位）；
- transformer 系（HAT/SwinIR/DAT2）与 SeemoRe 动态导出会烤死窗口/插值常量，
  一律 256 定长导出 + tile_hint=256（tile span 恒 ≤ tile，fixed_hw 补边路径接管）；
- IllustrationJaNai 2x 是上游官方 fp16 导出（fp16:false 惯例、u8 包装 A/B 差 3
  超 DML 容差 → manifest 直接禁包装免每次加载空试）；
- 社区三款为 CC-BY-NC-SA（项目非商用口径可用），UltraSharp/AnimeSharp 用官方 ONNX。
"""
from pathlib import Path

import numpy as np
import pytest

from sv.models.registry import (
    auto_variant,
    load_registry,
    model_file,
)

NEW_IDS = [
    "mangajanai", "illustrationjanai-2x", "illustrationjanai-4x",
    "illustrationjanai-4x-dat2", "hat-real-x4", "hat-real-x4-sharp",
    "swinir-real-x4", "realesr-general-x4v3", "realesr-general-x4v3-wdn",
    "realesrgan-x4plus-anime", "seemore-b",
    "ultrasharp-4x", "animesharp-4x", "remacri-4x",
]
HEIGHTS = ["1200p", "1300p", "1400p", "1500p", "1600p", "1920p", "2048p"]


# ---- 注册表 manifest ----

def test_registry_loads_new_entries():
    specs = load_registry()
    for mid in NEW_IDS:
        assert mid in specs, mid


def test_mangajanai_height_variants():
    """漫画专模：2x/4x × 7 高度档，变体唯一、auto_variant=height、NC 许可。"""
    spec = load_registry()["mangajanai"]
    assert spec.scale == [2, 4] and spec.content == ["comic"]
    assert spec.io.get("auto_variant") == "height"
    assert spec.license == "CC-BY-NC-4.0"
    assert len(spec.files) == 14
    variants = [(f["scale"], f["variant"]) for f in spec.files]
    assert len(set(variants)) == 14
    for s in (2, 4):
        assert {v for sc, v in variants if sc == s} == set(HEIGHTS)
    # 每个文件 sha256 互不相同（不同高度/倍率是不同权重）
    hashes = [f["sha256"] for f in spec.files]
    assert len(set(hashes)) == 14


def test_transformer_fixed_tile_semantics():
    """窗口注意力/MoE 系：256 定长 + tile_hint=256 + 禁包装（fixed_hw 本就跳过）。"""
    for mid in ("hat-real-x4", "hat-real-x4-sharp", "swinir-real-x4",
                "illustrationjanai-4x-dat2", "seemore-b"):
        spec = load_registry()[mid]
        assert spec.tile_hint == 256, mid
        assert spec.u8_wrap is False, mid
        assert spec.io["color"] == "rgb" and spec.io["range"] == "0-1"


def test_real_photo_entries_semantics():
    """真人图片档：HAT 双风格不同权重、x4v3/wdn 不同权重、社区三款 NC-SA。"""
    specs = load_registry()
    for mid in ("hat-real-x4", "hat-real-x4-sharp", "swinir-real-x4"):
        assert specs[mid].scale == [4] and specs[mid].license == "Apache-2.0"
        assert specs[mid].content == ["general"] and specs[mid].speed == "slow"
    h = {mid: specs[mid].files[0]["sha256"] for mid in ("hat-real-x4", "hat-real-x4-sharp")}
    assert h["hat-real-x4"] != h["hat-real-x4-sharp"]
    for mid in ("realesr-general-x4v3", "realesr-general-x4v3-wdn"):
        assert specs[mid].scale == [4] and specs[mid].license == "BSD-3-Clause"
        assert specs[mid].speed == "fastest"
    h = {mid: specs[mid].files[0]["sha256"] for mid in
         ("realesr-general-x4v3", "realesr-general-x4v3-wdn")}
    assert h["realesr-general-x4v3"] != h["realesr-general-x4v3-wdn"]
    for mid in ("ultrasharp-4x", "animesharp-4x", "remacri-4x"):
        assert specs[mid].scale == [4] and specs[mid].license == "CC-BY-NC-SA-4.0"
    assert specs["animesharp-4x"].content == ["anime"]


def test_illustrationjanai_semantics():
    """彩色页系：2x 为官方 fp16 导出（不再转换、禁包装），4x 双架构分档。"""
    specs = load_registry()
    fast = specs["illustrationjanai-2x"]
    assert fast.fp16 is False and fast.u8_wrap is False
    assert fast.io.get("batch_hint") == 1 and fast.speed == "fastest"
    dat2 = specs["illustrationjanai-4x-dat2"]
    assert dat2.speed == "slow" and dat2.tile_hint == 256
    h = {mid: specs[mid].files[0]["sha256"] for mid in
         ("illustrationjanai-4x", "illustrationjanai-4x-dat2")}
    assert h["illustrationjanai-4x"] != h["illustrationjanai-4x-dat2"]


# ---- 场景标签（scenes：video/manga/image，卡片角标+筛选）----

def test_scenes_semantics():
    """全注册表 scenes 合法；漫画系=manga+image；HAT 等图片向=image；RIFE=video。"""
    specs = load_registry()
    allowed = {"video", "manga", "image"}
    for mid, spec in specs.items():
        assert spec.scenes and set(spec.scenes) <= allowed, mid
        if spec.kind == "interp":
            continue  # rife 补帧：video 专属
        assert "image" in spec.scenes, f"{mid} 超分模型必须可用图片场景"
    assert specs["mangajanai"].scenes == ["manga", "image"]
    assert specs["illustrationjanai-4x-dat2"].scenes == ["manga", "image"]
    for mid in ("hat-real-x4", "swinir-real-x4", "seemore-b", "ultrasharp-4x",
                "realesrgan-x4plus-anime"):
        assert specs[mid].scenes == ["image"], mid
    assert specs["rife-v4.26"].scenes == ["video"]
    for mid in ("animejanai-v31-hd-balanced", "real-cugan", "realesrgan-x4plus",
                "realesr-animevideov3"):
        assert specs[mid].scenes == ["video", "image"], mid


# ---- auto_variant（按源高度选权重，无权重依赖）----

def test_auto_variant_picks_nearest_height():
    spec = load_registry()["mangajanai"]
    assert auto_variant(spec, 2, 1199) == "1200p"
    assert auto_variant(spec, 2, 1250) in ("1200p", "1300p")  # 正中分档边界
    assert auto_variant(spec, 2, 1251) == "1300p"
    assert auto_variant(spec, 2, 3000) == "2048p"
    assert auto_variant(spec, 2, 480) == "1200p"  # 低清源取最低档
    assert auto_variant(spec, 4, 1080) == "1200p"  # 按倍率独立


def test_auto_variant_guard_clauses():
    spec = load_registry()["mangajanai"]
    assert auto_variant(spec, 2, None) is None  # 未知高度
    assert auto_variant(load_registry()["hat-real-x4"], 4, 1080) is None  # 非 height 家族
    # 平手取更低档：1050 到 1200/…无平手场景，构造平手验证排序键
    class FakeSpec:
        io = {"auto_variant": "height"}
        files = [
            {"name": "a", "scale": 2, "variant": "1000p"},
            {"name": "b", "scale": 2, "variant": "1400p"},
        ]
    assert auto_variant(FakeSpec(), 2, 1200) == "1000p"


# ---- 真实权重端到端（权重在 models_store；无权重跳过）----

def _spec(mid: str):
    return load_registry()[mid]


def _weight(mid: str, scale: int | None = None, variant: str | None = None):
    spec = _spec(mid)
    w = model_file(spec, scale or spec.scale[0], variant=variant)
    if not w.exists():
        pytest.skip(f"{mid} 权重未就绪")
    return w


def test_mangajanai_real_weight_cpu():
    """漫画专模 CPU：2x、奇数边长、灰度输入三通道一致（灰度模型不染色）、黑帧保持黑。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    w = _weight("mangajanai", 2, variant="1200p")
    eng = OnnxSrEngine(w, 2, io=_spec("mangajanai").io, device="cpu")
    eng.load()
    rng = np.random.default_rng(7)
    out = eng.process(rng.integers(0, 256, (47, 63, 3), dtype=np.uint8))
    assert out.shape == (94, 126, 3)
    gray = np.full((32, 32, 3), 100, np.uint8)
    gout = eng.process(gray)
    spread = int((gout.max(axis=2).astype(int) - gout.min(axis=2).astype(int)).max())
    assert spread <= 12, f"灰度输入输出通道发散: {spread}"
    black = eng.process(np.zeros((32, 32, 3), np.uint8))
    assert black.mean() <= 3.0


def test_hat_real_weight_cpu_fixed_tile():
    """HAT 定长路径：47x63 经 fixed_hw(256) 补边 → 4x 裁剪回正确尺寸。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    w = _weight("hat-real-x4")
    eng = OnnxSrEngine(w, 4, io=_spec("hat-real-x4").io, tile=256, device="cpu")
    eng.load()
    assert eng.fixed_hw == (256, 256)
    rng = np.random.default_rng(7)
    out = eng.process(rng.integers(0, 256, (47, 63, 3), dtype=np.uint8))
    assert out.shape == (188, 252, 3)


def test_seemore_real_weight_cpu_all_scales():
    """SeemoRe 2x/3x/4x 定长路径全倍率形状正确（3x 空档是它的核心价值）。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    spec = _spec("seemore-b")
    for scale in (2, 3, 4):
        w = model_file(spec, scale)
        if not w.exists():
            pytest.skip("seemore 权重未就绪")
        eng = OnnxSrEngine(w, scale, io=spec.io, tile=256, device="cpu")
        eng.load()
        rng = np.random.default_rng(7)
        out = eng.process(rng.integers(0, 256, (40, 56, 3), dtype=np.uint8))
        assert out.shape == (40 * scale, 56 * scale, 3), scale


def test_x4v3_wdn_differs_from_base():
    """wdn 是与 base 做 dni 插值的独立降噪权重：输出必须与 base 实质不同。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    outs = {}
    for mid in ("realesr-general-x4v3", "realesr-general-x4v3-wdn"):
        w = _weight(mid)
        if not w.exists():
            pytest.skip(f"{mid} 权重未就绪")
        eng = OnnxSrEngine(w, 4, io=_spec(mid).io, device="cpu")
        eng.load()
        rng = np.random.default_rng(7)
        outs[mid] = eng.process(rng.integers(0, 256, (48, 64, 3), dtype=np.uint8))
    diff = np.abs(outs["realesr-general-x4v3"].astype(int)
                  - outs["realesr-general-x4v3-wdn"].astype(int)).mean()
    assert diff > 2.0, f"wdn 与 base 输出几乎相同 (meandiff={diff:.2f})，疑似同一权重"


def test_anime6b_real_weight_cpu():
    from sv.engines.onnx_engine import OnnxSrEngine

    w = _weight("realesrgan-x4plus-anime")
    if not w.exists():
        pytest.skip("anime_6B 权重未就绪")
    eng = OnnxSrEngine(w, 4, io=_spec("realesrgan-x4plus-anime").io, device="cpu")
    eng.load()
    rng = np.random.default_rng(7)
    out = eng.process(rng.integers(0, 256, (31, 41, 3), dtype=np.uint8))
    assert out.shape == (124, 164, 3)


def test_illustrationjanai_span_fp16_cpu():
    """官方 fp16 导出：引擎 fp16 输入路径 + 奇数边长。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    w = _weight("illustrationjanai-2x")
    if not w.exists():
        pytest.skip("SPAN_S 权重未就绪")
    eng = OnnxSrEngine(w, 2, io=_spec("illustrationjanai-2x").io, device="cpu")
    eng.load()
    assert eng._in_fp16
    rng = np.random.default_rng(7)
    out = eng.process(rng.integers(0, 256, (47, 63, 3), dtype=np.uint8))
    assert out.shape == (94, 126, 3)


def test_ultrasharp_real_weight_cpu_batch():
    """官方 ONNX 动态 batch：批量路径 2 帧一次推理。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    w = _weight("ultrasharp-4x")
    if not w.exists():
        pytest.skip("UltraSharp 权重未就绪")
    eng = OnnxSrEngine(w, 4, io=_spec("ultrasharp-4x").io, device="cpu")
    eng.load()
    assert eng.max_batch == 0, "官方导出 batch 轴应为动态"
    frames = np.random.default_rng(7).integers(0, 256, (2, 16, 16, 3), dtype=np.uint8)
    out = eng.process_batch(frames)
    assert out.shape == (2, 64, 64, 3)


def test_local_weights_match_manifest():
    """本地已就位的新权重 sha256 必须与注册表一致（models-v1 唯一源约定）。"""
    import hashlib

    for mid in NEW_IDS:
        spec = _spec(mid)
        for f in spec.files:
            p = model_file(spec, f.get("scale", spec.scale[0]),
                           variant=f.get("variant"))
            if not p.exists():
                continue
            actual = hashlib.sha256(p.read_bytes()).hexdigest()
            assert actual == f["sha256"], (mid, f["name"])


# ---- DML 门控（无 DML 设备跳过，同 V3.1 惯例）----

@pytest.mark.skipif(not __import__("conftest").dml_available(), reason="无 DML 设备")
@pytest.mark.parametrize("mid,scale,tile,expect_wrap", [
    ("mangajanai", 2, 0, True),
    ("hat-real-x4", 4, 256, False),
    ("illustrationjanai-2x", 2, 0, False),
    ("ultrasharp-4x", 4, 0, True),
    ("seemore-b", 3, 256, False),
])
def test_new_models_real_weights_dml(mid, scale, tile, expect_wrap):
    """DML 端到端：动态 conv 系包装生效、定长/官方 fp16 不包装，奇数边长。"""
    from sv.engines.onnx_engine import OnnxSrEngine

    spec = _spec(mid)
    variant = "1200p" if mid == "mangajanai" else None
    if mid == "seemore-b":
        w = model_file(spec, scale)
    else:
        w = model_file(spec, scale, variant=variant)
    if not w.exists():
        pytest.skip(f"{mid} 权重未就绪")
    eng = OnnxSrEngine(w, scale, io=spec.io, tile=tile,
                       device="auto", validate_hw=(240, 320))
    eng.load()
    assert "Dml" in eng.provider_used[0]
    rng = np.random.default_rng(7)
    out = eng.process(rng.integers(0, 256, (47, 63, 3), dtype=np.uint8))
    assert out.shape == (47 * scale, 63 * scale, 3)
    assert eng.u8_wrapped is expect_wrap, f"{mid} u8 包装状态不符"
    # 连续多帧压力：定长 transformer 在 DML 下不能崩（HAT 153MB 显存路径）
    for _ in range(3):
        eng.process(rng.integers(0, 256, (64, 96, 3), dtype=np.uint8))

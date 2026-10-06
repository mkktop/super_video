"""模型分类透出、默认选择与运行配置分离。"""
from sv.models.registry import ModelSpec, load_registry
from sv.pipeline.recommend import DEFAULT_ANIME_MODEL, DEFAULT_MANGA_MODEL, DEFAULT_COLOR_MODEL


def test_builtin_categories_and_default_models():
    specs = load_registry()
    expected = {
        DEFAULT_ANIME_MODEL: "anime_video", DEFAULT_MANGA_MODEL: "manga_bw",
        DEFAULT_COLOR_MODEL: "illustration", "artcnn-r8f64": "anime_restore",
        "real-cugan-pro": "anime_restore", "hat-real-x4": "photo_restore",
        "realesrgan-x4plus-torch": "photo_restore", "dis-2x-balanced": "general_upscale",
        "seemore-b": "general_upscale", "rife-v4.26": "interp",
    }
    for mid, category in expected.items():
        assert specs[mid].category == category
    assert specs[DEFAULT_COLOR_MODEL].version == "V1 DAT2"
    assert specs["illustrationjanai-2x"].version == "V3 SPAN S"
    assert specs["artcnn-r8f64"].io["color"] == "y"
    assert specs["mangajanai"].io["auto_variant"] == "height"
    assert specs["realesrgan-x4plus-torch"].content == specs["realesrgan-x4plus"].content


def test_old_custom_manifest_keeps_loading():
    spec = ModelSpec.from_dict({"id": "custom", "name": "Custom", "engine": "onnx", "scale": [2]})
    assert spec.category == "" and spec.version == "" and spec.temporal is False


def test_models_api_exposes_classification_without_claiming_temporal_sr(monkeypatch):
    from sv.server.routes import models
    monkeypatch.setattr(models, "cached_hardware", lambda: {"gpus": []})
    monkeypatch.setattr(models.manager, "is_downloaded", lambda spec: False)
    rows = {row["id"]: row for row in models.get_models()}
    assert rows[DEFAULT_COLOR_MODEL]["category"] == "illustration"
    assert rows[DEFAULT_COLOR_MODEL]["version"] == "V1 DAT2"
    assert rows[DEFAULT_ANIME_MODEL]["temporal"] is False
    assert rows["rife-v4.26"]["kind"] == "interp"
    assert rows["rife-v4.26"]["category"] == "interp"

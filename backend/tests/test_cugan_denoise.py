"""按原生倍率校验降噪变体：不存在的档位不能静默回退。"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sv.models.registry import ModelNotFoundError, denoise_levels, file_for_scale, get_model
from sv.server import app as app_module, db, settings, user_presets
from sv.server.routes import models, tasks


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SV_DB", str(tmp_path / "tasks.db"))
    db.init_db()
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(user_presets, "PRESETS_PATH", tmp_path / "presets.json")
    monkeypatch.setattr(models, "cached_hardware", lambda: {"gpus": []})
    monkeypatch.setattr(tasks, "probe", lambda _: SimpleNamespace(width=640, height=360, fps=24, total_frames=24))
    monkeypatch.setattr(tasks, "validate_m0", lambda _: None)
    # 启动应用生命周期，但阻止 worker 取队列，仅检验请求是否能正确入队。
    with TestClient(app_module.app) as c:
        monkeypatch.setattr(db, "next_queued", lambda: None)
        yield c


def test_model_options_match_each_scale(client):
    result = {m["id"]: m for m in client.get("/api/models").json()}
    assert result["real-cugan"]["denoise_levels_by_scale"] == {"2": [0, 1, 2, 3], "3": [0, 3], "4": [0, 3]}
    assert result["real-cugan-pro"]["denoise_levels_by_scale"] == {"2": [0, 3], "3": [0, 3]}


@pytest.mark.parametrize("mid,scale", [("real-cugan", 3), ("real-cugan", 4), ("real-cugan-pro", 2), ("real-cugan-pro", 3)])
@pytest.mark.parametrize("level", [1, 2])
def test_missing_variant_never_falls_back(mid, scale, level):
    spec = get_model(mid)
    assert level not in denoise_levels(spec, scale)
    with pytest.raises(ModelNotFoundError):
        file_for_scale(spec, scale, f"denoise{level}")


@pytest.mark.parametrize("kind", ["video", "image", "preset"])
@pytest.mark.parametrize("scale,level,expected", [(2, 1, 201), (2, 2, 201), (3, 1, 400), (4, 2, 400), (3, 0, 201), (4, 3, 201)])
def test_api_rejects_unsupported_denoise_before_queueing(client, tmp_path, kind, scale, level, expected):
    if kind == "preset":
        r = client.post("/api/presets", json={"name": "CUGAN", "model_id": "real-cugan", "target_scale": scale, "denoise": level})
    else:
        src = tmp_path / ("clip.mp4" if kind == "video" else "page.png")
        if kind == "video":
            src.write_bytes(b"placeholder")
        else:
            Image.new("RGB", (32, 24), "white").save(src)
        r = client.post("/api/tasks", json={"input": str(src), "model_id": "real-cugan",
                       "params": {"scale": scale, "denoise": level}})
    assert r.status_code == expected, r.text
    if expected == 400:
        assert "不支持降噪" in r.json()["detail"]
        assert db.list_tasks() == []

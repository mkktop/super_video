"""M2 接口测试：预设 / 探测 / 设置 / 模型管理。"""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.paths import MODELS_DIR, TEMP_DIR

os.environ["SV_DB"] = str(TEMP_DIR / "test_sidecar_m2.db")


@pytest.fixture(scope="module")
def client():
    if Path(os.environ["SV_DB"]).exists():
        Path(os.environ["SV_DB"]).unlink()
    from sv.server.app import app

    with TestClient(app) as c:
        yield c


def test_presets(client):
    presets = client.get("/api/presets").json()
    assert len(presets) >= 3
    ids = {p["id"] for p in presets}
    assert {"anime-fast", "anime-hq", "live-restore"} <= ids
    models = {m["id"] for m in client.get("/api/models").json()}
    assert all(p["model_id"] in models for p in presets)


def test_probe_endpoint(client):
    r = client.post("/api/probe", json={"path": "Z:/not_exist.mp4"})
    assert r.status_code == 400
    clip = TEMP_DIR / "srv_tiny.mp4"
    if not clip.exists():
        pytest.skip("测试片段不存在（先跑 test_server）")
    r = client.post("/api/probe", json={"path": str(clip)})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["width"] == 160 and d["has_audio"]


def test_settings_roundtrip(client):
    r = client.put("/api/settings", json={"engine": "cuda"})
    assert r.status_code == 200 and r.json()["engine"] == "cuda"
    r = client.put("/api/settings", json={"engine": "bogus"})
    assert r.status_code == 400
    client.put("/api/settings", json={"engine": "auto"})
    assert client.get("/api/settings").json()["engine"] == "auto"


def test_models_adaptability(client):
    models = {m["id"]: m for m in client.get("/api/models").json()}
    m = models["realesr-animevideov3"]
    assert m["bundled"] is True and m["installed"] is True
    assert isinstance(m["vram_ok"], bool) and "size_mb" in m


def test_model_delete_with_custom(client):
    """用临时自定义模型验证删除接口，不动真实模型。"""
    custom_dir = MODELS_DIR / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    manifest = custom_dir / "tmp-del-me.json"
    model_dir = MODELS_DIR / "tmp-del-me"
    model_dir.mkdir(parents=True, exist_ok=True)
    try:
        manifest.write_text(json.dumps({
            "id": "tmp-del-me", "name": "临时删除测试", "engine": "onnx",
            "scale": [2], "content": [], "speed": "fast", "vram_gb": 1,
            "files": [{"name": "fake.onnx", "url": "http://localhost/x.onnx", "size": 10}],
        }), encoding="utf-8")
        (model_dir / "fake.onnx").write_bytes(b"0123456789")
        ids = {m["id"] for m in client.get("/api/models").json()}
        assert "tmp-del-me" in ids

        r = client.delete("/api/models/tmp-del-me")
        assert r.status_code == 200
        assert not model_dir.exists()  # 目录连同权重被删除
        # 自定义模型的 manifest 一并移除（导入功能后的语义：卸载即彻底删除）；
        # 内置模型删除仅清权重、注册表条目保留
        models = {m["id"] for m in client.get("/api/models").json()}
        assert "tmp-del-me" not in models and not manifest.exists()
    finally:
        manifest.unlink(missing_ok=True)
        import shutil

        shutil.rmtree(model_dir, ignore_errors=True)


def test_log_tail(client):
    d = client.get("/api/log-tail?n=5").json()
    assert "lines" in d

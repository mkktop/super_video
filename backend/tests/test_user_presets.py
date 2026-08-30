"""用户自定义预设：CRUD 端点、字段校验、与内置预设合并展示、内置不可删。"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from sv.server import user_presets

    monkeypatch.setattr(user_presets, "PRESETS_PATH", tmp_path / "user_presets.json")
    monkeypatch.setenv("SV_DB", str(tmp_path / "up.db"))
    from sv.server import db as sv_db

    sv_db.init_db()
    monkeypatch.setattr(sv_db, "next_queued", lambda: None)

    from sv.server.app import app

    with TestClient(app) as c:
        yield c


def _body(**over):
    d = {"name": "我的修复档", "model_id": "realesr-animevideov3",
         "target_scale": 2, "codec": "h264", "crf": 17}
    d.update(over)
    return d


def test_create_list_delete_roundtrip(client):
    r = client.post("/api/presets", json=_body())
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["user"] is True and rec["id"].startswith("user-")
    assert rec["denoise"] is None and rec["deinterlace"] is False

    lst = client.get("/api/presets").json()
    ids = [p["id"] for p in lst]
    assert rec["id"] in ids
    # 内置在前（anime-fast 等），用户预设追加在后
    assert ids.index("anime-fast") < ids.index(rec["id"])
    mine = next(p for p in lst if p["id"] == rec["id"])
    assert mine["model_id"] == "realesr-animevideov3" and mine["crf"] == 17

    assert client.delete(f"/api/presets/{rec['id']}").json()["ok"] is True
    assert rec["id"] not in [p["id"] for p in client.get("/api/presets").json()]


def test_preset_persists_extra_fields(client):
    r = client.post("/api/presets", json=_body(
        interp="rife2x", denoise=3, deinterlace=True, deband=True,
        container="mkv", audio_mode="copy", icon="🔧"))
    assert r.status_code == 201
    rec = r.json()
    assert rec["interp"] == "rife2x" and rec["denoise"] == 3
    assert rec["deinterlace"] is True and rec["deband"] is True
    assert rec["container"] == "mkv" and rec["audio_mode"] == "copy"
    # 重启语义：重新 load 仍在（文件落盘）
    from sv.server import user_presets

    assert any(p["id"] == rec["id"] for p in user_presets.load())


def test_builtin_preset_not_deletable(client):
    r = client.delete("/api/presets/anime-fast")
    assert r.status_code == 409
    assert "内置" in r.json()["detail"]
    assert any(p["id"] == "anime-fast" for p in client.get("/api/presets").json())


def test_delete_unknown_404(client):
    assert client.delete("/api/presets/user-nope").status_code == 404


def test_create_validation(client):
    # 未知模型
    assert client.post("/api/presets", json=_body(model_id="ghost")).status_code == 404
    # 模型不支持该倍率
    assert client.post("/api/presets", json=_body(model_id="realesrgan-x4plus",
                                                  target_scale=2)).status_code == 400
    # 各枚举字段白名单
    assert client.post("/api/presets", json=_body(codec="mpeg2")).status_code == 400
    assert client.post("/api/presets", json=_body(crf=99)).status_code == 400
    assert client.post("/api/presets", json=_body(container="avi")).status_code == 400
    assert client.post("/api/presets", json=_body(audio_mode="opus")).status_code == 400
    assert client.post("/api/presets", json=_body(interp="rife4x")).status_code == 400
    assert client.post("/api/presets", json=_body(denoise=7)).status_code == 400
    # 名字必填
    assert client.post("/api/presets", json=_body(name="")).status_code == 422

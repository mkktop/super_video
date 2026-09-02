"""更新通道设置：合法值持久化 + 默认 stable（存量用户零迁移）+ 非法值 400。

通道的消费方在 Electron 主进程（electron-updater allowPrerelease），后端只负责
存储与校验——这里钉死白名单，防止 renderer 侧拼错字符串静默吞掉通道切换。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from sv.server import settings as sv_settings

    monkeypatch.setattr(sv_settings, "SETTINGS_PATH", tmp_path / "settings.json")

    from sv.server.app import app

    with TestClient(app) as c:
        yield c


def test_default_is_stable(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["update_channel"] == "stable"


def test_save_preview_persists(client):
    r = client.put("/api/settings", json={"update_channel": "preview"})
    assert r.status_code == 200
    assert r.json()["update_channel"] == "preview"
    # 再拉一次确认真正落盘（读的是同一路径）
    assert client.get("/api/settings").json()["update_channel"] == "preview"


def test_rejects_invalid_channel(client):
    for bad in ("beta", "nightly", "stable ", "", 1, None):
        r = client.put("/api/settings", json={"update_channel": bad})
        assert r.status_code == 400, bad

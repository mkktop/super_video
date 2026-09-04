"""更新下载源设置：合法值持久化 + 默认 auto（存量用户零迁移）+ 非法值 400。

与 update_channel 同构：消费方在 Electron 主进程（检查/下载走哪个源的白名单），
后端只负责存储与校验——这里钉死三档，防止 renderer 侧拼错字符串静默吞掉切换。
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


def test_default_is_auto(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["update_source"] == "auto"


@pytest.mark.parametrize("value", ["github", "r2"])
def test_save_each_choice_persists(client, value):
    r = client.put("/api/settings", json={"update_source": value})
    assert r.status_code == 200
    assert r.json()["update_source"] == value
    # 再拉一次确认真正落盘（读的是同一路径）
    assert client.get("/api/settings").json()["update_source"] == value


def test_rejects_invalid_source(client):
    for bad in ("mirror", "auto ", "AUTO", "", 1, None):
        r = client.put("/api/settings", json={"update_source": bad})
        assert r.status_code == 400, bad

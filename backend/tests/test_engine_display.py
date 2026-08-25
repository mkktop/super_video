"""设置切换后 /api/engine 展示口径：空闲时按设置推算下一任务的后端，任务中如实返回。"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR

os.environ["SV_DB"] = str(TEMP_DIR / "test_engine_display.db")


@pytest.fixture(scope="module")
def client():
    p = Path(os.environ["SV_DB"])
    if p.exists():
        p.unlink()
    from sv.server import app as appmod

    with TestClient(appmod.app) as c:  # 触发 lifespan: init_db + runner
        yield c


def test_engine_display_follows_setting(client, monkeypatch):
    """空闲时展示 = select_engine(设置值)：切到 trt 立即反映，auto 传 None。"""
    from sv.server import app as appmod
    from sv.server.engine_select import EngineChoice

    calls = []

    def fake_select(force=None):
        calls.append(force)
        return EngineChoice("directml" if force is None else str(force), "py", "")

    monkeypatch.setattr(appmod, "select_engine", fake_select)

    orig = client.get("/api/settings").json().get("engine", "auto")
    try:
        assert client.put("/api/settings", json={"engine": "trt"}).status_code == 200
        r = client.get("/api/engine").json()
        assert calls[-1] == "trt"
        assert r["backend"] == "trt"

        client.put("/api/settings", json={"engine": "auto"})
        r = client.get("/api/engine").json()
        assert calls[-1] is None
        assert r["backend"] == "directml"
    finally:
        client.put("/api/settings", json={"engine": orig})


def test_engine_display_while_running(client, monkeypatch):
    """任务进行中返回 runner.engine 实况，不按设置重新推算。"""
    from sv.server import app as appmod
    from sv.server.engine_select import EngineChoice

    def boom(force=None):
        raise AssertionError("任务进行中不应重新探测")

    monkeypatch.setattr(appmod, "select_engine", boom)
    monkeypatch.setattr(appmod.runner, "current_id", "fake-task")
    monkeypatch.setattr(appmod.runner, "engine",
                        EngineChoice("trt", "py", "组件 CUDA + TensorRT"))
    r = client.get("/api/engine").json()
    assert r["backend"] == "trt"
    assert "TensorRT" in r["detail"]

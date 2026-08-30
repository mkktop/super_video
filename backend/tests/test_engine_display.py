"""设置切换后 /api/engine 展示口径：backend 恒为按设置推算的下一任务后端，
任务进行中另附 running 字段如实给出当前任务实际后端。"""
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
    """展示 = select_engine(设置值)：切到 trt 立即反映，auto 传 None；空闲无 running。"""
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
        assert "running" not in r

        client.put("/api/settings", json={"engine": "auto"})
        r = client.get("/api/engine").json()
        assert calls[-1] is None
        assert r["backend"] == "directml"
        assert "running" not in r
    finally:
        client.put("/api/settings", json={"engine": orig})


def test_engine_display_while_running(client, monkeypatch):
    """任务进行中：backend 仍按设置推算（切后端高频时机是任务在跑时，标签必须立即动），
    当前任务实况走 running 字段——两者并排，前端据此提示"下一任务起生效"。"""
    from sv.server import app as appmod
    from sv.server.engine_select import EngineChoice

    def fake_select(force=None):
        return EngineChoice("directml" if force is None else str(force), "py", "")

    monkeypatch.setattr(appmod, "select_engine", fake_select)
    monkeypatch.setattr(appmod.runner, "current_id", "fake-task")
    monkeypatch.setattr(appmod.runner, "engine",
                        EngineChoice("trt", "py", "组件 CUDA + TensorRT"))

    orig = client.get("/api/settings").json().get("engine", "auto")
    try:
        # 设置仍是 directml（或 auto），任务在跑 trt：推算值在前，实况在 running
        client.put("/api/settings", json={"engine": "directml"})
        r = client.get("/api/engine").json()
        assert r["backend"] == "directml"
        assert r["running"]["backend"] == "trt"
        assert "TensorRT" in r["running"]["detail"]

        # 任务中切换设置并查询：标签立即跟随设置，不受运行中任务钉死
        client.put("/api/settings", json={"engine": "trt"})
        r = client.get("/api/engine").json()
        assert r["backend"] == "trt"
        assert r["running"]["backend"] == "trt"
    finally:
        client.put("/api/settings", json={"engine": orig})

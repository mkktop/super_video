"""M2 接口测试：预设 / 探测 / 设置 / 模型管理。"""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.paths import MODELS_DIR, TEMP_DIR

os.environ["SV_DB"] = str(TEMP_DIR / "test_sidecar_m2.db")


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    """设置读写指向临时文件：中途断言失败也不会把代理/引擎设置残留在开发机真实配置。"""
    from sv.server import settings as _settings

    monkeypatch.setattr(_settings, "SETTINGS_PATH", tmp_path / "settings.json")


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
        _make_tiny(clip)  # 自足：字母序 test_m2 先于 test_server，全新环境没人先生成
    r = client.post("/api/probe", json={"path": str(clip)})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["width"] == 160 and d["has_audio"]


def _make_tiny(path: Path):
    import subprocess

    from sv.utils.process import WINDOWS_CREATE_FLAGS
    from sv.paths import ffmpeg_bin

    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", "2", "-c:v", "libx264", "-crf", "22", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


def test_settings_roundtrip(client):
    r = client.put("/api/settings", json={"engine": "cuda"})
    assert r.status_code == 200 and r.json()["engine"] == "cuda"
    r = client.put("/api/settings", json={"engine": "bogus"})
    assert r.status_code == 400
    client.put("/api/settings", json={"engine": "auto"})
    assert client.get("/api/settings").json()["engine"] == "auto"


def test_download_proxy_setting(client):
    """下载代理设置：自定义地址/直连/非法值 的存取与校验。"""
    r = client.put("/api/settings", json={"download_proxy": "http://127.0.0.1:7890"})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["download_proxy"] == "http://127.0.0.1:7890"
    r = client.put("/api/settings", json={"download_proxy": "direct"})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["download_proxy"] == "direct"
    r = client.put("/api/settings", json={"download_proxy": "socks5://127.0.0.1:1080"})
    assert r.status_code == 400  # urllib 只支持 http(s) 代理
    r = client.put("/api/settings", json={"download_proxy": "bogus"})
    assert r.status_code == 400
    client.put("/api/settings", json={"download_proxy": ""})  # 还原默认


def test_download_opener_modes():
    """下载 opener 三态：直连/自定义代理（默认态跟随系统，不做断言）。"""
    import urllib.request

    from sv.models.manager import _opener
    from sv.server.settings import save as save_settings

    def has_proxy(o) -> bool:
        # 空映射的 ProxyHandler 不注册进 handlers（=强制直连），带映射的才算有代理
        return any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in o.handlers)

    # 直连：不得携带任何代理
    save_settings({"download_proxy": "direct"})
    assert not has_proxy(_opener()), "direct 模式不得有代理"
    # 自定义
    save_settings({"download_proxy": "http://127.0.0.1:7890"})
    o = _opener()
    ph = [h for h in o.handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies]
    assert ph and ph[0].proxies.get("https") == "http://127.0.0.1:7890"
    save_settings({"download_proxy": ""})  # 还原默认


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

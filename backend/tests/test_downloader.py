"""模型下载器失败路径（本地 file:// 模拟远端）+ 应用层下载并发守卫/事件广播。

审查 M5 缺口：sha 校验失败、大小不符、镜像回退、已存在跳过此前全无测试；
应用层"下载中拒绝并发"与"失败事件广播"也未覆盖。
"""
import hashlib
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.models.manager import DownloadError, download
from sv.models.registry import ModelSpec
from sv.paths import TEMP_DIR

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_downloader.db"))


def _spec(tmp_path, monkeypatch, files) -> ModelSpec:
    import sv.models.registry as reg

    monkeypatch.setattr(reg, "MODELS_DIR", tmp_path / "models")
    return ModelSpec(
        id="dl-test", name="t", engine="onnx", scale=[2], content=[],
        speed="fast", vram_gb=1, files=files,
    )


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---- manager 层：校验失败 / 镜像回退 / 跳过已存在 ----

def test_download_sha_mismatch(tmp_path, monkeypatch):
    """sha 不符必须失败且不留半成品（.part/dst 都不能有）。"""
    src = tmp_path / "a.onnx"
    src.write_bytes(b"payload-abc")
    spec = _spec(tmp_path, monkeypatch, [{
        "name": "a.onnx", "url": src.as_uri(),
        "size": src.stat().st_size, "sha256": "00" * 32,
    }])
    with pytest.raises(DownloadError, match="sha256"):
        download(spec)
    d = tmp_path / "models" / "dl-test"
    assert not (d / "a.onnx").exists()
    assert not list(d.glob("*.part")), "校验失败后不得留下 .part 残片"


def test_download_size_mismatch(tmp_path, monkeypatch):
    src = tmp_path / "b.onnx"
    src.write_bytes(b"0123456789")
    spec = _spec(tmp_path, monkeypatch, [{
        "name": "b.onnx", "url": src.as_uri(), "size": 999,
    }])
    with pytest.raises(DownloadError, match="大小不符"):
        download(spec)


def test_download_mirror_fallback(tmp_path, monkeypatch):
    """主源失败 → 依次镜像重试，成功落盘且内容一致；source_cb 按实际发起顺序回调。"""
    good = tmp_path / "good.onnx"
    good.write_bytes(b"mirror-content")
    dead = tmp_path / "no-such-file.onnx"  # 主源 404（file:// 打不开 → URLError）
    spec = _spec(tmp_path, monkeypatch, [{
        "name": "m.onnx", "url": dead.as_uri(),
        "size": good.stat().st_size, "sha256": _sha(good),
        "mirror_urls": [dead.as_uri(), good.as_uri()],  # 第一个镜像也挂，验证逐个尝试
    }])
    sources: list[str] = []
    download(spec, source_cb=lambda url, name: sources.append(url))
    dest = tmp_path / "models" / "dl-test" / "m.onnx"
    assert dest.read_bytes() == b"mirror-content"
    assert sources == [dead.as_uri(), dead.as_uri(), good.as_uri()]


def test_download_source_cb_primary(tmp_path, monkeypatch):
    """主源一次成功的常规路径：source_cb 恰好回调一次、带主源 URL。"""
    src = tmp_path / "ok.onnx"
    src.write_bytes(b"primary-content")
    spec = _spec(tmp_path, monkeypatch, [{
        "name": "p.onnx", "url": src.as_uri(),
        "size": src.stat().st_size, "sha256": _sha(src),
    }])
    sources: list[str] = []
    download(spec, source_cb=lambda url, name: sources.append(url))
    assert sources == [src.as_uri()]


def test_download_all_mirrors_exhausted(tmp_path, monkeypatch):
    good = tmp_path / "g.onnx"
    good.write_bytes(b"x")
    dead = tmp_path / "missing.onnx"
    spec = _spec(tmp_path, monkeypatch, [{
        "name": "m.onnx", "url": dead.as_uri(),
        "mirror_urls": [dead.as_uri()],
    }])
    with pytest.raises(DownloadError, match="镜像"):
        download(spec)


def test_download_skips_existing_valid_file(tmp_path, monkeypatch):
    """已存在且 sha 正确的文件跳过下载（url 指向不存在的源，一旦真下载必失败）。"""
    dest_dir = tmp_path / "models" / "dl-test"
    dest_dir.mkdir(parents=True)
    (dest_dir / "ok.onnx").write_bytes(b"already-here")
    dead = tmp_path / "missing2.onnx"
    spec = _spec(tmp_path, monkeypatch, [{
        "name": "ok.onnx", "url": dead.as_uri(),
        "size": 12, "sha256": _sha(dest_dir / "ok.onnx"),
    }])
    download(spec)  # 不抛即证明跳过了下载
    assert (dest_dir / "ok.onnx").read_bytes() == b"already-here"


# ---- 应用层：并发守卫 / 失败事件广播 ----

@pytest.fixture()
def dl_client(monkeypatch):
    import sv.server.app as app_mod

    rec: list[dict] = []

    def rec_publish(ev: dict) -> None:
        rec.append(ev)

    monkeypatch.setattr(app_mod.bus, "publish", rec_publish)
    with TestClient(app_mod.app) as c:  # lifespan：db/runner（队列空转，无干扰）
        yield app_mod, c, rec, monkeypatch


def _wait(cond, timeout_s=15.0, step=0.1) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if cond():
            return
        time.sleep(step)
    raise TimeoutError("等待条件超时")


def test_download_concurrent_rejected(dl_client):
    """下载进行中第二个请求 409；完成后广播 done 事件。"""
    app_mod, c, rec, monkeypatch = dl_client
    started = threading.Event()
    release = threading.Event()

    def fake_download(spec, cb=None, only_files=None, source_cb=None):
        started.set()
        release.wait(20)

    monkeypatch.setattr(app_mod.manager, "is_downloaded", lambda spec: False)
    monkeypatch.setattr(app_mod.manager, "download", fake_download)
    monkeypatch.setattr("sv.models.fp16.ensure_fp16", lambda spec: [])

    r1 = c.post("/api/models/realesr-animevideov3/download")
    assert r1.status_code == 200 and r1.json()["started"] is True
    _wait(started.is_set)  # 等 fake 真正拿到锁，排除 409 的时序偶然
    r2 = c.post("/api/models/realesr-animevideov3/download")
    assert r2.status_code == 409, "下载进行中必须拒绝并发请求"
    release.set()
    _wait(lambda: any(e.get("type") == "model_download" and e.get("done")
                      for e in rec))


def test_download_failure_broadcast(dl_client):
    """下载异常必须广播 failed 事件（UI 进度条据此解除卡住）。"""
    app_mod, c, rec, monkeypatch = dl_client

    def boom(spec, cb=None, only_files=None, source_cb=None):
        raise DownloadError("模拟网络故障")

    monkeypatch.setattr(app_mod.manager, "is_downloaded", lambda spec: False)
    monkeypatch.setattr(app_mod.manager, "download", boom)
    r = c.post("/api/models/realesr-animevideov3/download")
    assert r.status_code == 200
    _wait(lambda: any(e.get("type") == "model_download" and e.get("failed")
                      for e in rec))
    ev = next(e for e in rec if e.get("failed"))
    assert "模拟网络故障" in ev["failed"]


def test_download_immediate_zero_progress_event(dl_client):
    """点击响应性：端点返回前就必须广播 0% 进度事件。

    真实进度要等远端连上并收到首块数据才会来（直连 GitHub 常以十秒计），
    UI 靠这条事件即时出进度条——回归口径：POST 同步返回后 rec 里已有。
    """
    app_mod, c, rec, monkeypatch = dl_client
    gate = threading.Event()

    def fake_download(spec, cb=None, only_files=None, source_cb=None):
        gate.wait(20)

    monkeypatch.setattr(app_mod.manager, "is_downloaded", lambda spec: False)
    monkeypatch.setattr(app_mod.manager, "download", fake_download)
    r = c.post("/api/models/realesr-animevideov3/download")
    assert r.status_code == 200
    assert any(e.get("type") == "model_download" and e.get("progress") == 0 for e in rec), \
        "端点返回前必须已广播 0% 进度事件"
    gate.set()

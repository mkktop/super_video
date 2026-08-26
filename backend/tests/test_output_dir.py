"""全局输出目录设置：任务/剪切创建时未显式指定输出 → 设置目录优先、
不存在自动创建、指向文件路径时报可读 400；显式 output 始终最高优先。
"""
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.utils.process import WINDOWS_CREATE_FLAGS

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_outdir.db"))


@pytest.fixture(scope="module")
def client():
    from sv.server.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    """设置读写指向临时文件，不污染开发机真实配置。"""
    from sv.server import settings as _settings

    monkeypatch.setattr(_settings, "SETTINGS_PATH", tmp_path / "data" / "settings.json")


def _make_clip(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=64x36:rate=24",
         "-frames:v", "6", "-c:v", "libx264", "-crf", "22",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


@pytest.fixture(scope="module")
def model_scale(client):
    models = client.get("/api/models").json()
    for m in models:
        if m.get("kind") != "interp" and 2 in (m.get("scale") or []):
            return m["id"]
    pytest.fail("注册表中无支持 x2 的超分模型，测试无法构造合法任务")


def _set_output_dir(client, d: str):
    r = client.put("/api/settings", json={"output_dir": d})
    assert r.status_code == 200, r.text


def test_default_beside_source_when_unset(client, model_scale, tmp_path):
    src = tmp_path / "src" / "clip.mp4"
    _make_clip(src)
    r = client.post("/api/tasks", json={
        "input": str(src), "model_id": model_scale,
        "params": {"scale": 2, "target_scale": 2},
    })
    assert r.status_code == 201, r.text
    out = Path(r.json()["output_path"])
    assert out.parent == src.parent and out.name == "clip_2x.mp4"


def test_task_routes_to_output_dir_and_creates_it(client, model_scale, tmp_path):
    outdir = tmp_path / "deep" / "nested" / "newdir"  # 不存在：应自动创建
    _set_output_dir(client, str(outdir))
    assert client.get("/api/settings").json()["output_dir"] == str(outdir)
    src = tmp_path / "src" / "clip.mp4"
    _make_clip(src)
    r = client.post("/api/tasks", json={
        "input": str(src), "model_id": model_scale,
        "params": {"scale": 2, "target_scale": 2},
    })
    assert r.status_code == 201, r.text
    out = Path(r.json()["output_path"])
    # 目录内无同名 → 沿用原文件名（不加 _2x 后缀）
    assert out.parent == outdir and out.name == "clip.mp4"
    assert outdir.is_dir(), "输出目录未自动创建"


def test_output_name_collision_falls_back_to_suffix(client, model_scale, tmp_path):
    """目标目录已有同名文件（上次产物/用户文件）→ 退 _倍率 后缀，不覆盖。"""
    outdir = tmp_path / "outs"
    outdir.mkdir()
    (outdir / "clip.mp4").write_bytes(b"existing precious file")
    _set_output_dir(client, str(outdir))
    src = tmp_path / "src" / "clip.mp4"
    _make_clip(src)
    r = client.post("/api/tasks", json={
        "input": str(src), "model_id": model_scale,
        "params": {"scale": 2, "target_scale": 2},
    })
    assert r.status_code == 201, r.text
    out = Path(r.json()["output_path"])
    assert out.name == "clip_2x.mp4", "同名冲突必须退后缀，不得覆盖现有文件"
    assert (outdir / "clip.mp4").read_bytes() == b"existing precious file"


def test_explicit_output_beats_setting(client, model_scale, tmp_path):
    _set_output_dir(client, str(tmp_path / "should_not_used"))
    src = tmp_path / "src" / "clip.mp4"
    _make_clip(src)
    explicit = tmp_path / "custom" / "named.mp4"
    r = client.post("/api/tasks", json={
        "input": str(src), "output": str(explicit), "model_id": model_scale,
        "params": {"scale": 2, "target_scale": 2},
    })
    assert r.status_code == 201, r.text
    assert Path(r.json()["output_path"]) == explicit


def test_output_dir_pointing_at_file_gives_400(client, model_scale, tmp_path):
    blocker = tmp_path / "occupied.mp4"
    _make_clip(blocker)  # 目录位置被一个文件占住：mkdir 必失败
    _set_output_dir(client, str(blocker))
    src = tmp_path / "src" / "clip.mp4"
    _make_clip(src)
    r = client.post("/api/tasks", json={
        "input": str(src), "model_id": model_scale,
        "params": {"scale": 2, "target_scale": 2},
    })
    assert r.status_code == 400
    assert "无法创建输出目录" in r.json()["detail"]


def test_trim_routes_to_output_dir(client, tmp_path):
    outdir = tmp_path / "trims"
    _set_output_dir(client, str(outdir))
    src = tmp_path / "src" / "clip.mp4"
    _make_clip(src)
    r = client.post("/api/trim", json={
        "input": str(src), "start_s": 0.0, "end_s": 0.2, "mode": "fast",
    })
    assert r.status_code == 201, r.text
    assert Path(r.json()["output"]).parent == outdir
    assert outdir.is_dir(), "剪切输出目录未自动创建"
    # 立即取消：本测试只验证路径推导，不让 ffmpeg 跑完浪费时长
    jid = r.json()["job_id"]
    deadline = __import__("time").time() + 10
    while __import__("time").time() < deadline:
        if client.get(f"/api/trim/{jid}").json()["state"] in ("queued", "running"):
            break
    client.post(f"/api/trim/{jid}/cancel")


def test_output_dir_type_validation(client):
    r = client.put("/api/settings", json={"output_dir": 12345})
    assert r.status_code == 400

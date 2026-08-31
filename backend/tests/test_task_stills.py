"""任务对比页多帧静帧：状态轮询/懒构建、三种任务形态路由、失效重建与缓存管理。

ffmpeg 真跑（64x36 小片段）+ PIL 造序列帧，不依赖 GPU 与模型注册表——
只测 task_stills.py 的胶水层（选帧/抽帧/缓存失效/清理链路）。
"""
import io
import os
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.utils.process import WINDOWS_CREATE_FLAGS

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_task_stills.db"))


def _make_clip(path: Path, frames: int = 24):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=64x36:rate=24",
         "-frames:v", str(frames), "-c:v", "libx264", "-crf", "22",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """独立库 + 静帧根/设置隔离；造好源/输出视频供任务行引用。"""
    monkeypatch.setenv("SV_DB", str(tmp_path / "ts.db"))
    from sv.server import db as sv_db
    from sv.server import compare, task_stills
    from sv.server import settings as settings_mod

    sv_db.init_db()
    monkeypatch.setattr(sv_db, "next_queued", lambda: None)
    monkeypatch.setattr(task_stills, "STILLS_ROOT", tmp_path / "task_stills")
    monkeypatch.setattr(compare, "COMPARE_ROOT", tmp_path / "compare")
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    task_stills._BUILDING.clear()
    task_stills._FAILED.clear()
    task_stills._FAIL_SIG.clear()

    src, out = tmp_path / "s.mp4", tmp_path / "o.mp4"
    _make_clip(src)
    _make_clip(out)
    from sv.server.app import app

    with TestClient(app) as c:
        yield c, tmp_path, src, out


def _mk_task(src, out, status="done", params=None) -> str:
    from sv.server import db

    tid = db.new_task(str(src), str(out), "realesr-animevideov3",
                      params if params is not None else
                      {"scale": 2, "target_scale": 2, "out_kind": "video"},
                      src={"w": 64, "h": 36, "fps": 24, "total_frames": 24})["id"]
    db.update_task(tid, status=status)
    return tid


def _wait_ready(client, tid, timeout=60):
    """轮询状态到脱离 building；返回最终 JSON。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/tasks/{tid}/stills")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] != "building":
            return body
        time.sleep(0.1)
    pytest.fail("静帧构建超时")


def test_video_task_stills_roundtrip(client):
    c, tmp, src, out = client
    tid = _mk_task(src, out)
    body = _wait_ready(c, tid)
    assert body["status"] == "ready"
    assert body["count"] == 4  # 默认样本数
    assert body["built_at"] > 0
    for i in range(4):
        for q in ("src=1", "src=0"):
            r = c.get(f"/api/tasks/{tid}/stills/{i}?{q}")
            assert r.status_code == 200, f"i={i} {q}: {r.status_code}"
            assert r.headers["content-type"] == "image/png"
            im = Image.open(io.BytesIO(r.content))
            assert im.size == (64, 36)  # 源/输出同素材同尺寸（无损 PNG）
    assert c.get(f"/api/tasks/{tid}/stills/4?src=1").status_code == 404  # 越界
    assert c.get("/api/tasks/ghost/stills").status_code == 404


def test_stills_cache_hit_no_rebuild(client, monkeypatch):
    """第二次查询直接命中缓存：不再起构建线程。"""
    c, tmp, src, out = client
    tid = _mk_task(src, out)
    _wait_ready(c, tid)
    from sv.server import task_stills

    def _boom(*a, **k):
        raise AssertionError("缓存命中时不应重建")

    monkeypatch.setattr(task_stills, "_build", _boom)
    body = c.get(f"/api/tasks/{tid}/stills").json()
    assert body["status"] == "ready"


def test_image_task_unsupported(client):
    c, tmp, src, out = client
    png = tmp / "in.png"
    Image.fromarray(np.zeros((16, 16, 3), dtype=np.uint8)).save(png)
    tid = _mk_task(png, tmp / "out.png", params={"images": [{"in": str(png)}]})
    body = c.get(f"/api/tasks/{tid}/stills").json()
    assert body["status"] == "unsupported"
    assert c.get(f"/api/tasks/{tid}/stills/0?src=1").status_code == 404
    # 未完成任务同样不支持（对比页回落预览对/占位）
    tid2 = _mk_task(src, out, status="running")
    assert c.get(f"/api/tasks/{tid2}/stills").json()["status"] == "unsupported"


def test_missing_output_unsupported(client):
    c, tmp, src, out = client
    out.unlink()
    tid = _mk_task(src, out)
    assert c.get(f"/api/tasks/{tid}/stills").json()["status"] == "unsupported"


def test_seq_task_stills(client):
    c, tmp, src, out = client
    frames = tmp / "frames"
    frames.mkdir()
    for i in range(1, 25):  # 000001.png 起编号（主任务序列输出的命名系）
        arr = np.full((36, 64, 3), 40 + i * 8, dtype=np.uint8)  # 亮帧，避开黑场
        Image.fromarray(arr).save(frames / f"{i:06d}.png")
    tid = _mk_task(src, frames, params={"scale": 2, "target_scale": 2, "out_kind": "png"})
    body = _wait_ready(c, tid)
    assert body["status"] == "ready"
    assert body["count"] == 4
    for i in range(4):
        r = c.get(f"/api/tasks/{tid}/stills/{i}")
        assert r.status_code == 200
        assert Image.open(io.BytesIO(r.content)).size == (64, 36)
    # 帧不够样本数 → failed（前端回落单帧预览），错误信息可读
    tiny = tmp / "tiny"
    tiny.mkdir()
    Image.fromarray(np.full((36, 64, 3), 200, dtype=np.uint8)).save(tiny / "000001.png")
    tid2 = _mk_task(src, tiny, params={"scale": 2, "target_scale": 2, "out_kind": "png"})
    body2 = _wait_ready(c, tid2)
    assert body2["status"] == "failed"
    assert "不足" in body2["error"]


def test_setting_change_rebuilds(client):
    c, tmp, src, out = client
    from sv.server import settings as settings_mod

    tid = _mk_task(src, out)
    assert _wait_ready(c, tid)["count"] == 4
    settings_mod.save({"compare_still_count": 2})
    body = _wait_ready(c, tid)
    assert body["status"] == "ready"
    assert body["count"] == 2
    assert c.get(f"/api/tasks/{tid}/stills/2?src=1").status_code == 404  # 旧索引已失效
    # 样本数 1 = 无多帧概念：回落单帧预览对
    settings_mod.save({"compare_still_count": 1})
    assert c.get(f"/api/tasks/{tid}/stills").json()["status"] == "unsupported"


def test_delete_task_clears_cache(client):
    c, tmp, src, out = client
    from sv.server import task_stills

    tid = _mk_task(src, out)
    _wait_ready(c, tid)
    assert (task_stills.STILLS_ROOT / tid).is_dir()
    assert c.delete(f"/api/tasks/{tid}").status_code == 200
    deadline = time.time() + 15  # purge 在后台线程，轮询目录消失
    while time.time() < deadline and (task_stills.STILLS_ROOT / tid).exists():
        time.sleep(0.1)
    assert not (task_stills.STILLS_ROOT / tid).exists()


def test_compare_cache_card_includes_stills(client):
    c, tmp, src, out = client
    tid = _mk_task(src, out)
    _wait_ready(c, tid)
    stats = c.get("/api/compare/cache").json()
    assert stats["jobs"] >= 1 and stats["bytes"] > 0
    r = c.delete("/api/compare/cache")
    assert r.status_code == 200
    assert r.json()["removed_jobs"] >= 1
    stats2 = c.get("/api/compare/cache").json()
    assert stats2 == {"jobs": 0, "bytes": 0}


def test_pick_seq_frames_avoids_dark(tmp_path):
    """段中点为锚、段内避黑：亮帧可达时不选黑帧；整段全黑则保留段内锚点。"""
    from sv.server.task_stills import _pick_seq_frames

    bright = np.full((8, 8, 3), 200, dtype=np.uint8)
    dark = np.full((8, 8, 3), 5, dtype=np.uint8)
    frames = []
    for i in range(16):
        arr = dark if 4 <= i < 8 else bright  # 第 2 段（4~7）整段黑场
        p = tmp_path / f"frame_{i:02d}.png"
        Image.fromarray(arr).save(p)
        frames.append(p)
    picks = _pick_seq_frames(frames, 4)
    assert picks[1] in (4, 5, 6, 7)  # 全黑段：如实保留段内样本
    for i in (0, 2, 3):
        assert picks[i] not in (4, 5, 6, 7)  # 其余段避黑成功

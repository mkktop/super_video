"""对比分享卡片：长图/滑块 GIF 合成（真实抽帧）、任务端点链路（404/409/201/取文件）。"""
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.sharecard import CARD_W, GIF_W, extract_frame, make_long_image, make_slider_gif
from sv.utils.process import WINDOWS_CREATE_FLAGS


def make_clip(path: Path, duration=2.0) -> None:
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
         "-t", str(duration), "-c:v", "libx264", "-crf", "20",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


@pytest.fixture(scope="module")
def clips():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    src = TEMP_DIR / "sc_src.mp4"
    out = TEMP_DIR / "sc_out.mp4"
    make_clip(src)
    make_clip(out)
    return {"src": src, "out": out}


# ---- 合成函数（真实 ffmpeg 抽帧） ----


def test_extract_frame_dimensions(clips, tmp_path):
    img = extract_frame(clips["src"], 0.5, CARD_W)
    assert img.size[0] == CARD_W
    assert abs(img.size[1] - round(360 * CARD_W / 640)) <= 2


def test_long_image_layout(clips, tmp_path):
    dest = make_long_image(clips["src"], clips["out"], 0.5, 0.5,
                           {"model": "测试模型", "scale": "x2", "name": "demo",
                            "date": "2026-08-30"}, tmp_path / "card.png")
    assert dest.exists()
    img = Image.open(dest)
    assert img.size[0] == CARD_W
    # 信息条 + 两张画面 + 标签行：总高明显大于两倍画面高
    frame_h = round(360 * CARD_W / 640)
    assert img.size[1] > frame_h * 2


def test_slider_gif_animated(clips, tmp_path):
    dest = make_slider_gif(clips["src"], clips["out"], 0.5, 0.5,
                           {"model": "m", "scale": "x2", "name": "demo",
                            "date": "d"}, tmp_path / "card.gif")
    assert dest.exists()
    img = Image.open(dest)
    assert img.n_frames >= 20  # 来回扫至少 2×10 帧
    assert img.size[0] == GIF_W
    assert img.info.get("loop") == 0  # 无限循环（晒图场景）


# ---- 端点链路 ----


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SV_DB", str(tmp_path / "sc.db"))
    from sv.server import db as sv_db

    sv_db.init_db()
    monkeypatch.setattr(sv_db, "next_queued", lambda: None)
    src = tmp_path / "s.mp4"
    out = tmp_path / "o.mp4"
    make_clip(src, 1.5)
    make_clip(out, 1.5)
    from sv.server.app import app

    with TestClient(app) as c:
        yield c, src, out, tmp_path


def _mk_done_task(src, out) -> str:
    from sv.server import db

    tid = db.new_task(str(src), str(out), "realesr-animevideov3",
                      {"scale": 2, "target_scale": 2},
                      src={"w": 640, "h": 360, "fps": 24, "total_frames": 36})["id"]
    db.update_task(tid, status="done")
    return tid


def _mk_task_status(src, out, status) -> str:
    from sv.server import db

    t = db.new_task(str(src), str(out), "realesr-animevideov3", {},
                    src={"w": 640, "h": 360, "fps": 24, "total_frames": 36})
    db.update_task(t["id"], status=status)
    return t["id"]


def test_share_card_endpoint_roundtrip(client):
    c, src, out, tmp = client
    tid = _mk_done_task(src, out)
    r = c.post(f"/api/tasks/{tid}/share-card", json={"kind": "image"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert Path(body["path"]).exists()
    f = c.get(f"/api/tasks/{tid}/share-card/file?kind=image")
    assert f.status_code == 200
    assert f.headers["content-type"].startswith("image/png")

    r2 = c.post(f"/api/tasks/{tid}/share-card", json={"kind": "gif"})
    assert r2.status_code == 201
    f2 = c.get(f"/api/tasks/{tid}/share-card/file?kind=gif")
    assert f2.status_code == 200
    assert f2.headers["content-type"].startswith("image/gif")


def test_share_card_endpoint_errors(client):
    c, src, out, tmp = client
    assert c.post("/api/tasks/ghost/share-card", json={"kind": "image"}).status_code == 404
    # 未完成的任务不给生成
    tid = _mk_task_status(src, out, "failed")
    assert c.post(f"/api/tasks/{tid}/share-card", json={"kind": "image"}).status_code == 409
    assert c.post(f"/api/tasks/{tid}/share-card", json={"kind": "mp4"}).status_code == 400
    assert c.get(f"/api/tasks/{tid}/share-card/file?kind=image").status_code == 404


def test_share_card_image_sequence_rejected(client, tmp_path):
    """图片序列输出是目录：明确 409 而不是拿目录当视频抽帧。"""
    c, src, _, _ = client
    from sv.server import db

    d = tmp_path / "frames_out"
    d.mkdir()
    t = db.new_task(str(src), str(d), "realesr-animevideov3",
                    {"out_kind": "png"}, src={"w": 640, "h": 360, "fps": 24,
                                              "total_frames": 36})
    db.update_task(t["id"], status="done")
    assert c.post(f"/api/tasks/{t['id']}/share-card", json={"kind": "image"}).status_code == 409

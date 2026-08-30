"""输出命名模板：变量渲染、非法字符兜底、创建端点落到视频/图片/图片序列、
冲突不覆盖语义保持、设置校验。"""
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.server.app import _render_output_stem
from sv.utils.process import WINDOWS_CREATE_FLAGS


# ---- 渲染函数单测 ----


def test_render_variables():
    out = _render_output_stem("{name}_{model}_{scale}", "clip", "realesr-animevideov3",
                              "2x", 1920, 1080)
    assert out == "clip_realesr-animevideov3_2x"
    assert _render_output_stem("{res}", "c", "m", "2x", 3840, 2160) == "3840x2160"
    # {date} 当天日期（8 位）
    assert len(_render_output_stem("{date}", "c", "m", "2x", 1, 1)) == 8


def test_render_empty_template_keeps_legacy_stem():
    assert _render_output_stem("", "orig", "m", "2x", 1, 1) == "orig"


def test_render_unknown_var_left_as_is():
    """未知变量原样保留：用户能一眼看出 {nmae} 拼错了。"""
    assert _render_output_stem("{name}_{oops}", "clip", "m", "2x", 1, 1) == "clip_{oops}"


def test_render_sanitizes_illegal_chars():
    out = _render_output_stem("{name}<:>ok", "clip", "m", "2x", 1, 1)
    assert out == "clip___ok"


def test_render_degenerate_falls_back_to_stem():
    # 渲染后只剩点/空白：回退原名（不产出 ".mp4" 这种垃圾名）
    assert _render_output_stem("{name}", "..", "m", "2x", 1, 1) == ".."  # stem 本身是点串仍保留
    assert _render_output_stem("...", "clip", "m", "2x", 1, 1) == "clip"


# ---- 端点级：模板经创建路径落到产物名 ----


def make_clip(path: Path) -> None:
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
         "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SV_DB", str(tmp_path / "nt.db"))
    from sv.server import db as sv_db

    sv_db.init_db()
    monkeypatch.setattr(sv_db, "next_queued", lambda: None)
    clip = tmp_path / "a_clip.mp4"
    make_clip(clip)
    from sv.server.app import app

    with TestClient(app) as c:
        yield c, str(clip), tmp_path


def _set_template(client, tpl: str):
    assert client.put("/api/settings", json={"output_name_template": tpl}).status_code == 200


def test_video_task_uses_template(client):
    c, clip, tmp = client
    _set_template(c, "{name}_{model}_{scale}")
    r = c.post("/api/tasks", json={"input": clip, "model_id": "realesr-animevideov3",
                                   "params": {"scale": 2, "target_scale": 2}})
    assert r.status_code == 201, r.text
    out = r.json()["output_path"]
    assert out.endswith("a_clip_realesr-animevideov3_2x.mp4")


def test_template_conflict_appends_suffix_no_overwrite(client):
    c, clip, tmp = client
    _set_template(c, "{name}_{scale}")
    # 预占渲染后的目标名：冲突应退 _倍率 后缀（res_label 再拼一次），不覆盖
    blocker = Path(clip).parent / "a_clip_2x.mp4"
    blocker.write_bytes(b"existing")
    r = c.post("/api/tasks", json={"input": clip, "model_id": "realesr-animevideov3",
                                   "params": {"scale": 2, "target_scale": 2}})
    out = r.json()["output_path"]
    assert Path(out).name != "a_clip_2x.mp4"
    assert blocker.read_bytes() == b"existing"


def test_empty_template_keeps_legacy_naming(client):
    c, clip, tmp = client
    _set_template(c, "")
    r = c.post("/api/tasks", json={"input": clip, "model_id": "realesr-animevideov3",
                                   "params": {"scale": 2, "target_scale": 2}})
    out = r.json()["output_path"]
    # 源同目录同名即源本身（mp4→mp4）→ 老规则退 _倍率 后缀
    assert Path(out).name == "a_clip_2x.mp4"


def test_image_sequence_dir_uses_template(client):
    c, clip, tmp = client
    _set_template(c, "{name}-SR-{scale}")
    r = c.post("/api/tasks", json={"input": clip, "model_id": "realesr-animevideov3",
                                   "params": {"out_kind": "png", "scale": 2,
                                              "target_scale": 2}})
    out = r.json()["output_path"]
    assert Path(out).name == "a_clip-SR-2x_2x_frames"


def test_image_batch_renders_per_image(client, tmp_path):
    c, _, tmp = client
    from PIL import Image

    imgs = []
    for n in ("one", "two"):
        p = tmp_path / f"{n}.png"
        Image.new("RGB", (64, 64), "red").save(p)
        imgs.append(str(p))
    _set_template(c, "{name}_{model}")
    r = c.post("/api/tasks", json={"inputs": imgs, "model_id": "realesr-animevideov3",
                                   "params": {}})
    assert r.status_code == 201, r.text
    names = [Path(m["out"]).name for m in r.json()["params"]["images"]]
    assert names[0].startswith("one_realesr-animevideov3")
    assert names[1].startswith("two_realesr-animevideov3")


def test_settings_rejects_illegal_template(client):
    c, _, _ = client
    for bad in ('a/b', 'a\\b', 'a:b', 'a*b', 'a?b'):
        r = c.put("/api/settings", json={"output_name_template": bad})
        assert r.status_code == 400, bad
    assert c.put("/api/settings", json={"output_name_template": "{name}_{scale}"}).status_code == 200

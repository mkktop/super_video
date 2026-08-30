"""反交错/去色带：滤镜链拼接、解码命令挂点、三管线透传、预验证带同款滤镜、
创建端点校验与真实管线端到端（帧数不变式）。"""
import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.chunked import ChunkedPipeline
from sv.pipeline.probe import probe
from sv.pipeline.segmented import SegmentedPipeline
from sv.pipeline.stream import (
    EncodeOpts,
    StreamPipeline,
    decoder_cmd,
    prefilter_chain,
)
from sv.utils.process import WINDOWS_CREATE_FLAGS


class Nearest2x:
    scale = 2

    def process(self, frame):
        import numpy as np

        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


def make_video(path: Path, duration=2) -> None:
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
         "-t", str(duration), "-c:v", "libx264", "-crf", "20",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


# ---- 滤镜链与命令挂点（纯构造，零 ffmpeg） ----


def test_prefilter_chain_composition():
    assert prefilter_chain() is None
    assert prefilter_chain(False, False) is None
    assert prefilter_chain(True, False) == "bwdif=mode=send_frame"
    assert prefilter_chain(False, True) == "deband"
    # 反交错在前、去色带在后（梳齿会干扰 deband 的渐变检测，顺序是功能语义）
    assert prefilter_chain(True, True) == "bwdif=mode=send_frame,deband"


def test_decoder_cmd_vf_position():
    """-vf 是输出侧选项须在 -i 之后；关闭时完全不出现（与无滤镜命令逐字节一致）。"""
    vf = prefilter_chain(True, True)
    on = decoder_cmd(Path("in.mp4"), hwaccel="cuda", vf=vf)
    i_pos = on.index("-i")
    vf_pos = on.index("-vf")
    assert i_pos < vf_pos < on.index("-f")
    assert on[vf_pos + 1] == "bwdif=mode=send_frame,deband"
    assert on[on.index("-hwaccel") + 1] == "cuda"

    assert decoder_cmd(Path("in.mp4")) == decoder_cmd(Path("in.mp4"), vf=None)
    assert "-vf" not in decoder_cmd(Path("in.mp4"))


def test_pipelines_accept_decode_vf(tmp_path):
    """三条管线都接收 decode_vf 并透传到解码命令层。"""
    s = StreamPipeline(
        tmp_path / "in.mp4", tmp_path / "o.mp4", Nearest2x(),
        encode=EncodeOpts(), decode_hwaccel="cuda",
        decode_vf="bwdif=mode=send_frame",
    )
    assert s.decode_vf == "bwdif=mode=send_frame"
    seg = SegmentedPipeline(
        tmp_path / "in.mp4", tmp_path / "o2.mp4", Nearest2x(),
        task_id="t-vf", decode_vf="deband",
    )
    assert seg.decode_vf == "deband"
    ck = ChunkedPipeline(
        tmp_path / "in.mp4", tmp_path / "o3.mp4", Nearest2x(),
        task_id="t-vf2", decode_vf="deband",
    )
    assert ck.decode_vf == "deband"


def test_probe_hwaccel_includes_vf(monkeypatch, tmp_path):
    """硬解预验证命令必须带同一滤镜链：滤镜×硬解组合兼容性要与真实解码一致地测。"""
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = b""

        return R()

    monkeypatch.setattr("sv.pipeline.probe.subprocess.run", fake_run)
    from sv.pipeline.probe import probe_hwaccel

    video = tmp_path / "v.mp4"
    assert probe_hwaccel(video, "cuda", "h264", vf="bwdif=mode=send_frame,deband")
    cmd = captured["cmd"]
    assert cmd[cmd.index("-vf") + 1] == "bwdif=mode=send_frame,deband"
    assert cmd[cmd.index("-hwaccel") + 1] == "cuda"
    assert "-vf" in cmd and cmd.index("-vf") > cmd.index("-i")


# ---- 创建端点校验 ----


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """任务只入库不执行（禁掉取队列），专测创建期校验与 params 落库。"""
    db_file = tmp_path / "pf.db"
    monkeypatch.setenv("SV_DB", str(db_file))
    from sv.server import db as sv_db

    sv_db.init_db()
    monkeypatch.setattr(sv_db, "next_queued", lambda: None)

    clip = tmp_path / "clip.mp4"
    make_video(clip, duration=1)

    from sv.server.app import app

    with TestClient(app) as c:
        yield c, str(clip)


def test_create_task_accepts_prefilters(client):
    c, clip = client
    r = c.post("/api/tasks", json={
        "input": clip, "model_id": "realesr-animevideov3",
        "params": {"deinterlace": True, "deband": True},
    })
    assert r.status_code == 201, r.text
    p = r.json()["params"]
    assert p["deinterlace"] is True
    assert p["deband"] is True


def test_create_task_defaults_prefilters_off(client):
    c, clip = client
    r = c.post("/api/tasks", json={"input": clip, "model_id": "realesr-animevideov3",
                                   "params": {}})
    assert r.status_code == 201
    p = r.json()["params"]
    assert p["deinterlace"] is False
    assert p["deband"] is False


def test_create_task_rejects_non_bool(client):
    c, clip = client
    for key in ("deinterlace", "deband"):
        r = c.post("/api/tasks", json={
            "input": clip, "model_id": "realesr-animevideov3",
            "params": {key: "yes"},
        })
        assert r.status_code == 400, f"{key} 非布尔应 400"
        assert key in r.json()["detail"]


def test_image_task_drops_prefilters(client, tmp_path):
    """图片任务自成 params（不读视频字段）：预处理对单帧无意义，静默丢弃。"""
    c, _ = client
    from PIL import Image

    img = tmp_path / "pic.png"
    Image.new("RGB", (64, 64), "green").save(img)
    r = c.post("/api/tasks", json={
        "inputs": [str(img)], "model_id": "realesr-animevideov3",
        "params": {"deinterlace": True, "deband": True},
    })
    assert r.status_code == 201, r.text
    p = r.json()["params"]
    assert "deinterlace" not in p and "deband" not in p


# ---- 真实管线端到端：滤镜不破坏帧数不变式（checkpoint 语义的硬前提） ----


def test_segmented_with_prefilters_frame_count_invariant(tmp_path):
    clip = tmp_path / "pf_e2e.mp4"
    make_video(clip, duration=2)  # 48 帧
    info = probe(clip)

    plain = SegmentedPipeline(
        info, tmp_path / "plain.mp4", Nearest2x(), encode=EncodeOpts(),
        task_id="t-plain", seg_frames=12,
    )
    stats_plain = asyncio.run(plain.run())

    filtered = SegmentedPipeline(
        info, tmp_path / "filt.mp4", Nearest2x(), encode=EncodeOpts(),
        task_id="t-filt", seg_frames=12,
        decode_vf=prefilter_chain(True, True),
    )
    stats_filt = asyncio.run(filtered.run())

    assert stats_filt.frames == stats_plain.frames == info.total_frames
    # 分段校验（每段 expect=got）在管线内已强校验，能跑完即证明帧数守恒
    assert (tmp_path / "filt.mp4").stat().st_size > 0

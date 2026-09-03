"""视频剪切（移植 AIOV smart cut）：三模式正确性 + 帧精确(SSIM) + API 轮询。"""
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR, ffmpeg_bin, ffprobe_bin
from sv.pipeline.probe import probe
from sv.pipeline.trim import TrimError, run_trim, scan_keyframes
from sv.utils.process import WINDOWS_CREATE_FLAGS


@pytest.fixture(scope="module")
def client():
    """剪切端点不依赖队列/db，无需 lifespan（与 test_server 的 client 隔离）。"""
    from sv.server.app import app

    return TestClient(app)

FPS = 24
DUR = 6.0


def make_fixture(path: Path) -> None:
    """320x240 24fps 6s，强制每 1s 一个 GOP（-g 24，关键帧在整数秒）。"""
    subprocess.run([
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", str(DUR), "-c:v", "libx264", "-g", "24", "-keyint_min", "24",
        "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True, creationflags=WINDOWS_CREATE_FLAGS)


def ssim_vs_source(source: Path, output: Path, start: float, dur: float) -> float:
    """输出全片 vs 源 [start, start+dur) 窗口的帧序配对 SSIM（时间戳归零）。"""
    r = subprocess.run(
        [ffmpeg_bin(), "-hide_banner",
         "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(source),
         "-i", str(output),
         "-lavfi", "[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];[a][b]ssim",
         "-f", "null", "-"],
        capture_output=True, text=True, creationflags=WINDOWS_CREATE_FLAGS,
    )
    import re
    vals = re.findall(r"SSIM Y:([\d.]+)", r.stderr)
    if not vals:
        raise AssertionError(f"SSIM 未输出: {r.stderr[-300:]}")
    return float(vals[-1])


@pytest.fixture(scope="module")
def fixture_video():
    TEMP_DIR.mkdir(exist_ok=True)
    p = TEMP_DIR / "trim_fixture.mp4"
    if not p.exists():
        make_fixture(p)
    return p


def test_scan_keyframes(fixture_video):
    kfs = scan_keyframes(fixture_video)
    assert 0.0 in [round(k, 2) for k in kfs]
    assert any(abs(k - 3.0) < 0.05 for k in kfs), "强制 GOP 应在整数秒有关键帧"


def test_fast_mode_snaps_to_keyframe(fixture_video):
    """fast：入点 2.5s 吸附到关键帧 2.0s，时长 ≈ 3.0s（无损）。"""
    out = TEMP_DIR / "trim_fast.mp4"
    res = run_trim(fixture_video, 2.5, 5.0, "fast", out)
    assert res.actual_start == pytest.approx(2.0, abs=0.05)
    o = probe(out)
    assert abs(o.duration_s - 3.0) < 0.2
    assert o.has_audio
    # 纯复制：SSIM 应近乎无损（与源 [2.0,5.0) 窗口比）
    assert ssim_vs_source(fixture_video, out, res.actual_start, o.duration_s) > 0.97


def frame_mad(src: Path, out: Path, out_idx: int, src_idx: int, w=320, h=240) -> float:
    """输出第 out_idx 帧与源第 src_idx 帧的平均绝对差（逐位一致 = 0）。"""
    def grab(path: Path, idx: int):
        r = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-vf", f"select=eq(n\\,{idx})", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, creationflags=WINDOWS_CREATE_FLAGS,
        )
        import numpy as np
        return np.frombuffer(r.stdout, dtype=np.uint8).reshape(h, w, 3).astype(np.float32)

    import numpy as np
    return float(np.abs(grab(out, out_idx) - grab(src, src_idx)).mean())


def test_smart_mode_frame_exact(fixture_video):
    """smart：入点 2.5s 不在关键帧 → 头部转码+尾部复制，起点帧精确
    （尾部按包边界截断，允许 ~0.3s 漂移，与 AIOV 行为一致）。"""
    out = TEMP_DIR / "trim_smart.mp4"
    res = run_trim(fixture_video, 2.5, 5.0, "smart", out)
    assert res.mode == "smart"
    assert res.actual_start == pytest.approx(2.5, abs=0.01)
    o = probe(out)
    assert -0.05 < o.duration_s - 2.5 < 0.4
    assert 2.5 * FPS <= o.total_frames <= 2.5 * FPS + 10
    assert ssim_vs_source(fixture_video, out, 2.5, o.duration_s) > 0.87
    # 逐帧对齐：首帧==源第 60 帧（转码段，残差应很小）；
    # 第 12 帧==源第 72 帧（复制段首帧 = 关键帧 3.0s，应逐位一致）
    assert frame_mad(fixture_video, out, 0, 60) < 2.0
    assert frame_mad(fixture_video, out, 12, 72) < 0.1


def test_smart_on_keyframe_degrades_to_copy(fixture_video):
    """smart 且起点恰在关键帧 → 纯复制（actual_start == 入点）。"""
    out = TEMP_DIR / "trim_smart_kf.mp4"
    res = run_trim(fixture_video, 3.0, 5.0, "smart", out)
    assert res.mode == "fast"
    assert res.actual_start == pytest.approx(3.0, abs=0.05)


def test_exact_mode(fixture_video):
    out = TEMP_DIR / "trim_exact.mp4"
    res = run_trim(fixture_video, 1.3, 4.2, "exact", out)
    assert res.mode == "exact"
    o = probe(out)
    assert abs(o.duration_s - 2.9) < 0.15
    assert o.has_audio


def test_invalid_range(fixture_video):
    with pytest.raises(TrimError):
        run_trim(fixture_video, 3.0, 3.0, "smart", TEMP_DIR / "nope.mp4")


def test_trim_api(client, fixture_video):
    """API：提交 → 轮询 done → 产物存在且时长正确。"""
    out = TEMP_DIR / "trim_api_out.mp4"
    r = client.post("/api/trim", json={
        "input": str(fixture_video), "start_s": 2.5, "end_s": 5.0,
        "mode": "smart", "output": str(out),
        "overwrite": True,  # 复跑覆盖上次产物（覆盖预检只挡非显式确认）
    })
    assert r.status_code == 201, r.text
    jid = r.json()["job_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        job = client.get(f"/api/trim/{jid}").json()
        if job["state"] in ("done", "failed"):
            break
        time.sleep(0.3)
    assert job["state"] == "done", job.get("error")
    assert job["mode"] == "smart"
    assert -0.05 < job["duration_s"] - 2.5 < 0.4
    assert Path(job["output"]).exists()
    assert probe(Path(job["output"])).has_audio

    # 校验错误参数
    r = client.post("/api/trim", json={
        "input": str(fixture_video), "start_s": 4.0, "end_s": 2.0, "mode": "smart",
    })
    assert r.status_code == 400
    r = client.post("/api/trim", json={
        "input": str(fixture_video), "start_s": 1.0, "end_s": 2.0, "mode": "bad",
    })
    assert r.status_code == 400
    assert client.get("/api/trim/nonexistent").status_code == 404

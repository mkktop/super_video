"""硬件解码：decoder_cmd 参数、管线透传、真实源预验证、任务链路校验。"""
import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.probe import (
    DECODERS,
    HWACCEL_CODECS,
    probe,
    probe_hwaccel,
    validate_m0,
)
from sv.pipeline.segmented import SegmentedPipeline
from sv.pipeline.stream import EncodeOpts, StreamPipeline, decoder_cmd
from sv.utils.process import WINDOWS_CREATE_FLAGS


class PassThrough:
    scale = 1

    def process(self, frame):
        return frame


def _make_video(path: Path, w=320, h=240, duration=1) -> None:
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=24",
        "-t", str(duration), "-c:v", "libx264", "-crf", "20",
        "-pix_fmt", "yuv420p", str(path),
    ]
    subprocess.run(cmd, check=True, creationflags=WINDOWS_CREATE_FLAGS)


def test_decoder_cmd_hwaccel_position():
    """hwaccel 参数必须作为输入选项出现在 -i 之前；None 时完全不出现。"""
    base = decoder_cmd(Path("in.mp4"))
    assert not any(a == "cuda" for a in base)

    on = decoder_cmd(Path("in.mp4"), seek_s=1.5, max_frames=10, hwaccel="cuda")
    i_pos = on.index("-i")
    hw_pos = on.index("-hwaccel")
    assert hw_pos < i_pos
    assert on[on.index("cuda") - 1] == "-hwaccel"
    # 其余参数原样保留
    assert on[on.index("-ss") + 1] == "1.500000"
    assert on[on.index("-frames:v") + 1] == "10"


def test_pipelines_accept_decode_hwaccel(tmp_path):
    pipe = StreamPipeline(
        tmp_path / "in.mp4", tmp_path / "out.mp4", PassThrough(),
        encode=EncodeOpts(), decode_hwaccel="cuda",
    )
    assert pipe.decode_hwaccel == "cuda"
    seg = SegmentedPipeline(
        tmp_path / "in.mp4", tmp_path / "out2.mp4", PassThrough(),
        task_id="t-dec", decode_hwaccel="d3d11va",
    )
    assert seg.decode_hwaccel == "d3d11va"


def test_decoders_table_maps_to_hwaccel_values():
    """decoder 参数值 → '-hwaccel' 值映射完整（软解 None，其余为合法 hwaccel 名）。"""
    assert DECODERS["sw"] is None
    assert DECODERS["nvdec"] == "cuda"
    assert DECODERS["d3d11va"] == "d3d11va"
    for hw in DECODERS.values():
        if hw is not None:
            assert hw in HWACCEL_CODECS


def test_probe_hwaccel_matrix_gate(monkeypatch, tmp_path):
    """源编码不在矩阵内：不跑 ffmpeg 直接判不可用（矩阵外 -hwaccel 静默软解，退出码判不出）。"""
    def _boom(*a, **kw):
        raise AssertionError("矩阵外的编码不应触发真解码验证")

    monkeypatch.setattr("sv.pipeline.probe.subprocess.run", _boom)
    video = tmp_path / "old.avi"  # 编码名手造，不真探测
    assert probe_hwaccel(video, "cuda", "msmpeg4v2") is False
    assert probe_hwaccel(video, "d3d11va", "flv1") is False
    assert probe_hwaccel(video, "d3d11va", "vp8") is False  # d3d11va 无 vp8，cuda 才有


def test_probe_hwaccel_fallback_warning_detection(monkeypatch, tmp_path):
    """rc=0 但 stderr 有 hwaccel 初始化失败警告 = 静默回退软解，必须判不可用。"""
    monkeypatch.setattr(
        "sv.pipeline.probe.subprocess.run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stderr=(
            b"[h264 @ 0x1] Failed setup for format cuda: "
            b"hwaccel initialisation returned error.\n")))
    assert probe_hwaccel(tmp_path / "x.mp4", "cuda", "h264") is False

    # 干净 stderr + rc=0 → 可用
    monkeypatch.setattr(
        "sv.pipeline.probe.subprocess.run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stderr=b""))
    assert probe_hwaccel(tmp_path / "x.mp4", "cuda", "h264") is True

    # 硬失败（rc!=0，如文件损坏/驱动崩溃）→ 不可用
    monkeypatch.setattr(
        "sv.pipeline.probe.subprocess.run",
        lambda cmd, **kw: SimpleNamespace(returncode=1, stderr=b"boom"))
    assert probe_hwaccel(tmp_path / "x.mp4", "cuda", "h264") is False


def test_probe_hwaccel_reports_real_capability():
    """真解码验证：本机有 NVDEC/D3D11VA 且编码受支持时为 True，否则 False（CI 无卡路径同样覆盖）。"""
    video = TEMP_DIR / "test_gpu_decode_src.mp4"
    try:
        _make_video(video)
        got = probe_hwaccel(video, "cuda", "h264")
        assert got in (True, False)
        if got:
            # 报告可用时必须真的能硬解出 3 帧 rgb24
            r = subprocess.run(
                decoder_cmd(video, hwaccel="cuda"),
                capture_output=True, timeout=30,
                creationflags=WINDOWS_CREATE_FLAGS,
            )
            assert r.returncode == 0 and len(r.stdout) >= 320 * 240 * 3 * 3
    finally:
        video.unlink(missing_ok=True)


def test_passthrough_with_hwaccel_if_available():
    """真实链路冒烟：预验证可用时整段硬解跑通；不可用跳过（软解已由它测覆盖）。"""
    src = TEMP_DIR / "test_gpu_decode_e2e.mp4"
    out = TEMP_DIR / "test_gpu_decode_e2e_out.mp4"
    try:
        _make_video(src, duration=0.5)
        hw = None
        for name in ("cuda", "d3d11va"):
            if probe_hwaccel(src, name, "h264"):
                hw = name
                break
        if hw is None:
            pytest.skip("本机无可用 GPU 硬解，冒烟跳过")
        info = probe(src)
        validate_m0(info)
        pipe = StreamPipeline(
            info, out, PassThrough(), EncodeOpts(codec="h264"), decode_hwaccel=hw,
        )
        stats = asyncio.run(pipe.run())
        assert stats.frames == info.total_frames
    finally:
        for p in (src, out):
            p.unlink(missing_ok=True)


# ---- 任务创建端点校验 ----


@pytest.fixture()
def api_client(monkeypatch, tmp_path):
    """轻量 app 测试：绕过真实探测/硬件检测（本用例只关心 decoder 参数校验）。"""
    from fastapi.testclient import TestClient

    info = SimpleNamespace(
        width=320, height=240, fps=24.0, fps_str="24/1", vfr=False,
        video_codec="h264", pix_fmt="yuv420p", duration_s=1.0,
        total_frames=24, has_audio=False, audio=[], subtitles=[],
        path=tmp_path / "in.mp4",
    )
    monkeypatch.setattr("sv.server.routes.tasks.probe", lambda p: info)
    monkeypatch.setattr("sv.server.routes.tasks.validate_m0", lambda i: None)
    monkeypatch.setattr("sv.server.routes.tasks.cached_hardware", lambda: {
        "nvenc": False, "av1_nvenc": False, "amf": False, "svt_av1": True})
    monkeypatch.setattr("sv.server.routes.tasks.load_registry", lambda: {
        "m-test": SimpleNamespace(
            id="m-test", engine="onnx", scale=[2], io={}, tile_hint=0,
            installed=True, bundled=True, vram_ok=True, fp16=False)})
    monkeypatch.setattr("sv.server.db.new_task", lambda *a, **kw: {
        "id": "t1", "status": "queued", "params": a[3] if len(a) > 3 else {}})
    (tmp_path / "in.mp4").write_bytes(b"x")  # 端点先做存在性检查
    from sv.server.app import app

    with TestClient(app) as c:
        yield c, info


def test_create_task_decoder_validation(api_client):
    c, info = api_client

    def _post(decoder):
        return c.post("/api/tasks", json={
            "input": str(info.path), "model_id": "m-test",
            "params": {"scale": 2, "decoder": decoder},
        })

    assert _post("sw").status_code == 201
    assert _post("nvdec").status_code == 201  # h264 在矩阵内（本机可用性由 worker 预验证）
    assert _post("bogus").status_code == 400

    info.video_codec = "msmpeg4v2"  # 老编码：一切硬解都不支持
    r = _post("nvdec")
    assert r.status_code == 400
    assert "msmpeg4v2" in r.json()["detail"]
    assert _post("sw").status_code == 201


def test_probe_endpoint_decoder_map(monkeypatch, tmp_path):
    """/api/probe hwdecode=true 附带按文件实测的可用性 map；默认不带。"""
    from fastapi.testclient import TestClient

    calls: list[tuple[str, str | None]] = []

    def _fake_probe_hwaccel(path, hw, codec=None):
        calls.append((hw, codec))
        return hw == "cuda"

    info = SimpleNamespace(
        width=320, height=240, fps=24.0, fps_str="24/1", vfr=False,
        video_codec="hevc", pix_fmt="yuv420p10le", duration_s=1.0,
        total_frames=24, has_audio=False, audio=[], subtitles=[],
        path=tmp_path / "in.mkv",
    )
    monkeypatch.setattr("sv.server.routes.models.probe", lambda p: info)
    monkeypatch.setattr("sv.server.routes.models.validate_m0", lambda i: None)
    monkeypatch.setattr("sv.server.routes.models.probe_hwaccel", _fake_probe_hwaccel)

    (tmp_path / "in.mkv").write_bytes(b"x")  # 端点先做存在性检查
    from sv.server.app import app

    with TestClient(app) as c:
        body = {"path": str(info.path), "hwdecode": True}
        r = c.post("/api/probe", json=body).json()
        assert r["decoder"] == {"nvdec": True, "d3d11va": False}
        assert ("cuda", "hevc") in calls and ("d3d11va", "hevc") in calls

        calls.clear()
        r2 = c.post("/api/probe", json={"path": str(info.path)}).json()
        assert "decoder" not in r2
        assert calls == []

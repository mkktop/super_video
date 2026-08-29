"""模型对比：创建校验 / 图片与视频两条作业链路 / 取消语义 / 资产白名单。

推理与引擎加载全部打桩（不依赖 GPU 与真实模型），覆盖胶水层：
素材准备（ffmpeg 真跑）、逐模型串行、指标记录、产物落盘。
"""
import io
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.utils.process import WINDOWS_CREATE_FLAGS

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_compare.db"))

SPEC_A = SimpleNamespace(id="cmp-a", name="模型A", engine="onnx", scale=[2],
                         fp16=False, tile_hint=0, io={"color": "rgb", "range": "0-255", "pad": 2})
SPEC_B = SimpleNamespace(id="cmp-b", name="模型B", engine="onnx", scale=[2, 4],
                         fp16=False, tile_hint=0, io={"color": "rgb", "range": "0-255", "pad": 2})


class _Nearest2x:
    scale = 2

    def load(self):
        pass

    def process(self, frame):
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


@pytest.fixture()
def fake_engines(monkeypatch):
    """桩掉引擎加载与模型下载；worker 线程真跑（素材准备/管线/落盘是真路径）。"""
    import sv.server.compare as cmp_mod
    import sv.models.manager as mgr

    monkeypatch.setattr(cmp_mod, "_load_engine", lambda spec, scale, hw, log: _Nearest2x())
    monkeypatch.setattr(mgr, "ensure_files", lambda spec, needs: None)
    monkeypatch.setattr(
        "sv.models.registry.get_model",
        lambda mid: {"cmp-a": SPEC_A, "cmp-b": SPEC_B}[mid])
    monkeypatch.setattr(
        "sv.models.registry.file_for_scale",
        lambda spec, scale, variant=None: Path("fake.onnx"))


@pytest.fixture(scope="module")
def client():
    from sv.server.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _fake_registry(monkeypatch, client):
    """路由层注册表替换（创建校验走假模型表）。"""
    import sv.server.app as app_mod

    monkeypatch.setattr(
        app_mod, "load_registry",
        lambda: {"cmp-a": SPEC_A, "cmp-b": SPEC_B})


def _make_clip(path: Path, frames: int = 24):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=64x36:rate=24",
         "-frames:v", str(frames), "-c:v", "libx264", "-crf", "22",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS)


def _make_black_lead_clip(path: Path, black_s: float = 1.2, tail_s: float = 0.8):
    """前段黑场后段 testsrc2（默认共 2s、中点 1.0 恰落黑场）——静帧避黑的素材。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=black:size=64x36:rate=24:duration={black_s}",
         "-f", "lavfi", "-i", f"testsrc2=size=64x36:rate=24:duration={tail_s}",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
         "-map", "[v]", "-c:v", "libx264", "-crf", "22",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS)


def _gray_mean(png_bytes: bytes) -> float:
    import io

    from PIL import Image, ImageStat

    return ImageStat.Stat(Image.open(io.BytesIO(png_bytes)).convert("L")).mean[0]


def _make_png(path: Path, w=32, h=20):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.arange(w * h * 3, dtype=np.uint8).reshape(h, w, 3)).save(str(path))


def _wait_job(client, jid, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/compare/{jid}").json()
        if job["status"] in ("done", "failed", "canceled"):
            return job
        time.sleep(0.05)
    pytest.fail("对比作业超时未结束")


# ---- 创建校验 ----

def test_create_validation(client, tmp_path):
    clip = tmp_path / "c.mp4"
    _make_clip(clip)
    base = {"kind": "video", "input": str(clip), "start_s": 0, "end_s": 1}
    assert client.post("/api/compare", json={
        **base, "models": ["cmp-a"], "scale": 2}).status_code == 400  # 至少 2 个
    assert client.post("/api/compare", json={
        **base, "models": ["cmp-a", "cmp-a"], "scale": 2}).status_code == 400  # 重复
    assert client.post("/api/compare", json={
        **base, "models": ["cmp-a", "nope"], "scale": 2}).status_code == 404
    assert client.post("/api/compare", json={
        **base, "models": ["cmp-a", "cmp-b"], "scale": 3}).status_code == 400  # A 不支持 x3
    assert client.post("/api/compare", json={
        **base, "models": ["cmp-a", "cmp-b"], "scale": 2,
        "start_s": 0.9, "end_s": 0.1}).status_code == 400  # 出点早于入点
    assert client.post("/api/compare", json={
        **base, "models": ["cmp-a", "cmp-b"], "scale": 2}).status_code == 201


def test_video_compare_runs_all_models(client, tmp_path, fake_engines):
    clip = tmp_path / "v.mp4"
    _make_clip(clip, 24)  # 1s @ 24fps
    r = client.post("/api/compare", json={
        "kind": "video", "input": str(clip), "start_s": 0, "end_s": 1,
        "models": ["cmp-a", "cmp-b"], "scale": 2})
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    job = _wait_job(client, jid)
    assert job["status"] == "done"
    assert [e["status"] for e in job["entries"]] == ["done", "done"]
    for e in job["entries"]:
        assert e["fps"] > 0 and e["elapsed_s"] > 0
        assert e["out_w"] == 128 and e["out_h"] == 72
        assert e["has_output"] is True
        assert client.get(f"/api/compare/{jid}/asset/still/{e['model_id']}/0").status_code == 200
        assert client.get(f"/api/compare/{jid}/asset/out/{e['model_id']}").status_code == 200
    assert client.get(f"/api/compare/{jid}/asset/src_still/0").status_code == 200
    assert client.get(f"/api/compare/{jid}/asset/src_still/3").status_code == 200
    assert client.get(f"/api/compare/{jid}/asset/seg").status_code == 200
    # 白名单外键 404（目录穿越防护）
    assert client.get(f"/api/compare/{jid}/asset/zzz").status_code == 404


def test_image_compare_runs(client, tmp_path, fake_engines):
    img = tmp_path / "p.png"
    _make_png(img, 40, 24)
    r = client.post("/api/compare", json={
        "kind": "image", "input": str(img),
        "models": ["cmp-a", "cmp-b"], "scale": 2})
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    job = _wait_job(client, jid)
    assert job["status"] == "done"
    assert job["still_count"] == 1  # 图片模式无多帧概念
    e = job["entries"][0]
    assert e["out_w"] == 80 and e["out_h"] == 48
    # 图片模式：out 与 still 同为成品图
    assert client.get(f"/api/compare/{jid}/asset/out/{e['model_id']}").status_code == 200
    assert client.get(f"/api/compare/{jid}/asset/still/{e['model_id']}").status_code == 200


def test_seg_too_long_auto_capped(client, tmp_path, fake_engines):
    clip = tmp_path / "long.mp4"
    _make_clip(clip, 240)  # 10s
    r = client.post("/api/compare", json={
        "kind": "video", "input": str(clip), "start_s": 0, "end_s": 10,
        "models": ["cmp-a", "cmp-b"], "scale": 2})
    assert r.status_code == 201
    jid = r.json()["id"]
    job = _wait_job(client, jid)
    assert job["end_s"] - job["start_s"] <= 20.0 + 1e-6
    assert job["status"] == "done"


# ---- 静帧取帧：多帧样本 + 避黑场 + 源/成片同时间戳 ----

def test_pick_still_times_skip_black(tmp_path):
    """四段采样：前两段全黑保留锚点（如实展示黑场），后两段落正常画面。
    容器 duration 含末帧长度（约 2.04s），断言按语义/区间而非硬编码时间。"""
    clip = tmp_path / "blacklead.mp4"
    _make_black_lead_clip(clip)  # 黑约 1.208s + testsrc，共 49 帧
    ts = cmp_mod._pick_still_times(clip)
    assert len(ts) == 4
    # 段2 锚已越过黑段末尾，应命中非黑帧
    assert 1.21 < ts[2] < 1.5
    assert cmp_mod._frame_dark(clip, ts[2]) is False
    assert cmp_mod._frame_dark(clip, ts[3]) is False
    # 前两段整段皆黑：保留锚（仍落黑场段内），不越段硬挪
    assert cmp_mod._frame_dark(clip, ts[0]) is True
    assert cmp_mod._frame_dark(clip, ts[1]) is True


def test_pick_still_times_all_black_returns_anchors(tmp_path):
    """整段全黑（素材本身如此）时四段各保留锚点：互异、递增、都在片内。"""
    clip = tmp_path / "allblack.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=black:size=64x36:rate=24:duration=1",
         "-c:v", "libx264", "-crf", "22", "-pix_fmt", "yuv420p", str(clip)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS)
    ts = cmp_mod._pick_still_times(clip)
    assert len(ts) == 4 and ts == sorted(ts) and len(set(ts)) == 4
    assert 0.0 <= ts[0] and ts[3] <= 1.05


def test_video_compare_still_avoids_black(client, tmp_path, fake_engines):
    """黑场素材跑完整作业：正常段样本帧避黑；同索引源/模型静帧取自同一
    时间戳（模型是 2x 最近邻放大，整数下采样后灰度均值应几乎一致）。"""
    clip = tmp_path / "blackmid.mp4"
    _make_black_lead_clip(clip)
    r = client.post("/api/compare", json={
        "kind": "video", "input": str(clip), "start_s": 0, "end_s": 2,
        "models": ["cmp-a", "cmp-b"], "scale": 2})
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    job = _wait_job(client, jid)
    assert job["status"] == "done"
    assert job["still_count"] == 4
    # 黑场段的样本如实是黑，正常段避黑
    assert _gray_mean(client.get(f"/api/compare/{jid}/asset/src_still/0").content) < 20
    for i in (2, 3):
        assert _gray_mean(client.get(f"/api/compare/{jid}/asset/src_still/{i}").content) > 20
    # 越界/非数字索引与无索引 key 都应 404
    assert client.get(f"/api/compare/{jid}/asset/src_still/9").status_code == 404
    assert client.get(f"/api/compare/{jid}/asset/src_still/x").status_code == 404
    assert client.get(f"/api/compare/{jid}/asset/src_still").status_code == 404
    for e in job["entries"]:
        src = np.asarray(Image.open(io.BytesIO(
            client.get(f"/api/compare/{jid}/asset/src_still/2").content)).convert("L"),
            dtype=np.float64)
        out = np.asarray(Image.open(io.BytesIO(
            client.get(f"/api/compare/{jid}/asset/still/{e['model_id']}/2").content)
        ).convert("L"), dtype=np.float64)[::2, ::2]
        assert out.shape == src.shape
        assert abs(out.mean() - src.mean()) < 3, "同索引静帧不是同一时间戳"


def test_cancel_skips_remaining_models(client, tmp_path, fake_engines, monkeypatch):
    """取消粒度=模型间：A 运行中请求取消 → A 完成后 B 跳过，作业落 canceled。"""
    import sv.server.compare as cmp_mod

    gate = threading.Event()

    def gated(spec, scale, hw, log):
        gate.wait(timeout=10)  # 卡住 A 的引擎加载，制造确定性的取消窗口
        return _Nearest2x()

    monkeypatch.setattr(cmp_mod, "_load_engine", gated)
    clip = tmp_path / "cc.mp4"
    _make_clip(clip, 12)
    r = client.post("/api/compare", json={
        "kind": "video", "input": str(clip), "start_s": 0, "end_s": 0.5,
        "models": ["cmp-a", "cmp-b"], "scale": 2})
    jid = r.json()["id"]
    # 等 A 进入 running
    deadline = time.time() + 10
    while time.time() < deadline:
        job = client.get(f"/api/compare/{jid}").json()
        if job["entries"][0]["status"] == "running":
            break
        time.sleep(0.05)
    else:
        pytest.fail("模型 A 未进入 running")
    assert client.post(f"/api/compare/{jid}/cancel").status_code == 200
    gate.set()  # 放行 A：跑到自然结束
    job = _wait_job(client, jid)
    assert job["status"] == "canceled"
    assert job["entries"][0]["status"] == "done", "运行中的模型应跑到自然结束"
    assert job["entries"][1]["status"] == "canceled", "后续模型应跳过"


def test_cancel_queued_job(client, tmp_path, fake_engines, monkeypatch):
    import sv.server.compare as cmp_mod

    gate = threading.Event()
    monkeypatch.setattr(
        cmp_mod, "_load_engine",
        lambda spec, scale, hw, log: (gate.wait(5), _Nearest2x())[1])
    gate.set()  # 不阻塞，走正常队列
    clip = tmp_path / "cq.mp4"
    _make_clip(clip, 12)
    # 用一个占满 worker 的前置作业制造 queued 状态
    r0 = client.post("/api/compare", json={
        "kind": "video", "input": str(clip), "start_s": 0, "end_s": 0.5,
        "models": ["cmp-a", "cmp-b"], "scale": 2})
    hold = threading.Event()

    def holding(spec, scale, hw, log):
        hold.wait(timeout=10)
        return _Nearest2x()

    monkeypatch.setattr(cmp_mod, "_load_engine", holding)
    r1 = client.post("/api/compare", json={
        "kind": "video", "input": str(clip), "start_s": 0, "end_s": 0.5,
        "models": ["cmp-a", "cmp-b"], "scale": 2})
    jid1 = r1.json()["id"]
    _wait_job(client, r0.json()["id"])  # 前置完成，jid1 开始跑并卡在 hold
    deadline = time.time() + 10
    while time.time() < deadline:
        if client.get(f"/api/compare/{jid1}").json()["entries"][0]["status"] == "running":
            break
        time.sleep(0.05)
    assert client.post(f"/api/compare/{jid1}/cancel").status_code == 200
    hold.set()
    job = _wait_job(client, jid1)
    assert job["status"] == "canceled"


# ---- 对比缓存清理（设置页入口） ----

import sv.server.compare as cmp_mod

@pytest.fixture()
def sandbox_cache(monkeypatch, tmp_path):
    """COMPARE_ROOT 与 JOBS 打到沙箱：清理是全局破坏性操作，
    绝不能碰开发者磁盘上的真实对比产物。"""
    root = tmp_path / "compare"
    root.mkdir()
    monkeypatch.setattr(cmp_mod, "COMPARE_ROOT", root)
    monkeypatch.setattr(cmp_mod, "JOBS", {})
    return root


def _seed_job(root: Path, jid: str, size: int = 128) -> None:
    d = root / jid
    d.mkdir()
    (d / "seg.mp4").write_bytes(b"x" * size)
    cmp_mod.JOBS[jid] = {"id": jid, "status": "done", "entries": []}


def test_cache_stats_and_clear(client, sandbox_cache):
    _seed_job(sandbox_cache, "job-a")
    _seed_job(sandbox_cache, "job-b", size=50)
    st = client.get("/api/compare/cache")
    assert st.status_code == 200
    body = st.json()
    assert body["jobs"] == 2
    assert body["bytes"] == 128 + 50

    r = client.delete("/api/compare/cache")
    assert r.status_code == 200
    cleared = r.json()
    assert cleared["removed_jobs"] == 2
    assert cleared["freed_bytes"] == 128 + 50
    assert not (sandbox_cache / "job-a").exists()
    assert "job-a" not in cmp_mod.JOBS and "job-b" not in cmp_mod.JOBS

    # 空目录再清一次：幂等，0 作业 0 字节
    r2 = client.delete("/api/compare/cache")
    assert r2.status_code == 200
    assert r2.json() == {"removed_jobs": 0, "freed_bytes": 0}


def test_cache_clear_refused_while_active(client, sandbox_cache):
    _seed_job(sandbox_cache, "done-job")
    cmp_mod.JOBS["live"] = {"id": "live", "status": "running", "entries": []}
    r = client.delete("/api/compare/cache")
    assert r.status_code == 409
    assert "正在进行" in r.json()["detail"]
    # 拒绝时不得破坏任何产物
    assert (sandbox_cache / "done-job").exists()
    assert "done-job" in cmp_mod.JOBS


def test_cache_route_not_shadowed_by_job_id(client, sandbox_cache):
    """GET /api/compare/cache 不能被 /{job_id} 路由吞成 404。"""
    _seed_job(sandbox_cache, "job-c")
    assert client.get("/api/compare/cache").status_code == 200

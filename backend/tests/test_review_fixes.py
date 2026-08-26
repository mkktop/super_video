"""v0.2.4 审查修复的回归测试。

覆盖：RIFE 通道序契约 / batch 尾批不丢帧 / 段产出帧数校验 / fp16 原子写 /
u8 校验用真实帧形状 / EventBus 线程安全 / 本地 token 鉴权 / concat 转义 /
trim 取消 / runner 退出语义（标记取消）。
"""
import asyncio
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.utils.process import WINDOWS_CREATE_FLAGS

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_review.db"))


# ---- RIFE：RGB 进 RGB 出（历史 bug：按 BGR 翻转 → 插帧红蓝错配） ----

class _EchoSession:
    """回声 I0 三通道——t 处的输出应恰为第一帧，任何通道翻转/尺寸错都暴露。"""

    def run(self, _outs, feeds):
        return [feeds["input"][:, :3]]


def test_rife_rgb_contract_and_align32():
    from sv.engines.rife import Rife2x

    r = Rife2x("fake.onnx")
    r.session = _EchoSession()
    r._input_name = "input"
    rng = np.random.default_rng(3)
    a = rng.integers(0, 256, (61, 66, 3), dtype=np.uint8)  # 双轴均非 32 对齐
    b = rng.integers(0, 256, (61, 66, 3), dtype=np.uint8)
    out = r.interpolate(a, b)
    assert out.shape == a.shape and out.dtype == np.uint8
    assert np.array_equal(out, a), (
        "RIFE 接口必须 RGB 进 RGB 出且 32 对齐填充裁回无损——通道翻转=红蓝错配")


# ---- batch 尾批不丢帧（总帧数 % batch != 0 时最多丢 batch-1 帧） ----

class Nearest2x:
    scale = 2
    batch = 4

    def process(self, frame):
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)

    def process_batch(self, frames):
        return np.stack([self.process(f) for f in frames])


def _make_clip(path: Path, frames: int):
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=64x36:rate=24",
         "-frames:v", str(frames), "-c:v", "libx264", "-crf", "22",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


def test_stream_batch_tail_not_dropped(tmp_path):
    """33 帧 @ batch=4：尾批 1 帧必须进输出（修复前静默丢弃 → 输出短于预期）。"""
    from sv.pipeline.probe import probe
    from sv.pipeline.stream import EncodeOpts, StreamPipeline

    clip = tmp_path / "odd.mp4"
    _make_clip(clip, 33)
    info = probe(clip)
    assert info.total_frames == 33
    stats = asyncio.run(StreamPipeline(
        info, tmp_path / "out.mp4", Nearest2x(), EncodeOpts()).run())
    assert stats.frames == 33, "尾批帧被丢弃：总帧数 % batch != 0 时输出变短"


def test_segmented_short_segment_hard_fails(tmp_path, monkeypatch):
    """非末段短产必须硬失败且不入 checkpoint（静默固化=concat 后音画漂移无法自愈）。"""
    import sv.pipeline.segmented as seg
    from sv.pipeline.stream import PipelineError, RunStats

    class FakePipe:
        def __init__(self, *a, **k):
            self.stage_stats: dict[str, float] = {}

        async def run(self):
            self.stage_stats["frames"] = 3.0  # 远小于期望的 seg_frames
            return RunStats(3, 0.1, 30.0, Path("out"), 0)

    from types import SimpleNamespace

    info = SimpleNamespace(path=Path("x.mp4"), fps=24.0, fps_str="24/1",
                           total_frames=100, width=64, height=36,
                           has_audio=False, audio=[], subtitles=[])

    monkeypatch.setattr(seg, "StreamPipeline", FakePipe)
    pipe = seg.SegmentedPipeline(
        info, tmp_path / "out.mp4", object(), task_id="rev-short-seg",
        seg_frames=25, cleanup=False)
    with pytest.raises(PipelineError, match="产出 3 帧"):
        asyncio.run(pipe.run())
    ckpt = TEMP_DIR / "segmented" / "rev-short-seg" / "checkpoint.json"
    assert not ckpt.exists(), "短段不得写入 checkpoint"


def test_concat_line_escapes_single_quote():
    from sv.pipeline.segmented import _concat_line

    line = _concat_line(Path("C:/di'r/seg.mp4"))
    assert line == "file 'C:/di'\\''r/seg.mp4'"


# ---- fp16 原子写：成功无 .tmp 残片；失败不留半写 dst ----

def test_fp16_convert_atomicity(tmp_path, monkeypatch):
    import onnx

    from sv.models.fp16 import convert_file, fp16_path

    base = Path(__file__).parent.parent / "sv" / "models" / "bundled" / \
        "RealESRGANv2-animevideo-xsx2.onnx"
    if not base.exists():
        pytest.skip("bundled 模型缺失")

    src = tmp_path / "m.onnx"
    shutil.copy(base, src)
    dst = fp16_path(src)
    convert_file(src, dst)
    assert dst.exists() and onnx.load(str(dst))
    assert not list(tmp_path.glob("*.tmp")), "成功路径不得留下 .tmp 残片"

    src2 = tmp_path / "m2.onnx"
    shutil.copy(base, src2)
    dst2 = fp16_path(src2)
    monkeypatch.setattr(
        onnx, "load_model",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        convert_file(src2, dst2)
    assert not dst2.exists() and not list(tmp_path.glob("*.tmp")), (
        "失败路径不得留下半写 dst 或 tmp 残片（残缺 fp16 会被当成有效变体永久使用）")


# ---- u8 校验形状：必须用源帧真实尺寸（小形状会不可逆毒害 GPU 会话执行路径） ----

def test_validate_u8_uses_real_frame_shape(tmp_path):
    from sv.engines.onnx_engine import OnnxSrEngine

    eng = OnnxSrEngine(tmp_path / "m.onnx", 2, io={"color": "rgb", "range": "0-1"},
                       validate_hw=(120, 160))
    seen: list[tuple[int, int]] = []

    def fake_infer(f):
        seen.append(f.shape[:2])
        return np.zeros((f.shape[0] * 2, f.shape[1] * 2, 3), np.uint8)

    def fake_run_u8(sess, name, f):
        seen.append(f.shape[:2])
        return np.zeros((f.shape[0] * 2, f.shape[1] * 2, 3), np.uint8)

    eng._infer = fake_infer
    eng._run_u8 = fake_run_u8
    eng._validate_u8(None, "x")  # 不抛=逐位一致（假引擎输出恒等）
    assert (120, 160) in seen and (119, 159) in seen, (
        "校验形状必须来自 validate_hw（真实帧尺寸），不得回退 96x128 小形状")
    assert all(h >= 119 for h, _ in seen)


# ---- EventBus：工作线程必须走 publish_threadsafe（asyncio.Queue 非线程安全） ----

def test_eventbus_publish_threadsafe():
    from sv.server.events import EventBus

    async def main() -> list[int]:
        bus = EventBus()
        q = bus.subscribe()
        bus.publish({"seq": 0})  # 绑定事件循环

        def worker():
            for i in range(1, 50):
                bus.publish_threadsafe({"seq": i})

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        await asyncio.sleep(0.2)  # call_soon 队列排空
        got: list[int] = []
        while not q.empty():
            got.append(q.get_nowait()["seq"])
        return got

    got = asyncio.run(main())
    assert got == list(range(50)), "线程发布的事件必须全量有序到达"


# ---- 本地 token 鉴权：无令牌 401，健康检查豁免，WS 自查 ----

def test_token_auth(monkeypatch, tmp_path):
    import sv.server.app as app_mod

    monkeypatch.setenv("SV_TOKEN", "sekrit")
    monkeypatch.setattr(app_mod, "TEMP_DIR", tmp_path)  # token 文件源也隔离
    with TestClient(app_mod.app) as c:
        assert c.get("/api/health").status_code == 200  # 复用探测豁免
        assert c.get("/api/tasks").status_code == 401  # 无令牌拒绝
        assert c.get("/api/tasks", headers={"X-SV-Token": "wrong"}).status_code == 401
        assert c.get("/api/tasks", headers={"X-SV-Token": "sekrit"}).status_code == 200
        assert c.get("/api/tasks?token=sekrit").status_code == 200  # img/video 等无头场景
        # WS 不受 CORS 约束，必须自查令牌：无令牌连接被 4401 关闭
        with pytest.raises(Exception):
            with c.websocket_connect("/ws") as ws:
                ws.receive_text()


# ---- runner：退出语义=标记取消（可续跑），孤儿 worker pid 清扫 ----

def test_runner_stop_marks_cancel_requested():
    from sv.server.events import EventBus
    from sv.server.runner import Runner

    r = Runner(EventBus())
    r.current_id = "task-x"
    asyncio.run(r.stop())  # proc/_loop_task 均未起：无跨循环 await
    assert r._cancel_requested == "task-x", (
        "stop 必须落取消标记——否则带任务退出落成 failed+删半成品，不可续跑")


def test_reap_orphan_workers_clears_stale_pidfile(tmp_path, monkeypatch):
    import sv.server.runner as runner_mod

    seg = tmp_path / "segmented" / "t1"
    seg.mkdir(parents=True)
    (seg / "owner.pid").write_text(str(os.getpid()))  # 本进程非 worker
    monkeypatch.setattr(runner_mod, "TEMP_DIR", tmp_path)
    Runner = runner_mod.Runner
    r = Runner(runner_mod.EventBus())
    r._reap_orphan_workers()
    assert not (seg / "owner.pid").exists(), "陈旧 pid 文件应被清理"


# ---- trim 取消端点 ----

@pytest.fixture(scope="module")
def trim_client():
    from sv.server.app import app

    return TestClient(app)


def test_trim_cancel(trim_client, tmp_path):
    clip = tmp_path / "tc.mp4"
    _make_clip_long(clip)
    r = trim_client.post("/api/trim", json={
        "input": str(clip), "start_s": 0, "end_s": 24, "mode": "exact"})
    assert r.status_code == 201
    jid = r.json()["job_id"]
    # 等到 running（ffmpeg 已拉起，有 proc 可杀）；高频轮询把取消落在编码中段
    deadline = time.time() + 30
    while time.time() < deadline:
        job = trim_client.get(f"/api/trim/{jid}").json()
        if job["state"] == "running":
            break
        time.sleep(0.02)
    assert job["state"] == "running", job
    rr = trim_client.post(f"/api/trim/{jid}/cancel")
    assert rr.status_code == 200
    deadline = time.time() + 60
    while time.time() < deadline:
        job = trim_client.get(f"/api/trim/{jid}").json()
        if job["state"] in ("canceled", "done", "failed"):
            break
        time.sleep(0.05)
    assert job["state"] == "canceled", f"取消后应收尾为 canceled，实际 {job['state']}"
    # 已结束任务不可再取消
    assert trim_client.post(f"/api/trim/{jid}/cancel").status_code == 409


def test_trim_cancel_queued(trim_client, tmp_path):
    """排队期取消（worker 未领走）：直接落终态，不占队列。"""
    first = trim_client.post("/api/trim", json={
        "input": str(_make_clip_long(tmp_path / "tq.mp4")),
        "start_s": 0, "end_s": 24, "mode": "exact"}).json()
    # 第一个占住单线程队列后，第二个稳定停在 queued
    second = trim_client.post("/api/trim", json={
        "input": str(tmp_path / "tq.mp4"), "start_s": 0, "end_s": 5,
        "mode": "exact"}).json()
    jid = second["job_id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        job = trim_client.get(f"/api/trim/{jid}").json()
        if job["state"] == "queued":
            break
        time.sleep(0.02)
    assert job["state"] == "queued", job
    assert trim_client.post(f"/api/trim/{jid}/cancel").status_code == 200
    assert trim_client.get(f"/api/trim/{jid}").json()["state"] == "canceled"
    # 收尾：把占位的长任务也取消掉，别拖慢整个测试文件
    trim_client.post(f"/api/trim/{first['job_id']}/cancel")


def _make_clip_long(path: Path) -> Path:
    # 1080p x 25s：exact 整段转码在桌面 CPU 上也要数秒，保证取消窗口足够
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", "25", "-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )
    return path

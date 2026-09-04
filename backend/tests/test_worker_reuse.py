"""引擎跨任务进程复用：worker --serve 循环 + 进程内引擎签名缓存 + runner 续喂。"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.server import db as sv_db
from sv.server import worker as worker_mod
from sv.server import worker_engine
from sv.server.runner import _worker_spawn_cmd
from sv.utils.process import WINDOWS_CREATE_FLAGS

_CLIP_A = TEMP_DIR / "reuse_a.mp4"  # 160x90
_CLIP_B = TEMP_DIR / "reuse_b.mp4"  # 128x72（不同源分辨率 = 不同签名）


def _make_clip(path: Path, size: str) -> None:
    if path.exists():
        return
    TEMP_DIR.mkdir(exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24",
         "-t", "1", "-c:v", "libx264", "-crf", "22", "-pix_fmt", "yuv420p",
         str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


class _Counting2x:
    """最近邻 x2 假引擎：构造计数即「加载+预热」次数。"""
    constructed = 0
    scale = 2
    batch = 1
    provider_used = ["test-fake"]

    def __init__(self, weight, scale, io=None, tile=0, batch=1,
                 device="auto", validate_hw=None):
        type(self).constructed += 1

    def load(self):
        pass

    def process(self, frame):
        import numpy as np

        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


def _new_video_task(src: Path, out: Path) -> dict:
    sv_db.init_db()
    return sv_db.new_task(str(src), str(out), "realesr-animevideov3", params={"scale": 2})


@pytest.fixture()
def counting_engine(monkeypatch):
    monkeypatch.setattr(worker_engine, "OnnxSrEngine", _Counting2x)
    _Counting2x.constructed = 0
    worker_engine._ENGINE_CACHE.clear()  # 模块级缓存跨测试隔离
    yield _Counting2x
    worker_engine._ENGINE_CACHE.clear()


def test_spawn_cmd_carries_serve_flag():
    """runner 拉起命令必须带 --serve（常驻模式开关），frozen 与 dev 同构。"""
    for cmd in (_worker_spawn_cmd("py.exe", "t1"),
                _worker_spawn_cmd("sidecar.exe", "t1", )):
        assert cmd[-1] == "--serve" and "t1" in cmd


def test_serve_reuses_engine_same_signature(counting_engine, monkeypatch, tmp_path):
    """同签名两任务经 stdin 接续：第二个任务跳过加载+预热（构造仅 1 次）。"""
    _make_clip(_CLIP_A, "160x90")
    t1 = _new_video_task(_CLIP_A, tmp_path / "o1.mp4")
    t2 = _new_video_task(_CLIP_A, tmp_path / "o2.mp4")
    monkeypatch.setattr(sys, "stdin", io.StringIO(t2["id"] + "\n"))
    try:
        rc = worker_mod.main(t1["id"], serve=True)
        assert rc == 0
        assert counting_engine.constructed == 1, "同签名任务不应重建引擎"
        assert (tmp_path / "o1.mp4").stat().st_size > 0
        assert (tmp_path / "o2.mp4").stat().st_size > 0
    finally:
        sv_db.delete_task(t1["id"])
        sv_db.delete_task(t2["id"])


def test_serve_rebuilds_on_signature_change(counting_engine, monkeypatch, tmp_path):
    """源分辨率不同（签名不同）：复用进程但重建引擎（构造 2 次），任务都成功。"""
    _make_clip(_CLIP_A, "160x90")
    _make_clip(_CLIP_B, "128x72")
    t1 = _new_video_task(_CLIP_A, tmp_path / "a1.mp4")
    t2 = _new_video_task(_CLIP_B, tmp_path / "b1.mp4")
    monkeypatch.setattr(sys, "stdin", io.StringIO(t2["id"] + "\n"))
    try:
        rc = worker_mod.main(t1["id"], serve=True)
        assert rc == 0
        assert counting_engine.constructed == 2, "换签名必须重建（warmup 形状毒性）"
        assert (tmp_path / "b1.mp4").stat().st_size > 0
    finally:
        sv_db.delete_task(t1["id"])
        sv_db.delete_task(t2["id"])


def test_serve_aborts_on_failed_task(counting_engine, monkeypatch, tmp_path):
    """失败任务（rc!=0）直接退出，不再消费 stdin——失败后会话健康度不可信。"""
    _make_clip(_CLIP_A, "160x90")
    t1 = _new_video_task(_CLIP_A, tmp_path / "ok.mp4")
    stdin = io.StringIO("ghost-task-id\nmore-stuff\n")
    monkeypatch.setattr(sys, "stdin", stdin)
    try:
        # 第一个任务成功后经 stdin 接 ghost（任务不存在 rc=2）→ serve 循环以
        # 最后任务退出码收尾；runner 侧由事件流保证（failed → 不续喂、关 stdin）
        rc = worker_mod.main(t1["id"], serve=True)
        assert rc == 2
        assert stdin.read() == "more-stuff\n", "失败后不应消费后续 stdin"
        assert counting_engine.constructed == 1
    finally:
        sv_db.delete_task(t1["id"])


def test_engine_cache_isolated_per_process(counting_engine, tmp_path):
    """非 serve 单次模式：引擎缓存同进程内也生效（连续两次加载只构造一次），
    且换签名（不同 warmup 尺寸）后重建。"""
    from sv.models.registry import get_model

    spec = get_model("realesr-animevideov3")
    weight = spec.files[0]["name"]  # 路径真实性不影响打桩引擎,只进签名
    common = dict(spec=spec, scale=2, variant=None, precision="fp32", batch=1, log=None)
    e1, _ = worker_engine._load_onnx_engine(
        Path(weight), tile=0, warmup_hw=(90, 160), **common)
    e2, _ = worker_engine._load_onnx_engine(
        Path(weight), tile=0, warmup_hw=(90, 160), **common)
    assert e1 is e2
    assert counting_engine.constructed == 1
    e3, _ = worker_engine._load_onnx_engine(
        Path(weight), tile=0, warmup_hw=(72, 128), **common)
    assert e3 is not e1
    assert counting_engine.constructed == 2

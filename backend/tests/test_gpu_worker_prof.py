"""GPU 直连链 worker 装配回归：sr_profiling 开启时剖析头不得引用 engine=None。

P1 回归（曾崩）：GPU 链命中后 engine=None，剖析头直接 engine.provider_used
→ AttributeError 打死 worker（不在任何 try 里，连 failed 事件都发不出）。
用假 GpuInfer/GpuStreamPipeline 打桩走通 main() 全流程（CPU 环境，无需
PyNvVideoCodec），断言任务 rc=0 且剖析头落的是 GPU 路径的 provider。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sv.paths import TEMP_DIR, ffmpeg_bin  # noqa: E402
from sv.utils.process import WINDOWS_CREATE_FLAGS  # noqa: E402
from sv.pipeline.stream import RunStats  # noqa: E402

MODEL = "realesr-animevideov3"  # onnx 引擎 / tile_hint=0 / io 无特殊约定
_SRC = TEMP_DIR / "gpu_prof_hevc10.mkv"


def _make_src() -> None:
    if _SRC.exists():
        return
    r = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=3",
         "-c:v", "libx265", "-pix_fmt", "yuv420p10le", str(_SRC)],
        capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-300:]


class _FakeGpuInfer:
    provider_used = ["FakeGpuChainProvider"]

    def __init__(self, weight, h, w, bit_depth, scale=2):
        pass


class _FakeGpuSegPipeline:
    """替身 GpuStreamPipeline：真 ffmpeg 出段 mp4，帧数/统计口径对齐契约。"""

    def __init__(self, info, out_path, tx, enc, *, gpu_infer=None, seek_s=None,
                 max_frames=None, seg_start=0, seg_total=0, progress_cb=None,
                 cancel_event=None, **kw):
        self.info, self.out_path = info, Path(out_path)
        self.n = max_frames or 10 ** 9
        self.seg_start, self.seg_total = seg_start, seg_total
        self.progress_cb = progress_cb
        self.stage_stats: dict[str, float] = {}

    async def run(self) -> RunStats:
        w, h = self.info.width * 2, self.info.height * 2
        subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=24",
             "-frames:v", str(self.n), "-c:v", "libx264",
             "-pix_fmt", "yuv420p", str(self.out_path)],
            check=True, capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
        self.stage_stats = {"frames": float(self.n), "wall": 0.1, "decode_eof": 1.0}
        if self.progress_cb and self.seg_total:
            self.progress_cb(self.seg_start + self.n, self.seg_total, self.n, 0.0)
        return RunStats(frames=self.n, elapsed_s=0.1, fps=self.n,
                        out_path=self.out_path,
                        out_bytes=self.out_path.stat().st_size)


def test_gpu_chain_with_sr_profiling(tmp_path, monkeypatch):
    """GPU 链 + sr_profiling=true：任务成功完成，剖析头写 GPU provider 不崩。"""
    from sv.server import db, settings as settings_mod
    from sv.server import worker_common
    from sv.server import worker as worker_mod
    from sv.pipeline import stream_gpu

    _make_src()
    db.init_db()
    out = tmp_path / "out.mp4"
    task = db.new_task(str(_SRC), str(out), MODEL,
                       params={"scale": 2, "codec": "hevc_nvenc"})
    log_dir = tmp_path / "sr_logs"
    monkeypatch.setattr(settings_mod, "load", lambda: {
        "engine": "auto", "precision": "fp32", "parallel_streams": False,
        "sr_profiling": True,
    })
    monkeypatch.setattr(worker_common, "SR_LOG_DIR", log_dir)
    monkeypatch.setattr(stream_gpu, "GpuInfer", _FakeGpuInfer)
    monkeypatch.setattr(stream_gpu, "GpuStreamPipeline", _FakeGpuSegPipeline)
    try:
        assert worker_mod.main(task["id"]) == 0  # 修复前：AttributeError 裸奔
        assert out.exists(), "GPU 链任务未产出最终文件"
        log = (log_dir / f"{task['id']}.log").read_text(encoding="utf-8")
        assert "FakeGpuChainProvider" in log, "剖析头未落 GPU 路径 provider"
    finally:
        db.delete_task(task["id"])
        import shutil

        shutil.rmtree(TEMP_DIR / "segmented" / task["id"], ignore_errors=True)

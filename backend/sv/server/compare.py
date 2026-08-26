"""模型对比作业：同一素材 × 多个模型并排处理，产出可对比的结果与速度指标。

与主任务队列独立（后台线程串行，同 trim worker 模式）：对比是短期试验性
操作，不占任务队列、不写任务库，重启后对比记录丢失（产物文件仍在磁盘）。

产物目录 DATA_ROOT/compare/<job_id>/：
  seg.mp4            视频模式切出的标准化片段（图片模式无）
  src_still.png      源素材中点静帧（像素级对比基准）
  <model_id>.mp4     视频模式：该模型的成片
  <model_id>.png     图片模式：该模型的成品图（视频模式为中点静帧）

取消粒度=模型之间：当前模型跑到自然结束（片段短），后续模型跳过。
"""
from __future__ import annotations

import asyncio
import gc
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

from ..paths import DATA_ROOT, ffmpeg_bin
from ..utils.process import WINDOWS_CREATE_FLAGS
from .events import EventBus

COMPARE_ROOT = DATA_ROOT / "compare"
MAX_MODELS = 6
MAX_SEG_S = 20.0  # 对比片段时长上限：对比要快，长片段没有额外信息量

JOBS: dict[str, dict] = {}
_QUEUE: "queue.Queue[str]" = queue.Queue()
_THREAD: threading.Thread | None = None
_BUS: EventBus | None = None  # lifespan 注入；测试环境无总线时静默跳过


def _publish(ev: dict) -> None:
    if _BUS is not None:
        _BUS.publish_threadsafe(ev)


def start(bus: EventBus | None = None) -> None:
    """lifespan 启动（幂等）；测试不触发 lifespan 时由 create 懒启动。"""
    global _BUS, _THREAD
    if bus is not None:
        _BUS = bus
    if _THREAD is None or not _THREAD.is_alive():
        _THREAD = threading.Thread(target=_worker, daemon=True, name="compare")
        _THREAD.start()


def _worker() -> None:
    while True:
        jid = _QUEUE.get()
        job = JOBS.get(jid)
        if job is None or job["status"] not in ("queued",):
            continue
        try:
            _run_job(job)
        except Exception as e:  # noqa: BLE001 — 单作业异常不能杀死 worker
            job["status"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
            _publish({"type": "compare", "id": jid, "status": "failed"})


def create(kind: str, input_path: Path, start_s: float, end_s: float,
           model_ids: list[str], scale: int) -> dict:
    """校验并排入对比作业。调用方（app.py 路由）已完成模型注册表校验。"""
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "kind": kind, "input": str(input_path),
        "start_s": start_s, "end_s": end_s, "scale": scale,
        "status": "queued", "error": None, "cancel_requested": False,
        "created_at": time.time(),
        "entries": [
            {"model_id": m, "status": "queued", "pct": 0.0,
             "error": None, "output": None, "still": None,
             "fps": 0.0, "elapsed_s": 0.0, "out_bytes": 0,
             "out_w": 0, "out_h": 0}
            for m in model_ids
        ],
    }
    JOBS[jid] = job
    # 简单 GC：完成态对比超过 30 个丢最旧（产物文件保留在磁盘）
    done = [k for k, v in JOBS.items() if v["status"] in ("done", "failed", "canceled")]
    for k in done[: max(0, len(done) - 30)]:
        JOBS.pop(k, None)
    start()
    _QUEUE.put(jid)
    return job


def get(jid: str) -> dict | None:
    return JOBS.get(jid)


def request_cancel(jid: str) -> bool:
    job = JOBS.get(jid)
    if job is None or job["status"] not in ("queued", "running"):
        return False
    job["cancel_requested"] = True
    if job["status"] == "queued":  # 还没开跑：直接落定取消
        job["status"] = "canceled"
        for e in job["entries"]:
            e["status"] = "canceled"
        _publish({"type": "compare", "id": jid, "status": "canceled"})
    return True


def asset_path(jid: str, key: str) -> Path | None:
    """产物文件解析：key ∈ {seg, src_still, out/<model_id>, still/<model_id>}，
    白名单标识，不开放任意路径（防目录穿越读任意文件）。"""
    job = JOBS.get(jid)
    if job is None:
        return None
    root = COMPARE_ROOT / jid
    if key == "seg":
        p = root / "seg.mp4"
        return p if p.exists() else None
    if key == "src_still":
        p = root / "src_still.png"
        return p if p.exists() else None
    ids = {e["model_id"] for e in job["entries"]}
    if key.startswith("out/"):
        m = key[4:]
        if m in ids:
            ext = ".mp4" if job["kind"] == "video" else ".png"
            p = root / f"{m}{ext}"
            if p.exists():
                return p
    elif key.startswith("still/"):
        m = key[6:]
        if m in ids:
            # 视频模式：成片中点静帧；图片模式：成品图本身就是静帧
            p = root / (f"{m}_still.png" if job["kind"] == "video" else f"{m}.png")
            if p.exists():
                return p
    return None


# ---- 作业执行 ----

def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 失败: {proc.stderr.decode('utf-8', 'replace').strip()[-300:]}")


def _extract_still(video: Path, out_png: Path) -> None:
    """中点静帧（PNG 无损，供像素级对比缩放）。"""
    from ..pipeline.probe import probe

    mid = probe(video).duration_s / 2
    _ffmpeg(["-ss", f"{mid:.3f}", "-i", str(video),
             "-frames:v", "1", str(out_png)])


def _load_engine(spec, scale: int, warmup_hw: tuple[int, int], log):
    """按任务同款规则加载引擎（复用 worker 的公共函数：fp16 补转/TRT/降档）。"""
    from ..models.registry import model_file
    from .settings import load as load_settings
    from .worker import _load_onnx_engine

    if spec.engine == "torch":
        from ..engines.torch_engine import TorchSrEngine

        eng = TorchSrEngine(model_file(spec, scale), scale, io=spec.io, tile=512)
        eng.load()
        return eng
    precision = load_settings().get("precision", "fp32")
    weight = model_file(spec, scale, precision)
    eng, _ = _load_onnx_engine(
        weight, spec, scale, None, precision,
        int(spec.tile_hint or 0), warmup_hw, batch=1, log=log)
    return eng


def _run_job(job: dict) -> None:
    from ..pipeline.probe import probe
    from ..pipeline.stream import EncodeOpts, StreamPipeline
    from ..models import manager
    from ..models.registry import file_for_scale, get_model

    jid = job["id"]
    work = COMPARE_ROOT / jid
    work.mkdir(parents=True, exist_ok=True)
    job["status"] = "running"
    _publish({"type": "compare", "id": jid, "status": "running"})

    def _log(line: str) -> None:
        _publish({"type": "compare_log", "id": jid, "line": line})

    # ---- 素材准备：视频切标准化片段 + 源静帧；图片直接取中点静帧 ----
    src_still = work / "src_still.png"
    if job["kind"] == "video":
        seg = work / "seg.mp4"
        _ffmpeg(["-ss", f"{job['start_s']:.3f}", "-to", f"{job['end_s']:.3f}",
                 "-i", job["input"], "-c:v", "libx264", "-crf", "18",
                 "-preset", "veryfast", "-an", "-pix_fmt", "yuv420p", str(seg)])
        _extract_still(seg, src_still)
        info = probe(seg)
        warmup_hw = (info.height, info.width)
    else:
        from PIL import Image, ImageOps

        with Image.open(job["input"]) as im:
            im = ImageOps.exif_transpose(im)
            im.convert("RGB").save(str(src_still), "PNG")
        with Image.open(str(src_still)) as im:
            warmup_hw = (im.height, im.width)

    # ---- 逐模型串行：各自加载引擎、处理、落盘、记录指标 ----
    for e in job["entries"]:
        if job["cancel_requested"]:
            e["status"] = "canceled"
            continue
        mid = e["model_id"]
        e["status"] = "running"
        _publish({"type": "compare", "id": jid, "status": "running", "model_id": mid})
        t0 = time.perf_counter()
        try:
            spec = get_model(mid)
            need = file_for_scale(spec, job["scale"], None)
            manager.ensure_files(spec, [need])  # 未安装模型当场下载
            engine = _load_engine(spec, job["scale"], warmup_hw, _log)

            if job["kind"] == "video":
                out = work / f"{mid}.mp4"
                _log(f"{spec.name}：开始处理片段")

                def cb(frames, total, fps, eta, _e=e):
                    _e["pct"] = min(1.0, frames / total) if total else 0.0

                stats = asyncio.run(StreamPipeline(
                    info, out, engine,
                    EncodeOpts(audio_mode="none"), progress_cb=cb).run())
                still = work / f"{mid}_still.png"
                _extract_still(out, still)
                e["output"], e["still"] = str(out), str(still)
                e["fps"] = round(stats.fps, 2)
                e["out_w"], e["out_h"] = info.width * job["scale"], info.height * job["scale"]
                e["out_bytes"] = out.stat().st_size if out.exists() else 0
            else:
                import numpy as np
                from PIL import Image, ImageOps

                out = work / f"{mid}.png"
                with Image.open(job["input"]) as im:
                    frame = np.asarray(
                        ImageOps.exif_transpose(im).convert("RGB"), dtype=np.uint8)
                result = engine.process(frame)
                Image.fromarray(result).save(str(out), "PNG")
                e["output"] = e["still"] = str(out)
                e["out_w"], e["out_h"] = result.shape[1], result.shape[0]
                e["out_bytes"] = out.stat().st_size if out.exists() else 0
            e["elapsed_s"] = round(time.perf_counter() - t0, 1)
            e["pct"] = 1.0
            e["status"] = "done"
            _publish({"type": "compare", "id": jid, "status": "entry_done",
                      "model_id": mid})
        except Exception as ex:  # noqa: BLE001 — 单模型失败不拖垮整组对比
            e["status"] = "failed"
            e["error"] = f"{type(ex).__name__}: {ex}"
            _publish({"type": "compare", "id": jid, "status": "entry_failed",
                      "model_id": mid})
        finally:
            # 引擎/会话立即释放：多个模型连跑显存逐个叠加会把后面的模型挤爆
            engine = locals().get("engine")
            if engine is not None:
                try:
                    del engine
                except Exception:  # noqa: BLE001
                    pass
            gc.collect()

    job["status"] = "canceled" if job["cancel_requested"] else "done"
    _publish({"type": "compare", "id": jid, "status": job["status"]})

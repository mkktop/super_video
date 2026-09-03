"""模型对比作业：同一素材 × 多个模型并排处理，产出可对比的结果与速度指标。

与主任务队列独立（后台线程串行，同 trim worker 模式）：对比是短期试验性
操作，不占任务队列、不写任务库，重启后对比记录丢失（产物文件仍在磁盘）。

产物目录 DATA_ROOT/compare/<job_id>/：
seg.mp4            视频模式切出的标准化片段（图片模式无）
stills/src_<i>.png 视频模式源静帧样本（4 帧避黑选定，像素级对比基准）
<model_id>.mp4     视频模式：该模型的成片
stills/<model>_<i>.png 视频模式：各模型成片与源同时间戳的静帧样本
<model_id>.png     图片模式：该模型的成品图

取消粒度=模型之间：当前模型跑到自然结束（片段短），后续模型跳过。
"""
from __future__ import annotations

import asyncio
import gc
import queue
import shutil
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
STILL_COUNT = 4  # 静帧样本数默认值（settings.compare_still_count 可调，创建作业时快照）
MAX_STILL_COUNT = 8  # 样本数上限：PNG 无损+超分分辨率，多了产物体积与抽帧耗时失控

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


def _still_count_setting() -> int:
    """设置项读入并钳制：json 可能被手改越界/写坏，这里兜底成合法值。"""
    from .settings import load as load_settings

    v = load_settings().get("compare_still_count", STILL_COUNT)
    try:
        return max(1, min(MAX_STILL_COUNT, int(v)))
    except (TypeError, ValueError):
        return STILL_COUNT


def create(kind: str, input_path: Path, start_s: float, end_s: float,
           model_ids: list[str], scale: int) -> dict:
    """校验并排入对比作业。调用方（app.py 路由）已完成模型注册表校验。"""
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "kind": kind, "input": str(input_path),
        "start_s": start_s, "end_s": end_s, "scale": scale,
        # 创建时快照：之后改设置不影响已排队的作业，still_count 恒等于产物张数
        "still_count": _still_count_setting() if kind == "video" else 1,
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
    """产物文件解析：key ∈ {seg, src_still（图片源图，平铺文件）, src_still/<i>（视频源静帧）,
    out/<model_id>, still/<model_id>/<i>（视频多帧）| still/<model_id>（图片成品图）}，
    白名单标识，不开放任意路径（防目录穿越读任意文件）。"""
    job = JOBS.get(jid)
    if job is None:
        return None
    root = COMPARE_ROOT / jid
    if key == "seg":
        p = root / "seg.mp4"
        return p if p.exists() else None
    if key == "src_still":
        # 图片作业的源图是平铺的 src_still.png（v0.2.10 视频静帧迁 stills/ 子目录时
        # 图片形态没有跟随，此前没有任何 key 能取到图片作业的源图）
        p = root / "src_still.png"
        return p if p.exists() else None
    if key.startswith("src_still/"):
        i = key[len("src_still/"):]
        if i.isdigit():
            p = root / "stills" / f"src_{int(i)}.png"
            return p if p.exists() else None
        return None
    ids = {e["model_id"] for e in job["entries"]}
    if key.startswith("out/"):
        m = key[4:]
        if m in ids:
            ext = ".mp4" if job["kind"] == "video" else ".png"
            p = root / f"{m}{ext}"
            if p.exists():
                return p
    elif key.startswith("still/"):
        parts = key[6:].split("/")
        m = parts[0]
        if m in ids:
            # 视频模式：与源同时间戳的成片多帧静帧；图片模式：成品图本身就是静帧
            if len(parts) == 2 and parts[1].isdigit():
                p = root / "stills" / f"{m}_{int(parts[1])}.png"
                if p.exists():
                    return p
            elif len(parts) == 1 and job["kind"] == "image":
                p = root / f"{m}.png"
                if p.exists():
                    return p
    return None


# ---- 作业执行 ----

def active_jobs() -> list[dict]:
    """排队/运行中的作业（清理缓存时必须拒绝，产物正在写入）。"""
    return [j for j in JOBS.values() if j["status"] in ("queued", "running")]


def _dir_size(d: Path) -> int:
    total = 0
    try:
        for f in d.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def cache_stats() -> dict:
    """设置页展示：磁盘上的作业目录数与总占用字节。"""
    jobs, total = 0, 0
    if COMPARE_ROOT.is_dir():
        try:
            entries = list(COMPARE_ROOT.iterdir())
        except OSError:
            entries = []
        for d in entries:
            if d.is_dir():
                jobs += 1
                total += _dir_size(d)
    return {"jobs": jobs, "bytes": total}


def clear_cache() -> dict:
    """删除全部对比产物目录并清空内存记录（对比结果页引用的资产随之失效）。
    有排队/运行中的作业时抛 ValueError，由路由转 409——正在写入的目录不能动。"""
    if active_jobs():
        raise ValueError("有对比作业正在进行，请等它结束后再清理")
    freed, jobs = 0, 0
    if COMPARE_ROOT.is_dir():
        for d in list(COMPARE_ROOT.iterdir()):
            if not d.is_dir():
                continue
            freed += _dir_size(d)
            jobs += 1
            shutil.rmtree(d, ignore_errors=True)
    JOBS.clear()
    return {"removed_jobs": jobs, "freed_bytes": freed}


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 失败: {proc.stderr.decode('utf-8', 'replace').strip()[-300:]}")


def _extract_still(video: Path, out_png: Path, t: float | None = None) -> None:
    """静帧（PNG 无损，供像素级对比缩放）。t 缺省取中点。"""
    from ..pipeline.probe import probe

    if t is None:
        t = probe(video).duration_s / 2
    _ffmpeg(["-ss", f"{t:.3f}", "-i", str(video),
             "-frames:v", "1", str(out_png)])


def _frame_dark(video: Path, t: float) -> bool | None:
    """t 时刻抽一帧判黑：灰度均值 < 20 视为黑场（转场/淡入淡出）。
    抽不出帧（如 t 落在片尾之外）返回 None——与黑场区分，调用方换候选。"""
    import io

    from PIL import Image, ImageStat

    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
         "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        im = Image.open(io.BytesIO(proc.stdout)).convert("L")
    except Exception:
        return None
    return ImageStat.Stat(im).mean[0] < 20


def _pick_still_times(video: Path, duration_s: float | None = None,
                      n: int = STILL_COUNT) -> list[float]:
    """挑 n 个静帧时间戳：片段均分 n 段、段中点为锚、段内避黑（扩散钳在段内，
    相邻段不会选到同一帧）。时间戳由源与各模型成片共用——统一 -ss 才能帧级
    对齐（各自取中点会因时长探测的 ±半帧偏差错开一帧）。段内全黑则保留锚点：
    该段素材本来就是黑场，如实展示比挪去别段冒重复样本好。"""
    from ..pipeline.probe import probe

    d = duration_s if duration_s is not None else probe(video).duration_s
    ts: list[float] = []
    for i in range(n):
        lo = d * i / n
        hi = d * (i + 1) / n
        anchor = (lo + hi) / 2
        t = anchor
        for off in (0.0, -0.5, 0.5, -1.0, 1.0):
            cand = min(max(anchor + off, lo), max(hi - 0.05, lo))
            if _frame_dark(video, cand) is False:
                t = cand
                break
        ts.append(t)
    return ts


def _load_engine(spec, scale: int, warmup_hw: tuple[int, int], log, variant: str | None = None):
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
    weight = model_file(spec, scale, precision, variant)
    eng, _ = _load_onnx_engine(
        weight, spec, scale, variant, precision,
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

    # ---- 素材准备：视频切标准化片段 + 多帧源静帧；图片直接取原图 ----
    src_still = work / "src_still.png"
    still_ts: list[float] = []  # 视频模式：源/各模型成片共用的时间戳组（帧级对齐+避黑）
    if job["kind"] == "video":
        seg = work / "seg.mp4"
        _ffmpeg(["-ss", f"{job['start_s']:.3f}", "-to", f"{job['end_s']:.3f}",
                 "-i", job["input"], "-c:v", "libx264", "-crf", "18",
                 "-preset", "veryfast", "-an", "-pix_fmt", "yuv420p", str(seg)])
        info = probe(seg)
        stills = work / "stills"
        stills.mkdir(exist_ok=True)
        still_ts = _pick_still_times(seg, info.duration_s, job["still_count"])
        for i, t in enumerate(still_ts):
            _extract_still(seg, stills / f"src_{i}.png", t)
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
            from ..models.registry import auto_variant
            variant = auto_variant(spec, job["scale"], warmup_hw[0])
            need = file_for_scale(spec, job["scale"], variant)
            manager.ensure_files(spec, [need])  # 未安装模型当场下载
            engine = _load_engine(spec, job["scale"], warmup_hw, _log, variant)

            if job["kind"] == "video":
                out = work / f"{mid}.mp4"
                _log(f"{spec.name}：开始处理片段")

                def cb(frames, total, fps, eta, _e=e):
                    _e["pct"] = min(1.0, frames / total) if total else 0.0

                stats = asyncio.run(StreamPipeline(
                    info, out, engine,
                    EncodeOpts(audio_mode="none"), progress_cb=cb).run())
                stills_dir = work / "stills"
                for i, t in enumerate(still_ts):
                    _extract_still(out, stills_dir / f"{mid}_{i}.png", t)
                e["output"] = str(out)
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
